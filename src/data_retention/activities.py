"""Activity records built from the ``*_by_courses`` endpoints.

Each module type gets its own record shape. A shared one would force a lossy
choice at extraction time, since a workshop carries four phase timestamps and
an assignment three distinct dates. What the shapes share is an identity
block, so records of different types stay joinable.

Moodle writes ``0`` into timestamps it has no value for; every timestamp here
passes through ``ActivityRecord.timestamp`` so "not set" arrives as ``None``.
"""

from __future__ import annotations

from typing import Any


class ActivityRecord:
    """Shared identity block and field coercion for every activity type."""

    #: Present in all four record shapes, so records stay joinable.
    IDENTITY_FIELDS = (
        "type",
        "course_id",
        "course_name",
        "cmid",
        "instance",
        "name",
        "url",
        "intro",
        "visible",
    )

    #: Type-specific fields; each subclass redefines this.
    FIELDS: tuple[str, ...] = ()

    #: Moodle module name, used to build the view URL.
    MODNAME = ""

    @classmethod
    def blank(cls) -> dict[str, Any]:
        """Return the record shape with every field set to ``None``."""
        return {name: None for name in cls.IDENTITY_FIELDS + cls.FIELDS}

    # -- field hygiene -----------------------------------------------------

    @staticmethod
    def timestamp(value: Any) -> int | None:
        """Coerce a Moodle timestamp; ``0`` and missing become ``None``."""
        try:
            number = int(value)
        except (TypeError, ValueError):
            return None
        return number or None

    @staticmethod
    def text(value: Any) -> str | None:
        """Strip an HTML fragment to plain text; empty becomes ``None``."""
        cleaned = None
        return cleaned or None

    @staticmethod
    def number(value: Any) -> float | None:
        """Coerce to ``float``, or ``None`` if not numeric."""
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def flag(value: Any) -> bool | None:
        """Coerce to ``bool``, or ``None`` if absent."""
        return None if value is None else bool(value)

    @classmethod
    def identity(
        cls,
        raw: dict[str, Any],
        *,
        course_id: int,
        course_name: str | None,
        site_url: str,
    ) -> dict[str, Any]:
        """Build the shared identity fields from one payload entry."""
        cmid = raw.get("coursemodule") or raw.get("cmid")
        return {
            "type": cls.MODNAME,
            "course_id": course_id or None,
            "course_name": course_name or None,
            "cmid": int(cmid) if cmid else None,
            "instance": int(raw["id"]) if raw.get("id") else None,
            "name": (str(raw.get("name") or "").strip() or None),
            "url": (
                f"{site_url}/mod/{cls.MODNAME}/view.php?id={cmid}" if cmid and site_url else None
            ),
            "intro": cls.text(raw.get("intro")),
            "visible": cls.flag(raw.get("visible")),
        }


class QuizRecord(ActivityRecord):
    """One quiz from ``mod_quiz_get_quizzes_by_courses``."""

    MODNAME = "quiz"
    FIELDS = (
        "opens_at",
        "closes_at",
        "time_limit_sec",
        "attempts_allowed",
        "grade_max",
        "grade_method",
        "overdue_handling",
        "has_time_window",
    )

    @classmethod
    def from_payload(
        cls, raw: dict[str, Any], *, course_id: int, course_name: str | None, site_url: str
    ) -> dict[str, Any]:
        """Build a record from one quiz payload entry."""
        record = cls.blank()
        record.update(
            cls.identity(raw, course_id=course_id, course_name=course_name, site_url=site_url)
        )
        opens = cls.timestamp(raw.get("timeopen"))
        closes = cls.timestamp(raw.get("timeclose"))
        record.update(
            {
                "opens_at": opens,
                "closes_at": closes,
                # 0 means "no limit" here, which is information, not absence --
                # but it is reported as None so the field reads uniformly.
                "time_limit_sec": cls.timestamp(raw.get("timelimit")),
                # 0 means unlimited attempts: kept as 0, not None.
                "attempts_allowed": (
                    int(raw["attempts"]) if raw.get("attempts") is not None else None
                ),
                "grade_max": cls.number(raw.get("grade")),
                "grade_method": raw.get("grademethod"),
                "overdue_handling": raw.get("overduehandling"),
                "has_time_window": bool(opens or closes),
            }
        )
        return record


