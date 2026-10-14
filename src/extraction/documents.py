"""Turn a harvested corpus into model-readable content blocks.

The harvest stores everything verbatim: raw HTML, PDF bytes, images, archives,
stylesheets. This module decides, per file, how it becomes model input or that
it cannot.

Three readable shapes come out, matching the content-block types the Messages
API accepts:

``text``
    HTML stripped to its visible words, plus plain-text formats.
``image``
    Raster images, base64 in an ``image`` block.
``pdf``
    The PDF's own bytes in a ``document`` block, used only when text
    extraction returns nearly nothing, which indicates a scanned document. A
    PDF with a real text layer takes the cheaper text route.

Anything else is returned as unreadable with a reason, rather than dropped: a
format nothing can parse yet is a gap in coverage, and a gap no one can see is
worse than one that is recorded.
"""

from __future__ import annotations

import base64
import datetime as dt
import html
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from ..config import (
    IMAGE_SUFFIXES,
    INERT_SUFFIXES,
    MANIFEST_FILENAME,
    MAX_BINARY_BYTES,
    MAX_SCANNED_PAGES,
    MAX_TEXT_CHARS,
    MIN_CHARS_PER_PAGE,
    NEEDS_CONVERSION_SUFFIXES,
    TEXT_SUFFIXES,
    TRUNCATED_SUFFIXES,
)
from ..logs import get_logger

log = get_logger("corpus.reader")


def _when(timestamp: Any) -> str:
    """Render a manifest timestamp for the log; a unix epoch reads as nothing."""
    if not isinstance(timestamp, (int, float)):
        return "unknown"
    return dt.datetime.fromtimestamp(timestamp).isoformat(timespec="seconds")


@dataclass
class ReadableDocument:
    """One corpus entry, prepared for or excluded from a model call.

    ``blocks`` is ready to use as a message's ``content``. When ``unreadable``
    is set, ``blocks`` is empty and the reason says why.
    """

    #: Path relative to the corpus root -- the stable id for this document.
    stored_path: str
    #: ``text`` from the manifest's ``texts[]``, or ``file`` from ``files[]``.
    origin: str
    #: Human-facing label: the module or file name.
    name: str
    #: Provenance carried straight through from the manifest.
    meta: dict[str, Any] = field(default_factory=dict)
    #: ``text`` | ``image`` | ``pdf`` | ``""`` when unreadable.
    medium: str = ""
    #: Message content blocks for this document.
    blocks: list[dict[str, Any]] = field(default_factory=list)
    #: Why this document was not prepared, if it was not.
    unreadable: str | None = None
    #: True when the text was cut at :data:`MAX_TEXT_CHARS`.
    truncated: bool = False
    #: Visible characters found, for sorting and for the report.
    chars: int = 0

    @property
    def is_readable(self) -> bool:
        """True when the document has content blocks and no failure reason."""
        return self.unreadable is None and bool(self.blocks)

    def describe(self) -> str:
        """Return a one-line provenance header for the model's prompt."""
        parts = [f"path: {self.stored_path}", f"name: {self.name or '(unnamed)'}"]
        for key in ("kind", "modname", "module_name", "section", "source"):
            value = self.meta.get(key)
            if value:
                parts.append(f"{key}: {value}")
        if self.truncated:
            parts.append("note: text truncated")
        return " | ".join(parts)


