"""One object tying login, token storage and API calls together.

::

    session = MoodleSession()
    session.connect()          # stored token, or browser SSO login
    session.courses()

Every method returns plain Python data; nothing here formats output or exits
the process.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Iterable

from .activities import AssignRecord, LessonRecord, QuizRecord, WorkshopRecord
from ..errors import MoodleApiError, MoodleAuthError
from .progress import AssignStatusRecord, CompletionRecord, QuizStatusRecord
from .client import DEFAULT_ALLOWED_FUNCTIONS, MoodleClient
from ..config import DEFAULT_SITE, LOGIN_TIMEOUT_SECONDS
from ..auth.flow import (
    CapturedToken,
    MoodleAuthenticator,
    PublicConfig,
    TokenMask,
)
from ..auth.tokens import MemoryTokenStore, TokenStore, TokenStoreProtocol
from ..logs import echo, get_logger

log = get_logger("moodle.session")


def _tristate(value: Any) -> bool | None:
    """Coerce to ``bool``, leaving ``None`` as ``None``."""
    return None if value is None else bool(value)


class MoodleSession:
    """An authenticated, or not yet authenticated, connection to one site.

    Owns an authenticator for the SSO handshake, a token store, and -- once a
    token exists -- a client for the read-only API calls.
    """

    def __init__(
        self,
        site_url: str = DEFAULT_SITE,
        *,
        store: TokenStoreProtocol | None = None,
        allowed_functions: Iterable[str] | None = DEFAULT_ALLOWED_FUNCTIONS,
        login_timeout_seconds: int = LOGIN_TIMEOUT_SECONDS,
        on_message: Callable[[str], None] = echo,
    ) -> None:
        """Build the authenticator and pick a token store."""
        self.auth = MoodleAuthenticator(
            site_url, login_timeout_seconds=login_timeout_seconds
        )
        self.site_url = self.auth.site_url
        self.store = store if store is not None else self._default_store(on_message)
        self.allowed_functions = allowed_functions
        self.on_message = on_message
        self._token: str | None = None
        self._private_token: str | None = None
        self._client: MoodleClient | None = None
        self._site_info: dict[str, Any] | None = None

    def __repr__(self) -> str:
        """Describe the session without exposing the token."""
        state = "authenticated" if self._token else "not authenticated"
        return f"MoodleSession({self.site_url!r}, {state})"

    @staticmethod
    def _default_store(on_message: Callable[[str], None]) -> TokenStoreProtocol:
        """Return the keyring store when usable, otherwise an in-memory one."""
        if TokenStore.available():
            return TokenStore()
        on_message(
            "No usable keyring backend found -- keeping the token in memory "
            "only (it is gone when the kernel restarts)."
        )
        return MemoryTokenStore()

    # ------------------------------------------------------------------
    # Token state
    # ------------------------------------------------------------------

    @property
    def token(self) -> str | None:
        """The current token, or ``None``."""
        return self._token

    @property
    def masked_token(self) -> str:
        """The current token, masked for display."""
        return TokenMask.mask(self._token)

    @property
    def private_token(self) -> str | None:
        """The current private token, or ``None``."""
        return self._private_token

    @property
    def is_authenticated(self) -> bool:
        """True when a token is held."""
        return self._token is not None

    @property
    def client(self) -> MoodleClient:
        """The API client. Raises if there is no token yet."""
        if self._client is None:
            raise MoodleAuthError(
                "Not authenticated yet -- call connect() or login() first."
            )
        return self._client

    def use_token(self, token: str, private_token: str | None = None) -> None:
        """Adopt a token obtained elsewhere, without storing it."""
        self._token = token
        self._private_token = private_token
        self._site_info = None
        self._client = MoodleClient(
            self.site_url, token, allowed_functions=self.allowed_functions
        )

    def public_config(self, *, refresh: bool = False) -> PublicConfig:
        """Return the site's mobile login configuration. No authentication needed."""
        return self.auth.fetch_public_config(refresh=refresh)

    # ------------------------------------------------------------------
    # Getting a token
    # ------------------------------------------------------------------

    def load_stored_token(self, *, validate: bool = True) -> bool:
        """Adopt the stored token if there is one. True when it is usable."""
        try:
            token = self.store.load(self.site_url)
        except Exception as exc:
            self.on_message(f"Could not read the token store: {exc}")
            return False
        if not token:
            return False

        self.use_token(token, self._load_stored_private_token())
        if not validate:
            return True

        try:
            self._site_info = self.client.get_site_info()
        except MoodleApiError as exc:
            self.on_message(f"Stored token unusable: {exc}")
            self.clear()
            return False
        return True

    def _load_stored_private_token(self) -> str | None:
        """Return the stored private token, or ``None`` if the store has none."""
        loader = getattr(self.store, "load_private", None)
        if loader is None:
            return None
        try:
            return loader(self.site_url)
        except Exception:
            return None

    def login(
        self,
        *,
        headless: bool = False,
        timeout_seconds: int | None = None,
        store: bool = True,
        validate: bool = True,
    ) -> CapturedToken:
        """Run a full SSO login and adopt the resulting token.

        The password is typed in the browser window; this code never sees it. The
        token is verified against our own passport before being adopted, and
        validated against the web service before being stored.
        """
        self.auth.begin_login()
        captured = self.auth.capture_in_browser(
            headless=headless,
            timeout_seconds=timeout_seconds,
            on_message=self.on_message,
        )
        self._adopt(captured, store=store, validate=validate)
        return captured

    def begin_manual_login(self) -> str:
        """Return the launch URL to open by hand.

        Log in there, then pass the resulting app-scheme URL to
        ``complete_manual_login``.
        """
        return self.auth.begin_login()

    def complete_manual_login(
        self, token_url: str, *, store: bool = True, validate: bool = True
    ) -> CapturedToken:
        """Finish the manual flow with a pasted app-scheme URL."""
        captured = self.auth.complete_manual(token_url)
        self._adopt(captured, store=store, validate=validate)
        return captured

    def connect(
        self,
        *,
        force_login: bool = False,
        headless: bool = False,
        timeout_seconds: int | None = None,
    ) -> dict[str, Any]:
        """Return site info, using the stored token if it still works.

        Falls back to a fresh login otherwise.
        """
        log.info("connecting to %s (force_login=%s)", self.site_url, force_login)
        if not force_login and self.load_stored_token():
            self.on_message(f"Using the stored token ({self.masked_token}).")
            return self._log_connected("stored token")
        log.info("no usable stored token -- opening a browser login")
        self.login(headless=headless, timeout_seconds=timeout_seconds)
        return self._log_connected("browser login")

    def _log_connected(self, route: str) -> dict[str, Any]:
        """Record who we are connected as, and return the site info."""
        info = self.site_info()
        log.info(
            "connected to %r (%s) as %s [user %s] via %s -- Moodle %s",
            info.get("sitename") or self.site_url,
            info.get("siteurl") or self.site_url,
            info.get("username") or info.get("fullname") or "unknown",
            info.get("userid"),
            route,
            info.get("release") or info.get("version") or "unknown version",
        )
        return info

    def _adopt(
        self, captured: CapturedToken, *, store: bool, validate: bool
    ) -> None:
        """Adopt a captured token, optionally validating and storing it."""
        self.use_token(captured.token, captured.private_token)
        if validate:
            # Raises if the token is not usable -- never store one that isn't.
            self._site_info = self.client.get_site_info()
        if store:
            try:
                self.store.save(
                    self.site_url, captured.token, captured.private_token
                )
            except Exception as exc:
                self.on_message(f"Token not persisted: {exc}")

    def clear(self) -> None:
        """Forget the in-memory token. The store is left untouched."""
        self._token = None
        self._private_token = None
        self._client = None
        self._site_info = None

    def logout(self) -> list[str]:
        """Delete the stored tokens and forget the in-memory one."""
        try:
            removed = self.store.delete(self.site_url)
        except Exception as exc:
            self.on_message(f"Could not delete the stored token: {exc}")
            removed = []
        self.clear()
        log.info("logged out of %s (%d stored secret(s) deleted)", self.site_url, len(removed))
        return removed

    # ------------------------------------------------------------------
    # Read-only data
    # ------------------------------------------------------------------

    def site_info(self, *, refresh: bool = False) -> dict[str, Any]:
        """Return site info for the current token. Cached on the session."""
        if self._site_info is None or refresh:
            self._site_info = self.client.get_site_info()
        return self._site_info

    @property
    def user_id(self) -> int:
        """The authenticated user's numeric id."""
        userid = self.site_info().get("userid")
        if not isinstance(userid, int):
            raise MoodleApiError("Site info did not contain a numeric user ID.")
        return userid

    def courses(self, *, userid: int | None = None) -> list[dict[str, Any]]:
        """Return enrolled courses sorted by full name, each with a view URL."""
        raw = self.client.get_users_courses(userid if userid is not None else self.user_id)
        courses = [
            {
                "id": course.get("id"),
                "shortname": course.get("shortname"),
                "fullname": course.get("fullname"),
                "visible": bool(course.get("visible", 1)),
                "url": f"{self.site_url}/course/view.php?id={course.get('id')}",
                "raw": course,
            }
            for course in raw
        ]
        courses.sort(key=lambda c: str(c["fullname"] or "").lower())
        log.info("retrieved %d enrolled course(s)", len(courses))
        return courses

    def favourite_courses(
        self, *, use_timeline: bool = True, enrich: bool = True
    ) -> list[dict[str, Any]]:
        """Return the courses starred in Moodle.

        Two routes to the same answer: the timeline-classification endpoint filters
        server-side, the enrolment endpoint filters client-side on ``isfavourite``.
        ``use_timeline=False`` forces the second.
        """
        payload: list[dict[str, Any]] = []
        if use_timeline:
            try:
                result = self.client.get_enrolled_courses_by_classification("favourites")
                payload = list(result.get("courses") or [])
            except (MoodleApiError, ValueError) as exc:
                # ValueError: blocked by the allowlist. MoodleApiError: the
                # site does not expose it. The fallback covers both.
                self.on_message(f"Favourites endpoint unavailable ({exc}); using enrolments.")
                payload = []
                use_timeline = False

        if not use_timeline or not payload:
            payload = [
                course
                for course in self.client.get_users_courses(self.user_id)
                if course.get("isfavourite")
            ]

        if use_timeline and enrich and payload:
            payload = self._merge_enrolment_fields(payload)

        courses = [self._normalise_course(course) for course in payload]
        courses.sort(key=lambda c: c["fullname"].lower())
        log.info(
            "retrieved %d favourite course(s) via %s: %s",
            len(courses),
            "timeline" if use_timeline else "enrolments",
            ", ".join(f"{c['shortname']}({c['id']})" for c in courses) or "none",
        )
        return courses

    def _merge_enrolment_fields(self, payload: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Fill timeline records with fields only the enrolment endpoint returns."""
        try:
            enrolments = {
                int(course.get("id") or 0): course
                for course in self.client.get_users_courses(self.user_id)
            }
        except MoodleApiError as exc:
            self.on_message(f"Could not enrich courses ({exc}); flags stay unknown.")
            return payload
        return [{**enrolments.get(int(course.get("id") or 0), {}), **course} for course in payload]

    def _normalise_course(self, course: dict[str, Any]) -> dict[str, Any]:
        """Normalise one course record, whichever endpoint it came from."""
        course_id = int(course.get("id") or 0)
        return {
            "id": course_id,
            "shortname": str(course.get("shortname") or "").strip(),
            "fullname": str(course.get("fullname") or "").strip(),
            # Three endpoints, three spellings of the same field.
            "category": str(
                course.get("coursecategory") or course.get("categoryname") or ""
            ).strip(),
            "favourite": bool(course.get("isfavourite", True)),
            "visible": bool(course.get("visible", 1)) and not course.get("hidden", False),
            "startdate": course.get("startdate"),
            "enddate": course.get("enddate") or None,
            "progress": course.get("progress"),
            # Capability flags gate the later layers, so an empty deadline
            # list stays distinguishable from a disabled feature.
            "format": course.get("format"),
            "completion_enabled": _tristate(course.get("enablecompletion")),
            "activity_dates_shown": _tristate(course.get("showactivitydates")),
            "url": str(course.get("viewurl") or f"{self.site_url}/course/view.php?id={course_id}"),
            "summary": None,
            "raw": course,
        }

    def course_by_field(
        self, value: int | str | Iterable[int], *, field: str = "id"
    ) -> list[dict[str, Any]]:
        """Return course metadata the course list does not carry."""
        if isinstance(value, (list, tuple, set)):
            field, value = "ids", ",".join(str(v) for v in value)
        result = self.client.call(
            "core_course_get_courses_by_field", field=field, value=value
        )
        courses = [self._normalise_course(c) for c in (result.get("courses") or [])]
        for course, raw in zip(courses, result.get("courses") or []):
            course["contacts"] = [
                str(c.get("fullname") or "").strip() for c in (raw.get("contacts") or [])
            ]
            course["image"] = next(
                (f.get("fileurl") for f in (raw.get("overviewfiles") or []) if f.get("fileurl")),
                None,
            )
        return courses

    # ------------------------------------------------------------------
    # Channel 1 -- dates activities carry in their own fields
    # ------------------------------------------------------------------

    def _course_scope(
        self, course_ids: Iterable[int] | None
    ) -> tuple[list[int], dict[int, str]]:
        """Resolve the course ids a pull applies to, defaulting to the favourites."""
        if course_ids is None:
            courses = self.favourite_courses()
            return [c["id"] for c in courses], {c["id"]: c["fullname"] for c in courses}
        ids = [int(cid) for cid in course_ids]
        names = {c["id"]: c["fullname"] for c in self._course_name_cache(ids)}
        return ids, names

    def _course_name_cache(self, ids: list[int]) -> list[dict[str, Any]]:
        """Return course id, shortname and fullname for the given ids."""
        try:
            return self.course_by_field(ids)
        except (MoodleApiError, ValueError):
            return []

    def quizzes(
        self, course_ids: Iterable[int] | None = None, *, include_empty: bool = True
    ) -> list[dict[str, Any]]:
        """Return quiz records for the given courses."""
        ids, names = self._course_scope(course_ids)
        payload = self.client.get_quizzes_by_courses(ids)
        return self._records(
            QuizRecord, payload.get("quizzes") or [], ids, names, include_empty
        )

    def assignments(
        self, course_ids: Iterable[int] | None = None, *, include_empty: bool = True
    ) -> list[dict[str, Any]]:
        """Return assignment records.

        The payload nests assignments under courses, so it is flattened here.
        """
        ids, names = self._course_scope(course_ids)
        payload = self.client.get_assignments(ids)
        records: list[dict[str, Any]] = []
        for course in payload.get("courses") or []:
            course_id = int(course.get("id") or 0)
            course_name = str(course.get("fullname") or "").strip() or names.get(course_id)
            for raw in course.get("assignments") or []:
                records.append(
                    AssignRecord.from_payload(
                        raw,
                        course_id=course_id,
                        course_name=course_name,
                        site_url=self.site_url,
                    )
                )
        if include_empty:
            records.extend(self._blanks(AssignRecord, records, ids, names))
        return records

    def workshops(
        self, course_ids: Iterable[int] | None = None, *, include_empty: bool = True
    ) -> list[dict[str, Any]]:
        """Return workshop records."""
        ids, names = self._course_scope(course_ids)
        payload = self.client.get_workshops_by_courses(ids)
        return self._records(
            WorkshopRecord, payload.get("workshops") or [], ids, names, include_empty
        )

    def lessons(
        self, course_ids: Iterable[int] | None = None, *, include_empty: bool = True
    ) -> list[dict[str, Any]]:
        """Return lesson records."""
        ids, names = self._course_scope(course_ids)
        payload = self.client.get_lessons_by_courses(ids)
        return self._records(
            LessonRecord, payload.get("lessons") or [], ids, names, include_empty
        )

    def _records(
        self,
        record_cls,
        raw_items: list[dict[str, Any]],
        ids: list[int],
        names: dict[int, str],
        include_empty: bool,
    ) -> list[dict[str, Any]]:
        """Map a flat ``*_by_courses`` payload into records of one type."""
        records = [
            record_cls.from_payload(
                raw,
                course_id=int(raw.get("course") or 0),
                course_name=names.get(int(raw.get("course") or 0)),
                site_url=self.site_url,
            )
            for raw in raw_items
        ]
        if include_empty:
            records.extend(self._blanks(record_cls, records, ids, names))
        return records

    def _blanks(
        self,
        record_cls,
        records: list[dict[str, Any]],
        ids: list[int],
        names: dict[int, str],
    ) -> list[dict[str, Any]]:
        """Return one all-``None`` record per course with no activity of this type."""
        seen = {record["course_id"] for record in records}
        blanks = []
        for course_id in ids:
            if course_id in seen:
                continue
            blank = record_cls.blank()
            blank["type"] = record_cls.MODNAME
            blank["course_id"] = course_id
            blank["course_name"] = names.get(course_id)
            blanks.append(blank)
        return blanks

    # ------------------------------------------------------------------
    # Layer F -- what the user has actually done
    # ------------------------------------------------------------------

    def quiz_status(
        self,
        quizzes: list[dict[str, Any]] | None = None,
        *,
        dated_only: bool = True,
        include_grades: bool = True,
    ) -> list[dict[str, Any]]:
        """Return attempt state and best grade per quiz."""
        if quizzes is None:
            quizzes = self.quizzes(include_empty=False)
        targets = [
            q for q in quizzes
            if q.get("instance") and (q.get("has_time_window") or not dated_only)
        ]
        userid = self.user_id
        records = []
        for quiz in targets:
            attempts = self.client.get_quiz_user_attempts(quiz["instance"], userid)
            best = (
                self.client.get_quiz_best_grade(quiz["instance"], userid)
                if include_grades
                else None
            )
            records.append(
                QuizStatusRecord.from_payload(
                    quiz,
                    attempts.get("attempts") or [],
                    best,
                    closes_at=quiz.get("closes_at"),
                )
            )
        log.info("polled %d of %d quiz(zes) for attempt state", len(records), len(quizzes))
        return records

    def assignment_status(
        self, assignments: list[dict[str, Any]] | None = None
    ) -> list[dict[str, Any]]:
        """Return submission state per assignment, including lateness."""
        if assignments is None:
            assignments = self.assignments(include_empty=False)
        userid = self.user_id
        records = []
        for assignment in assignments:
            if not assignment.get("instance"):
                continue
            status = self.client.get_assign_submission_status(assignment["instance"], userid)
            records.append(AssignStatusRecord.from_payload(assignment, status))
        log.info(
            "polled %d of %d assignment(s) for submission state",
            len(records),
            len(assignments),
        )
        return records

    def completion(
        self, course_ids: Iterable[int] | None = None, *, with_names: bool = True
    ) -> list[dict[str, Any]]:
        """Return per-activity completion state.

        Completion tracking is configured per activity, so an unset value means
        "not tracked" rather than "not done".
        """
        ids, names = self._course_scope(course_ids)
        userid = self.user_id
        records: list[dict[str, Any]] = []
        for course_id in ids:
            try:
                payload = self.client.get_activities_completion_status(course_id, userid)
            except MoodleApiError as exc:
                self.on_message(f"No completion data for course {course_id}: {exc}")
                continue
            statuses = payload.get("statuses") or []
            titles = self._module_names(course_id) if (with_names and statuses) else {}
            for raw in statuses:
                records.append(
                    CompletionRecord.from_payload(
                        raw,
                        course_id=course_id,
                        course_name=names.get(course_id),
                        name=titles.get(raw.get("cmid")),
                    )
                )
        return records

    def _module_names(self, course_id: int) -> dict[int, str]:
        """Return ``cmid -> activity name`` for payloads that carry only the cmid."""
        try:
            sections = self.client.get_course_contents(course_id)
        except MoodleApiError:
            return {}
        return {
            int(module["id"]): str(module.get("name") or "").strip()
            for section in sections
            for module in (section.get("modules") or [])
            if module.get("id")
        }

    def course_contents(self, courseid: int) -> list[dict[str, Any]]:
        """Return the sections and modules of one course."""
        return self.client.get_course_contents(courseid)
