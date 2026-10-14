"""Pass 2: fill the record fields no Moodle endpoint can answer.

One call per course, sending the record built from the API together with the
documents pass 1 kept. What comes back is a proposal, not a document: a list
of additions, each with its source, a verbatim quote and a confidence. The
finished record is assembled by ``merge.merge_additive``.

That split is deliberate. If the model returned a rewritten document, finding
out what it changed would mean diffing and trusting the diff. Returning only
additions makes "nothing existing was changed" true by construction: no code
here writes to a field that holds a value.

::

    extractor = FieldExtractor(CorpusReader(root), CourseStore(db))
    report = extractor.run()
    extractor.run(write=False)
"""

from __future__ import annotations

import concurrent.futures
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ..config import (
    EXTRACT_ACTIVITY_ARRAYS,
    EXTRACT_CONFIDENCE_LEVELS,
    EXTRACT_EFFORT,
    EXTRACT_FALLBACK_BETA,
    EXTRACT_MAX_INPUT_CHARS,
    EXTRACT_MAX_RETRIES,
    EXTRACT_MAX_TOKENS,
    EXTRACT_MIN_CONFIDENCE,
    EXTRACT_MODEL,
    EXTRACT_SCHEMA_VERSION,
    EXTRACT_SYSTEM_PROMPT,
    EXTRACT_TIMEOUT,
    EXTRACT_USE_FALLBACKS,
    EXTRACT_WORKERS,
    EXTRACTION_FILENAME,
    SCREENING_FILENAME,
)
from ..logs import echo, get_logger
from .documents import CorpusReader, ReadableDocument
from .merge import MergeReport, merge_additive
from .screen import resolve_api_key

log = get_logger("extract")

#: Reused by every proposal entry: where it came from and how sure the model is.
_PROVENANCE = {
    "source": {"type": "string",
               "description": "The document path, exactly as given in its header."},
    "confidence": {"type": "string", "enum": list(EXTRACT_CONFIDENCE_LEVELS)},
    "quote": {"type": "string",
              "description": "Short verbatim excerpt from the document supporting this."},
}

#: ``anyOf`` with null is how an optional scalar is expressed -- structured
#: outputs reject most other ways of saying "or missing".
_NULLABLE_STR = {"anyOf": [{"type": "string"}, {"type": "null"}]}
_NULLABLE_INT = {"anyOf": [{"type": "integer"}, {"type": "null"}]}


def _obj(properties: dict[str, Any]) -> dict[str, Any]:
    """Build a closed, fully-required object schema."""
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


PROPOSAL_SCHEMA: dict[str, Any] = _obj({
    "credits": {
        "anyOf": [_obj({"value": {"type": "integer"}, **_PROVENANCE}), {"type": "null"}],
        "description": "ECTS credits, or null if no document states them.",
    },
    "grading": {
        "type": "array",
        "description": "One entry per component of the module mark.",
        "items": _obj({
            "name": {"type": "string"},
            "weight_percent": _NULLABLE_INT,
            "type": {"anyOf": [
                {"type": "string", "enum": ["exam", "assignment", "quiz", "project",
                                            "presentation", "participation", "other"]},
                {"type": "null"},
            ]},
            "description": {"type": "string"},
            **_PROVENANCE,
        }),
    },
    "mandatory_onsite": {
        "type": "array",
        "description": "Sessions requiring physical attendance.",
        "items": _obj({
            "date": {"type": "string", "description": "YYYY-MM-DD"},
            "time": _NULLABLE_STR,
            "name": {"type": "string"},
            "description": {"type": "string"},
            **_PROVENANCE,
        }),
    },
    "new_activities": {
        "type": "array",
        "description": "Assessments described in a document but absent from the record.",
        "items": _obj({
            "type": {"type": "string", "enum": list(EXTRACT_ACTIVITY_ARRAYS)},
            "name": {"type": "string"},
            "due_date": _NULLABLE_STR,
            "due_time": _NULLABLE_STR,
            "description": {"type": "string"},
            **_PROVENANCE,
        }),
    },
    "enrichments": {
        "type": "array",
        "description": "Values for fields that are currently null, keyed by cmid.",
        "items": _obj({
            "cmid": {"type": "integer"},
            "field": {"type": "string"},
            "value": {"type": "string"},
            **_PROVENANCE,
        }),
    },
    "notes": {
        "type": "string",
        "description": (
            "Anything worth a human's attention: a document that contradicts the "
            "record, a rule you could not turn into a field. Empty string if none."
        ),
    },
})


