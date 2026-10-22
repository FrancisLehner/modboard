"""Turn stored course documents into the shape the dashboard renders.

The database holds one JSON document per course, built by the sync and extended
by the two model passes. The dashboard wants something else: deadlines bucketed
by urgency, a grade broken into weighted components, and every activity sitting
under the component it counts towards.

Three of those are a reshuffle of stored fields. The fourth -- which activity
belongs to which grading component -- is nowhere in the data: Moodle's
gradebook and the weights read out of a syllabus are unrelated records with no
shared key. ``match_components`` guesses the link from the names and marks
every guess, so the frontend can show what was inferred rather than fetched.
See ``docs/not-implemented.md`` for the full list of gaps and why they are
gaps.

Nothing here reads the clock for a decision: ``today`` is computed once, passed
out with the payload, and the frontend derives every "in N days" from it. One
reference date for the whole page means a render at midnight cannot disagree
with itself.
"""

from __future__ import annotations

import datetime as dt
import re
import time
from typing import Any
from zoneinfo import ZoneInfo

from ..config import ACTIVITY_GROUPS, DEFAULT_TIMEZONE
from ..storage.sqlite import item_key

#: Moodle activity type -> the grading-component type the design colours by.
#: Workshops and lessons have no counterpart and fall back to "other".
ACTIVITY_TYPE_TO_COMPONENT_TYPE = {
    "quiz": "quiz",
    "assign": "assignment",
    "workshop": "other",
    "lesson": "other",
}

#: Human label per Moodle activity type, used when an activity could not be
#: tied to a grading component and has no component name to show.
ACTIVITY_TYPE_LABELS = {
    "quiz": "Quiz",
    "assign": "Assignment",
    "workshop": "Workshop",
    "lesson": "Lesson",
}

#: Shown in the component slot of an activity that matched nothing. Naming the
#: absence is the point: the item is real, its share of the grade is not known.
UNMATCHED_LABEL = "Not in the grading scheme"

#: Dropped before comparing two names. Function words carry no signal and two
#: German titles sharing only "und" would otherwise score as related.
STOPWORDS = frozenset({
    "and", "the", "for", "with", "from", "all", "any", "its", "not", "are",
    "was", "has", "per", "via", "end", "one", "two", "each", "also", "plus",
    "der", "die", "das", "den", "dem", "des", "und", "von", "vom", "mit",
    "zum", "zur", "bis", "auf", "aus", "ein", "eine", "als", "wird", "sind",
    "jeweils", "vor", "ort",
})

#: A token also counts as a hit when one side is a prefix of the other and both
#: are at least this long: "challenge" ~ "challenges", "part" ~ "partb".
#: Below four characters prefixes collide too easily ("lab" ~ "labor").
PREFIX_MIN = 4

#: Weight per pair of fields compared, activity side first. A name is chosen
#: to identify a thing; a description merely mentions things, and on both
#: sides it is the noisier signal. GStat's "Quiz 2" matches "Quiz 2 (Bonus)"
#: on its name while its description, which says the bonus is 0.1, mentions a
#: "1" that belongs to the other component -- so name-to-name has to outweigh
#: anything the descriptions contribute.
NAME_TO_NAME = 4
NAME_TO_DESCRIPTION = 2
DESCRIPTION_TO_NAME = 1
TYPE_BONUS = 2

#: A type agreement alone never links an activity to a component -- every quiz
#: would land under the first quiz component. At least one word has to match.
MIN_WORD_HITS = 1

_WORD_SPLIT = re.compile(r"[^0-9a-zäöüàéèêçß]+")

#: Trailing role in a name, as Moodle returns it: "Mark Cieliebak (T
#: Professor)" for a lecturer, "Fynn Fischer (T LE-T-BA-DS-24HS-T)" for the
#: logged-in student, whose suffix is the class group. Noise in both places.
_ROLE_SUFFIX = re.compile(r"\s*\([^)]*\)\s*$")

