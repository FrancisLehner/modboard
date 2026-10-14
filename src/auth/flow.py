"""Moodle mobile-app SSO login: public config, launch URL, token capture, verify.

Moodle issues web-service tokens to the mobile app through
``admin/tool/mobile/launch.php``. The client invents a random passport, and
the token Moodle returns is accompanied by ``md5(wwwroot + passport)``. Only a
response to our own launch request can carry the right hash, so a third party
cannot supply a token for a different site or replay an old one. This module
reimplements that handshake.

The password is typed by the user in a real browser window. Nothing here
reads, types or stores credentials; it opens the page and waits for the
redirect that carries the token.

The steps are exposed as classes so a caller can drive them one at a time::

    auth = MoodleAuthenticator("https://moodle.example.edu")
    config = auth.fetch_public_config()
    captured = auth.login()
    captured.token
"""

from __future__ import annotations

import base64
import concurrent.futures
import hashlib
import re
import secrets
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, Callable, Iterable

import requests

from ..config import (
    DEFAULT_URL_SCHEME,
    HTTP_TIMEOUT,
    LOGIN_TIMEOUT_SECONDS,
    MOBILE_SERVICE,
)
from ..errors import MoodleAuthError
from ..logs import echo, get_logger

log = get_logger("moodle.auth")


@dataclass(frozen=True)
class PublicConfig:
    """Subset of ``tool_mobile_get_public_config`` the login flow needs."""

    site_url: str
    site_name: str
    typeoflogin: int
    launch_url: str
    url_scheme: str
    wwwroot_candidates: tuple[str, ...]
    raw: dict[str, Any]

    @property
    def login_type_label(self) -> str:
        """Human-readable label for ``typeoflogin``."""
        return {
            1: "1 (in-app login form)",
            2: "2 (browser window)",
            3: "3 (embedded browser)",
        }.get(self.typeoflogin, f"{self.typeoflogin} (unknown)")

    @property
    def supports_browser_login(self) -> bool:
        """True unless the site uses an in-app login form."""
        return self.typeoflogin != 1

    @property
    def wwwroot(self) -> str:
        """The site's canonical root, as reported by the site."""
        return self.wwwroot_candidates[0] if self.wwwroot_candidates else self.site_url

    def summary(self) -> dict[str, Any]:
        """Flat dict of the interesting fields."""
        return {
            "site_name": self.site_name,
            "site_url": self.site_url,
            "login_type": self.login_type_label,
            "launch_url": self.launch_url,
            "url_scheme": f"{self.url_scheme}://",
            "wwwroot": self.wwwroot,
        }


@dataclass(frozen=True)
class CapturedToken:
    """A decoded and signature-verified token payload."""

    site_hash: str
    token: str
    private_token: str | None
    verified_wwwroot: str

    @property
    def masked_token(self) -> str:
        """The token, masked for display."""
        return TokenMask.mask(self.token)


class TokenMask:
    """Token masking, shared by every layer that logs or prints one."""

    @staticmethod
    def mask(token: str | None) -> str:
        """Return enough of a token to recognise it and not enough to use it."""
        if not token:
            return "<empty>"
        if len(token) <= 8:
            return "*" * len(token)
        return f"{token[:4]}...{token[-4:]} ({len(token)} chars)"