@dataclass
class CourseExtraction:
    """One course's pass-2 result: what was proposed, merged and left out."""

    course_id: int
    shortname: str
    documents_read: list[str] = field(default_factory=list)
    documents_skipped: list[dict[str, Any]] = field(default_factory=list)
    proposal: dict[str, Any] | None = None
    merge: MergeReport | None = None
    document: dict[str, Any] | None = None
    notes: str = ""
    error: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return the report entry for this course."""
        return {
            "course_id": self.course_id,
            "shortname": self.shortname,
            "documents_read": self.documents_read,
            "documents_skipped": self.documents_skipped,
            "notes": self.notes,
            "error": self.error,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "proposal": self.proposal,
            "merge": self.merge.as_dict() if self.merge else None,
        }


class FieldExtractor:
    """Read a course's relevant documents and extend its record.

    One API call per course. Documents come from the corpus, the record from the
    store, and the extended record goes back to the store. A second run proposes
    the same additions and the merge rejects them as already present.
    """

    def __init__(
        self,
        reader: CorpusReader,
        store,
        *,
        model: str = EXTRACT_MODEL,
        workers: int = EXTRACT_WORKERS,
        effort: str = EXTRACT_EFFORT,
        min_confidence: str = EXTRACT_MIN_CONFIDENCE,
        max_input_chars: int = EXTRACT_MAX_INPUT_CHARS,
        use_fallbacks: bool = EXTRACT_USE_FALLBACKS,
        on_message: Callable[[str], None] = echo,
    ) -> None:
        """Build the API client and configure the model, effort and budgets."""
        import anthropic

        self.reader = reader
        self.store = store
        self.model = model
        self.workers = max(1, workers)
        self.effort = effort
        self.min_confidence = min_confidence
        self.max_input_chars = max_input_chars
        self.use_fallbacks = use_fallbacks
        self.on_message = on_message
        self._anthropic = anthropic
        self.client = anthropic.Anthropic(
            api_key=resolve_api_key(),
            max_retries=EXTRACT_MAX_RETRIES,
            timeout=EXTRACT_TIMEOUT,
        )

    def __repr__(self) -> str:
        """Describe the extractor's model and effort."""
        return f"FieldExtractor(model={self.model!r}, effort={self.effort!r})"

    # -- entry point -------------------------------------------------------

    def run(
        self,
        course_ids: list[int] | None = None,
        *,
        write: bool = True,
    ) -> dict[str, Any]:
        """Extend every screened course. ``write=False`` saves nothing."""
        screening = self._load_screening()
        relevant = self._relevant_by_course(screening, course_ids)
        if not relevant:
            self.on_message("No documents were marked relevant -- nothing to extract.")
            log.warning("no documents were marked relevant -- nothing to extract")
            return self._build_report([], 0.0, write=write)

        self.on_message(
            f"{len(relevant)} course(s) with "
            f"{sum(len(v) for v in relevant.values())} relevant document(s)."
        )
        log.info(
            "extracting from %d course(s) / %d relevant document(s) with %s "
            "at %s effort on %d worker(s)",
            len(relevant),
            sum(len(v) for v in relevant.values()),
            self.model,
            self.effort,
            self.workers,
        )

        started = time.monotonic()
        results: list[CourseExtraction] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = [
                pool.submit(self._extract_course, course_id, paths)
                for course_id, paths in sorted(relevant.items())
            ]
            for future in concurrent.futures.as_completed(futures):
                result = future.result()
                results.append(result)
                self._announce(result)
        elapsed = time.monotonic() - started

        if write:
            saved = 0
            for result in results:
                if result.document is not None and result.merge and result.merge.applied:
                    self.store.save(result.document)
                    saved += 1
            self.on_message("Saved extended documents to the store.")
            log.info("saved %d extended course document(s) to the store", saved)

        return self._build_report(results, elapsed, write=write)

    def _announce(self, result: CourseExtraction) -> None:
        """Print one line summarising a course's result."""
        if result.error:
            self.on_message(f"  {result.shortname}: FAILED -- {result.error}")
            log.error("%s (%s) failed: %s", result.shortname, result.course_id, result.error)
            return
        counts = result.merge.as_dict()["counts"] if result.merge else {"applied": 0, "rejected": 0}
        self.on_message(
            f"  {result.shortname}: {counts['applied']} added, "
            f"{counts['rejected']} rejected, {len(result.documents_read)} doc(s) read"
        )
        log.info(
            "%s (%s): %d added, %d rejected, %d doc(s) read",
            result.shortname,
            result.course_id,
            counts["applied"],
            counts["rejected"],
            len(result.documents_read),
        )

    # -- inputs ------------------------------------------------------------

    def _load_screening(self) -> dict[str, Any]:
        """Load the screening report; raise if the pass has not been run."""
        path = self.reader.root / SCREENING_FILENAME
        if not path.exists():
            raise FileNotFoundError(
                f"No {SCREENING_FILENAME} in {self.reader.root}. Run the screening pass first."
            )
        return json.loads(path.read_text())

    @staticmethod
    def _relevant_by_course(
        screening: dict[str, Any], course_ids: list[int] | None
    ) -> dict[int, list[str]]:
        """Group the relevant document paths by course id."""
        wanted = {int(c) for c in course_ids} if course_ids else None
        grouped: dict[int, list[str]] = {}
        for verdict in screening.get("verdicts") or []:
            if verdict.get("relevant") is not True:
                continue
            course_id = int(verdict.get("course_id") or 0)
            if wanted is not None and course_id not in wanted:
                continue
            grouped.setdefault(course_id, []).append(str(verdict.get("stored_path")))
        return grouped

    def _documents_for(
        self, course_id: int, paths: list[str]
    ) -> tuple[list[ReadableDocument], list[dict[str, Any]]]:
        """Re-read a course's relevant documents, honouring the input budget.

        Prose before files and smallest first, so a short page stating the rules is
        never what the budget cuts.
        """
        wanted = set(paths)
        found = [doc for cid, doc in self.reader.walk([course_id])
                 if doc.stored_path in wanted]
        # Prose before files, smallest first, so a short page stating the
        # rules is never what the budget cuts.
        found.sort(key=lambda d: (d.origin != "text", d.chars))

        kept: list[ReadableDocument] = []
        skipped: list[dict[str, Any]] = []
        budget = self.max_input_chars
        for doc in found:
            if not doc.is_readable:
                skipped.append({"stored_path": doc.stored_path,
                                "reason": doc.unreadable or "unreadable"})
                continue
            cost = doc.chars if doc.medium == "text" else 0
            if cost > budget:
                skipped.append({"stored_path": doc.stored_path,
                                "reason": f"input budget exhausted ({doc.chars} chars)"})
                continue
            budget -= cost
            kept.append(doc)
        return kept, skipped

    # -- one course --------------------------------------------------------

    def _extract_course(self, course_id: int, paths: list[str]) -> CourseExtraction:
        """Propose and merge additions for one course."""
        document = self.store.load(course_id)
        shortname = str((document or {}).get("general", {}).get("shortname") or course_id)
        result = CourseExtraction(course_id=course_id, shortname=shortname)
        if document is None:
            result.error = "no course document in the store -- run the sync first"
            return result

        documents, skipped = self._documents_for(course_id, paths)
        result.documents_read = [d.stored_path for d in documents]
        result.documents_skipped = skipped
        if not documents:
            result.error = "no readable relevant documents"
            return result

        try:
            proposal, usage = self._propose(document, documents)
        except Exception as exc:
            result.error = f"{type(exc).__name__}: {exc}"
            return result

        result.proposal = proposal
        result.notes = str(proposal.get("notes") or "").strip()
        result.input_tokens, result.output_tokens = usage
        merged, merge_report = merge_additive(
            document, proposal, min_confidence=self.min_confidence
        )
        result.document = merged
        result.merge = merge_report
        # Bookkeeping about the pass itself.
        merged.setdefault("sync", {})["extracted_at"] = int(time.time())
        merged["sync"]["extraction_model"] = self.model
        return result

    def _propose(
        self, document: dict[str, Any], documents: list[ReadableDocument]
    ) -> tuple[dict[str, Any], tuple[int | None, int | None]]:
        """Send one call and return the parsed proposal with its token usage."""
        content = self._user_content(document, documents)
        request: dict[str, Any] = {
            "model": self.model,
            "max_tokens": EXTRACT_MAX_TOKENS,
            "system": [{
                "type": "text",
                "text": EXTRACT_SYSTEM_PROMPT,
                # Stable across the run; the documents that follow are not,
                # so this is the only cacheable prefix.
                "cache_control": {"type": "ephemeral"},
            }],
            "messages": [{"role": "user", "content": content}],
            "output_config": {
                "effort": self.effort,
                "format": {"type": "json_schema", "schema": PROPOSAL_SCHEMA},
            },
        }

        message = self._send(request)
        if message.stop_reason == "refusal":
            raise RuntimeError("the model declined to answer")
        if message.stop_reason == "max_tokens":
            raise RuntimeError(
                f"answer hit max_tokens ({EXTRACT_MAX_TOKENS}) -- raise it or "
                "split the course"
            )
        text = next((b.text for b in message.content if b.type == "text"), None)
        if text is None:
            raise RuntimeError("no text block in the response")
        return json.loads(text), (message.usage.input_tokens, message.usage.output_tokens)

    def _send(self, request: dict[str, Any]):
        """Stream the request, with server-side fallbacks when they are accepted.

        Streaming because ``max_tokens`` is above the point where a non-streaming
        request risks an HTTP timeout. Fallbacks so a declined request is answered
        by another model; a rejection of the parameter downgrades to a plain call.
        """
        if self.use_fallbacks:
            try:
                with self.client.beta.messages.stream(
                    **request, betas=[EXTRACT_FALLBACK_BETA], fallbacks="default"
                ) as stream:
                    return stream.get_final_message()
            except self._anthropic.BadRequestError as exc:
                if "fallback" not in str(exc).lower():
                    raise
                self.use_fallbacks = False
                self.on_message("  (server-side fallbacks refused; continuing without)")
                log.warning("server-side fallbacks refused by the API; continuing without")

        with self.client.messages.stream(**request) as stream:
            return stream.get_final_message()

    def _user_content(
        self, document: dict[str, Any], documents: list[ReadableDocument]
    ) -> list[dict[str, Any]]:
        """Build the message content: the record, then the documents, then the ask.

        The record goes first so the model knows what is already answered before it
        reads anything.
        """
        content: list[dict[str, Any]] = [{
            "type": "text",
            "text": (
                "Here is the record built from Moodle's API so far. Every value in "
                "it is authoritative -- propose a value only where you see null, "
                "and never propose a different value for a field that has one.\n\n"
                "```json\n"
                + json.dumps(document, indent=2, ensure_ascii=False)
                + "\n```"
            ),
        }]
        content.append({
            "type": "text",
            "text": f"\nThe {len(documents)} document(s) this course's screening pass "
                    "marked relevant follow. Cite them by the path in each header.",
        })
        for doc in documents:
            content.append({"type": "text", "text": f"\n--- DOCUMENT {doc.describe()}"})
            content.extend(doc.blocks)
        content.append({
            "type": "text",
            "text": "\nNow add what is missing from the record.",
        })
        return content

    # -- reporting ---------------------------------------------------------

    def _build_report(
        self, results: list[CourseExtraction], elapsed: float, *, write: bool
    ) -> dict[str, Any]:
        """Assemble the extraction report and write it into the corpus."""
        results.sort(key=lambda r: r.course_id)
        rows = [r.as_dict() for r in results]
        applied = sum(len(r.merge.applied) for r in results if r.merge)
        rejected = sum(len(r.merge.rejected) for r in results if r.merge)
        conflicts = [
            {**entry, "course_id": r.course_id}
            for r in results if r.merge
            for entry in r.merge.rejected if entry.get("conflict")
        ]
        report = {
            "schema_version": EXTRACT_SCHEMA_VERSION,
            "extracted_at": int(time.time()),
            "model": self.model,
            "effort": self.effort,
            "min_confidence": self.min_confidence,
            "corpus_root": str(self.reader.root),
            "saved": write,
            "elapsed_seconds": round(elapsed, 1),
            "totals": {
                "courses": len(results),
                "applied": applied,
                "rejected": rejected,
                "conflicts": len(conflicts),
                "errors": sum(1 for r in results if r.error),
                "input_tokens": sum(r.input_tokens or 0 for r in results),
                "output_tokens": sum(r.output_tokens or 0 for r in results),
            },
            "conflicts": conflicts,
            "courses": rows,
        }
        totals = report["totals"]
        log.info(
            "extraction finished in %.1fs: %d course(s), %d addition(s), "
            "%d rejected, %d conflict(s), %d error(s); %d in / %d out token(s)",
            elapsed,
            totals["courses"],
            totals["applied"],
            totals["rejected"],
            totals["conflicts"],
            totals["errors"],
            totals["input_tokens"],
            totals["output_tokens"],
        )
        for conflict in conflicts:
            log.warning("conflict in course %s: %s", conflict["course_id"], conflict)
        path = self.reader.root / EXTRACTION_FILENAME
        path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        self.on_message(f"Wrote {path}")
        return report