class AssignRecord(ActivityRecord):
    """One assignment from ``mod_assign_get_assignments``."""

    MODNAME = "assign"
    FIELDS = (
        "opens_at",
        "due_at",
        "cutoff_at",
        "grading_due_at",
        "max_attempts",
        "team_submission",
        "submissions_enabled",
        "grade_max",
        "intro_attachments",
    )

    @classmethod
    def from_payload(
        cls, raw: dict[str, Any], *, course_id: int, course_name: str | None, site_url: str
    ) -> dict[str, Any]:
        """Build a record from one assignment payload entry."""
        record = cls.blank()
        record.update(
            cls.identity(raw, course_id=course_id, course_name=course_name, site_url=site_url)
        )
        # The assignment payload calls the course-module id "cmid".
        if record["cmid"] is None and raw.get("cmid"):
            record["cmid"] = int(raw["cmid"])
            record["url"] = f"{site_url}/mod/assign/view.php?id={raw['cmid']}"
        attachments = [
            {
                "filename": f.get("filename"),
                "fileurl": f.get("fileurl"),
                "filesize": f.get("filesize"),
                "mimetype": f.get("mimetype"),
                "timemodified": f.get("timemodified"),
            }
            for f in (raw.get("introattachments") or [])
            if f.get("fileurl")
        ]
        record.update(
            {
                "opens_at": cls.timestamp(raw.get("allowsubmissionsfromdate")),
                "due_at": cls.timestamp(raw.get("duedate")),
                "cutoff_at": cls.timestamp(raw.get("cutoffdate")),
                "grading_due_at": cls.timestamp(raw.get("gradingduedate")),
                # -1 means unlimited; kept verbatim rather than flattened.
                "max_attempts": (
                    int(raw["maxattempts"]) if raw.get("maxattempts") is not None else None
                ),
                "team_submission": cls.flag(raw.get("teamsubmission")),
                "submissions_enabled": (
                    None if raw.get("nosubmissions") is None else not raw["nosubmissions"]
                ),
                "grade_max": cls.number(raw.get("grade")),
                "intro_attachments": attachments or None,
            }
        )
        return record


class WorkshopRecord(ActivityRecord):
    """One workshop from ``mod_workshop_get_workshops_by_courses``."""

    MODNAME = "workshop"
    FIELDS = (
        "submission_start_at",
        "submission_end_at",
        "assessment_start_at",
        "assessment_end_at",
        "phase",
        "late_submissions_allowed",
        "submission_grade_max",
        "assessment_grade_max",
    )

    @classmethod
    def from_payload(
        cls, raw: dict[str, Any], *, course_id: int, course_name: str | None, site_url: str
    ) -> dict[str, Any]:
        """Build a record from one workshop payload entry."""
        record = cls.blank()
        record.update(
            cls.identity(raw, course_id=course_id, course_name=course_name, site_url=site_url)
        )
        record.update(
            {
                "submission_start_at": cls.timestamp(raw.get("submissionstart")),
                "submission_end_at": cls.timestamp(raw.get("submissionend")),
                "assessment_start_at": cls.timestamp(raw.get("assessmentstart")),
                "assessment_end_at": cls.timestamp(raw.get("assessmentend")),
                "phase": raw.get("phase"),
                "late_submissions_allowed": cls.flag(raw.get("latesubmissions")),
                "submission_grade_max": cls.number(raw.get("grade")),
                "assessment_grade_max": cls.number(raw.get("gradinggrade")),
            }
        )
        return record


class LessonRecord(ActivityRecord):
    """One lesson from ``mod_lesson_get_lessons_by_courses``."""

    MODNAME = "lesson"
    FIELDS = (
        "available_from_at",
        "deadline_at",
        "time_limit_sec",
        "retakes_allowed",
        "grade_max",
    )

    @classmethod
    def from_payload(
        cls, raw: dict[str, Any], *, course_id: int, course_name: str | None, site_url: str
    ) -> dict[str, Any]:
        """Build a record from one lesson payload entry."""
        record = cls.blank()
        record.update(
            cls.identity(raw, course_id=course_id, course_name=course_name, site_url=site_url)
        )
        record.update(
            {
                "available_from_at": cls.timestamp(raw.get("available")),
                "deadline_at": cls.timestamp(raw.get("deadline")),
                "time_limit_sec": cls.timestamp(raw.get("timelimit")),
                "retakes_allowed": cls.flag(raw.get("retake")),
                "grade_max": cls.number(raw.get("grade")),
            }
        )
        return record