class BrowserTokenCapture:
    """Open the launch URL in a browser and catch the redirect.

    The browser context is created fresh and never persisted, so SSO session
    cookies die with the process. Browsers refuse to navigate to an unknown
    scheme, so capture watches several places at once:

    1. the ``Location`` header of ``launch.php``'s 3xx response,
    2. request and frame-navigation events for app-scheme URLs,
    3. the ``a#launchapp`` link rendered when the redirect cannot fire.
    """

    def __init__(
        self,
        *,
        headless: bool = False,
        timeout_seconds: int = LOGIN_TIMEOUT_SECONDS,
        on_message: Callable[[str], None] = echo,
    ) -> None:
        """Configure headless mode, the login timeout and the message sink."""
        self.headless = headless
        self.timeout_seconds = timeout_seconds
        self.on_message = on_message

    # -- public API --------------------------------------------------------

    def capture(self, launch_url: str) -> str:
        """Return the raw ``token=`` value once it appears.

        Runs the synchronous Playwright flow on a worker thread, whose sync API
        refuses to start inside a running asyncio loop.
        """
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self._capture_blocking, launch_url)
            return future.result()

    # -- internals ---------------------------------------------------------

    def _capture_blocking(self, launch_url: str) -> str:
        # Lazy import: the rest of the package works without Playwright.
        """Drive the browser and return the captured token value."""
        from playwright.sync_api import sync_playwright

        captured: dict[str, str] = {}

        def remember(url: str | None, source: str) -> None:
            """Record the first URL seen that carries a token."""
            if not url or "captured" in captured:
                return
            url = url.strip()
            if MoodleAuthenticator.looks_like_token_url(url):
                captured["captured"] = MoodleAuthenticator.extract_token_param(url)
                captured["source"] = source

        with sync_playwright() as playwright:
            browser, how = self._launch_browser(playwright)
            if how:
                self.on_message(how)
            # No storage_state, no user data dir: a throwaway profile.
            context = browser.new_context(ignore_https_errors=False)
            page = context.new_page()

            # Strategy 1: the redirect itself.
            def on_response(response) -> None:
                """Capture the ``Location`` header of any redirect."""
                try:
                    if 300 <= response.status < 400:
                        remember(response.headers.get("location"), "redirect header")
                except Exception:  # pragma: no cover - page may be closing
                    pass

            # Strategy 2: anything the browser tries to open with a custom scheme.
            def on_request(request) -> None:
                """Capture any URL the browser attempts to open."""
                try:
                    remember(request.url, "request event")
                except Exception:  # pragma: no cover
                    pass

            page.on("response", on_response)
            page.on("request", on_request)
            page.on("framenavigated", lambda frame: remember(frame.url, "navigation"))

            try:
                page.goto(launch_url, wait_until="domcontentloaded", timeout=60_000)
            except Exception as exc:
                # An app-scheme navigation aborts with an error; that is
                # expected, and harmless once the URL has been captured.
                if "captured" not in captured:
                    raise MoodleAuthError(
                        f"Could not open the launch URL: {exc}"
                    ) from exc

            self.on_message(
                "Log in with SWITCH edu-ID in the browser window. "
                "Capture continues automatically."
            )

            deadline = time.monotonic() + self.timeout_seconds
            while "captured" not in captured and time.monotonic() < deadline:
                # Strategy 3: the "Click here to launch the app" fallback page.
                try:
                    href = page.eval_on_selector(
                        "a#launchapp", "el => el.getAttribute('href')"
                    )
                    remember(href, "a#launchapp link")
                except Exception:
                    pass  # selector absent (the normal case) or page navigating
                if "captured" in captured:
                    break
                page.wait_for_timeout(500)

            # Close first: the SSO session must not outlive the capture.
            try:
                context.close()
            finally:
                browser.close()

        if "captured" not in captured:
            raise TimeoutError(
                f"No token seen within {self.timeout_seconds // 60} minutes. "
                "Login may not have completed, or the redirect was swallowed by "
                "the browser. Use the manual flow and paste the URL yourself."
            )

        self.on_message(f"Token captured via {captured['source']}.")
        return captured["captured"]

    def _launch_browser(self, playwright):
        """Launch the installed Chrome, falling back to bundled Chromium."""
        try:
            browser = playwright.chromium.launch(
                channel="chrome", headless=self.headless
            )
            return browser, "Using installed Google Chrome."
        except Exception:
            try:
                browser = playwright.chromium.launch(headless=self.headless)
            except Exception as exc:
                raise MoodleAuthError(
                    "Could not start a browser. Install Playwright's Chromium with:\n"
                    "  uv run playwright install chromium\n"
                    f"Underlying error: {exc}"
                ) from exc
            return browser, "Using Playwright's bundled Chromium."


