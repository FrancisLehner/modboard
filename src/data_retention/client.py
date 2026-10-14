"""Read-only REST client for Moodle web services.

Moodle reports application errors with HTTP 200 and a JSON body containing
``exception``/``errorcode``, so ``raise_for_status()`` sees nothing wrong.
This client turns those bodies into ``MoodleApiError``.

It also enforces an allowlist of callable functions, which makes a write call
impossible to express. Pass ``allowed_functions=`` to widen it, or ``None`` to
disable the check.

Every call leaves a line in the session log: the function, how long it took
and how much came back at ``DEBUG``, the reason at ``ERROR`` when it failed.
That record is what makes a sync that went wrong readable afterwards -- which
request was the last one, and whether the site refused it or never answered.
"""

from __future__ import annotations

import time
from typing import Any, Iterable

import requests

from ..config import DEFAULT_ALLOWED_FUNCTIONS, ERROR_HINTS, HTTP_TIMEOUT
from ..errors import MoodleApiError
from ..logs import get_logger

log = get_logger("moodle.client")


def _describe(payload: Any) -> str:
    """Summarise what came back, for the log, without logging its contents.

    Collections are counted, since the count is the fact worth having. An
    empty ``warnings`` is dropped: nearly every payload carries one, and
    "0 warnings" said on every line is noise that also hides the answer --
    a best-grade reply is a handful of scalar fields, not a list.
    """
    if isinstance(payload, list):
        return f"{len(payload)} record(s)"
    if isinstance(payload, dict):
        counts = [
            f"{len(value)} {key}"
            for key, value in payload.items()
            if isinstance(value, list) and (value or key != "warnings")
        ]
        return ", ".join(counts) if counts else f"{len(payload)} field(s)"
    return type(payload).__name__


