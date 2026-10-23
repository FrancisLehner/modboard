"""Token persistence.

The web-service token is a bearer credential for the whole account, so the
default store is the OS credential store via ``keyring``, read through
``src.keychain`` so each entry is fetched once per process. ``MemoryTokenStore``
is the opt-out for throwaway work.
"""

from __future__ import annotations

from typing import Protocol

from .. import keychain
from ..config import KEYRING_SERVICE_PRIVATE_TOKEN, KEYRING_SERVICE_TOKEN
from .flow import TokenMask

#: Local aliases -- the store addresses keyring by these, keyed by site URL so
#: several sites can coexist under one service name.
KEYRING_SERVICE = KEYRING_SERVICE_TOKEN
KEYRING_SERVICE_PRIVATE = KEYRING_SERVICE_PRIVATE_TOKEN


class TokenStoreProtocol(Protocol):
    """Interface a token store must implement."""

    def load(self, site_url: str) -> str | None:
        """Return the stored token for ``site_url``, or ``None``."""
        ...

    def save(self, site_url: str, token: str, private_token: str | None) -> None:
        """Store the token, and the private token when given."""
        ...

    def delete(self, site_url: str) -> list[str]:
        """Delete both entries; return the names that existed."""
        ...


class TokenStore:
    """``keyring``-backed store. One entry per site URL."""

    def __init__(
        self,
        *,
        service: str = KEYRING_SERVICE,
        private_service: str = KEYRING_SERVICE_PRIVATE,
    ) -> None:
        """Bind the store to a pair of keyring service names."""
        self.service = service
        self.private_service = private_service

    @staticmethod
    def available() -> bool:
        """True when ``keyring`` imports and exposes a working backend."""
        try:
            import keyring
            from keyring.backends.fail import Keyring as FailKeyring
        except ImportError:
            return False
        return not isinstance(keyring.get_keyring(), FailKeyring)

    def load(self, site_url: str) -> str | None:
        """Return the stored token for ``site_url``, or ``None``."""
        return keychain.get_password(self.service, site_url)

    def load_private(self, site_url: str) -> str | None:
        """Return the stored private token for ``site_url``, or ``None``."""
        return keychain.get_password(self.private_service, site_url)

    def save(self, site_url: str, token: str, private_token: str | None) -> None:
        """Store the token, and the private token when given."""
        keychain.set_password(self.service, site_url, token)
        if private_token:
            keychain.set_password(self.private_service, site_url, private_token)

    def delete(self, site_url: str) -> list[str]:
        """Delete both entries; return the names that existed."""
        removed: list[str] = []
        for service, label in (
            (self.service, "token"),
            (self.private_service, "private token"),
        ):
            if keychain.delete_password(service, site_url):
                removed.append(label)
        return removed


class MemoryTokenStore:
    """Process-local store. Contents are lost when the process exits."""

    def __init__(self) -> None:
        """Start with an empty store."""
        self._tokens: dict[str, str] = {}
        self._private: dict[str, str] = {}

    def __repr__(self) -> str:
        """Describe the store without exposing any token."""
        sites = ", ".join(sorted(self._tokens)) or "empty"
        return f"MemoryTokenStore({sites})"

    def load(self, site_url: str) -> str | None:
        """Return the stored token for ``site_url``, or ``None``."""
        return self._tokens.get(site_url)

    def load_private(self, site_url: str) -> str | None:
        """Return the stored private token for ``site_url``, or ``None``."""
        return self._private.get(site_url)

    def save(self, site_url: str, token: str, private_token: str | None) -> None:
        """Store the token, and the private token when given."""
        self._tokens[site_url] = token
        if private_token:
            self._private[site_url] = private_token

    def delete(self, site_url: str) -> list[str]:
        """Delete both entries; return the names that existed."""
        removed: list[str] = []
        if self._tokens.pop(site_url, None) is not None:
            removed.append("token")
        if self._private.pop(site_url, None) is not None:
            removed.append("private token")
        return removed

    @staticmethod
    def mask(token: str | None) -> str:
        """Return a masked form of the token, safe to print."""
        return TokenMask.mask(token)