class MoodleAuthenticator:
    """Drive the mobile-app token handshake for one site.

    Step by step::

        auth = MoodleAuthenticator("https://moodle.example.edu")
        auth.fetch_public_config()            # is mobile login enabled?
        url = auth.begin_login()              # fresh passport + launch URL
        captured = auth.capture_in_browser()  # browser capture + verify

    Or, for the manual path when a browser swallows the redirect::

        url = auth.begin_login()
        captured = auth.complete_manual("moodlemobile://token=...")
    """

    #: Matches any non-http(s) scheme URL carrying a token, e.g.
    #: ``moodlemobile://token=...`` or a site-forced ``zhawapp://token=...``.
    APP_SCHEME_TOKEN_RE = re.compile(
        r"^(?!https?:)[a-zA-Z][a-zA-Z0-9+.\-]*:.*token=", re.I
    )

    def __init__(
        self,
        site_url: str,
        *,
        timeout: int = HTTP_TIMEOUT,
        login_timeout_seconds: int = LOGIN_TIMEOUT_SECONDS,
    ) -> None:
        """Bind to one site URL and set the timeouts."""
        self.site_url = self.normalise_site_url(site_url)
        self.timeout = timeout
        self.login_timeout_seconds = login_timeout_seconds
        #: Filled in by :meth:`fetch_public_config`.
        self.config: PublicConfig | None = None
        #: The passport of the in-flight login; a verification needs it.
        self.passport: str | None = None
        #: The launch URL of the in-flight login.
        self.launch_url: str | None = None

    # ------------------------------------------------------------------
    # Step 0 -- public configuration
    # ------------------------------------------------------------------

    def fetch_public_config(self, *, refresh: bool = False) -> PublicConfig:
        """Return the site's mobile login configuration. No authentication needed.

        Reports whether mobile web services are enabled, which login style the site
        uses, and the exact ``wwwroot`` string hashed into the token signature.
        Cached on the instance.
        """
        if self.config is not None and not refresh:
            return self.config

        endpoint = f"{self.site_url}/lib/ajax/service-nologin.php"
        payload = [
            {"index": 0, "methodname": "tool_mobile_get_public_config", "args": {}}
        ]

        try:
            response = requests.post(
                endpoint,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=self.timeout,
            )
            response.raise_for_status()
            body = response.json()
        except (requests.RequestException, ValueError) as exc:
            log.error("could not read the mobile configuration from %s: %s", endpoint, exc)
            raise MoodleAuthError(
                f"Could not read the mobile configuration from {endpoint}.\n"
                "Mobile web services are probably disabled on this site "
                "(or the site URL is wrong / unreachable).\n"
                f"Underlying error: {exc}"
            ) from exc

        self.config = self._parse_public_config(self.site_url, body)
        log.info(
            "public config for %r: login type %s, launch %s, scheme %s://, wwwroot %s",
            self.config.site_name or self.site_url,
            self.config.login_type_label,
            self.config.launch_url,
            self.config.url_scheme,
            self.config.wwwroot,
        )
        return self.config

    @classmethod
    def _parse_public_config(cls, site_url: str, body: Any) -> PublicConfig:
        """Extract the needed fields from Moodle's AJAX envelope."""
        # service-nologin.php answers with a list, one entry per requested method.
        if isinstance(body, list):
            if not body:
                raise MoodleAuthError("Site returned an empty configuration response.")
            entry = body[0]
        elif isinstance(body, dict):
            entry = body
        else:
            raise MoodleAuthError(f"Unexpected configuration response: {body!r}")

        if entry.get("error"):
            raise MoodleAuthError(
                "Site refused the configuration request: "
                f"{entry.get('exception') or entry['error']}\n"
                "Mobile web services are probably disabled."
            )

        data = entry.get("data")
        if not isinstance(data, dict):
            raise MoodleAuthError(
                f"Configuration response had no data payload: {body!r}"
            )

        wwwroot = str(data.get("wwwroot") or "").strip()
        httpswwwroot = str(data.get("httpswwwroot") or "").strip()
        # Moodle hashes whatever root string its own config holds, which may
        # or may not end in a slash, so every spelling is tried later.
        candidates = cls._wwwroot_candidates([wwwroot, httpswwwroot, site_url])

        launch_url = str(data.get("launchurl") or "").strip()
        if not launch_url:
            launch_url = (
                f"{(wwwroot or site_url).rstrip('/')}/admin/tool/mobile/launch.php"
            )

        try:
            typeoflogin = int(data.get("typeoflogin") or 0)
        except (TypeError, ValueError):
            typeoflogin = 0

        return PublicConfig(
            site_url=site_url,
            site_name=str(data.get("sitename") or site_url),
            typeoflogin=typeoflogin,
            launch_url=launch_url,
            url_scheme=str(data.get("forcedurlscheme") or DEFAULT_URL_SCHEME).strip(),
            wwwroot_candidates=candidates,
            raw=data,
        )

    @staticmethod
    def _wwwroot_candidates(values: Iterable[str]) -> tuple[str, ...]:
        """Return each non-empty root with and without a trailing slash, order kept."""
        out: list[str] = []
        for value in values:
            value = (value or "").strip()
            if not value:
                continue
            for variant in (value.rstrip("/"), value.rstrip("/") + "/"):
                if variant and variant not in out:
                    out.append(variant)
        return tuple(out)

    @staticmethod
    def normalise_site_url(site_url: str) -> str:
        """Force HTTPS and strip trailing slashes; reject anything else."""
        site_url = (site_url or "").strip()
        if not site_url:
            raise MoodleAuthError("No site URL given.")
        if "://" not in site_url:
            site_url = "https://" + site_url
        parsed = urllib.parse.urlparse(site_url)
        if parsed.scheme != "https":
            raise MoodleAuthError(
                f"Refusing to use a non-HTTPS site URL: {site_url!r}. "
                "Tokens must never travel unencrypted."
            )
        return site_url.rstrip("/")

    # ------------------------------------------------------------------
    # Step 1 -- build the launch URL
    # ------------------------------------------------------------------

    @staticmethod
    def new_passport() -> str:
        """Return a fresh random passport, the secret behind the signature check."""
        return secrets.token_hex(16)

    def begin_login(self) -> str:
        """Start a login: new passport, new launch URL. Invalidates any previous attempt.
        """
        config = self.fetch_public_config()
        self.passport = self.new_passport()
        self.launch_url = self.build_launch_url(config, self.passport)
        # The passport is the secret behind the signature check and never
        # goes in the log; the launch URL carries it, so neither does that.
        log.info("login handshake begun against %s", config.launch_url)
        return self.launch_url

    @staticmethod
    def build_launch_url(config: PublicConfig, passport: str) -> str:
        """Build ``launch.php?service=...&passport=...&urlscheme=...``."""
        separator = "&" if "?" in config.launch_url else "?"
        query = urllib.parse.urlencode(
            {
                "service": MOBILE_SERVICE,
                "passport": passport,
                "urlscheme": config.url_scheme,
            }
        )
        return f"{config.launch_url}{separator}{query}"

    # ------------------------------------------------------------------
    # Step 2 -- open a browser and catch the redirect
    # ------------------------------------------------------------------

    def capture_in_browser(
        self,
        *,
        headless: bool = False,
        timeout_seconds: int | None = None,
        on_message: Callable[[str], None] = echo,
    ) -> CapturedToken:
        """Open the current launch URL in a browser and verify what comes back.

        Requires ``begin_login()`` first, or use ``login()`` which does both.
        """
        if not self.launch_url or not self.passport:
            raise MoodleAuthError(
                "No login in flight -- call begin_login() (or login()) first."
            )
        capture = BrowserTokenCapture(
            headless=headless,
            timeout_seconds=timeout_seconds or self.login_timeout_seconds,
            on_message=on_message,
        )
        raw_value = capture.capture(self.launch_url)
        return self.verify(raw_value)

    def login(
        self,
        *,
        headless: bool = False,
        timeout_seconds: int | None = None,
        on_message: Callable[[str], None] = echo,
    ) -> CapturedToken:
        """Run the whole flow: config, launch URL, browser capture, verify."""
        self.begin_login()
        return self.capture_in_browser(
            headless=headless,
            timeout_seconds=timeout_seconds,
            on_message=on_message,
        )

    def complete_manual(self, token_url: str) -> CapturedToken:
        """Verify an app-scheme URL pasted in by hand.

        Use when the browser shows an open-app dialog instead of exposing the
        redirect, or blocks custom schemes entirely.
        """
        token_url = (token_url or "").strip()
        if not token_url:
            raise MoodleAuthError("Nothing pasted -- no URL to verify.")
        if "token=" not in token_url:
            raise MoodleAuthError(
                "That does not look like a token URL (no 'token=' in it)."
            )
        return self.verify(self.extract_token_param(token_url))

    # ------------------------------------------------------------------
    # Step 3 -- decode and verify the token payload
    # ------------------------------------------------------------------

    def verify(self, raw_value: str, passport: str | None = None) -> CapturedToken:
        """Decode a captured payload and prove it answers our own launch request."""
        passport = passport or self.passport
        if not passport:
            raise MoodleAuthError(
                "No passport available -- a token can only be verified against "
                "the launch request that produced it."
            )
        config = self.fetch_public_config()
        return self.verify_and_decode(raw_value, passport, config.wwwroot_candidates)

    @classmethod
    def verify_and_decode(
        cls, raw_value: str, passport: str, wwwroot_candidates: Iterable[str]
    ) -> CapturedToken:
        """Check the signature against each plausible ``wwwroot`` spelling."""
        site_hash, token, private_token = cls.decode_token_payload(raw_value)
        candidates = tuple(wwwroot_candidates)

        for candidate in candidates:
            expected = hashlib.md5((candidate + passport).encode("utf-8")).hexdigest()
            if secrets.compare_digest(expected, site_hash):
                log.info(
                    "token signature verified against %s (token %s)",
                    candidate,
                    TokenMask.mask(token),
                )
                return CapturedToken(site_hash, token, private_token, candidate)

        log.error(
            "token signature mismatch -- tried wwwroot values: %s", ", ".join(candidates)
        )
        raise MoodleAuthError(
            "Token signature mismatch -- refusing to use this token.\n"
            f"Payload hash: {site_hash}\n"
            f"Tried wwwroot values: {', '.join(candidates)}\n"
            "This means the token did not come from our own launch request "
            "(wrong site, stale link, or a tampered redirect)."
        )

    @classmethod
    def looks_like_token_url(cls, url: str) -> bool:
        """True for an app-scheme URL carrying a ``token=`` parameter."""
        return bool(url) and bool(cls.APP_SCHEME_TOKEN_RE.match(url.strip()))

    @staticmethod
    def extract_token_param(url: str) -> str:
        """Return the raw, still-encoded value of the ``token`` parameter."""
        match = re.search(r"token=([^&#\s]+)", url)
        if not match:
            raise MoodleAuthError(f"No token= parameter found in {url!r}")
        return match.group(1)

    @classmethod
    def decode_token_payload(cls, raw_value: str) -> tuple[str, str, str | None]:
        """Split ``<site_hash>:::<token>[:::<private_token>]`` into its parts."""
        try:
            decoded = cls._b64decode_lenient(raw_value).decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            # Decoded to bytes but not to text, so it was never a payload.
            raise MoodleAuthError(
                "Token payload is not valid base64 (decoded to binary garbage)."
            ) from exc
        parts = decoded.split(":::")
        if len(parts) < 2:
            raise MoodleAuthError(
                "Decoded token payload did not have the expected "
                "'<hash>:::<token>' structure."
            )
        site_hash = parts[0].strip()
        token = parts[1].strip()
        private_token = parts[2].strip() if len(parts) > 2 and parts[2].strip() else None
        if not site_hash or not token:
            raise MoodleAuthError("Decoded token payload was missing the hash or token.")
        return site_hash, token, private_token

    @staticmethod
    def _b64decode_lenient(value: str) -> bytes:
        """Base64-decode tolerating URL-safe alphabets and missing padding."""
        value = value.strip()
        # Percent-decoding may be needed twice: once for the URL, once for an
        # already-encoded string pasted out of a browser dialog.
        for _ in range(2):
            unquoted = urllib.parse.unquote(value)
            if unquoted == value:
                break
            value = unquoted
        value = value.strip().replace("\n", "").replace("\r", "").replace(" ", "+")

        padded = value + "=" * (-len(value) % 4)
        # validate=True matters: without it base64 silently skips characters
        # outside the alphabet and decodes garbage.
        for altchars in (None, b"-_"):  # standard alphabet, then URL-safe
            try:
                return base64.b64decode(padded, altchars=altchars, validate=True)
            except (ValueError, base64.binascii.Error):
                continue
        raise MoodleAuthError("Token payload is not valid base64.")
