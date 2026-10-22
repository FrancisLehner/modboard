"""Harvest a course's prose and files verbatim to disk.

Structured endpoints answer what Moodle keeps in fields. Much of what a course
tells its students is not in any field: it is in a page module, a label, a
sidebar block, a forum announcement, or an attached file. None of that is
queryable, so this module collects it and writes it to a directory with an
index recording where each piece came from.

Nothing is summarised, converted or interpreted: HTML stays HTML, files stay
bytes, and files are deduplicated by ``fileurl`` rather than by filename.

A harvest updates the previous one in place unless told to start fresh. A file
is downloaded again only when Moodle reports a different ``timemodified`` or
size for its ``fileurl``; a text is rewritten only when its content differs.
Every item records when its bytes were retrieved, their ``sha256``, and
whether this run found it ``new``, ``modified`` or ``unchanged``, so the passes
after this one can skip what they have already seen. Whatever a course no
longer lists is deleted from disk.

Layout produced::

    <root>/
      manifest.json                 index with provenance for every item
      <course_id>-<shortname>/
        text/0007_page_<slug>.html
        files/0003_<slug>.pdf
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

import requests

from ..config import (
    CORPUS_DIR_PREFIX,
    CORPUS_SCHEMA_VERSION,
    DEFAULT_CORPUS_ROOT,
    DOWNLOAD_CHUNK,
    DOWNLOAD_TIMEOUT,
    EXTRACTION_FILENAME,
    HARVEST_INCLUDE_FORUM_POSTS,
    HARVEST_INCLUDE_LESSON_FILES,
    MANIFEST_FILENAME,
    MAX_FILE_BYTES,
    MAX_NAME_LENGTH,
    SCREENING_FILENAME,
    SKIP_MIMETYPE_PREFIXES,
    SYNC_REBUILDS_FROM_SCRATCH,
)
from ..errors import MoodleApiError
from ..logs import echo, get_logger

log = get_logger("harvest")

#: Characters no filesystem we care about tolerates in a name.
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

#: Everything a run writes into a corpus root: the three index files, plus one
#: directory per course. Clearing names these rather than emptying the
#: directory, so pointing ``root`` at the wrong place deletes nothing of yours.
_INDEX_FILENAMES = (MANIFEST_FILENAME, SCREENING_FILENAME, EXTRACTION_FILENAME)
_COURSE_DIR = re.compile(r"^\d+-")


# ----------------------------------------------------------------------
# Disk side
# ----------------------------------------------------------------------


def sha256_file(path: Path) -> str:
    """Hash a stored item's bytes -- the identity later passes cache by."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(DOWNLOAD_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def clear_corpus(root: str | Path) -> list[Path]:
    """Remove a previous harvest from ``root``. Returns what was removed.

    Only what a run writes is deleted -- the index files and the
    ``<course_id>-<shortname>`` directories -- never the directory itself and
    never anything else inside it. A corpus is derived data and re-pullable, but
    a root is a path a caller typed, so the blast radius stays named rather than
    "whatever is in here".
    """
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        return []

    removed = []
    for name in _INDEX_FILENAMES:
        target = root / name
        if target.is_file():
            target.unlink()
            removed.append(target)
    for entry in sorted(root.iterdir()):
        if entry.is_dir() and _COURSE_DIR.match(entry.name):
            shutil.rmtree(entry)
            removed.append(entry)
    return removed


