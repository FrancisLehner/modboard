"""Pass 1: decide which harvested documents are worth reading closely.

A cheap model looks at every document once and answers one question: does it
hold information about grading, course structure, assignments, quizzes or the
final exam? One call per document; the answer is a boolean plus the categories
matched and a quoted line of evidence.

Two verdicts are reached without an API call, because they are judgements
about the file format rather than its content: an inert format such as a
stylesheet or font cannot carry course information and is ``False``; a format
nothing can parse yet is ``None``.

``None`` is distinct from ``False``. A document nobody could read is not a
document found irrelevant, and conflating the two hides the pipeline's blind
spots.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Iterable

from ..config import (
    ENV_API_KEY_VAR,
    ENV_FILE,
    KEYRING_SERVICE_ANTHROPIC,
    KEYRING_USER_ANTHROPIC,
    SCREEN_CATEGORIES,
    SCREEN_MAX_RETRIES,
    SCREEN_MAX_TOKENS,
    SCREEN_MODEL,
    SCREEN_SYSTEM_PROMPT,
    SCREEN_TIMEOUT,
    SCREEN_WORKERS,
    SCREENING_FILENAME,
    SCREENING_SCHEMA_VERSION,
)
from ..logs import echo, get_logger
from .documents import CorpusReader, ReadableDocument

log = get_logger("screen")

VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "relevant": {
            "type": "boolean",
            "description": "True if the document says anything about how the course is run.",
        },
        "categories": {
            "type": "array",
            "items": {"type": "string", "enum": list(SCREEN_CATEGORIES)},
            "description": "Which of the five areas it covers. Empty when relevant is false.",
        },
        "reason": {
            "type": "string",
            "description": "One sentence: what this document is, and why it does or does not qualify.",
        },
        "evidence": {
            "type": "string",
            "description": (
                "A short verbatim quote from the document that supports a true "
                "verdict. Empty string when relevant is false."
            ),
        },
    },
    "required": ["relevant", "categories", "reason", "evidence"],
    "additionalProperties": False,
}


# ----------------------------------------------------------------------
# Credentials
# ----------------------------------------------------------------------


def load_dotenv_once() -> Path | None:
    """Load ``.env`` into the environment if present; return the file or ``None``.

    The SDK reads the environment and does not know about dotfiles. Existing
    environment variables win, so an exported key is not overridden.
    """
    from dotenv import load_dotenv

    # screen.py -> src/extraction -> src -> project root
    if not ENV_FILE.exists():
        return None
    load_dotenv(ENV_FILE, override=False)
    return ENV_FILE


def resolve_api_key() -> str | None:
    """Return the key from the OS keyring, or ``None`` to let the SDK resolve it.

    ``None`` is not a failure: the SDK has its own chain of environment
    variables and stored profiles. Only the keyring lookup is added here.
    """
    load_dotenv_once()
    if os.environ.get(ENV_API_KEY_VAR):
        return None
    try:
        import keyring

        return keyring.get_password(KEYRING_SERVICE_ANTHROPIC, KEYRING_USER_ANTHROPIC) or None
    except Exception:
        return None


def store_api_key(api_key: str) -> None:
    """Store the API key in the OS credential store."""
    import keyring

    keyring.set_password(KEYRING_SERVICE_ANTHROPIC, KEYRING_USER_ANTHROPIC, api_key.strip())


# ----------------------------------------------------------------------
# Results
# ----------------------------------------------------------------------


#: Substrings of a 400 that mean *every* remaining call will fail the same
#: way. A screen is hundreds of requests; discovering an exhausted balance on
#: request 1 and stopping is the difference between one error and hundreds.
FATAL_ERROR_MARKERS = (
    "credit balance is too low",
    "invalid x-api-key",
    "authentication_error",
    "permission_error",
)


def is_fatal(message: str) -> bool:
    """True when retrying, or trying the next document, cannot help."""
    lowered = message.lower()
    return any(marker in lowered for marker in FATAL_ERROR_MARKERS)


@dataclass
class Verdict:
    """One document's screening result and how it was reached.

    ``relevant`` is tri-state: ``True``/``False`` are answers, ``None`` means no
    answer could be obtained.
    """

    stored_path: str
    course_id: int
    name: str
    origin: str
    medium: str
    relevant: bool | None
    categories: list[str] = field(default_factory=list)
    reason: str = ""
    evidence: str = ""
    #: ``model`` | ``format-rule`` | ``unreadable`` | ``skipped`` | ``error``
    decided_by: str = "model"
    truncated: bool = False
    input_tokens: int | None = None
    output_tokens: int | None = None


class DocumentScreener:
    """Run pass 1 over a harvested corpus.

    ::

        screener = DocumentScreener(CorpusReader(root))
        report = screener.run()
        screener.relevant_paths(report)
    """

    def __init__(
        self,
        reader: CorpusReader,
        *,
        model: str = SCREEN_MODEL,
        workers: int = SCREEN_WORKERS,
        max_retries: int = SCREEN_MAX_RETRIES,
        on_message: Callable[[str], None] = echo,
    ) -> None:
        """Build the API client and configure the model and concurrency."""
        import anthropic

        self.reader = reader
        self.model = model
        self.workers = max(1, workers)
        self.on_message = on_message
        self._anthropic = anthropic
        #: Set when a call fails in a way the next call cannot survive.
        self._abort = threading.Event()
        self._abort_reason = ""
        self.client = anthropic.Anthropic(
            api_key=resolve_api_key(), max_retries=max_retries, timeout=SCREEN_TIMEOUT
        )

    def __repr__(self) -> str:
        """Describe the screener's model and worker count."""
        return f"DocumentScreener(model={self.model!r}, workers={self.workers})"

    # -- entry points ------------------------------------------------------

    def run(
        self,
        course_ids: list[int] | None = None,
        *,
        limit: int | None = None,
        write: bool = True,
    ) -> dict[str, Any]:
        """Screen every document and return the report.

        ``limit`` caps the number of model calls, which allows a trial run on a few
        documents. Format-rule verdicts are free and not counted.
        """
        documents = list(self.reader.walk(course_ids))
        decided: list[Verdict] = []
        to_call: list[tuple[int, ReadableDocument]] = []
        for course_id, doc in documents:
            verdict = self._decide_without_model(course_id, doc)
            if verdict is None:
                to_call.append((course_id, doc))
            else:
                decided.append(verdict)

        if limit is not None:
            skipped = to_call[limit:]
            to_call = to_call[:limit]
            for course_id, doc in skipped:
                decided.append(
                    Verdict(
                        stored_path=doc.stored_path, course_id=course_id, name=doc.name,
                        origin=doc.origin, medium=doc.medium, relevant=None,
                        reason="not screened: --limit reached", decided_by="skipped",
                        truncated=doc.truncated,
                    )
                )

        self.on_message(
            f"{len(documents)} document(s): {len(to_call)} to screen, "
            f"{len(decided)} decided without a call."
        )
        log.info(
            "screening %d document(s) with %s on %d worker(s): "
            "%d need a call, %d decided on format alone",
            len(documents),
            self.model,
            self.workers,
            len(to_call),
            len(decided),
        )

        started = time.monotonic()
        verdicts = list(decided)
        if to_call:
            verdicts.extend(self._screen_concurrently(to_call))
        elapsed = time.monotonic() - started

        report = self._build_report(verdicts, elapsed)
        totals = report["totals"]
        log.info(
            "screened %d document(s) in %.1fs: %d relevant, %d not relevant, "
            "%d undecided, %d error(s); %d in / %d out token(s)",
            totals["documents"],
            elapsed,
            totals["relevant"],
            totals["not_relevant"],
            totals["undecided"],
            totals["errors"],
            totals["input_tokens"],
            totals["output_tokens"],
        )
        if report["aborted"]:
            log.error("screening aborted: %s", report["aborted"])
        if write:
            path = self.reader.root / SCREENING_FILENAME
            path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
            self.on_message(f"Wrote {path}")
        return report

    def estimate(self, course_ids: list[int] | None = None) -> dict[str, Any]:
        """Count the input tokens a full run would send, without sending it."""
        total = 0
        counted = 0
        failed = 0
        first_error: str | None = None
        for course_id, doc in self.reader.walk(course_ids):
            if self._decide_without_model(course_id, doc) is not None:
                continue
            try:
                result = self.client.messages.count_tokens(
                    model=self.model,
                    system=SCREEN_SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": self._user_content(doc)}],
                )
            except Exception as exc:
                failed += 1
                # Keep the first reason: a run where every count fails is
                # almost always one cause (no key, wrong model), and a bare
                # count of failures hides it.
                if first_error is None:
                    first_error = f"{type(exc).__name__}: {exc}"
                continue
            total += result.input_tokens
            counted += 1
        return {
            "model": self.model,
            "documents": counted,
            "input_tokens": total,
            "count_failed": failed,
            "first_error": first_error,
        }

    @staticmethod
    def relevant_paths(report: dict[str, Any]) -> list[str]:
        """Return the documents pass 2 should read, in report order."""
        return [v["stored_path"] for v in report["verdicts"] if v["relevant"] is True]

    # -- the two free verdicts ---------------------------------------------

    def _decide_without_model(
        self, course_id: int, doc: ReadableDocument
    ) -> Verdict | None:
        """Return a format-based verdict, or ``None`` if a model call is needed."""
        if doc.is_readable:
            return None
        reason = doc.unreadable or "unreadable"
        base = dict(
            stored_path=doc.stored_path, course_id=course_id, name=doc.name,
            origin=doc.origin, medium=doc.medium, reason=reason,
            truncated=doc.truncated,
        )
        if reason.startswith("inert format"):
            # A stylesheet cannot describe how a course is graded.
            return Verdict(relevant=False, decided_by="format-rule", **base)
        # Everything else is a gap in coverage, not a negative answer.
        return Verdict(relevant=None, decided_by="unreadable", **base)

    # -- the model call ----------------------------------------------------

    def _screen_concurrently(
        self, work: Iterable[tuple[int, ReadableDocument]]
    ) -> list[Verdict]:
        """Screen documents in parallel, stopping early on a fatal error."""
        work = list(work)
        verdicts: list[Verdict] = []
        done = 0
        announced_abort = False
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = {
                pool.submit(self._screen_one, course_id, doc): doc.stored_path
                for course_id, doc in work
            }
            for future in concurrent.futures.as_completed(futures):
                verdicts.append(future.result())
                done += 1
                if done % 25 == 0 or done == len(work):
                    self.on_message(f"  screened {done}/{len(work)}")
                if self._abort.is_set() and not announced_abort:
                    announced_abort = True
                    self.on_message(f"  ABORTING: {self._abort_reason}")
        return verdicts

    def _screen_one(self, course_id: int, doc: ReadableDocument) -> Verdict:
        """Screen one document and return its verdict."""
        base = dict(
            stored_path=doc.stored_path, course_id=course_id, name=doc.name,
            origin=doc.origin, medium=doc.medium, truncated=doc.truncated,
        )
        if self._abort.is_set():
            return Verdict(relevant=None, reason=f"aborted: {self._abort_reason}",
                           decided_by="error", **base)
        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=SCREEN_MAX_TOKENS,
                system=SCREEN_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": self._user_content(doc)}],
                output_config={"format": {"type": "json_schema", "schema": VERDICT_SCHEMA}},
            )
        except self._anthropic.APIStatusError as exc:
            reason = f"API error {exc.status_code}: {exc.message}"
            if is_fatal(reason):
                self._abort_reason = exc.message
                self._abort.set()
                log.error("fatal API error, aborting the pass: %s", exc.message)
            else:
                log.warning("%s: %s", doc.stored_path, reason)
            return Verdict(relevant=None, reason=reason, decided_by="error", **base)
        except self._anthropic.APIConnectionError as exc:
            log.warning("%s: network error: %s", doc.stored_path, exc)
            return Verdict(relevant=None, reason=f"network error: {exc}",
                           decided_by="error", **base)

        if response.stop_reason == "refusal":
            return Verdict(relevant=None, reason="model declined to answer",
                           decided_by="error", **base)

        try:
            payload = json.loads(next(b.text for b in response.content if b.type == "text"))
        except (StopIteration, ValueError) as exc:
            return Verdict(relevant=None, reason=f"unparseable answer: {exc}",
                           decided_by="error", **base)

        relevant = bool(payload.get("relevant"))
        categories = [c for c in (payload.get("categories") or []) if c in SCREEN_CATEGORIES]
        return Verdict(
            relevant=relevant,
            categories=categories,
            reason=str(payload.get("reason") or "").strip(),
            evidence=str(payload.get("evidence") or "").strip(),
            decided_by="model",
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            **base,
        )

    def _user_content(self, doc: ReadableDocument) -> list[dict[str, Any]]:
        """Build the message content: the document, headed by its provenance.

        For an image or scanned PDF the attachment leads, as the API recommends.
        """
        header = {"type": "text", "text": f"Document metadata -- {doc.describe()}"}
        if doc.medium in {"image", "pdf"}:
            return [*doc.blocks, header, {"type": "text", "text": "Screen this document."}]
        return [header, *doc.blocks, {"type": "text", "text": "Screen this document."}]

    # -- reporting ---------------------------------------------------------

    def _build_report(self, verdicts: list[Verdict], elapsed: float) -> dict[str, Any]:
        """Assemble the screening report, with totals and per-course counts."""
        verdicts.sort(key=lambda v: (v.course_id, v.stored_path))
        rows = [asdict(v) for v in verdicts]
        by_course: dict[str, Any] = {}
        for verdict in verdicts:
            bucket = by_course.setdefault(
                str(verdict.course_id),
                {"relevant": 0, "not_relevant": 0, "undecided": 0, "categories": {}},
            )
            if verdict.relevant is True:
                bucket["relevant"] += 1
                for category in verdict.categories:
                    bucket["categories"][category] = bucket["categories"].get(category, 0) + 1
            elif verdict.relevant is False:
                bucket["not_relevant"] += 1
            else:
                bucket["undecided"] += 1

        return {
            "schema_version": SCREENING_SCHEMA_VERSION,
            "screened_at": int(time.time()),
            "model": self.model,
            "corpus_root": str(self.reader.root),
            "elapsed_seconds": round(elapsed, 1),
            "totals": {
                "documents": len(verdicts),
                "relevant": sum(1 for v in verdicts if v.relevant is True),
                "not_relevant": sum(1 for v in verdicts if v.relevant is False),
                "undecided": sum(1 for v in verdicts if v.relevant is None),
                "errors": sum(1 for v in verdicts if v.decided_by == "error"),
                "input_tokens": sum(v.input_tokens or 0 for v in verdicts),
                "output_tokens": sum(v.output_tokens or 0 for v in verdicts),
            },
            "aborted": self._abort_reason or None,
            "by_course": by_course,
            "verdicts": rows,
        }
