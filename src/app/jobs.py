"""The sync, run in the background and reported stage by stage.

The same five stages ``pipeline_test.py`` runs from a terminal, driven from a
button instead and with their progress readable while they run. One job at a
time: every stage rebuilds what the last one wrote, so two concurrent runs
would race over the database and the corpus.

The browser login is the reason this is a thread rather than a request. Stage 1
can block for minutes waiting for SSO in a browser window, and a request that
long is a request that times out. The frontend polls ``state()`` instead.

Failure stops the run but not the server: the stage keeps the exception as its
detail and ``state()`` reports it, which is the only way the user sees what an
expired token or an exhausted API balance did.

The detail a stage shows is one line; everything behind it goes to the session
log. Each stage hands its ``on_message`` callback to a logger, so the progress
the terminal would have shown is kept even though there is no terminal.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

from ..config import DEFAULT_CORPUS_ROOT, DEFAULT_DB_PATH, DEFAULT_SITE
from ..data_retention.corpus import UnstructuredHarvester
from ..data_retention.document import CourseDocumentBuilder
from ..data_retention.session import MoodleSession
from ..extraction.documents import CorpusReader
from ..extraction.extract import FieldExtractor
from ..extraction.screen import DocumentScreener
from ..logs import get_logger, message_sink
from ..storage.sqlite import CourseStore

log = get_logger("sync")

#: Stage keys and their labels, in the order they run. The frontend renders
#: this list as the checklist on the loading screen.
STAGES: tuple[tuple[str, str], ...] = (
    ("login", "Connecting to Moodle"),
    ("sync", "Fetching courses & activities"),
    ("harvest", "Downloading documents"),
    ("screen", "Screening documents"),
    ("extract", "Extracting grading & attendance"),
)

#: Stages that call the Anthropic API and therefore cost money. Skipped when a
#: sync is started with ``use_model=False``.
MODEL_STAGES = frozenset({"screen", "extract"})


class SyncJob:
    """One run of the pipeline, with a snapshot of its progress.

    Instances are not reused: ``SyncRunner`` builds a new one per run, so the
    state a poll reads is always the state of the latest run.
    """

    def __init__(self, *, site: str, db: str, use_model: bool = True) -> None:
        """Set up the stage list for a run against ``site``, writing ``db``."""
        self.site = site
        self.db = db
        self.use_model = use_model
        self.started_at = time.time()
        self.finished_at: float | None = None
        self.error: str | None = None
        #: Monotonic start times, for the durations the log reports. ``time()``
        #: is what the UI shows; a clock change must not make a stage negative.
        self._monotonic_start = time.monotonic()
        self._started: dict[str, float] = {}
        #: Site info from stage 1, kept for stage 2 to store. It cannot be
        #: written when it arrives: the database does not exist yet, and the
        #: fresh store stage 2 opens would drop whatever stage 1 had put there.
        self.site_info: dict[str, Any] = {}
        self._lock = threading.Lock()
        self._stages: list[dict[str, Any]] = [
            {
                "key": key,
                "label": label,
                "status": "skipped" if not use_model and key in MODEL_STAGES else "pending",
                "detail": "",
            }
            for key, label in STAGES
        ]

    # -- progress ----------------------------------------------------------

    def _stage(self, key: str) -> dict[str, Any]:
        """Return the stage entry for ``key``."""
        return next(s for s in self._stages if s["key"] == key)

    def begin(self, key: str) -> None:
        """Mark a stage as running."""
        with self._lock:
            self._stage(key).update(status="running", detail="")
            self._started[key] = time.monotonic()
        log.info("stage %s started", key)

    def finish(self, key: str, detail: str = "") -> None:
        """Mark a stage as done, with the one-line summary the UI shows."""
        with self._lock:
            self._stage(key).update(status="done", detail=detail)
        log.info("stage %s done in %.1fs -- %s", key, self._elapsed(key), detail or "no detail")

    def fail(self, key: str, detail: str) -> None:
        """Mark a stage as failed and record the run's error."""
        with self._lock:
            self._stage(key).update(status="failed", detail=detail)
            self.error = f"{self._stage(key)['label']}: {detail}"
        log.error("stage %s failed after %.1fs -- %s", key, self._elapsed(key), detail)

    def _elapsed(self, key: str) -> float:
        """Seconds since ``key`` began, or since the run did if it never began."""
        return time.monotonic() - self._started.get(key, self._monotonic_start)

    def state(self) -> dict[str, Any]:
        """Return a snapshot: the stages, the progress fraction and the outcome."""
        with self._lock:
            stages = [dict(s) for s in self._stages]
        counted = [s for s in stages if s["status"] != "skipped"]
        done = sum(1 for s in counted if s["status"] == "done")
        running = any(s["status"] == "running" for s in stages)
        return {
            "running": self.finished_at is None and (running or done < len(counted)),
            "finished": self.finished_at is not None,
            "error": self.error,
            "percent": int(round(done / len(counted) * 100)) if counted else 100,
            "stages": stages,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed": round((self.finished_at or time.time()) - self.started_at, 1),
            "used_model": self.use_model,
        }

    # -- the run -----------------------------------------------------------

    def run(self) -> None:
        """Run every stage in order, stopping at the first failure."""
        log.info(
            "sync started: site %s, database %s, model stages %s",
            self.site,
            self.db,
            "on" if self.use_model else "off",
        )
        try:
            session = self._login()
            if session is None:
                return
            store = self._sync(session)
            if store is None:
                return
            corpus = self._harvest(session)
            if corpus is None or not self.use_model:
                return
            reader = CorpusReader(corpus)
            if not self._screen(reader):
                return
            self._extract(reader, store)
        except Exception as exc:  # the thread must never die silently
            self.error = f"{type(exc).__name__}: {exc}"
            log.exception("sync aborted by an unhandled error")
        finally:
            self.finished_at = time.time()
            elapsed = self.finished_at - self.started_at
            if self.error:
                log.error("sync finished with an error after %.1fs: %s", elapsed, self.error)
            else:
                log.info("sync finished in %.1fs", elapsed)

    def _login(self) -> MoodleSession | None:
        """Stage 1: reuse the stored token, or open a browser for SSO."""
        self.begin("login")
        session = MoodleSession(self.site, on_message=message_sink("sync.login"))
        try:
            info = session.connect()
        except Exception as exc:
            log.exception("login failed")
            self.fail("login", f"{type(exc).__name__}: {exc}")
            return None
        self.site_info = info
        name = info.get("firstname") or info.get("fullname") or "unknown"
        self.finish("login", f"Signed in as {name} · {info.get('sitename') or self.site}")
        return session

    def _sync(self, session: MoodleSession) -> CourseStore | None:
        """Stage 2: rebuild the structured documents from Moodle's own fields."""
        self.begin("sync")
        try:
            documents = CourseDocumentBuilder(session).build_all()
        except Exception as exc:
            log.exception("building the course documents failed")
            self.fail("sync", f"{type(exc).__name__}: {exc}")
            return None
        if not documents:
            self.fail("sync", "no favourite courses found")
            return None
        store = CourseStore(self.db, fresh=True)
        store.save_account(self.site_info, site_url=session.site_url)
        reports = store.save_many(documents)
        activities = sum(r["activities"] for r in reports)
        self.finish("sync", f"{len(documents)} courses · {activities} activities")
        return store

    def _harvest(self, session: MoodleSession):
        """Stage 3: pull every prose page and file into the corpus directory."""
        self.begin("harvest")
        try:
            harvester = UnstructuredHarvester(
                session,
                root=DEFAULT_CORPUS_ROOT,
                fresh=True,
                on_message=message_sink("sync.harvest"),
            )
            manifest = harvester.harvest_all()
        except Exception as exc:
            log.exception("the harvest failed")
            self.fail("harvest", f"{type(exc).__name__}: {exc}")
            return None
        totals = manifest["totals"]
        self.finish(
            "harvest",
            f"{totals['files_downloaded']} files · {totals['texts']} text items",
        )
        return harvester.root

    def _screen(self, reader: CorpusReader) -> bool:
        """Stage 4: decide which harvested documents describe the course rules."""
        self.begin("screen")
        try:
            report = DocumentScreener(
                reader, on_message=message_sink("sync.screen")
            ).run()
        except Exception as exc:
            log.exception("screening failed")
            self.fail("screen", f"{type(exc).__name__}: {exc}")
            return False
        totals = report["totals"]
        if totals.get("aborted"):
            self.fail("screen", str(totals["aborted"]))
            return False
        log.info("screening totals: %s", totals)
        self.finish("screen", f"{totals['relevant']} relevant")
        return True

    def _extract(self, reader: CorpusReader, store: CourseStore) -> bool:
        """Stage 5: read the relevant documents and extend each course record."""
        self.begin("extract")
        try:
            report = FieldExtractor(
                reader, store, on_message=message_sink("sync.extract")
            ).run()
        except Exception as exc:
            log.exception("extraction failed")
            self.fail("extract", f"{type(exc).__name__}: {exc}")
            return False
        totals = report["totals"]
        log.info("extraction totals: %s", totals)
        self.finish(
            "extract",
            f"{totals['courses']} courses · {totals['applied']} additions",
        )
        return True


