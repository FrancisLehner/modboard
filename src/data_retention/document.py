"""Build one JSON document per course from the pulled records.

Fills every field structured Moodle data can answer and leaves the rest
``None``; those are the extraction pass's input.

Two conventions from the schema:

* Dates are stored twice. ``due_at`` (unix) is the source of truth and the
  only field that may be sorted or compared; ``due_date``/``due_time`` are
  pre-rendered so consumers never format on the fly.
* ``None`` and ``[]`` differ. ``[]`` means fetched and genuinely none;
  ``None`` means never attempted.
"""

from __future__ import annotations

import datetime as dt
import time
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from ..config import DEFAULT_TIMEZONE, DOCUMENT_SCHEMA_VERSION
from ..logs import get_logger

log = get_logger("documents")


class CourseDocumentBuilder:
    """Assemble course documents from an authenticated session.

    ``build_all()`` makes one call per source rather than per course, since the
    ``*_by_courses`` endpoints accept a course list.
    """

    def __init__(self, session, *, timezone: str = DEFAULT_TIMEZONE) -> None:
        """Bind to a session and the timezone course dates are rendered in."""
        self.session = session
        self.tz = ZoneInfo(timezone)

    # -- date helpers ------------------------------------------------------

    def _split(self, timestamp: int | None) -> tuple[str | None, str | None]:
        """Split a unix timestamp into ``(date, time)`` strings; ``None`` stays ``None``.
        """
        if not timestamp:
            return None, None
        moment = dt.datetime.fromtimestamp(timestamp, self.tz)
        return moment.strftime("%Y-%m-%d"), moment.strftime("%H:%M")

    def _date(self, timestamp: int | None) -> str | None:
        """Return the date half of a unix timestamp."""
        return self._split(timestamp)[0]

    # -- per-type mappers --------------------------------------------------

    def _quiz(self, quiz: dict[str, Any], state: dict[str, Any] | None) -> dict[str, Any]:
        """Map a quiz record and its user state into document shape."""
        state = state or {}
        start_date, start_time = self._split(quiz.get("opens_at"))
        due_date, due_time = self._split(quiz.get("closes_at"))
        grade_max = quiz.get("grade_max")
        return {
            "cmid": quiz.get("cmid"),
            "name": quiz.get("name"),
            "start_date": start_date,
            "start_time": start_time,
            "due_date": due_date,
            "due_time": due_time,
            "opens_at": quiz.get("opens_at"),
            "due_at": quiz.get("closes_at"),
            "attempts_allowed": quiz.get("attempts_allowed"),
            "attempts_made": state.get("attempts_made"),
            # A quiz with no maximum grade cannot count toward the module mark.
            "is_graded": None if grade_max is None else grade_max > 0,
            "grade_max": grade_max,
            "grade_achieved": state.get("best_grade"),
            "is_graded_already": state.get("has_grade"),
            "is_outstanding": state.get("is_outstanding"),
            "url": quiz.get("url"),
        }

    def _assignment(
        self, assignment: dict[str, Any], state: dict[str, Any] | None
    ) -> dict[str, Any]:
        """Map an assignment record and its user state into document shape."""
        state = state or {}
        start_date, start_time = self._split(assignment.get("opens_at"))
        due_date, due_time = self._split(assignment.get("due_at"))
        cutoff_date, cutoff_time = self._split(assignment.get("cutoff_at"))
        grade_max = assignment.get("grade_max")
        return {
            "cmid": assignment.get("cmid"),
            "name": assignment.get("name"),
            "start_date": start_date,
            "start_time": start_time,
            "due_date": due_date,
            "due_time": due_time,
            "cutoff_date": cutoff_date,
            "cutoff_time": cutoff_time,
            "opens_at": assignment.get("opens_at"),
            "due_at": assignment.get("due_at"),
            "cutoff_at": assignment.get("cutoff_at"),
            "attempts_allowed": assignment.get("max_attempts"),
            "submission_status": state.get("submission_status"),
            "submitted_at": state.get("submitted_at"),
            "is_late": state.get("is_late"),
            "is_graded": None if grade_max is None else grade_max > 0,
            "grade_max": grade_max,
            "grade_achieved": None,  # needs the gradebook; not pulled yet
            "is_graded_already": state.get("graded"),
            "is_outstanding": state.get("is_outstanding"),
            "url": assignment.get("url"),
        }

    def _workshop(self, workshop: dict[str, Any]) -> dict[str, Any]:
        """Map a workshop record into document shape."""
        sub_start_d, sub_start_t = self._split(workshop.get("submission_start_at"))
        sub_end_d, sub_end_t = self._split(workshop.get("submission_end_at"))
        ass_start_d, ass_start_t = self._split(workshop.get("assessment_start_at"))
        ass_end_d, ass_end_t = self._split(workshop.get("assessment_end_at"))
        grade_max = workshop.get("submission_grade_max")
        return {
            "cmid": workshop.get("cmid"),
            "name": workshop.get("name"),
            "submission_start_date": sub_start_d,
            "submission_start_time": sub_start_t,
            "submission_end_date": sub_end_d,
            "submission_end_time": sub_end_t,
            "assessment_start_date": ass_start_d,
            "assessment_start_time": ass_start_t,
            "assessment_end_date": ass_end_d,
            "assessment_end_time": ass_end_t,
            "submission_start_at": workshop.get("submission_start_at"),
            # The submission end is the deadline a student acts on.
            "due_at": workshop.get("submission_end_at"),
            "assessment_end_at": workshop.get("assessment_end_at"),
            "phase": workshop.get("phase"),
            "is_graded": None if grade_max is None else grade_max > 0,
            "grade_max": grade_max,
            "grade_achieved": None,
            "is_graded_already": None,
            "is_outstanding": None,
            "url": workshop.get("url"),
        }

    def _lesson(self, lesson: dict[str, Any]) -> dict[str, Any]:
        """Map a lesson record into document shape."""
        start_date, start_time = self._split(lesson.get("available_from_at"))
        due_date, due_time = self._split(lesson.get("deadline_at"))
        grade_max = lesson.get("grade_max")
        limit = lesson.get("time_limit_sec")
        return {
            "cmid": lesson.get("cmid"),
            "name": lesson.get("name"),
            "start_date": start_date,
            "start_time": start_time,
            "due_date": due_date,
            "due_time": due_time,
            "opens_at": lesson.get("available_from_at"),
            "due_at": lesson.get("deadline_at"),
            "time_limit_min": (limit // 60) if limit else None,
            "retakes_allowed": lesson.get("retakes_allowed"),
            "is_graded": None if grade_max is None else grade_max > 0,
            "grade_max": grade_max,
            "grade_achieved": None,
            "is_graded_already": None,
            "is_outstanding": None,
            "url": lesson.get("url"),
        }

    def _general(self, course: dict[str, Any]) -> dict[str, Any]:
        """Map course metadata into the document's ``general`` block."""
        return {
            "course_id": course.get("id"),
            "name": course.get("fullname"),
            "shortname": course.get("shortname"),
            "lecturers": course.get("contacts"),
            "credits": None,  # phase 2: not exposed by any Moodle endpoint
            "category": course.get("category") or None,
            "format": course.get("format"),
            "start_date": self._date(course.get("startdate")),
            "end_date": self._date(course.get("enddate")),
            "url": course.get("url"),
        }

    # -- assembly ----------------------------------------------------------

    def build_all(self, course_ids: Iterable[int] | None = None) -> list[dict[str, Any]]:
        """Build one document per favourite course, or per id in ``course_ids``."""
        courses = self.session.favourite_courses()
        if course_ids is not None:
            wanted = {int(c) for c in course_ids}
            courses = [c for c in courses if c["id"] in wanted]
        ids = [c["id"] for c in courses]
        if not ids:
            log.warning("no courses to build documents for -- nothing starred in Moodle?")
            return []

        # The course list does not carry lecturers.
        details = {c["id"]: c for c in self.session.course_by_field(ids)}
        for course in courses:
            course.setdefault("contacts", None)
            detail = details.get(course["id"])
            if detail:
                course["contacts"] = detail.get("contacts")

        quizzes = self.session.quizzes(ids, include_empty=False)
        assignments = self.session.assignments(ids, include_empty=False)
        workshops = self.session.workshops(ids, include_empty=False)
        lessons = self.session.lessons(ids, include_empty=False)

        log.info(
            "retrieved for %d course(s): %d quiz(zes), %d assignment(s), "
            "%d workshop(s), %d lesson(s)",
            len(ids),
            len(quizzes),
            len(assignments),
            len(workshops),
            len(lessons),
        )

        quiz_state = {q["instance"]: q for q in self.session.quiz_status(quizzes)}
        assign_state = {
            a["instance"]: a for a in self.session.assignment_status(assignments)
        }

        now = int(time.time())
        documents = []
        for course in courses:
            cid = course["id"]
            documents.append(
                {
                    "schema_version": DOCUMENT_SCHEMA_VERSION,
                    "course_id": cid,
                    "fetched_at": now,
                    "general": self._general(course),
                    "quizzes": [
                        self._quiz(q, quiz_state.get(q["instance"]))
                        for q in quizzes
                        if q["course_id"] == cid
                    ],
                    "assignments": [
                        self._assignment(a, assign_state.get(a["instance"]))
                        for a in assignments
                        if a["course_id"] == cid
                    ],
                    "workshops": [
                        self._workshop(w) for w in workshops if w["course_id"] == cid
                    ],
                    "lessons": [
                        self._lesson(l) for l in lessons if l["course_id"] == cid
                    ],
                    # Filled by the extraction pass.
                    "grading": [],
                    "mandatory_onsite": [],
                    "sync": {
                        "fetched_at": now,
                    },
                }
            )
        for document in documents:
            log.debug(
                "built %s: %d quiz(zes), %d assignment(s), %d workshop(s), %d lesson(s)",
                document["general"].get("shortname") or document["course_id"],
                len(document["quizzes"]),
                len(document["assignments"]),
                len(document["workshops"]),
                len(document["lessons"]),
            )
        log.info("built %d course document(s)", len(documents))
        return documents

    def build(self, course_id: int) -> dict[str, Any] | None:
        """Build the document for one course, or ``None`` if it is not a favourite."""
        documents = self.build_all([course_id])
        return documents[0] if documents else None