#: Northern-hemisphere term label from the month a course starts in.
_TERMS = ((9, 12, "Autumn"), (1, 1, "Autumn"), (2, 6, "Spring"), (7, 8, "Summer"))

#: Trailing semester marker in a Moodle shortname: "DS.PM3 HS26" -> "DS.PM3",
#: "GStat DS 26" -> "GStat DS". Dropped only when something is left over, so
#: "IMLHS26", which has no separator, survives whole.
_TERM_SUFFIX = re.compile(r"[\s_-]+(?:HS|FS|SS|WS)?\s?\d{2,4}$", re.IGNORECASE)

#: Longest sidebar chip. Beyond this the course name has no room left.
CODE_MAX = 10


# ----------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------


def today_iso(timezone: str = DEFAULT_TIMEZONE) -> str:
    """Return today's date in the institution's timezone as ``YYYY-MM-DD``."""
    return dt.datetime.now(ZoneInfo(timezone)).date().isoformat()


def _tokens(*texts: Any) -> set[str]:
    """Split several strings into comparable words.

    Words shorter than three characters are noise -- the "B" of "Part B", the
    "in" of "Hand-in" -- with one exception: a bare number is often the only
    thing separating two components. "Quiz 1 (Bonus)" and "Quiz 2 (Bonus)"
    differ by a single digit and nothing else.
    """
    words: set[str] = set()
    for text in texts:
        for word in _WORD_SPLIT.split(str(text or "").lower()):
            if (len(word) > 2 or word.isdigit()) and word not in STOPWORDS:
                words.add(word)
    return words


def _hits(left: set[str], right: set[str]) -> int:
    """Count words of ``left`` present in ``right``, prefixes included."""
    found = 0
    for word in left:
        if word in right:
            found += 1
        elif len(word) >= PREFIX_MIN and any(
            len(other) >= PREFIX_MIN and (other.startswith(word) or word.startswith(other))
            for other in right
        ):
            found += 1
    return found


def _lecturers(names: list[str] | None) -> list[str]:
    """Strip the role suffix from each lecturer and drop repeats."""
    seen: list[str] = []
    for name in names or []:
        clean = _ROLE_SUFFIX.sub("", str(name)).strip()
        if clean and clean not in seen:
            seen.append(clean)
    return seen


def _term_label(start_date: str | None) -> str | None:
    """Return ``Autumn 2026`` for a course starting in September 2026."""
    if not start_date:
        return None
    try:
        date = dt.date.fromisoformat(start_date)
    except ValueError:
        return None
    for first, last, label in _TERMS:
        if first <= date.month <= last:
            return f"{label} {date.year}"
    return str(date.year)


def short_code(shortname: str) -> str:
    """Shorten a Moodle shortname to something that fits a chip.

    The semester is already on the page once, in the term label, so repeating
    it in every chip costs width and says nothing.
    """
    code = _TERM_SUFFIX.sub("", str(shortname or "")).strip() or str(shortname or "")
    return code if len(code) <= CODE_MAX else code[: CODE_MAX - 1].rstrip() + "…"


def _percent(value: float) -> int:
    """Round a ratio in 0..1 to whole percent."""
    return int(round(value * 100))


# ----------------------------------------------------------------------
# Activities
# ----------------------------------------------------------------------


def activity_status(item: dict[str, Any], marked_done: bool = False) -> str:
    """Classify one activity as ``graded``, ``submitted``, ``done`` or ``open``.

    Read off the fields the sync writes, in that order of certainty: a grade
    settles it, otherwise a recorded submission does, otherwise the item is
    still the student's to do. An extracted activity has none of these fields
    and is therefore always open -- Moodle never saw it, so nothing can have
    been handed in through it.

    ``done`` is the student's own word, from a manual mark. It only ever
    replaces ``open``: when Moodle records a submission or a grade, that is
    the better answer and the mark is moot.
    """
    if item.get("is_graded_already") or item.get("grade_achieved") is not None:
        return "graded"
    if item.get("submission_status") == "submitted":
        return "submitted"
    return "done" if marked_done else "open"