class CorpusWriter:
    """Owns the output directory: safe names, unique paths, the manifest.

    With no ``root`` it falls back to :data:`DEFAULT_CORPUS_ROOT`, and if that is
    ``None`` to a temp directory it creates and keeps -- rather than using
    ``TemporaryDirectory``, since later passes read it after this process exits.
    """

    def __init__(
        self,
        root: str | Path | None = None,
        *,
        prefix: str = CORPUS_DIR_PREFIX,
        fresh: bool = SYNC_REBUILDS_FROM_SCRATCH,
    ):
        """Resolve or create the output directory, clearing it when ``fresh``."""
        root = DEFAULT_CORPUS_ROOT if root is None else root
        if root is None:
            self.root = Path(tempfile.mkdtemp(prefix=prefix))
            self.is_temporary = True
            self.cleared: list[Path] = []
        else:
            self.root = Path(root).expanduser().resolve()
            self.is_temporary = False
            # Before mkdir, so a half-written corpus is cleared too.
            self.cleared = clear_corpus(self.root) if fresh else []
            self.root.mkdir(parents=True, exist_ok=True)
        self._counters: dict[Path, int] = {}

    def __repr__(self) -> str:
        """Describe the writer and its root."""
        return f"CorpusWriter(root={str(self.root)!r})"

    # -- naming ------------------------------------------------------------

    @staticmethod
    def safe_name(name: Any, *, max_length: int = MAX_NAME_LENGTH) -> str:
        """Reduce any string to one safe path component. Never empty."""
        text = _UNSAFE.sub("", str(name or "")).strip(". ")
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) > max_length:
            text = text[:max_length].rstrip(". ")
        return text or "untitled"

    @classmethod
    def slug(cls, name: Any, *, max_length: int = MAX_NAME_LENGTH) -> str:
        """Lowercase a name and replace runs of non-alphanumerics with hyphens."""
        text = cls.safe_name(name, max_length=max_length).lower()
        text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
        return text or "untitled"

    # -- directories -------------------------------------------------------

    def course_dir(self, course_id: Any, shortname: Any) -> Path:
        """Return the directory for one course, creating it if needed."""
        directory = self.root / f"{course_id}-{self.slug(shortname, max_length=40)}"
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def _next_index(self, directory: Path) -> int:
        """Return the next running number for a directory, so pull order is kept.

        Seeded from what is already there, so a file added by a later harvest
        is numbered after the ones an earlier harvest kept.
        """
        if directory not in self._counters:
            existing = (
                int(m.group(1))
                for entry in (directory.iterdir() if directory.is_dir() else ())
                if (m := re.match(r"^(\d+)_", entry.name))
            )
            self._counters[directory] = max(existing, default=0)
        self._counters[directory] += 1
        return self._counters[directory]

    def _unique(self, directory: Path, stem: str, suffix: str) -> Path:
        """Return an unused path in a directory, numbered and suffixed."""
        directory.mkdir(parents=True, exist_ok=True)
        base = f"{self._next_index(directory):04d}_{stem}"
        candidate = directory / f"{base}{suffix}"
        attempt = 2
        while candidate.exists():
            candidate = directory / f"{base}-{attempt}{suffix}"
            attempt += 1
        return candidate

    # -- writing -----------------------------------------------------------

    def write_text(self, course_dir: Path, kind: str, name: Any, body: str) -> Path:
        """Write one prose item verbatim. Returns the path written."""
        stem = f"{self.slug(kind, max_length=20)}_{self.slug(name)}"
        path = self._unique(course_dir / "text", stem, ".html")
        path.write_text(body, encoding="utf-8")
        return path

    def file_path(self, course_dir: Path, filename: Any) -> Path:
        """Reserve a path for a download, keeping the original extension."""
        safe = self.safe_name(filename)
        suffix = Path(safe).suffix[:12]
        stem = self.slug(Path(safe).stem)
        return self._unique(course_dir / "files", stem, suffix)

    def read_manifest(self) -> dict[str, Any] | None:
        """Return the manifest a previous harvest left in the root, if any."""
        path = self.root / MANIFEST_FILENAME
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def prune(self, keep: set[str], course_dirs: set[str]) -> list[Path]:
        """Delete stored items not in ``keep`` and course dirs not in ``course_dirs``.

        ``keep`` holds root-relative paths. Only what a run writes is touched,
        as in :func:`clear_corpus`.
        """
        removed = []
        for entry in sorted(self.root.iterdir()):
            if not (entry.is_dir() and _COURSE_DIR.match(entry.name)):
                continue
            if entry.name not in course_dirs:
                shutil.rmtree(entry)
                removed.append(entry)
                continue
            for sub in ("text", "files"):
                directory = entry / sub
                if not directory.is_dir():
                    continue
                for item in sorted(directory.iterdir()):
                    if item.is_file() and self.relative(item) not in keep:
                        item.unlink()
                        removed.append(item)
        return removed

    def write_manifest(self, manifest: dict[str, Any]) -> Path:
        """Write the manifest into the root. Returns the path."""
        path = self.root / MANIFEST_FILENAME
        path.write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return path

    def relative(self, path: Path) -> str:
        """Return a path relative to the root, so the corpus can be moved."""
        try:
            return str(path.relative_to(self.root))
        except ValueError:  # pragma: no cover - only if a caller passes a stray path
            return str(path)


# ----------------------------------------------------------------------
# Pull side
# ----------------------------------------------------------------------


