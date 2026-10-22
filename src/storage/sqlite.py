"""SQLite storage for course documents.

The main table, ``course_document``: a row per course holding the full JSON.
The document is the only source of truth and is rewritten whole on every sync.
Dated items are queried out of the JSON with ``json_each`` rather than
duplicated into a second table.

Beside it, ``account`` holds a single row: who the last login was, and which
Moodle it was against. It is the one fact the course documents cannot carry,
because they are keyed by course and the person is not a property of a course.

``manual_status`` holds what the student said about an activity that Moodle
cannot know: "I handed this in elsewhere". It is the one table no sync writes,
so it is carried across the drop a sync starts with -- see ``CourseStore``.

Within one sync, writing is an upsert and never a delete: a course that stops
appearing keeps its old ``last_seen_at``, which distinguishes an unenrolment
from a failed fetch. Across syncs that distinction is currently moot, because
``fresh=True`` drops the file first and every run starts from an empty table.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

from ..config import ACTIVITY_GROUPS, DEFAULT_DB_PATH
from ..logs import get_logger

log = get_logger("storage")

SCHEMA = """
CREATE TABLE IF NOT EXISTS course_document (
    course_id      INTEGER PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    fetched_at     INTEGER NOT NULL,
    last_seen_at   INTEGER NOT NULL,
    doc            TEXT    NOT NULL,
    shortname TEXT GENERATED ALWAYS AS (json_extract(doc, '$.general.shortname')) STORED,
    fullname  TEXT GENERATED ALWAYS AS (json_extract(doc, '$.general.name'))      STORED
);