class SyncRunner:
    """Holds the current or last job and refuses to start a second one."""

    def __init__(
        self,
        *,
        site: str = DEFAULT_SITE,
        db: str | None = None,
        spawn: Callable[[Callable[[], None]], None] | None = None,
    ) -> None:
        """Configure the site and database every job runs against.

        ``spawn`` exists for tests: the default starts a daemon thread, and a
        test can pass something that runs the job inline instead.
        """
        self.site = site
        self.db = db or str(DEFAULT_DB_PATH)
        self._spawn = spawn or self._spawn_thread
        self._job: SyncJob | None = None
        self._lock = threading.Lock()

    @staticmethod
    def _spawn_thread(target: Callable[[], None]) -> None:
        """Run ``target`` in a daemon thread so shutdown is never blocked."""
        threading.Thread(target=target, daemon=True, name="modboard-sync").start()

    @property
    def job(self) -> SyncJob | None:
        """The current or most recent job."""
        return self._job

    def busy(self) -> bool:
        """Whether a job is still running."""
        return self._job is not None and self._job.finished_at is None

    def start(self, *, use_model: bool = True) -> tuple[bool, dict[str, Any]]:
        """Start a job unless one is running. Returns ``(started, state)``."""
        with self._lock:
            if self.busy():
                log.warning("sync refused: one is already running")
                return False, self.state()
            job = SyncJob(site=self.site, db=self.db, use_model=use_model)
            self._job = job
        self._spawn(job.run)
        return True, job.state()

    def state(self) -> dict[str, Any]:
        """Return the current job's snapshot, or a never-run placeholder."""
        if self._job is None:
            return {
                "running": False,
                "finished": False,
                "error": None,
                "percent": 0,
                "stages": [
                    {"key": key, "label": label, "status": "pending", "detail": ""}
                    for key, label in STAGES
                ],
                "started_at": None,
                "finished_at": None,
                "elapsed": 0,
                "used_model": True,
            }
        return self._job.state()