@dataclass
class CourseHarvest:
    """What one course yielded, and what failed while yielding it.

    ``None`` and ``[]`` differ: ``[]`` is asked and genuinely nothing, ``None``
    is no answer obtained, with ``warnings`` saying why.
    """

    course_id: int
    shortname: str
    fullname: str
    directory: str
    texts: list[dict[str, Any]] = field(default_factory=list)
    files: list[dict[str, Any]] = field(default_factory=list)
    links: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: Fetched ``fileurl``s, so the same bytes are never pulled twice. Not part
    #: of the manifest -- it is bookkeeping, not provenance.
    seen_urls: set[str] = field(default_factory=set, repr=False)
    #: ``(cmid, body digest)`` of prose already written, and ``(cmid, url)`` of
    #: links already recorded. Several endpoints return the *same* module:
    #: a label's ``description`` in ``core_course_get_contents`` is its
    #: ``intro`` in ``mod_label_get_labels_by_courses``, and a ``url`` module
    #: shows up in both ``contents`` and ``mod_url``. Keyed by cmid rather than
    #: by text alone, so genuinely repeated boilerplate in two different
    #: modules still gets recorded twice -- which is the truth about the course.
    seen_texts: set[tuple[Any, str]] = field(default_factory=set, repr=False)
    seen_links: set[tuple[Any, str]] = field(default_factory=set, repr=False)
    #: The previous harvest's items for this course, so unchanged ones are
    #: reused rather than fetched again. Texts by ``(cmid, sha256)``, files by
    #: ``fileurl``; the ``*_names`` sets only tell "modified" from "new".
    previous_texts: dict[tuple[Any, str], dict[str, Any]] = field(
        default_factory=dict, repr=False
    )
    previous_files: dict[str, dict[str, Any]] = field(default_factory=dict, repr=False)
    previous_text_names: set[tuple[Any, ...]] = field(default_factory=set, repr=False)
    previous_file_names: set[tuple[Any, ...]] = field(default_factory=set, repr=False)

    def stored_paths(self) -> set[str]:
        """Every root-relative path this course's manifest entry points at."""
        return {
            item["stored_path"]
            for item in (*self.texts, *self.files)
            if item.get("stored_path")
        }

    def as_dict(self) -> dict[str, Any]:
        """Return the manifest entry for this course, with counts."""
        downloaded = [f for f in self.files if f.get("stored_path")]
        changes = [i.get("change") for i in (*self.texts, *downloaded)]
        return {
            "course_id": self.course_id,
            "shortname": self.shortname,
            "fullname": self.fullname,
            "directory": self.directory,
            "counts": {
                "texts": len(self.texts),
                "files_seen": len(self.files),
                "files_downloaded": len(downloaded),
                "bytes_downloaded": sum(f.get("bytes") or 0 for f in downloaded),
                "links": len(self.links),
                "warnings": len(self.warnings),
                "new": changes.count("new"),
                "modified": changes.count("modified"),
                "unchanged": changes.count("unchanged"),
            },
            "texts": self.texts,
            "files": self.files,
            "links": self.links,
            "warnings": self.warnings,
        }