CREATE TABLE IF NOT EXISTS account (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    site_url   TEXT    NOT NULL,
    site_name  TEXT    NOT NULL,
    user_id    INTEGER,
    username   TEXT,
    first_name TEXT,
    last_name  TEXT,
    full_name  TEXT    NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS manual_status (
    course_id  INTEGER NOT NULL,
    item_key   TEXT    NOT NULL,
    done       INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (course_id, item_key)
);
"""

#: ``account`` column -> the ``core_webservice_get_site_info`` key it comes
#: from. Moodle spells these without separators; the columns do not.
ACCOUNT_FIELDS = {
    "site_url": "siteurl",
    "site_name": "sitename",
    "user_id": "userid",
    "username": "username",
    "first_name": "firstname",
    "last_name": "lastname",
    "full_name": "fullname",
}

#: Text columns of ``account``, all normalised the same way: stripped, and
#: empty turned into ``None`` so a blank is not mistaken for a name.
_ACCOUNT_TEXT = (
    "site_url", "site_name", "username", "first_name", "last_name", "full_name",
)

#: The database file plus the sidecars WAL mode leaves beside it. Deleting the
#: database alone would leave a WAL that SQLite replays into the new file.
DB_SIDECAR_SUFFIXES = ("", "-wal", "-shm")


def item_key(item: dict[str, Any]) -> str:
    """Identify one activity within its course, across syncs.

    A Moodle activity keeps its ``cmid`` for life. An extracted one has none,
    so its name is the only handle left; renaming it in the syllabus loses the
    mark, which is the honest outcome for an item the model may now read
    differently.
    """
    cmid = item.get("cmid")
    return f"cmid:{int(cmid)}" if cmid else f"name:{item.get('name') or ''}"


def drop_database(path: str | Path) -> list[Path]:
    """Delete a database and its WAL sidecars. Returns what was removed.

    Missing files are not an error: the point is to end up with no database,
    not to assert there was one.
    """
    removed = []
    for suffix in DB_SIDECAR_SUFFIXES:
        target = Path(str(path) + suffix)
        if target.exists():
            target.unlink()
            removed.append(target)
    return removed


#: One ``json_each`` arm per activity group. Every type is normalised to
#: ``due_at`` by the document builder, so the arms differ only in the group
#: they walk and the type they report. Workshops carry their open date as
#: ``submission_start_at``, hence the COALESCE.
_ARM = """
SELECT c.course_id,
       c.shortname,
       c.fullname,
       '{type}' AS type,
       json_extract(j.value, '$.cmid')              AS cmid,
       COALESCE(json_extract(j.value, '$.origin'), 'moodle') AS origin,
       json_extract(j.value, '$.name')              AS name,
       json_extract(j.value, '$.url')               AS url,
       COALESCE(json_extract(j.value, '$.opens_at'),
                json_extract(j.value, '$.submission_start_at')) AS opens_at,
       json_extract(j.value, '$.due_at')            AS due_at,
       json_extract(j.value, '$.is_graded')         AS is_graded,
       json_extract(j.value, '$.is_graded_already') AS is_graded_already,
       json_extract(j.value, '$.is_outstanding')    AS is_outstanding,
       json_extract(j.value, '$.grade_max')         AS grade_max,
       json_extract(j.value, '$.grade_achieved')    AS grade_achieved
FROM course_document c, json_each(c.doc, '$.{group}') j
WHERE json_extract(j.value, '$.cmid') IS NOT NULL
   OR json_extract(j.value, '$.origin') = 'extracted'
"""

#: Every dated item in the database, as if it were a table.
ACTIVITIES_VIEW = "\nUNION ALL\n".join(
    _ARM.format(group=group, type=type_name) for group, type_name in ACTIVITY_GROUPS.items()
)


class CourseStore:
    """Read/write access to the course database."""

    def __init__(self, path: str | Path = DEFAULT_DB_PATH, *, fresh: bool = False) -> None:
        """Open ``path`` and create the schema if it is missing.

        ``fresh`` deletes the file first, so the store opens on an empty table
        and the sync rebuilds every document instead of updating it. It defaults
        to ``False`` because only the sync may drop the database: every later
        pass opens the same store to read and extend what the sync wrote.

        Manual marks survive ``fresh``: they are read out before the drop and
        written back after, because nothing a sync fetches could rebuild them.
        """
        self.path = str(path)
        # A first run of a packaged build starts without its data directory.
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        kept = self._read_manual_status() if fresh else []
        self.dropped = drop_database(self.path) if fresh else []
        self._init_schema()
        if kept:
            with self.connect() as conn:
                conn.executemany(
                    "INSERT INTO manual_status (course_id, item_key, done, updated_at) "
                    "VALUES (:course_id, :item_key, :done, :updated_at)",
                    kept,
                )
            log.info("kept %d manual status mark(s) across the drop", len(kept))
        if self.dropped:
            log.info(
                "dropped %s before opening it",
                ", ".join(str(p) for p in self.dropped),
            )
        log.debug("store open at %s (fresh=%s)", self.path, fresh)

    # -- connection --------------------------------------------------------

    def connect(self) -> sqlite3.Connection:
        """Return a row-factory connection in WAL mode."""
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_schema(self) -> None:
        """Create the table if it does not exist."""
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    def _read_manual_status(self) -> list[dict[str, Any]]:
        """Every manual mark in the file as it stands, or none if it is absent.

        Runs before the schema exists, so an older database without the table
        is the normal case, not an error.
        """
        if not Path(self.path).exists():
            return []
        try:
            with self.connect() as conn:
                return [dict(r) for r in conn.execute("SELECT * FROM manual_status")]
        except sqlite3.OperationalError:
            return []

    # -- writing -----------------------------------------------------------

    def save(self, document: dict[str, Any], *, seen_at: int | None = None) -> dict[str, Any]:
        """Upsert one document. Returns the course id and its activity count."""
        seen_at = seen_at or int(time.time())
        course_id = int(document["course_id"])

        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO course_document
                    (course_id, schema_version, fetched_at, last_seen_at, doc)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(course_id) DO UPDATE SET
                    schema_version = excluded.schema_version,
                    fetched_at     = excluded.fetched_at,
                    last_seen_at   = excluded.last_seen_at,
                    doc            = excluded.doc
                """,
                (
                    course_id,
                    int(document.get("schema_version", 1)),
                    int(document.get("fetched_at", seen_at)),
                    seen_at,
                    json.dumps(document, ensure_ascii=False),
                ),
            )

        return {"course_id": course_id, "activities": _count_activities(document)}

    def save_many(self, documents: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        """Upsert several documents under one ``last_seen_at``."""
        seen_at = int(time.time())
        reports = [self.save(doc, seen_at=seen_at) for doc in documents]
        log.info(
            "stored %d course document(s) with %d activity(ies) in %s",
            len(reports),
            sum(r["activities"] for r in reports),
            self.path,
        )
        return reports

    def save_account(
        self,
        site_info: dict[str, Any],
        *,
        site_url: str | None = None,
        seen_at: int | None = None,
    ) -> dict[str, Any]:
        """Store who the site info belongs to. Returns the row as written.

        There is only ever one row, and ``id = 1`` is the constraint that says
        so. A second account would own a second set of courses, and the table
        those live in has no owner column -- so logging in somewhere else
        overwrites the identity rather than adding to it.

        ``site_url`` is a fallback for a site that answers without ``siteurl``;
        the session always knows the host it just talked to.
        """
        row: dict[str, Any] = {
            column: site_info.get(key) for column, key in ACCOUNT_FIELDS.items()
        }
        for column in _ACCOUNT_TEXT:
            row[column] = str(row[column] or "").strip() or None
        row["user_id"] = None if row["user_id"] is None else int(row["user_id"])
        row["site_url"] = row["site_url"] or str(site_url or "").strip()
        # A site that states no name is still an organisation to the reader;
        # its host is then the only label left to show.
        row["site_name"] = row["site_name"] or row["site_url"]
        row["full_name"] = (
            row["full_name"]
            or " ".join(p for p in (row["first_name"], row["last_name"]) if p)
            or row["username"]
            or ""
        )
        row["updated_at"] = seen_at or int(time.time())

        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO account
                    (id, site_url, site_name, user_id, username,
                     first_name, last_name, full_name, updated_at)
                VALUES (1, :site_url, :site_name, :user_id, :username,
                        :first_name, :last_name, :full_name, :updated_at)
                """,
                row,
            )
        log.info(
            "stored account: %s at %s (user %s)",
            row["full_name"] or row["username"],
            row["site_name"],
            row["user_id"],
        )
        return row

    def set_manual_status(self, course_id: int, key: str, done: bool) -> None:
        """Record that the student marked one activity as done or not done.

        "Not done" is stored rather than deleted, so the table keeps the last
        thing the student said about every item they touched.
        """
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO manual_status (course_id, item_key, done, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(course_id, item_key) DO UPDATE SET
                    done       = excluded.done,
                    updated_at = excluded.updated_at
                """,
                (int(course_id), key, int(bool(done)), int(time.time())),
            )
        log.info(
            "course %s, %s marked as %s", course_id, key, "done" if done else "not done"
        )

    # -- reading -----------------------------------------------------------

    def manual_status(self, course_id: int) -> dict[str, bool]:
        """Return item key -> done for every mark in one course."""
        with self.connect() as conn:
            return {
                r["item_key"]: bool(r["done"])
                for r in conn.execute(
                    "SELECT item_key, done FROM manual_status WHERE course_id = ?",
                    (int(course_id),),
                )
            }

    def account(self) -> dict[str, Any] | None:
        """Return the stored identity, or ``None`` before the first login."""
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM account WHERE id = 1").fetchone()
        if row is None:
            return None
        return {key: row[key] for key in row.keys() if key != "id"}

    def load(self, course_id: int) -> dict[str, Any] | None:
        """Return one course document, or ``None``."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT doc FROM course_document WHERE course_id = ?", (course_id,)
            ).fetchone()
        return json.loads(row["doc"]) if row else None

    def courses(self) -> list[dict[str, Any]]:
        """Return id, shortname, fullname and fetch time per stored course."""
        with self.connect() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT course_id, shortname, fullname, fetched_at "
                    "FROM course_document ORDER BY fullname"
                )
            ]

    def activities(
        self,
        *,
        course_id: int | None = None,
        type: str | None = None,
        dated_only: bool = False,
    ) -> list[dict[str, Any]]:
        """Return dated items across all courses, undated last.

        Filters on course, type, and whether a due date exists.
        """
        clauses, args = [], []
        if course_id is not None:
            clauses.append("course_id = ?")
            args.append(int(course_id))
        if type is not None:
            clauses.append("type = ?")
            args.append(type)
        if dated_only:
            clauses.append("due_at IS NOT NULL")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        with self.connect() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    f"SELECT * FROM ({ACTIVITIES_VIEW}) {where} "
                    "ORDER BY due_at IS NULL, due_at, shortname",
                    args,
                )
            ]

    def upcoming(self, *, days: int = 14, outstanding_only: bool = True) -> list[dict[str, Any]]:
        """Return items due within ``days``, soonest first."""
        now = int(time.time())
        clause = "AND is_outstanding = 1" if outstanding_only else ""
        with self.connect() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    f"""
                    SELECT * FROM ({ACTIVITIES_VIEW})
                    WHERE due_at BETWEEN ? AND ? {clause}
                    ORDER BY due_at
                    """,
                    (now, now + days * 86_400),
                )
            ]


def _count_activities(document: dict[str, Any]) -> int:
    """Count real items in a document.

    A blank placeholder has neither a cmid nor an origin; an extracted entry has
    an origin but no cmid, and counts.
    """
    return sum(
        1
        for group in ACTIVITY_GROUPS
        for item in document.get(group) or []
        if item.get("cmid") or item.get("origin")
    )