class CorpusReader:
    """Walk a harvest manifest and yield prepared documents.

    ::

        reader = CorpusReader(root)
        for course_id, doc in reader.walk():
            ...
    """

    def __init__(self, root: str | Path) -> None:
        """Load the manifest from ``root``."""
        self.root = Path(root).expanduser().resolve()
        manifest_path = self.root / MANIFEST_FILENAME
        if not manifest_path.exists():
            raise FileNotFoundError(f"No {MANIFEST_FILENAME} in {self.root}")
        self.manifest: dict[str, Any] = json.loads(manifest_path.read_text())
        log.info(
            "reading corpus at %s: %d course(s), harvested %s",
            self.root,
            len(self.manifest.get("courses") or []),
            _when(self.manifest.get("fetched_at")),
        )

    def __repr__(self) -> str:
        """Describe the reader and its root."""
        return f"CorpusReader(root={str(self.root)!r})"

    # -- iteration ---------------------------------------------------------

    def courses(self) -> list[dict[str, Any]]:
        """Return the manifest's course entries."""
        return list(self.manifest.get("courses") or [])

    def walk(self, course_ids: list[int] | None = None) -> Iterator[tuple[int, ReadableDocument]]:
        """Yield ``(course_id, document)`` for every course, prose before files."""
        wanted = {int(c) for c in course_ids} if course_ids else None
        for course in self.courses():
            course_id = int(course.get("course_id") or 0)
            if wanted is not None and course_id not in wanted:
                continue
            for item in course.get("texts") or []:
                yield course_id, self._read_text_item(item)
            for item in course.get("files") or []:
                if item.get("stored_path"):
                    yield course_id, self._read_file_item(item)

    # -- per-item ----------------------------------------------------------

    def _read_text_item(self, item: dict[str, Any]) -> ReadableDocument:
        """Prepare a manifest ``texts[]`` entry, which is always HTML on disk."""
        doc = ReadableDocument(
            stored_path=str(item.get("stored_path") or ""),
            origin="text",
            name=str(item.get("name") or ""),
            meta={k: v for k, v in item.items() if k not in {"stored_path", "name"}},
        )
        path = self.root / doc.stored_path
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            doc.unreadable = f"could not read file: {exc}"
            return doc
        return self._as_text(doc, visible_text(raw))

    def _read_file_item(self, item: dict[str, Any]) -> ReadableDocument:
        """Prepare a downloaded file; the route depends on its format."""
        doc = ReadableDocument(
            stored_path=str(item.get("stored_path") or ""),
            origin="file",
            name=str(item.get("filename") or ""),
            meta={k: v for k, v in item.items()
                  if k in {"modname", "module_name", "section", "source", "mimetype",
                           "cmid", "filesize", "timemodified", "fileurl"}},
        )
        path = self.root / doc.stored_path
        suffix = path.suffix.lower()
        suffix = TRUNCATED_SUFFIXES.get(suffix, suffix)

        if suffix in INERT_SUFFIXES:
            doc.unreadable = f"inert format ({suffix or 'no extension'})"
            return doc
        if suffix in NEEDS_CONVERSION_SUFFIXES:
            # Deliberately *not* "not relevant": nothing here can open it yet.
            doc.unreadable = f"needs conversion ({suffix})"
            return doc
        if not path.exists():
            doc.unreadable = "file missing from corpus"
            return doc

        if suffix == ".pdf":
            return self._read_pdf(doc, path)
        if suffix in IMAGE_SUFFIXES:
            return self._read_image(doc, path, IMAGE_SUFFIXES[suffix])
        if suffix in TEXT_SUFFIXES or suffix == "":
            try:
                raw = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                doc.unreadable = f"could not read file: {exc}"
                return doc
            if suffix == ".ipynb":
                raw = notebook_text(raw)
            if suffix in {".html", ".htm", ".xml"}:
                raw = visible_text(raw)
            return self._as_text(doc, raw)

        doc.unreadable = f"unhandled format ({suffix})"
        return doc

    # -- medium builders ---------------------------------------------------

    def _as_text(self, doc: ReadableDocument, text: str) -> ReadableDocument:
        """Attach a text block, truncating at the character budget."""
        text = (text or "").strip()
        if not text:
            doc.unreadable = "no extractable text"
            return doc
        doc.chars = len(text)
        if len(text) > MAX_TEXT_CHARS:
            text = text[:MAX_TEXT_CHARS]
            doc.truncated = True
        doc.medium = "text"
        doc.blocks = [{"type": "text", "text": text}]
        return doc

    def _read_image(self, doc: ReadableDocument, path: Path, media_type: str) -> ReadableDocument:
        """Attach a base64 image block, or refuse an oversized file."""
        data = path.read_bytes()
        if len(data) > MAX_BINARY_BYTES:
            doc.unreadable = f"image too large ({len(data)} bytes)"
            return doc
        doc.medium = "image"
        doc.blocks = [{
            "type": "image",
            "source": {"type": "base64", "media_type": media_type,
                       "data": base64.standard_b64encode(data).decode()},
        }]
        return doc

    def _read_pdf(self, doc: ReadableDocument, path: Path) -> ReadableDocument:
        """Attach the PDF's text layer, or its pages when there is none."""
        import pymupdf  # imported lazily: only the PDF path needs it

        try:
            with pymupdf.open(path) as pdf:
                pages = pdf.page_count
                text = "".join(page.get_text() for page in pdf)
                if pages and len(text) / pages >= MIN_CHARS_PER_PAGE:
                    doc.meta["pdf_pages"] = pages
                    return self._as_text(doc, text)
                # Scanned: hand over the pages as a PDF the model can look at.
                excerpt = pymupdf.open()
                excerpt.insert_pdf(pdf, from_page=0, to_page=min(pages, MAX_SCANNED_PAGES) - 1)
                data = excerpt.tobytes()
                excerpt.close()
        except Exception as exc:  # encrypted, truncated, not really a PDF
            doc.unreadable = f"could not open PDF: {exc}"
            return doc

        if len(data) > MAX_BINARY_BYTES:
            doc.unreadable = f"scanned PDF too large ({len(data)} bytes)"
            return doc
        doc.meta["pdf_pages"] = pages
        doc.meta["pdf_scanned"] = True
        doc.truncated = pages > MAX_SCANNED_PAGES
        doc.medium = "pdf"
        doc.blocks = [{
            "type": "document",
            "source": {"type": "base64", "media_type": "application/pdf",
                       "data": base64.standard_b64encode(data).decode()},
        }]
        return doc


# ----------------------------------------------------------------------
# Text helpers
# ----------------------------------------------------------------------

_SCRIPT_RE = re.compile(r"<(script|style)\b.*?</\1>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_BLANKS_RE = re.compile(r"\n{3,}")


def visible_text(markup: str) -> str:
    """Strip HTML to the words a reader would see, preserving block structure.

    Tags become whitespace rather than nothing, so adjacent table cells do not
    run together and a tabular layout stays legible as rows.
    """
    text = _SCRIPT_RE.sub(" ", markup)
    text = re.sub(r"<(br|/p|/div|/tr|/li|/h[1-6])\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</t[dh]>", "\t", text, flags=re.I)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = "\n".join(re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines())
    return _BLANKS_RE.sub("\n\n", text).strip()


def notebook_text(raw: str) -> str:
    """Extract the source and markdown cells from a notebook's JSON."""
    try:
        payload = json.loads(raw)
    except ValueError:
        return raw
    chunks = []
    for cell in payload.get("cells") or []:
        source = cell.get("source")
        body = "".join(source) if isinstance(source, list) else str(source or "")
        if body.strip():
            chunks.append(f"[{cell.get('cell_type', 'cell')}]\n{body}")
    return "\n\n".join(chunks)