class UnstructuredHarvester:
    """Pull every prose and file source of a course into a directory.

    Batched ``*_by_courses`` sources are fetched once for all courses and then
    grouped. Per-course sources cost one call each, and forums one more per
    discussion.

    Every source is wrapped individually: a disabled forum, or an endpoint the
    site does not expose, produces a warning in the manifest and the rest of the
    harvest still runs.
    """

    def __init__(
        self,
        session,
        *,
        root: str | Path | None = None,
        fresh: bool = SYNC_REBUILDS_FROM_SCRATCH,
        download_files: bool = True,
        max_file_bytes: int = MAX_FILE_BYTES,
        skip_mimetype_prefixes: Iterable[str] = SKIP_MIMETYPE_PREFIXES,
        include_lesson_files: bool = HARVEST_INCLUDE_LESSON_FILES,
        include_forum_posts: bool = HARVEST_INCLUDE_FORUM_POSTS,
        on_message: Callable[[str], None] = echo,
    ) -> None:
        """Configure the output directory, download limits and optional sources.

        ``fresh`` clears a previous harvest out of ``root`` first and pulls
        everything again. Otherwise the previous manifest is read and whatever
        it holds unchanged is reused; either way a file a course has removed
        does not linger to be screened again.
        """
        self.session = session
        self.writer = CorpusWriter(root, fresh=fresh)
        self.previous = self._previous_manifest()
        self.download_files = download_files
        self.max_file_bytes = max_file_bytes
        self.skip_mimetype_prefixes = tuple(skip_mimetype_prefixes)
        #: Lessons cost one request *per page* and are off unless asked for.
        self.include_lesson_files = include_lesson_files
        self.include_forum_posts = include_forum_posts
        self.on_message = on_message
        #: Reused across every download: one TLS handshake, not one per file.
        self._http = requests.Session()

    def __repr__(self) -> str:
        """Describe the harvester and its root."""
        return f"UnstructuredHarvester(root={str(self.root)!r})"

    @property
    def root(self) -> Path:
        """The directory the corpus is written to."""
        return self.writer.root

    def _previous_manifest(self) -> dict[int, dict[str, Any]]:
        """Return the previous harvest's course entries by id, if reusable.

        A manifest from another site describes other files, so it is ignored
        and its course directories are pruned like any removed course's.
        """
        manifest = self.writer.read_manifest()
        if not manifest:
            return {}
        if manifest.get("site_url") != self.session.site_url:
            log.info("previous manifest is for %s -- not reusing it", manifest.get("site_url"))
            return {}
        return {
            int(course.get("course_id") or 0): course
            for course in manifest.get("courses") or []
        }

    # -- entry points ------------------------------------------------------

    def harvest_all(self, course_ids: Iterable[int] | None = None) -> dict[str, Any]:
        """Harvest every favourite course, or those in ``course_ids``.

        Writes the manifest into ``root`` and returns it.
        """
        courses = self.session.favourite_courses()
        if course_ids is not None:
            wanted = {int(c) for c in course_ids}
            courses = [c for c in courses if c.get("id") in wanted]
        if not courses:
            self.on_message("No courses matched -- nothing to harvest.")
            log.warning("no courses matched -- nothing to harvest")
            return self._write_manifest([])

        ids = [int(c["id"]) for c in courses]
        log.info("harvesting %d course(s) into %s", len(courses), self.root)
        batched = self._fetch_batched(ids)
        for warning in batched["_warnings"]:
            log.warning("batched source failed: %s", warning)

        harvests = []
        for course in courses:
            self.on_message(
                f"  {course.get('shortname')} ({course.get('id')}) ..."
            )
            harvest = self._harvest_course(course, batched)
            counts = harvest.as_dict()["counts"]
            self.on_message(
                f"    {counts['texts']} text item(s), "
                f"{counts['files_downloaded']}/{counts['files_seen']} file(s), "
                f"{counts['new']} new, {counts['modified']} modified, "
                f"{counts['warnings']} warning(s)"
            )
            for warning in harvest.warnings:
                log.warning("%s: %s", course.get("shortname"), warning)
            harvests.append(harvest)
        return self._write_manifest(harvests)

    def harvest(self, course_id: int) -> dict[str, Any]:
        """Harvest one course. Same manifest shape, one entry long."""
        return self.harvest_all([course_id])

    # -- batched sources ---------------------------------------------------

    def _fetch_batched(self, ids: list[int]) -> dict[str, Any]:
        """Run the ``*_by_courses`` calls, one request each for all courses.

        Returns ``{source: {course_id: [records]}}`` plus a ``_warnings`` key
        holding whatever failed, which each course inherits.
        """
        client = self.session.client
        warnings: list[str] = []

        def grouped(label: str, call, key: str) -> dict[int, list[dict[str, Any]]]:
            """Call one batched endpoint and group its records by course id."""
            out: dict[int, list[dict[str, Any]]] = {}
            try:
                payload = call(ids)
            except MoodleApiError as exc:
                warnings.append(f"{label}: {exc}")
                return out
            records = payload.get(key) if isinstance(payload, dict) else payload
            for record in records or []:
                if not isinstance(record, dict):
                    continue
                out.setdefault(int(record.get("course") or 0), []).append(record)
            return out

        return {
            "pages": grouped("pages", client.get_pages_by_courses, "pages"),
            "labels": grouped("labels", client.get_labels_by_courses, "labels"),
            "urls": grouped("urls", client.get_urls_by_courses, "urls"),
            "books": grouped("books", client.get_books_by_courses, "books"),
            "assignments": self._assignment_attachments(ids, warnings),
            "_warnings": warnings,
        }

    def _assignment_attachments(
        self, ids: list[int], warnings: list[str]
    ) -> dict[int, list[dict[str, Any]]]:
        """Collect assignment intro attachments, which the contents call omits."""
        out: dict[int, list[dict[str, Any]]] = {}
        try:
            payload = self.session.client.get_assignments(ids)
        except MoodleApiError as exc:
            warnings.append(f"assignment attachments: {exc}")
            return out
        for course in (payload or {}).get("courses") or []:
            course_id = int(course.get("id") or 0)
            for assignment in course.get("assignments") or []:
                attachments = list(assignment.get("introattachments") or [])
                attachments += list(assignment.get("introfiles") or [])
                for attachment in attachments:
                    out.setdefault(course_id, []).append(
                        {
                            "cmid": assignment.get("cmid"),
                            "module_name": assignment.get("name"),
                            "file": attachment,
                        }
                    )
        return out

    # -- one course --------------------------------------------------------

    def _harvest_course(
        self, course: dict[str, Any], batched: dict[str, Any]
    ) -> CourseHarvest:
        """Run every source for one course and return its harvest."""
        course_id = int(course["id"])
        directory = self.writer.course_dir(course_id, course.get("shortname"))
        harvest = CourseHarvest(
            course_id=course_id,
            shortname=str(course.get("shortname") or ""),
            fullname=str(course.get("fullname") or ""),
            directory=self.writer.relative(directory),
            warnings=list(batched["_warnings"]),
        )
        previous = self.previous.get(course_id) or {}
        for item in previous.get("texts") or []:
            if item.get("stored_path") and item.get("sha256"):
                harvest.previous_texts[(item.get("cmid"), item["sha256"])] = item
            harvest.previous_text_names.add(
                (item.get("kind"), item.get("cmid"), item.get("name"))
            )
        for item in previous.get("files") or []:
            if item.get("stored_path") and item.get("fileurl"):
                harvest.previous_files[item["fileurl"]] = item
            harvest.previous_file_names.add((item.get("cmid"), item.get("filename")))

        # Sections, module intros and the file inventory.
        self._harvest_contents(course_id, directory, harvest)
        # Prose the contents call does not return.
        self._harvest_batched_text(course_id, directory, harvest, batched)
        self._harvest_blocks(course_id, directory, harvest)
        if self.include_forum_posts:
            self._harvest_forums(course_id, directory, harvest)
        # Files reachable only through their own endpoints.
        self._harvest_assignment_attachments(course_id, directory, harvest, batched)
        if self.include_lesson_files:
            self._harvest_lesson_files(course_id, directory, harvest)
        return harvest

    def _harvest_contents(
        self, course_id: int, directory: Path, harvest: CourseHarvest
    ) -> None:
        """Harvest structure, intro HTML and the file inventory from one course.

        Type-agnostic: a module type this code has never seen still yields its
        name, description and ``contents[]``.
        """
        try:
            sections = self.session.course_contents(course_id)
        except MoodleApiError as exc:
            harvest.warnings.append(f"course contents: {exc}")
            return

        for section in sections or []:
            section_name = str(section.get("name") or "").strip()
            self._add_text(
                harvest,
                directory,
                kind="section-summary",
                name=section_name or "section",
                body=section.get("summary"),
                meta={"section": section_name, "section_id": section.get("id")},
            )

            for module in section.get("modules") or []:
                modname = str(module.get("modname") or "")
                module_name = str(module.get("name") or "").strip()
                base = {
                    "section": section_name,
                    "cmid": module.get("id"),
                    "instance": module.get("instance"),
                    "modname": modname,
                    "module_name": module_name,
                    "module_url": module.get("url"),
                    "availability": module.get("availabilityinfo"),
                }
                # For a label this *is* the content; elsewhere it is the intro.
                self._add_text(
                    harvest,
                    directory,
                    kind="label" if modname == "label" else "module-intro",
                    name=module_name or modname,
                    body=module.get("description"),
                    meta=base,
                )
                self._walk_contents(
                    module.get("contents") or [], directory, harvest, base
                )

    def _walk_contents(
        self,
        entries: Iterable[Any],
        directory: Path,
        harvest: CourseHarvest,
        base: dict[str, Any],
    ) -> None:
        """Recurse ``contents[]``: keep files, record links, descend into folders.

        ``type == "url"`` entries are recorded rather than ignored. They cannot be
        fetched, but dropping them would hide part of the course.
        """
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            entry_type = str(entry.get("type") or "")
            if entry_type == "file":
                self._add_file(harvest, directory, entry, base, source="contents")
            elif entry_type == "url":
                self._add_link(
                    harvest,
                    {**base, "source": "contents", "name": entry.get("filename"),
                     "url": entry.get("fileurl")},
                )
            elif entry_type == "content":
                # A folder module nests its real entries one level down.
                nested = entry.get("contents") or entry.get("content")
                if isinstance(nested, list):
                    self._walk_contents(nested, directory, harvest, base)

    def _harvest_batched_text(
        self,
        course_id: int,
        directory: Path,
        harvest: CourseHarvest,
        batched: dict[str, Any],
    ) -> None:
        """Add pages, labels, book intros and URLs from the pre-fetched batches.

        Page bodies are the reason this exists: the contents call returns a page
        module's description, never its ``content``.
        """
        for page in batched["pages"].get(course_id, []):
            self._add_text(
                harvest,
                directory,
                kind="page",
                name=page.get("name"),
                body=page.get("content") or page.get("intro"),
                meta={
                    "cmid": page.get("coursemodule"),
                    "instance": page.get("id"),
                    "modname": "page",
                    "module_name": page.get("name"),
                },
            )

        for label in batched["labels"].get(course_id, []):
            self._add_text(
                harvest,
                directory,
                kind="label",
                name=label.get("name"),
                body=label.get("intro"),
                meta={
                    "cmid": label.get("coursemodule"),
                    "instance": label.get("id"),
                    "modname": "label",
                    "module_name": label.get("name"),
                },
            )

        for book in batched["books"].get(course_id, []):
            self._add_text(
                harvest,
                directory,
                kind="book-intro",
                name=book.get("name"),
                body=book.get("intro"),
                meta={
                    "cmid": book.get("coursemodule"),
                    "instance": book.get("id"),
                    "modname": "book",
                    "module_name": book.get("name"),
                },
            )

        for url in batched["urls"].get(course_id, []):
            self._add_link(
                harvest,
                {
                    "source": "mod_url",
                    "cmid": url.get("coursemodule"),
                    "modname": "url",
                    "module_name": url.get("name"),
                    "name": url.get("name"),
                    "url": url.get("externalurl"),
                    "intro": url.get("intro"),
                },
            )

    def _harvest_blocks(
        self, course_id: int, directory: Path, harvest: CourseHarvest
    ) -> None:
        """Add sidebar block HTML, which no other call returns."""
        try:
            payload = self.session.client.get_course_blocks(course_id)
        except MoodleApiError as exc:
            harvest.warnings.append(f"course blocks: {exc}")
            return
        for block in (payload or {}).get("blocks") or []:
            contents = block.get("contents") or {}
            body = "\n".join(
                part
                for part in (contents.get("title"), contents.get("content"),
                             contents.get("footer"))
                if part
            )
            self._add_text(
                harvest,
                directory,
                kind="block",
                name=contents.get("title") or block.get("name"),
                body=body,
                meta={
                    "block_name": block.get("name"),
                    "region": block.get("region"),
                    "instance": block.get("instanceid"),
                },
            )

    def _harvest_forums(
        self, course_id: int, directory: Path, harvest: CourseHarvest
    ) -> None:
        """Add forum discussions and post attachments.

        Costs one call per forum plus one per discussion, which is why it is the
        only per-course source with its own switch.
        """
        try:
            forums = self.session.client.get_forums_by_courses([course_id])
        except MoodleApiError as exc:
            harvest.warnings.append(f"forums: {exc}")
            return

        for forum in forums:
            forum_id = forum.get("id")
            if not forum_id:
                continue
            try:
                discussions = self.session.client.get_forum_discussions(int(forum_id))
            except MoodleApiError as exc:
                harvest.warnings.append(f"forum {forum.get('name')!r}: {exc}")
                continue

            for discussion in (discussions or {}).get("discussions") or []:
                discussion_id = discussion.get("discussion") or discussion.get("id")
                if not discussion_id:
                    continue
                try:
                    payload = self.session.client.get_discussion_posts(
                        int(discussion_id)
                    )
                except MoodleApiError as exc:
                    harvest.warnings.append(
                        f"discussion {discussion.get('name')!r}: {exc}"
                    )
                    continue

                posts = (payload or {}).get("posts") or []
                base = {
                    "cmid": forum.get("cmid"),
                    "modname": "forum",
                    "module_name": forum.get("name"),
                    "forum_id": forum_id,
                    "forum_type": forum.get("type"),
                    "discussion_id": discussion_id,
                    "discussion_name": discussion.get("name"),
                }
                # One file per discussion: a thread read apart from its replies
                # loses the correction that usually lives in them.
                body = "\n\n".join(self._render_post(post) for post in posts)
                self._add_text(
                    harvest,
                    directory,
                    kind="forum",
                    name=discussion.get("name") or forum.get("name"),
                    body=body,
                    meta={**base, "posts": len(posts),
                          "last_modified": discussion.get("timemodified")},
                )
                for post in posts:
                    for attachment in post.get("attachments") or []:
                        self._add_file(
                            harvest, directory, attachment, base, source="forum"
                        )

    @staticmethod
    def _render_post(post: dict[str, Any]) -> str:
        """Render one post with the author and date header a reader needs."""
        author = (post.get("author") or {}).get("fullname") or "unknown"
        created = post.get("timecreated")
        when = (
            time.strftime("%Y-%m-%d %H:%M", time.localtime(created))
            if created
            else "unknown date"
        )
        subject = post.get("subject") or ""
        return (
            f"<!-- post {post.get('id')} by {author} at {when} -->\n"
            f"<h3>{subject}</h3>\n{post.get('message') or ''}"
        )

    def _harvest_assignment_attachments(
        self,
        course_id: int,
        directory: Path,
        harvest: CourseHarvest,
        batched: dict[str, Any],
    ) -> None:
        """Add the pre-fetched assignment intro attachments."""
        for record in batched["assignments"].get(course_id, []):
            base = {
                "cmid": record.get("cmid"),
                "modname": "assign",
                "module_name": record.get("module_name"),
            }
            self._add_file(
                harvest, directory, record["file"], base, source="assign-intro"
            )

    def _harvest_lesson_files(
        self, course_id: int, directory: Path, harvest: CourseHarvest
    ) -> None:
        """Add files embedded in lesson activities, which the contents call omits.

        Three nested calls deep, and the page-data endpoint returns the whole
        lesson's ``contentfiles`` for every page, hence the per-lesson dedupe.
        """
        try:
            lessons = (
                self.session.client.get_lessons_by_courses([course_id]) or {}
            ).get("lessons") or []
        except MoodleApiError as exc:
            harvest.warnings.append(f"lessons: {exc}")
            return

        for lesson in lessons:
            lesson_id = lesson.get("id")
            if not lesson_id:
                continue
            try:
                pages = (
                    self.session.client.get_lesson_pages(int(lesson_id)) or {}
                ).get("pages") or []
            except MoodleApiError as exc:
                harvest.warnings.append(f"lesson {lesson.get('name')!r}: {exc}")
                continue

            base = {
                "cmid": lesson.get("coursemodule"),
                "modname": "lesson",
                "module_name": lesson.get("name"),
                "instance": lesson_id,
            }
            seen: set[str] = set()
            for entry in pages:
                page_id = (entry.get("page") or {}).get("id") or entry.get("id")
                if not page_id:
                    continue
                try:
                    data = self.session.client.get_lesson_page_data(
                        int(lesson_id), int(page_id)
                    )
                except MoodleApiError as exc:
                    harvest.warnings.append(f"lesson page {page_id}: {exc}")
                    continue
                for item in (data or {}).get("contentfiles") or []:
                    fileurl = str(item.get("fileurl") or "")
                    if fileurl in seen:
                        continue
                    seen.add(fileurl)
                    self._add_file(harvest, directory, item, base, source="lesson")

    # -- item handling -----------------------------------------------------

    def _add_text(
        self,
        harvest: CourseHarvest,
        directory: Path,
        *,
        kind: str,
        name: Any,
        body: Any,
        meta: dict[str, Any] | None = None,
    ) -> None:
        """Write one prose item, unless it is empty or pure markup."""
        body = str(body or "").strip()
        if not body or not self._has_words(body):
            return
        meta = meta or {}
        key = (meta.get("cmid"), self._digest(body))
        if key in harvest.seen_texts:
            return  # the same module's text, reached through a second endpoint
        harvest.seen_texts.add(key)
        name = str(name or "").strip()
        sha256 = hashlib.sha256(body.encode("utf-8")).hexdigest()

        previous = harvest.previous_texts.get((meta.get("cmid"), sha256))
        if previous and (self.root / previous["stored_path"]).is_file():
            stored_path, retrieved_at, change = (
                previous["stored_path"], previous.get("retrieved_at"), "unchanged"
            )
        else:
            path = self.writer.write_text(directory, kind, name, body)
            stored_path, retrieved_at = self.writer.relative(path), int(time.time())
            change = (
                "modified"
                if (kind, meta.get("cmid"), name) in harvest.previous_text_names
                else "new"
            )
        harvest.texts.append(
            {
                "kind": kind,
                "name": name,
                "stored_path": stored_path,
                "chars": len(body),
                "sha256": sha256,
                "retrieved_at": retrieved_at,
                "change": change,
                **meta,
            }
        )

    @staticmethod
    def _add_link(harvest: CourseHarvest, link: dict[str, Any]) -> None:
        """Record an external link once. Links are never fetched."""
        url = str(link.get("url") or "").strip()
        if not url:
            return
        key = (link.get("cmid"), url)
        if key in harvest.seen_links:
            return
        harvest.seen_links.add(key)
        harvest.links.append(link)

    @staticmethod
    def _visible_text(body: str) -> str:
        """Strip markup and collapse whitespace to the words a reader would see."""
        stripped = re.sub(r"<[^>]+>", " ", body)
        stripped = html.unescape(stripped)
        return re.sub(r"\s+", " ", stripped).strip()

    @classmethod
    def _has_words(cls, body: str) -> bool:
        """False for markup that renders to nothing, such as an empty paragraph."""
        return bool(cls._visible_text(body))

    @classmethod
    def _digest(cls, body: str) -> str:
        """Hash a text item's visible words, to spot the same module twice.

        Taken over visible text rather than markup, since the contents call wraps a
        label's HTML in a container that the per-type endpoint does not. The stored
        file keeps its original markup either way.
        """
        return hashlib.sha1(
            cls._visible_text(body).lower().encode("utf-8")
        ).hexdigest()

    def _add_file(
        self,
        harvest: CourseHarvest,
        directory: Path,
        entry: dict[str, Any],
        base: dict[str, Any],
        *,
        source: str,
    ) -> None:
        """Record one file, and fetch its bytes unless a rule says not to."""
        fileurl = str(entry.get("fileurl") or "").strip()
        filename = str(entry.get("filename") or "").strip()
        if not fileurl or not filename:
            return
        if fileurl in harvest.seen_urls:
            return  # the same bytes, reached twice -- fetch once
        harvest.seen_urls.add(fileurl)

        record: dict[str, Any] = {
            **base,
            "source": source,
            "filename": filename,
            "fileurl": fileurl,
            "mimetype": entry.get("mimetype"),
            "filesize": entry.get("filesize"),
            "timemodified": entry.get("timemodified"),
            "stored_path": None,
            "bytes": None,
            "sha256": None,
            "retrieved_at": None,
            "change": None,
            "skipped": None,
        }
        harvest.files.append(record)

        skip = self._skip_reason(record)
        if skip:
            record["skipped"] = skip
            log.debug("skipped %s: %s", filename, skip)
            return

        previous = harvest.previous_files.get(fileurl)
        if previous and self._unchanged(previous, record):
            record.update(
                stored_path=previous["stored_path"],
                bytes=previous.get("bytes"),
                sha256=previous.get("sha256")
                or sha256_file(self.root / previous["stored_path"]),
                retrieved_at=previous.get("retrieved_at"),
                change="unchanged",
            )
            log.debug("kept %s: unchanged on Moodle", record["stored_path"])
            return

        path = self.writer.file_path(directory, filename)
        try:
            record["bytes"] = self._download(fileurl, path, record.get("mimetype"))
            record["stored_path"] = self.writer.relative(path)
            record["sha256"] = sha256_file(path)
            record["retrieved_at"] = int(time.time())
            # A resource's fileurl carries its revision, so an edited file
            # arrives under a new url; its module and name still match.
            record["change"] = (
                "modified"
                if previous or (base.get("cmid"), filename) in harvest.previous_file_names
                else "new"
            )
            log.debug("downloaded %s (%s bytes)", record["stored_path"], record["bytes"])
        except Exception as exc:  # network, disk, oversize -- never fatal
            record["skipped"] = f"download failed: {exc}"
            harvest.warnings.append(f"{filename}: {exc}")
            path.unlink(missing_ok=True)
            log.warning("download failed for %s: %s", filename, exc)

    def _unchanged(self, previous: dict[str, Any], record: dict[str, Any]) -> bool:
        """True when Moodle describes the file as it did when it was stored."""
        return (
            previous.get("timemodified") == record.get("timemodified")
            and previous.get("filesize") == record.get("filesize")
            and (self.root / previous["stored_path"]).is_file()
        )

    def _skip_reason(self, record: dict[str, Any]) -> str | None:
        """Return why a file will not be fetched, or ``None`` to fetch it."""
        if not self.download_files:
            return "downloads disabled"
        mimetype = str(record.get("mimetype") or "")
        if mimetype.startswith(self.skip_mimetype_prefixes):
            return f"skipped mimetype {mimetype}"
        size = record.get("filesize")
        if isinstance(size, int) and size > self.max_file_bytes:
            return f"too large ({size} bytes)"
        return None

    def _download(
        self, fileurl: str, destination: Path, mimetype: Any = None
    ) -> int:
        """Stream one file from ``pluginfile.php`` to disk; return its size.

        The token goes in the POST body, not the query string, so it does not land
        in access logs or proxy caches.
        """
        token = self.session.token
        if not token:
            raise MoodleApiError("No token on the session -- connect() first.")

        destination.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        with self._http.post(
            fileurl, data={"token": token}, stream=True, timeout=DOWNLOAD_TIMEOUT
        ) as response:
            response.raise_for_status()
            chunks = response.iter_content(DOWNLOAD_CHUNK)
            head = next(chunks, b"")
            error = self._file_error(head, response, mimetype)
            if error:
                raise MoodleApiError(f"the file endpoint refused it: {error}")

            with destination.open("wb") as handle:
                for chunk in (head, *chunks):
                    if not chunk:
                        continue
                    written += len(chunk)
                    if written > self.max_file_bytes:
                        raise MoodleApiError(
                            f"exceeded {self.max_file_bytes} bytes mid-download"
                        )
                    handle.write(chunk)
        return written

    @staticmethod
    def _file_error(head: bytes, response: Any, mimetype: Any) -> str | None:
        """Return Moodle's refusal message, or ``None`` if these are the file's bytes.

        ``pluginfile.php`` reports a rejected token with a JSON body and HTTP 200.
        Content type alone cannot decide it, since a course may legitimately ship a
        ``.json``: the test is a JSON response whose file was not declared as JSON
        and whose body is an error envelope. Anything ambiguous is treated as a
        real file.
        """
        if "json" not in str(response.headers.get("Content-Type") or "").lower():
            return None
        if "json" in str(mimetype or "").lower():
            return None  # the resource genuinely is a JSON document
        try:
            payload = json.loads(head.decode("utf-8", "replace"))
        except ValueError:
            return None  # truncated or not JSON after all; error bodies are tiny
        if not isinstance(payload, dict):
            return None
        for key in ("error", "message", "exception", "errorcode"):
            if payload.get(key):
                return str(payload[key])
        return None

    # -- manifest ----------------------------------------------------------

    def _write_manifest(self, harvests: list[CourseHarvest]) -> dict[str, Any]:
        """Prune what is no longer listed, then write the manifest and return it."""
        keep = set().union(*(h.stored_paths() for h in harvests))
        # A reused item may still sit in a directory named for an older
        # shortname, so that directory stays too.
        removed = self.writer.prune(
            keep=keep,
            course_dirs={Path(h.directory).name for h in harvests}
            | {Path(path).parts[0] for path in keep},
        )
        if removed:
            log.info("removed %d item(s) no longer on Moodle", len(removed))
        entries = [h.as_dict() for h in harvests]
        manifest = {
            "schema_version": CORPUS_SCHEMA_VERSION,
            "fetched_at": int(time.time()),
            "site_url": self.session.site_url,
            "root": str(self.root),
            "is_temporary": self.writer.is_temporary,
            "settings": {
                "download_files": self.download_files,
                "max_file_bytes": self.max_file_bytes,
                "skip_mimetype_prefixes": list(self.skip_mimetype_prefixes),
                "include_lesson_files": self.include_lesson_files,
                "include_forum_posts": self.include_forum_posts,
            },
            "totals": {
                "courses": len(entries),
                "texts": sum(e["counts"]["texts"] for e in entries),
                "files_downloaded": sum(
                    e["counts"]["files_downloaded"] for e in entries
                ),
                "bytes_downloaded": sum(
                    e["counts"]["bytes_downloaded"] for e in entries
                ),
                "links": sum(e["counts"]["links"] for e in entries),
                "warnings": sum(e["counts"]["warnings"] for e in entries),
                "new": sum(e["counts"]["new"] for e in entries),
                "modified": sum(e["counts"]["modified"] for e in entries),
                "unchanged": sum(e["counts"]["unchanged"] for e in entries),
                "removed": len(removed),
            },
            "courses": entries,
        }
        self.writer.write_manifest(manifest)
        totals = manifest["totals"]
        log.info(
            "harvest complete: %d course(s), %d text item(s), %d file(s) "
            "(%.1f MB), %d link(s), %d warning(s); %d new, %d modified, "
            "%d unchanged, %d removed -- manifest in %s",
            totals["courses"],
            totals["texts"],
            totals["files_downloaded"],
            totals["bytes_downloaded"] / 1_048_576,
            totals["links"],
            totals["warnings"],
            totals["new"],
            totals["modified"],
            totals["unchanged"],
            totals["removed"],
            self.root,
        )
        return manifest