def _activity_view(
    item: dict[str, Any], activity_type: str, manual: dict[str, bool]
) -> dict[str, Any]:
    """Reshape one stored activity into what a row in the UI needs."""
    key = item_key(item)
    status = activity_status(item, manual.get(key, False))
    achieved, maximum = item.get("grade_achieved"), item.get("grade_max")
    return {
        "name": item.get("name") or "(unnamed)",
        "activity_type": activity_type,
        "type": ACTIVITY_TYPE_TO_COMPONENT_TYPE.get(activity_type, "other"),
        "due": item.get("due_date"),
        "due_time": item.get("due_time"),
        "due_at": item.get("due_at"),
        "status": status,
        "points": [achieved, maximum] if status == "graded" and achieved is not None else None,
        "grade_max": maximum,
        "url": item.get("url"),
        "cmid": item.get("cmid"),
        # What POST /api/courses/<id>/status takes to mark this item.
        "key": key,
        # "extracted" means a model read this out of a document; anything the
        # Moodle API returned has no origin.
        "origin": item.get("origin") or "moodle",
        "confidence": item.get("confidence"),
        "source": item.get("source"),
        "description": item.get("description") or "",
        # Moodle's own gradebook flag. Overridden below by the matched
        # component's weight, which is the better answer when there is one.
        "counts": item.get("is_graded"),
        "component": None,
        "matched": False,
    }


def _activities(
    document: dict[str, Any], manual: dict[str, bool]
) -> list[dict[str, Any]]:
    """Every real activity of a course, across all four groups.

    A document reserves a slot per group even when a course has none, and the
    sync leaves placeholder entries with neither a ``cmid`` nor an origin.
    Those are not activities and are dropped here, the same test
    ``CourseStore`` uses when it counts them.
    """
    out: list[dict[str, Any]] = []
    for group, activity_type in ACTIVITY_GROUPS.items():
        for item in document.get(group) or []:
            if item.get("cmid") or item.get("origin"):
                out.append(_activity_view(item, activity_type, manual))
    return out


# ----------------------------------------------------------------------
# Linking activities to grading components
# ----------------------------------------------------------------------


def score_match(activity: dict[str, Any], component: dict[str, Any]) -> tuple[int, int]:
    """Score how well an activity fits a grading component.

    Returns ``(score, word_hits)``. ``word_hits`` is reported separately
    because a score made only of the type bonus is not a match at all: every
    quiz would otherwise land under whichever quiz component came first.
    """
    activity_name = _tokens(activity["name"])
    activity_text = _tokens(activity.get("description"))
    component_name = _tokens(component["name"])
    component_text = _tokens(component.get("description"))

    pairs = (
        (NAME_TO_NAME, _hits(activity_name, component_name)),
        (NAME_TO_DESCRIPTION, _hits(activity_name, component_text)),
        (DESCRIPTION_TO_NAME, _hits(activity_text, component_name)),
    )
    same_type = component.get("type") == activity["type"]
    score = sum(weight * hits for weight, hits in pairs) + (TYPE_BONUS if same_type else 0)
    return score, sum(hits for _, hits in pairs)