class MoodleClient:
    """POST/form-encoded client for ``webservice/rest/server.php``."""

    def __init__(
        self,
        site_url: str,
        token: str,
        *,
        timeout: int = HTTP_TIMEOUT,
        allowed_functions: Iterable[str] | None = DEFAULT_ALLOWED_FUNCTIONS,
    ):
        """Bind to one site and token; require HTTPS."""
        self.site_url = site_url.rstrip("/")
        if not self.site_url.startswith("https://"):
            raise ValueError("Refusing to send a token over a non-HTTPS connection.")
        self._token = token
        self.timeout = timeout
        #: ``None`` disables the check entirely.
        self.allowed_functions = (
            None if allowed_functions is None else frozenset(allowed_functions)
        )
        self.endpoint = f"{self.site_url}/webservice/rest/server.php"
        log.debug(
            "client bound to %s (%s allowlisted function(s), timeout %ss)",
            self.endpoint,
            "all" if self.allowed_functions is None else len(self.allowed_functions),
            self.timeout,
        )

    def __repr__(self) -> str:  # keeps the token out of notebook output
        """Describe the client without exposing the token."""
        return f"MoodleClient(site_url={self.site_url!r}, token=<hidden>)"

    # -- the one call everything else goes through -------------------------

    def call(self, wsfunction: str, **params: Any) -> Any:
        """Call one web-service function and return its decoded payload.

        Nested lists and dicts in ``params`` are flattened into Moodle's
        ``name[0][key]`` form. Raises ``ValueError`` if the function is not
        allowlisted, and ``MoodleApiError`` on a network or Moodle-reported error.
        """
        if self.allowed_functions is not None and wsfunction not in self.allowed_functions:
            log.error("%s blocked: not in this client's allowlist", wsfunction)
            raise ValueError(
                f"{wsfunction!r} is not in this client's allowlist "
                f"({', '.join(sorted(self.allowed_functions))}). Pass "
                "allowed_functions= to widen it, or None to disable the check."
            )

        data = {
            "wstoken": self._token,
            "wsfunction": wsfunction,
            "moodlewsrestformat": "json",
        }
        data.update(self._flatten_params(params))

        started = time.monotonic()
        try:
            response = requests.post(self.endpoint, data=data, timeout=self.timeout)
            response.raise_for_status()
        except requests.RequestException as exc:
            log.error(
                "%s: network error after %.2fs: %s", wsfunction, time.monotonic() - started, exc
            )
            raise MoodleApiError(f"Network error calling {wsfunction}: {exc}") from exc

        try:
            payload = response.json()
        except ValueError as exc:
            # Never echo the body: an error page could contain the token.
            log.error(
                "%s: HTTP %s with a non-JSON body (%s bytes)",
                wsfunction,
                response.status_code,
                len(response.content),
            )
            raise MoodleApiError(
                f"{wsfunction} returned a non-JSON response "
                f"(HTTP {response.status_code}). The endpoint may not be a "
                "Moodle web-service URL."
            ) from exc

        try:
            result = self._raise_for_moodle_error(payload, wsfunction)
        except MoodleApiError as exc:
            log.error("%s: %s", wsfunction, str(exc).replace("\n", " "))
            raise
        log.debug(
            "%s -> %s in %.2fs", wsfunction, _describe(result), time.monotonic() - started
        )
        return result

    # -- convenience wrappers ---------------------------------------------

    def get_site_info(self) -> dict[str, Any]:
        """Validate the token and return the caller's identity and site details."""
        return self.call("core_webservice_get_site_info")

    def get_users_courses(self, userid: int) -> list[dict[str, Any]]:
        """Return every course the given user is enrolled in."""
        result = self.call("core_enrol_get_users_courses", userid=userid)
        return list(result) if isinstance(result, list) else []

    def get_enrolled_courses_by_classification(
        self,
        classification: str = "favourites",
        *,
        limit: int = 0,
        offset: int = 0,
        sort: str | None = None,
    ) -> dict[str, Any]:
        """Return courses in one timeline bucket.

        ``classification`` is one of ``all``, ``inprogress``, ``future``, ``past``,
        ``favourites``, ``hidden``, ``allincludinghidden``. ``limit=0`` means no
        limit. Returns ``{"courses": [...], "nextoffset": n}``.
        """
        params: dict[str, Any] = {
            "classification": classification,
            "limit": limit,
            "offset": offset,
        }
        if sort:
            params["sort"] = sort
        return self.call(
            "core_course_get_enrolled_courses_by_timeline_classification", **params
        )

    def get_course_contents(self, courseid: int) -> list[dict[str, Any]]:
        """Return the sections and modules of one course."""
        result = self.call("core_course_get_contents", courseid=courseid)
        return list(result) if isinstance(result, list) else []

    def get_assignments(self, courseids: Iterable[int]) -> dict[str, Any]:
        """Return ``{"courses": [{"assignments": [...]}]}``."""
        return self.call("mod_assign_get_assignments", courseids=list(courseids))

    def get_quizzes_by_courses(self, courseids: Iterable[int]) -> dict[str, Any]:
        """Return ``{"quizzes": [...], "warnings": [...]}``."""
        return self.call("mod_quiz_get_quizzes_by_courses", courseids=list(courseids))

    def get_workshops_by_courses(self, courseids: Iterable[int]) -> dict[str, Any]:
        """Return ``{"workshops": [...]}``."""
        return self.call("mod_workshop_get_workshops_by_courses", courseids=list(courseids))

    def get_lessons_by_courses(self, courseids: Iterable[int]) -> dict[str, Any]:
        """Return ``{"lessons": [...]}``."""
        return self.call("mod_lesson_get_lessons_by_courses", courseids=list(courseids))

    def get_activities_completion_status(self, courseid: int, userid: int) -> dict[str, Any]:
        """Return ``{"statuses": [...]}`` for every activity in one course."""
        return self.call(
            "core_completion_get_activities_completion_status",
            courseid=courseid,
            userid=userid,
        )

    def get_assign_submission_status(
        self, assignid: int, userid: int | None = None
    ) -> dict[str, Any]:
        """Return ``{"lastattempt": {...}, "feedback": {...}}`` for one assignment."""
        params: dict[str, Any] = {"assignid": assignid}
        if userid is not None:
            params["userid"] = userid
        return self.call("mod_assign_get_submission_status", **params)

    def get_quiz_user_attempts(
        self, quizid: int, userid: int | None = None, *, status: str = "all"
    ) -> dict[str, Any]:
        """Return ``{"attempts": [...]}`` for one quiz."""
        params: dict[str, Any] = {"quizid": quizid, "status": status, "includepreviews": 0}
        if userid is not None:
            params["userid"] = userid
        return self.call("mod_quiz_get_user_attempts", **params)

    def get_quiz_best_grade(self, quizid: int, userid: int | None = None) -> dict[str, Any]:
        """Return ``{"hasgrade": bool, "grade": float}`` for one quiz."""
        params: dict[str, Any] = {"quizid": quizid}
        if userid is not None:
            params["userid"] = userid
        return self.call("mod_quiz_get_user_best_grade", **params)

    def get_action_events_by_courses(
        self, courseids: Iterable[int], *, timesortfrom: int | None = None
    ) -> dict[str, Any]:
        """Return calendar action events for the given courses."""
        params: dict[str, Any] = {"courseids": list(courseids)}
        if timesortfrom is not None:
            params["timesortfrom"] = timesortfrom
        return self.call("core_calendar_get_action_events_by_courses", **params)

    def get_action_events_by_timesort(
        self,
        *,
        timesortfrom: int | None = None,
        timesortto: int | None = None,
        limitnum: int = 50,
    ) -> dict[str, Any]:
        """Return upcoming action events across every course, ordered by due date."""
        params: dict[str, Any] = {"limitnum": limitnum}
        if timesortfrom is not None:
            params["timesortfrom"] = timesortfrom
        if timesortto is not None:
            params["timesortto"] = timesortto
        return self.call("core_calendar_get_action_events_by_timesort", **params)

    # -- free text (see docs/data-sources.qmd, section D) ------------------

    def get_course_blocks(self, courseid: int) -> dict[str, Any]:
        """Return ``{"blocks": [...]}`` including rendered block contents."""
        return self.call(
            "core_block_get_course_blocks", courseid=courseid, returncontents=True
        )

    def get_pages_by_courses(self, courseids: Iterable[int]) -> dict[str, Any]:
        """Return ``{"pages": [...]}`` with each page's full body."""
        return self.call("mod_page_get_pages_by_courses", courseids=list(courseids))

    def get_labels_by_courses(self, courseids: Iterable[int]) -> dict[str, Any]:
        """Return ``{"labels": [...]}``."""
        return self.call("mod_label_get_labels_by_courses", courseids=list(courseids))

    def get_urls_by_courses(self, courseids: Iterable[int]) -> dict[str, Any]:
        """Return ``{"urls": [...]}``."""
        return self.call("mod_url_get_urls_by_courses", courseids=list(courseids))

    def get_books_by_courses(self, courseids: Iterable[int]) -> dict[str, Any]:
        """Return ``{"books": [...]}``."""
        return self.call("mod_book_get_books_by_courses", courseids=list(courseids))

    def get_forums_by_courses(self, courseids: Iterable[int]) -> list[dict[str, Any]]:
        """Return a flat list of forums. This endpoint does not wrap its result."""
        result = self.call("mod_forum_get_forums_by_courses", courseids=list(courseids))
        return list(result) if isinstance(result, list) else []

    def get_forum_discussions(
        self, forumid: int, *, page: int = 0, perpage: int = 0, sortorder: int = 1
    ) -> dict[str, Any]:
        """Return ``{"discussions": [...]}``; ``perpage=0`` means every discussion.

        ``sortorder`` is an integer enum, not the ``sortby``/``sortdirection``
        string pair the superseded ``..._paginated`` function took. ``1`` is last
        post first.
        """
        return self.call(
            "mod_forum_get_forum_discussions",
            forumid=forumid,
            sortorder=sortorder,
            page=page,
            perpage=perpage,
        )

    def get_discussion_posts(self, discussionid: int) -> dict[str, Any]:
        """Return ``{"posts": [...]}`` for one discussion."""
        return self.call(
            "mod_forum_get_discussion_posts",
            discussionid=discussionid,
            sortby="created",
            sortdirection="ASC",
        )

    def get_lesson_pages(self, lessonid: int) -> dict[str, Any]:
        """Return ``{"pages": [...]}`` for one lesson."""
        return self.call("mod_lesson_get_pages", lessonid=lessonid)

    def get_lesson_page_data(self, lessonid: int, pageid: int) -> dict[str, Any]:
        """Return ``{"contentfiles": [...]}`` for one lesson page."""
        return self.call(
            "mod_lesson_get_page_data", lessonid=lessonid, pageid=pageid
        )

    # -- internals ---------------------------------------------------------

    @classmethod
    def _flatten_params(cls, params: Any, prefix: str = "") -> dict[str, Any]:
        """Flatten nested params: ``{"ids": [7, 9]}`` -> ``{"ids[0]": 7, "ids[1]": 9}``.
        """
        flat: dict[str, Any] = {}
        if isinstance(params, dict):
            for key, value in params.items():
                name = f"{prefix}[{key}]" if prefix else str(key)
                flat.update(cls._flatten_params(value, name))
        elif isinstance(params, (list, tuple)):
            for index, value in enumerate(params):
                flat.update(cls._flatten_params(value, f"{prefix}[{index}]"))
        elif isinstance(params, bool):
            flat[prefix] = 1 if params else 0
        elif params is not None:
            flat[prefix] = params
        return flat

    @staticmethod
    def _raise_for_moodle_error(payload: Any, wsfunction: str) -> Any:
        """Raise ``MoodleApiError`` if the payload is an error envelope."""
        if isinstance(payload, dict) and (
            "exception" in payload or "errorcode" in payload
        ):
            errorcode = str(payload.get("errorcode") or "") or None
            exception = str(payload.get("exception") or "") or None
            message = str(payload.get("message") or "Unknown Moodle error").strip()
            hint = ERROR_HINTS.get(errorcode or "")
            text = f"Moodle rejected {wsfunction}: {message}"
            if errorcode:
                text += f" [errorcode: {errorcode}]"
            if hint:
                text += f"\n{hint}"
            raise MoodleApiError(text, errorcode=errorcode, exception=exception)
        return payload
