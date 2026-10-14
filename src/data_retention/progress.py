"""Per-user activity state: attempts, submissions and completion.

Separate from the activity records because these are answers about the
caller, not about the course, and they cost one call per activity rather than
one per course.
"""

from __future__ import annotations

from typing import Any

from .activities import ActivityRecord


class QuizStatusRecord(ActivityRecord):
    """One quiz's attempt count and best grade for the current user."""

    MODNAME = "quiz"
    FIELDS = (
        "attempts_made",
        "last_attempt_state",
        "last_attempt_at",
        "last_attempt_sum_grades",
        "best_grade",
        "has_grade",
        "grade_max",
        "is_attempted",
        "is_outstanding",
    )

    #: Attempt states Moodle reports; "finished" is the only one that counts.
    FINISHED_STATES = ("finished",)

    @classmethod
    def from_payload(
        cls,
        quiz: dict[str, Any],
        attempts: list[dict[str, Any]],
        best_grade: dict[str, Any] | None,
        *,
        closes_at: int | None = None,
    ) -> dict[str, Any]:
        """Build a record from the attempts and best-grade payloads."""
        record = cls.blank()
        # The quiz record from Channel 1 already carries the identity block.
        for key in cls.IDENTITY_FIELDS:
            record[key] = quiz.get(key)
        record["type"] = "quiz_status"

        real = [a for a in attempts if not a.get("preview")]
        last = real[-1] if real else None
        best_grade = best_grade or {}
        has_grade = best_grade.get("hasgrade")
        state = str(last.get("state")) if last and last.get("state") else None

        record.update(
            {
                "attempts_made": len(real),
                "last_attempt_state": state,
                "last_attempt_at": cls.timestamp(
                    (last or {}).get("timefinish") or (last or {}).get("timestart")
                ),
                "last_attempt_sum_grades": cls.number((last or {}).get("sumgrades")),
                "best_grade": cls.number(best_grade.get("grade")) if has_grade else None,
                "has_grade": cls.flag(has_grade),
                "grade_max": cls.number(quiz.get("grade_max")),
                "is_attempted": bool(real),
                # Outstanding = nothing finished yet and the window has not shut.
                "is_outstanding": not any(
                    a.get("state") in cls.FINISHED_STATES for a in real
                ),
            }
        )
        if closes_at is not None:
            record["closes_at"] = closes_at
        return record

    @classmethod
    def blank(cls) -> dict[str, Any]:
        """Return the record shape with every field set to ``None``."""
        record = super().blank()
        record["closes_at"] = None
        return record


class AssignStatusRecord(ActivityRecord):
    """One assignment's submission and grading state for the current user."""

    MODNAME = "assign"
    FIELDS = (
        "submission_status",
        "grading_status",
        "graded",
        "submitted_at",
        "attempt_number",
        "can_submit",
        "submissions_enabled",
        "locked",
        "extension_due_at",
        "submitted_files",
        "is_outstanding",
        "is_late",
    )

    @classmethod
    def from_payload(
        cls,
        assignment: dict[str, Any],
        status: dict[str, Any],
    ) -> dict[str, Any]:
        """Build a record from a ``mod_assign_get_submission_status`` payload."""
        record = cls.blank()
        for key in cls.IDENTITY_FIELDS:
            record[key] = assignment.get(key)
        record["type"] = "assign_status"

        last = status.get("lastattempt") or {}
        submission = last.get("submission") or {}
        submitted_at = cls.timestamp(submission.get("timemodified"))
        submission_status = submission.get("status") or None
        due_at = assignment.get("due_at")

        files: list[dict[str, Any]] = []
        for plugin in submission.get("plugins") or []:
            for area in plugin.get("fileareas") or []:
                for f in area.get("files") or []:
                    files.append(
                        {
                            "filename": f.get("filename"),
                            "fileurl": f.get("fileurl"),
                            "filesize": f.get("filesize"),
                            "timemodified": f.get("timemodified"),
                        }
                    )

        record.update(
            {
                "submission_status": submission_status,
                "grading_status": last.get("gradingstatus"),
                "graded": cls.flag(last.get("graded")),
                "submitted_at": submitted_at,
                "attempt_number": submission.get("attemptnumber"),
                "can_submit": cls.flag(last.get("cansubmit")),
                "submissions_enabled": cls.flag(last.get("submissionsenabled")),
                "locked": cls.flag(last.get("locked")),
                "extension_due_at": cls.timestamp(last.get("extensionduedate")),
                "submitted_files": files or None,
                "is_outstanding": (
                    None if submission_status is None else submission_status != "submitted"
                ),
                # Moodle does not hand us a "late" flag; it is the comparison
                # of the submission time against the due date.
                "is_late": (
                    None
                    if not (submitted_at and due_at)
                    else submitted_at > due_at
                ),
            }
        )
        return record


class CompletionRecord(ActivityRecord):
    """One activity's completion state for the current user."""

    MODNAME = "completion"
    FIELDS = (
        "modname",
        "state",
        "state_label",
        "completed",
        "time_completed",
        "tracking",
        "tracking_label",
        "is_automatic",
        "has_completion",
        "is_tracked_user",
        "user_visible",
        "override_by",
        "is_overall_complete",
    )

    STATE_LABELS = {0: "incomplete", 1: "complete", 2: "complete_pass", 3: "complete_fail"}
    TRACKING_LABELS = {0: "none", 1: "manual", 2: "automatic"}

    @classmethod
    def from_payload(
        cls,
        raw: dict[str, Any],
        *,
        course_id: int,
        course_name: str | None,
        name: str | None = None,
    ) -> dict[str, Any]:
        """Build a record from one completion-status entry."""
        record = cls.blank()
        state = raw.get("state")
        tracking = raw.get("tracking")
        record.update(
            {
                "type": "completion",
                "course_id": course_id or None,
                "course_name": course_name,
                "cmid": raw.get("cmid"),
                "instance": raw.get("instance"),
                # The payload has no activity name; callers can join on cmid.
                "name": name,
                "modname": raw.get("modname"),
                "state": state,
                "state_label": cls.STATE_LABELS.get(state),
                # None when nothing is tracked -- never a bare False, which
                # would read as "not done".
                "completed": None if tracking == 0 else state in (1, 2),
                "time_completed": cls.timestamp(raw.get("timecompleted")),
                "tracking": tracking,
                "tracking_label": cls.TRACKING_LABELS.get(tracking),
                "is_automatic": cls.flag(raw.get("isautomatic")),
                "has_completion": cls.flag(raw.get("hascompletion")),
                "is_tracked_user": cls.flag(raw.get("istrackeduser")),
                "user_visible": cls.flag(raw.get("uservisible")),
                "override_by": raw.get("overrideby"),
                "is_overall_complete": cls.flag(raw.get("isoverallcomplete")),
            }
        )
        return record
