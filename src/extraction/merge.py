"""Apply a proposal to a course document, additively.

A model instructed not to overwrite will comply almost always, and almost is
not a guarantee. The rule is stated in the prompt and enforced here, in code
that cannot do otherwise:

* a field that already has a value is never written, whatever the proposal
  says; the attempt is recorded as rejected, with both values,
* a field that is ``None`` may be filled,
* list entries are matched by ``cmid`` and may only gain keys,
* new entries are appended, never substituted, and carry an ``origin`` marker,
* nothing is removed. There is no code path that deletes.

The merge is pure: it takes a document and a proposal and returns a new
document plus a report. Every proposal appears in the report exactly once,
applied or rejected with a reason.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any

from ..config import (
    ENRICHABLE_FIELDS,
    EXTRACT_ACTIVITY_ARRAYS,
    EXTRACT_CONFIDENCE_LEVELS,
    EXTRACT_MIN_CONFIDENCE,
    EXTRACTED_ORIGIN,
)

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_RE = re.compile(r"^\d{2}:\d{2}$")


@dataclass
class MergeReport:
    """What the merge did, proposal by proposal.

    A rejection naming a value different from the document's is a conflict
    between a document and the API, and is flagged as such.
    """

    course_id: int
    applied: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)

    def accept(self, kind: str, detail: str, **extra: Any) -> None:
        """Record an applied proposal."""
        self.applied.append({"kind": kind, "detail": detail, **extra})

    def reject(self, kind: str, detail: str, reason: str, **extra: Any) -> None:
        """Record a refused proposal and why."""
        self.rejected.append({"kind": kind, "detail": detail, "reason": reason, **extra})

    def as_dict(self) -> dict[str, Any]:
        """Return the report with counts."""
        return {
            "course_id": self.course_id,
            "counts": {"applied": len(self.applied), "rejected": len(self.rejected)},
            "applied": self.applied,
            "rejected": self.rejected,
        }


def merge_additive(
    document: dict[str, Any],
    proposal: dict[str, Any],
    *,
    min_confidence: str = EXTRACT_MIN_CONFIDENCE,
) -> tuple[dict[str, Any], MergeReport]:
    """Return ``(extended_document, report)``. The input is never mutated."""
    merged = copy.deepcopy(document)
    report = MergeReport(course_id=int(document.get("course_id") or 0))
    threshold = _confidence_rank(min_confidence)

    _merge_credits(merged, proposal, report, threshold)
    _merge_list(merged, proposal, report, threshold, key="grading",
                identity=lambda e: _norm(e.get("name")))
    _merge_list(merged, proposal, report, threshold, key="mandatory_onsite",
                identity=lambda e: (str(e.get("date") or ""), _norm(e.get("name"))))
    _merge_new_activities(merged, proposal, report, threshold)
    _merge_enrichments(merged, proposal, report, threshold)
    return merged, report


# ----------------------------------------------------------------------
# One target at a time
# ----------------------------------------------------------------------


def _merge_credits(
    merged: dict[str, Any], proposal: dict[str, Any], report: MergeReport, threshold: int
) -> None:
    """Fill ``general.credits`` if it is currently ``None``."""
    entry = proposal.get("credits")
    if not isinstance(entry, dict) or entry.get("value") in (None, ""):
        return
    general = merged.setdefault("general", {})
    detail = f"general.credits = {entry.get('value')}"
    if not _confident(entry, threshold):
        report.reject("credits", detail, f"confidence {entry.get('confidence')!r}", **_prov(entry))
        return
    if general.get("credits") is not None:
        report.reject("credits", detail,
                      f"already set to {general['credits']!r}", **_prov(entry))
        return
    value = _coerce(entry.get("value"), "int")
    if value is None:
        report.reject("credits", detail, "not an integer", **_prov(entry))
        return
    general["credits"] = value
    report.accept("credits", detail, **_prov(entry))


def _merge_list(
    merged: dict[str, Any],
    proposal: dict[str, Any],
    report: MergeReport,
    threshold: int,
    *,
    key: str,
    identity,
) -> None:
    """Append-only merge for list fields no endpoint fills.

    These start empty, so there is nothing to overwrite; the only guard needed
    is against the same entry arriving twice, which ``identity`` decides.
    """
    entries = proposal.get(key)
    if not isinstance(entries, list):
        return
    target = merged.setdefault(key, [])
    seen = {identity(e) for e in target}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        detail = f"{key}: {name or '(unnamed)'}"
        if not name:
            report.reject(key, detail, "no name", **_prov(entry))
            continue
        if not _confident(entry, threshold):
            report.reject(key, detail, f"confidence {entry.get('confidence')!r}", **_prov(entry))
            continue
        marker = identity(entry)
        if marker in seen:
            report.reject(key, detail, "already present", **_prov(entry))
            continue
        seen.add(marker)
        target.append({**entry, "origin": EXTRACTED_ORIGIN})
        report.accept(key, detail, **_prov(entry))


def _merge_new_activities(
    merged: dict[str, Any], proposal: dict[str, Any], report: MergeReport, threshold: int
) -> None:
    """Append activities found only in prose, alongside the API's own.

    They land in the array a reader would look in, but with ``cmid: None`` and
    an ``origin`` marker, so a consumer can tell which rows the API asserted.
    Matching an existing name is a rejection.
    """
    entries = proposal.get("new_activities")
    if not isinstance(entries, list):
        return

    known: dict[str, set[str]] = {}
    for array in EXTRACT_ACTIVITY_ARRAYS.values():
        known[array] = {_norm(e.get("name")) for e in merged.get(array) or []}

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        kind = str(entry.get("type") or "").strip()
        name = str(entry.get("name") or "").strip()
        detail = f"new {kind or '?'}: {name or '(unnamed)'}"
        array = EXTRACT_ACTIVITY_ARRAYS.get(kind)
        if array is None:
            report.reject("new_activity", detail,
                          f"type {kind!r} not one of {sorted(EXTRACT_ACTIVITY_ARRAYS)}",
                          **_prov(entry))
            continue
        if not name:
            report.reject("new_activity", detail, "no name", **_prov(entry))
            continue
        if not _confident(entry, threshold):
            report.reject("new_activity", detail,
                          f"confidence {entry.get('confidence')!r}", **_prov(entry))
            continue
        if _norm(name) in known[array]:
            report.reject("new_activity", detail,
                          f"{array} already has an entry with this name", **_prov(entry))
            continue

        known[array].add(_norm(name))
        merged.setdefault(array, []).append({
            "cmid": None,
            "name": name,
            "due_date": _coerce(entry.get("due_date"), "date"),
            "due_time": _coerce(entry.get("due_time"), "time"),
            "description": str(entry.get("description") or "").strip() or None,
            "origin": EXTRACTED_ORIGIN,
            "source": entry.get("source"),
            "confidence": entry.get("confidence"),
            "quote": entry.get("quote"),
        })
        report.accept("new_activity", detail, **_prov(entry))


def _merge_enrichments(
    merged: dict[str, Any], proposal: dict[str, Any], report: MergeReport, threshold: int
) -> None:
    """Fill ``None`` fields on activities the API already returned.

    Three gates must all pass: the ``cmid`` must exist, the field must be
    enrichable, and the current value must be ``None``. The third makes
    overwriting impossible rather than discouraged.
    """
    entries = proposal.get("enrichments")
    if not isinstance(entries, list):
        return

    by_cmid: dict[int, dict[str, Any]] = {}
    for array in EXTRACT_ACTIVITY_ARRAYS.values():
        for item in merged.get(array) or []:
            if item.get("cmid") is not None:
                by_cmid[int(item["cmid"])] = item

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        field_name = str(entry.get("field") or "").strip()
        raw_cmid = entry.get("cmid")
        detail = f"cmid {raw_cmid}.{field_name or '?'} = {entry.get('value')!r}"
        try:
            cmid = int(raw_cmid)
        except (TypeError, ValueError):
            report.reject("enrichment", detail, "cmid is not an integer", **_prov(entry))
            continue
        target = by_cmid.get(cmid)
        if target is None:
            report.reject("enrichment", detail, "no activity with that cmid", **_prov(entry))
            continue
        kind = ENRICHABLE_FIELDS.get(field_name)
        if kind is None:
            report.reject("enrichment", detail, f"{field_name!r} is not enrichable",
                          **_prov(entry))
            continue
        if not _confident(entry, threshold):
            report.reject("enrichment", detail,
                          f"confidence {entry.get('confidence')!r}", **_prov(entry))
            continue
        if target.get(field_name) is not None:
            # The API supplied a value; a prose reading cannot replace it.
            report.reject("enrichment", detail,
                          f"already set to {target[field_name]!r}",
                          conflict=True, **_prov(entry))
            continue
        value = _coerce(entry.get("value"), kind)
        if value is None:
            report.reject("enrichment", detail, f"not a valid {kind}", **_prov(entry))
            continue

        target[field_name] = value
        # Record which fields on this entry did not come from the API.
        extracted = target.setdefault("extracted_fields", {})
        extracted[field_name] = {
            "source": entry.get("source"),
            "confidence": entry.get("confidence"),
            "quote": entry.get("quote"),
        }
        report.accept("enrichment", detail, **_prov(entry))


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _prov(entry: dict[str, Any]) -> dict[str, Any]:
    """Return the provenance an entry carries, for the report."""
    return {
        "source": entry.get("source"),
        "confidence": entry.get("confidence"),
        "quote": (str(entry.get("quote") or "")[:300]) or None,
    }


def _confidence_rank(level: Any) -> int:
    """Rank a confidence level; unknown values sort last."""
    try:
        return EXTRACT_CONFIDENCE_LEVELS.index(str(level).strip().lower())
    except ValueError:
        return len(EXTRACT_CONFIDENCE_LEVELS)


def _confident(entry: dict[str, Any], threshold: int) -> bool:
    """True when an entry's confidence meets the threshold."""
    return _confidence_rank(entry.get("confidence")) <= threshold


def _norm(value: Any) -> str:
    """Normalise a name for loose identity: case and spacing are ignored."""
    return re.sub(r"\s+", " ", str(value or "")).strip().lower()


def _coerce(value: Any, kind: str) -> Any:
    """Coerce a model-supplied value to the expected type, or ``None``.

    Returning ``None`` rejects the value. A wrong-typed field is worse than a
    missing one, since consumers rely on the stored format without re-parsing.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if kind == "text":
        return text
    if kind == "date":
        return text if _DATE_RE.match(text) else None
    if kind == "time":
        return text if _TIME_RE.match(text) else None
    if kind == "int":
        try:
            return int(float(text))
        except ValueError:
            return None
    if kind == "float":
        try:
            return float(text)
        except ValueError:
            return None
    if kind == "bool":
        if text.lower() in {"true", "yes", "1"}:
            return True
        if text.lower() in {"false", "no", "0"}:
            return False
        return None
    return None