def match_components(
    activities: list[dict[str, Any]], components: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Attach each activity to its best-scoring component, or to none.

    Mutates the activities in place and returns the ones nothing claimed. An
    activity can only belong to one component, a component takes any number of
    activities, and a component with no match keeps an empty list -- a 70%
    final exam that exists only in a syllabus is the normal case, not an error.
    """
    for component in components:
        component["items"] = []

    unmatched: list[dict[str, Any]] = []
    for activity in activities:
        best, best_score = None, 0
        for component in components:
            score, word_hits = score_match(activity, component)
            if word_hits >= MIN_WORD_HITS and score > best_score:
                best, best_score = component, score
        if best is None:
            activity["component"] = UNMATCHED_LABEL
            activity["component_label"] = ACTIVITY_TYPE_LABELS.get(
                activity["activity_type"], "Activity"
            )
            unmatched.append(activity)
            continue
        activity["matched"] = True
        activity["component"] = best["name"]
        activity["component_label"] = best["name"]
        activity["match_score"] = best_score
        weight = best.get("weight")
        # The component's weight is a better answer than Moodle's gradebook
        # flag: it says how much the item is worth, not merely that a grade
        # column exists. A component with no stated weight answers neither,
        # so the flag is left as it was.
        if weight is not None:
            activity["counts"] = weight > 0
        best["items"].append(activity)
    return unmatched


# ----------------------------------------------------------------------
# One course
# ----------------------------------------------------------------------


def _component_view(entry: dict[str, Any]) -> dict[str, Any]:
    """Reshape one stored grading component."""
    return {
        "name": entry.get("name") or "(unnamed)",
        "type": entry.get("type") or "other",
        "weight": entry.get("weight_percent"),
        "rule": entry.get("description") or "",
        "source": entry.get("source"),
        "confidence": entry.get("confidence"),
        "origin": entry.get("origin") or "moodle",
        "items": [],
    }


def _onsite_view(entry: dict[str, Any]) -> dict[str, Any]:
    """Reshape one mandatory on-site session."""
    return {
        "date": entry.get("date"),
        "time": entry.get("time"),
        "name": entry.get("name") or "(unnamed)",
        "description": entry.get("description") or "",
        "source": entry.get("source"),
        "confidence": entry.get("confidence"),
    }


def _component_progress(component: dict[str, Any]) -> dict[str, Any]:
    """Count how far through a component's activities the student is.

    Progress is counted in items rather than in each item's share of the
    weight, because that share is not recorded anywhere: a syllabus says
    "labs 30%", never "lab 2 is 7.5% of it". Equal weight per item is the
    only split the data supports.

    An item the student marked as done counts as submitted: handed in, not
    yet graded as far as anything stored knows.
    """
    items = component["items"]
    total = len(items)
    graded = sum(1 for i in items if i["status"] == "graded")
    submitted = sum(1 for i in items if i["status"] in ("submitted", "done"))
    return {
        "items_total": total,
        "items_graded": graded,
        "items_submitted": submitted,
        "items_done": graded + submitted,
        "graded_ratio": graded / total if total else 0.0,
        "submitted_ratio": submitted / total if total else 0.0,
        "done_ratio": (graded + submitted) / total if total else 0.0,
    }


def course_view(
    document: dict[str, Any], manual: dict[str, bool] | None = None
) -> dict[str, Any]:
    """Build the dashboard view of one stored course document.

    ``manual`` is the course's item key -> done marks, from
    ``CourseStore.manual_status``.
    """
    general = document.get("general") or {}
    components = [_component_view(entry) for entry in document.get("grading") or []]
    activities = _activities(document, manual or {})
    unmatched = match_components(activities, components)

    for component in components:
        component.update(_component_progress(component))

    weighted = [c for c in components if c["weight"] is not None]
    weighted_total = sum(c["weight"] for c in weighted)
    # Share of the final grade already handed in. Components without a stated
    # weight cannot contribute; a course where none has one reports null
    # rather than a confident 0%.
    handed_in = (
        _percent(sum(c["weight"] * c["done_ratio"] for c in weighted) / 100)
        if weighted
        else None
    )

    lecturers = _lecturers(general.get("lecturers"))
    sources = {
        entry.get("source")
        for entry in (document.get("grading") or []) + (document.get("mandatory_onsite") or [])
        if entry.get("source")
    }
    onsite = [_onsite_view(entry) for entry in document.get("mandatory_onsite") or []]
    onsite.sort(key=lambda o: o["date"] or "9999")

    return {
        "course_id": document.get("course_id"),
        "id": general.get("shortname") or str(document.get("course_id")),
        "code": short_code(general.get("shortname") or str(document.get("course_id"))),
        "name": general.get("name") or "(unnamed course)",
        "lecturers": lecturers,
        "lecturer": lecturers[0] if lecturers else None,
        "ects": general.get("credits"),
        "category": general.get("category"),
        "term": _term_label(general.get("start_date")),
        "start_date": general.get("start_date"),
        "end_date": general.get("end_date"),
        "url": general.get("url"),
        "grading": components,
        "unmatched": unmatched,
        "onsite": onsite,
        "sources": len(sources),
        "weighted_total": weighted_total if weighted else None,
        "weights_known": len(weighted) == len(components) and bool(components),
        "handed_in_percent": handed_in,
        "fetched_at": document.get("fetched_at"),
        "extracted_at": (document.get("sync") or {}).get("extracted_at"),
        # Fields the design asks for that no document records. Sent as nulls on
        # purpose: the frontend hides the block rather than inventing a value.
        "pass_rule": None,
        "notes": None,
    }


# ----------------------------------------------------------------------
# The whole dashboard
# ----------------------------------------------------------------------


def _term_window(courses: list[dict[str, Any]], today: str) -> dict[str, Any]:
    """Derive "week N of M" from the earliest start and latest end date.

    Courses do not agree on when the semester starts -- Moodle's course start
    is whatever the course was configured with -- so this is the span of the
    courses on the dashboard, not an official academic calendar.
    """
    starts = [c["start_date"] for c in courses if c["start_date"]]
    ends = [c["end_date"] for c in courses if c["end_date"]]
    if not starts or not ends:
        return {"week": None, "weeks": None, "label": None}
    start, end = dt.date.fromisoformat(min(starts)), dt.date.fromisoformat(max(ends))
    now = dt.date.fromisoformat(today)
    weeks = max(1, round((end - start).days / 7))
    week = (now - start).days // 7 + 1
    return {
        "week": min(max(week, 1), weeks) if start <= now else None,
        "weeks": weeks,
        "label": _term_label(start.isoformat()),
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
    }


def _user(store) -> dict[str, Any]:
    """The person the last sync logged in as, for the greeting and the sidebar.

    ``first_name`` is sent separately because the greeting wants it alone:
    "Good evening, Fynn" reads as a greeting where the full name reads as a
    header. The sidebar's second line is the organisation the token belongs to,
    which is the one thing about the account that is actually stored -- the
    degree programme the design put there is in no record. See
    ``docs/not-implemented.md``.
    """
    account = store.account()
    if not account:
        return {"name": None, "first_name": None, "organisation": None}
    # The site info spells the full name with the class group attached, the
    # same way it spells a lecturer with their role. Both are stored as Moodle
    # sent them and cleaned here.
    full_name = _ROLE_SUFFIX.sub("", account["full_name"] or "").strip() or None
    first_name = _ROLE_SUFFIX.sub("", account["first_name"] or "").strip()
    return {
        "name": full_name,
        # A site that returns no firstname still has a full name to cut.
        "first_name": first_name or (full_name or "").split(" ")[0] or None,
        "organisation": account["site_name"] or None,
    }


def dashboard(store, *, timezone: str = DEFAULT_TIMEZONE) -> dict[str, Any]:
    """Build the whole payload the frontend renders, newest sync included."""
    documents = [
        store.load(row["course_id"]) for row in store.courses()
    ]
    courses = [
        course_view(doc, store.manual_status(doc["course_id"])) for doc in documents if doc
    ]
    courses.sort(key=lambda c: c["name"])
    today = today_iso(timezone)
    synced = [c["fetched_at"] for c in courses if c["fetched_at"]]

    return {
        "today": today,
        "now": int(time.time()),
        "synced_at": max(synced) if synced else None,
        "term": _term_window(courses, today),
        "user": _user(store),
        "courses": courses,
    }
