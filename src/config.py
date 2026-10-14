"""Every tunable value in the project.

Grouped by pipeline stage: site and web services, credentials, storage
locations, structured documents, harvest, document reading, and the two model
passes.

Not here, deliberately: SQL, regular expressions and JSON wire schemas, which
are the shape of a thing rather than a setting, and private module-level
helpers. Prompts are here, since they are the main lever on model output.
"""

from __future__ import annotations

from pathlib import Path

#: Repository root. Paths resolved against this rather than the working
#: directory, which depends on where a script was run from.
PROJECT_ROOT = Path(__file__).resolve().parents[1]


# ======================================================================
# Moodle site and web services
# ======================================================================

#: Default Moodle site.
DEFAULT_SITE = "https://moodle.zhaw.ch"

#: The web service Moodle reserves for the official mobile app.
MOBILE_SERVICE = "moodle_mobile_app"

#: Default custom URL scheme of the official app. A site can override it via
#: ``forcedurlscheme``, so capture matches any non-http scheme.
DEFAULT_URL_SCHEME = "moodlemobile"

#: Timeout for every network call, in seconds.
HTTP_TIMEOUT = 15

#: How long to wait for the user to finish SSO, in seconds.
LOGIN_TIMEOUT_SECONDS = 300

#: Read-only functions the client may call. Anything else raises before a
#: request is made, which makes a write call impossible to express.
DEFAULT_ALLOWED_FUNCTIONS = frozenset(
    {
        # Identity and course set
        "core_webservice_get_site_info",
        "core_enrol_get_users_courses",
        "core_course_get_enrolled_courses_by_timeline_classification",
        "core_course_get_contents",
        "core_course_get_courses_by_field",
        # Dates
        "core_calendar_get_action_events_by_courses",
        "core_calendar_get_action_events_by_timesort",
        "mod_assign_get_assignments",
        "mod_quiz_get_quizzes_by_courses",
        "mod_workshop_get_workshops_by_courses",
        "mod_lesson_get_lessons_by_courses",
        # Personal state
        "core_completion_get_activities_completion_status",
        "mod_assign_get_submission_status",
        "mod_quiz_get_user_attempts",
        "mod_quiz_get_user_best_grade",
        # Free text and files -- the unstructured harvest
        "core_block_get_course_blocks",
        "mod_page_get_pages_by_courses",
        "mod_label_get_labels_by_courses",
        "mod_url_get_urls_by_courses",
        "mod_book_get_books_by_courses",
        "mod_forum_get_forums_by_courses",
        "mod_forum_get_forum_discussions",
        "mod_forum_get_discussion_posts",
        "mod_lesson_get_pages",
        "mod_lesson_get_page_data",
    }
)

#: Advice attached to the Moodle error codes worth explaining.
ERROR_HINTS = {
    "invalidtoken": (
        "The stored token is invalid or has been revoked. Log in again to get "
        "a new one."
    ),
    "accessexception": (
        "The token exists but is not allowed to call this function. "
        "The site may restrict the mobile service."
    ),
    "invalidparameter": "Moodle rejected a parameter value.",
    "requirecorrectaccess": (
        "Moodle refused the request as unauthorised for this service."
    ),
    "servicenotavailable": "The mobile web service is disabled on this site.",
    "enablewsdescription": "Web services are disabled on this site.",
}


# ======================================================================
# Credentials
# ======================================================================

#: OS credential store entries, keyed by service name so several secrets can
#: coexist. Both are bearer credentials and neither belongs in a file.
KEYRING_SERVICE_TOKEN = "zhaw-moodle-token"
KEYRING_SERVICE_PRIVATE_TOKEN = "zhaw-moodle-private-token"
KEYRING_SERVICE_ANTHROPIC = "anthropic-api-key"
KEYRING_USER_ANTHROPIC = "default"

#: Optional ``.env`` read into the environment at startup. The SDK reads the
#: environment and knows nothing about dotfiles. Gitignored.
ENV_FILE = PROJECT_ROOT / ".env"

#: Environment variable the SDK reads.
ENV_API_KEY_VAR = "ANTHROPIC_API_KEY"


# ======================================================================
# Storage locations
# ======================================================================

#: SQLite database: one JSON document per course.
DEFAULT_DB_PATH = PROJECT_ROOT / "moodle.sqlite"

#: Pretty-printed dump of the same documents.
DEFAULT_DUMP_PATH = PROJECT_ROOT / "course_documents.txt"

#: Harvest output. A path keeps every corpus in one place, next to the
#: database; ``None`` falls back to a fresh temp directory per run.
CORPUS_DIR_PREFIX = "moodle_corpus_"
DEFAULT_CORPUS_ROOT: Path | None = PROJECT_ROOT / "corpus"

#: Index files written into a corpus directory.
MANIFEST_FILENAME = "manifest.json"
SCREENING_FILENAME = "screening.json"
EXTRACTION_FILENAME = "extraction.json"

#: A sync rebuilds rather than updates: the database and the corpus are
#: cleared before they are rewritten, so a run cannot inherit a stale
#: document or a file a course has since removed. Set to ``False`` once
#: there is a reuse check worth having -- nothing else needs changing.
SYNC_REBUILDS_FROM_SCRATCH = True


# ======================================================================
# Session logging
# ======================================================================

#: One log file per application start is written here. Gitignored: a log
#: belongs to the run that produced it, not to the repository.
LOG_DIR = PROJECT_ROOT / "logs"

#: Every module's logger hangs off this name, so one level setting covers the
#: whole application and third-party loggers stay separable from ours.
LOG_ROOT_NAME = "modboard"

#: Session file name. ``started`` is :data:`LOG_TIMESTAMP_FORMAT`; ``pid``
#: separates two starts that land in the same second.
LOG_FILENAME_TEMPLATE = "modboard-{started}-{pid}.log"
LOG_TIMESTAMP_FORMAT = "%Y%m%d-%H%M%S"

#: What the file keeps, and what the terminal shows. The file takes
#: everything, including one line per web-service call; the terminal stays at
#: warnings so the startup banner and real problems are all that appear
#: there. Raise the file level to ``"INFO"`` to drop the per-call lines.
LOG_FILE_LEVEL = "DEBUG"
LOG_CONSOLE_LEVEL = "WARNING"

LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)-26s %(message)s"
LOG_DATE_FORMAT = "%H:%M:%S"

#: Session files kept in :data:`LOG_DIR`; older ones are deleted on startup.
#: ``None`` keeps every one.
LOG_KEEP_SESSIONS = 20

#: Third-party loggers and the level they are pinned to. Flask's request log
#: would otherwise record every poll the loading screen makes, and the HTTP
#: libraries log a line per connection.
LOG_QUIET_LOGGERS = {
    "werkzeug": "WARNING",
    "urllib3": "WARNING",
    "httpx": "WARNING",
    "httpcore": "WARNING",
    "anthropic": "INFO",
    "asyncio": "WARNING",
    # Lists every credential backend on the machine on the first lookup.
    "keyring": "WARNING",
}


# ======================================================================
# Structured documents (phase 1)
# ======================================================================

#: Bumped when the course-document shape changes.
DOCUMENT_SCHEMA_VERSION = 1

#: Course dates are local to the institution, not to the caller.
DEFAULT_TIMEZONE = "Europe/Zurich"

#: Document keys holding dated items -> activity type. Spelled out rather
#: than derived from the key.
ACTIVITY_GROUPS = {
    "quizzes": "quiz",
    "assignments": "assign",
    "workshops": "workshop",
    "lessons": "lesson",
}

#: Window for the upcoming-deadlines summary, in days.
DEFAULT_UPCOMING_DAYS = 60


# ======================================================================
# Harvest (phase 1b) -- pulling raw prose and files
# ======================================================================

CORPUS_SCHEMA_VERSION = 1

#: Refuse a single file larger than this.
MAX_FILE_BYTES = 128 * 1024 * 1024

#: Download chunk size, so a large file never sits in memory twice.
DOWNLOAD_CHUNK = 64 * 1024

#: Per-file download timeout, in seconds.
DOWNLOAD_TIMEOUT = 60

#: Media skipped by default: no text to extract.
SKIP_MIMETYPE_PREFIXES = ("video/", "audio/")

#: Longest path component the harvest will write.
MAX_NAME_LENGTH = 60

#: Forum posts cost one call per forum plus one per discussion; lesson files
#: cost one call per lesson page.
HARVEST_INCLUDE_FORUM_POSTS = True
HARVEST_INCLUDE_LESSON_FILES = False


# ======================================================================
# Reading documents for a model (phase 2a)
# ======================================================================

#: Formats read as UTF-8 text, with markup stripped.
TEXT_SUFFIXES = {
    ".html", ".htm", ".md", ".txt", ".csv", ".tsv", ".json", ".xml",
    ".py", ".r", ".qmd", ".rmd", ".tex", ".bib", ".yml", ".yaml", ".sql",
    ".ipynb",
}

#: Image formats, mapped to their media type.
IMAGE_SUFFIXES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

#: Formats that cannot carry course information. Skipped without an API call,
#: on the format alone.
INERT_SUFFIXES = {
    ".css", ".js", ".map", ".ttf", ".woff", ".woff2", ".eot", ".ico", ".svg",
    ".zip", ".tar", ".gz", ".7z", ".rar", ".mp4", ".mp3", ".mov", ".avi",
    ".whl", ".so", ".dylib", ".exe", ".bin",
    ".rdata", ".rds", ".npy", ".npz", ".pkl", ".pt", ".h5", ".parquet",
}

#: Formats that may carry course information but cannot be opened yet.
#: Reported as a gap, never as not relevant.
NEEDS_CONVERSION_SUFFIXES = {
    ".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".xls", ".odt",
}

#: Suffixes the :data:`MAX_NAME_LENGTH` cap truncated, mapped back.
TRUNCATED_SUFFIXES = {".ipyn": ".ipynb", ".htm l": ".html", ".pd": ".pdf"}

#: Screening needs enough text to recognise what a document is. Truncation is
#: recorded, so a later pass can re-read the whole file.
MAX_TEXT_CHARS = 60_000

#: Below this many extracted characters per page, a PDF is treated as
#: scanned and sent as a PDF rather than as text.
MIN_CHARS_PER_PAGE = 100

#: Pages of a scanned PDF to send. Each page costs image tokens.
MAX_SCANNED_PAGES = 20

#: Hard ceiling per attachment; the API rejects oversized images.
MAX_BINARY_BYTES = 4 * 1024 * 1024


# ======================================================================
# LLM passes (phase 2)
# ======================================================================

SCREENING_SCHEMA_VERSION = 1

#: Pass 1 reads every document once.
SCREEN_MODEL = "claude-haiku-4-5"

#: Pass 2 reads only what pass 1 kept.
EXTRACT_MODEL = "claude-sonnet-5-5"

#: A verdict is a small object; the cap only has to fit the quote.
SCREEN_MAX_TOKENS = 1024

#: Concurrent in-flight requests. Trades latency against rate limit; the SDK
#: retries 429s on its own.
SCREEN_WORKERS = 8

#: Passed to the SDK, which retries 408/409/429/5xx with backoff.
SCREEN_MAX_RETRIES = 4

#: Per-request timeout, in seconds.
SCREEN_TIMEOUT = 120.0

#: What pass 1 looks for. A document matching none of these is dropped.
SCREEN_CATEGORIES = (
    "grading",
    "course_structure",
    "assignments",
    "quizzes",
    "final_exam",
)

#: The pass-1 prompt. The category wording must stay in step with
#: :data:`SCREEN_CATEGORIES`, which the response schema is generated from.
SCREEN_SYSTEM_PROMPT = """\
You are screening documents pulled from a university Moodle course. Your only \
job is to decide whether a document is worth reading closely later -- you are \
not extracting anything yet.

Answer true when the document states, in text or in a table or image, anything \
about:
- grading: how the module mark is composed, weights, pass criteria, what counts
- course_structure: the shape of the course -- semester plan, week-by-week \
topics, which sessions happen when, lecture/lab organisation
- assignments: hand-ins, their briefs, their deadlines, submission rules
- quizzes: tests and their timing, attempts, or rules
- final_exam: the exam -- date, format, scope, permitted materials

Answer false for teaching material that only conveys subject content: lecture \
slides explaining a topic, exercise notebooks, datasets, code, readings, \
figures. These are what a course is *about*; we want documents about how the \
course is *run*.

Judge what the document actually says, not what its filename suggests. A file \
named after a semester plan that turns out to be a lecture on planning is \
false; an untitled screenshot showing a week-by-week table is true.

When in doubt, answer true -- a later pass filters again, and a missed \
grading rule cannot be recovered."""


# ======================================================================
# Extraction (phase 2b) -- filling the gaps the API cannot answer
# ======================================================================

#: Bumped when the extraction report shape changes.
EXTRACT_SCHEMA_VERSION = 1

#: Room for thinking and the proposal object; ``max_tokens`` caps both.
EXTRACT_MAX_TOKENS = 32_000

#: One of ``low``, ``medium``, ``high``, ``xhigh``, ``max``.
EXTRACT_EFFORT = "high"

#: Per-request timeout, in seconds. One call reads a whole course.
EXTRACT_TIMEOUT = 900.0

EXTRACT_MAX_RETRIES = 3

#: Courses processed concurrently.
EXTRACT_WORKERS = 3

#: Ceiling on the document text sent for one course. A cost guard; documents
#: beyond it are reported as skipped rather than dropped.
EXTRACT_MAX_INPUT_CHARS = 600_000

#: Server-side fallbacks: a declined request is answered by another model in
#: the same call. Disabled automatically if the API rejects the parameter.
EXTRACT_USE_FALLBACKS = True
EXTRACT_FALLBACK_BETA = "server-side-fallback-2026-07-01"

#: Confidence levels, most confident first. Anything below
#: :data:`EXTRACT_MIN_CONFIDENCE` is reported but not merged.
EXTRACT_CONFIDENCE_LEVELS = ("high", "medium", "low")
EXTRACT_MIN_CONFIDENCE = "low"

#: Activity types pass 2 may propose as new entries, mapped to the document
#: array they land in. Such entries carry an origin marker and no ``cmid``.
EXTRACT_ACTIVITY_ARRAYS = {
    "quiz": "quizzes",
    "assign": "assignments",
    "workshop": "workshops",
    "lesson": "lessons",
}

#: Fields on an existing activity that pass 2 may fill when they are ``None``,
#: mapped to the coercion applied before writing. Every other field on an
#: existing entry is read-only.
ENRICHABLE_FIELDS = {
    "start_date": "date",
    "start_time": "time",
    "due_date": "date",
    "due_time": "time",
    "cutoff_date": "date",
    "cutoff_time": "time",
    "attempts_allowed": "int",
    "grade_max": "float",
    "is_graded": "bool",
    "description": "text",
}

#: Marks an entry produced by the extraction pass. Absent on everything the
#: API supplied, so the two stay distinguishable without rewriting a field.
EXTRACTED_ORIGIN = "extracted"

#: The pass-2 prompt. States in words what :mod:`src.extraction.merge`
#: enforces in code.
EXTRACT_SYSTEM_PROMPT = """\
You are completing a structured record of a university course.

You are given two things: the record built from Moodle's own API, and the \
documents from that course which an earlier pass judged to contain \
information about how the course is run.

Your job is to **add what is missing**. Specifically:

1. `credits` -- the module's ECTS credits, if a document states them.
2. `grading` -- one entry per component of the module mark (exam, project, \
   hand-ins, ...), with its weight as a percentage where one is given.
3. `mandatory_onsite` -- sessions where physical attendance is required: \
   presentations, lab exams, compulsory excursions.
4. `new_activities` -- assessments a document describes that are **absent** \
   from the record. Check the existing names first; if an activity is already \
   listed, do not propose it again.
5. `enrichments` -- values for fields that are `null` in the record, keyed by \
   the activity's `cmid`.

Rules
-----
**Never contradict the record.** Every value already in it came from Moodle \
and is authoritative. Propose a value for a field only when that field is \
currently `null`. If a document disagrees with a value that is already there, \
do not propose a change -- say so in `notes` instead.

**Cite everything.** Each proposal carries `source` (the exact document path \
as given in that document's header), `confidence`, and `quote` -- a short \
verbatim excerpt that supports it. A proposal you cannot quote for does not \
belong in the answer.

**Confidence means what it says.** `high` -- the document states it plainly. \
`medium` -- it follows clearly from what the document says. `low` -- you are \
inferring. Do not inflate; a `low` is more useful than a wrong `high`.

**Dates are `YYYY-MM-DD` and times are `HH:MM`, 24-hour.** A date you cannot \
pin to a day is not a date -- leave it null and describe it in the text \
instead. Mind the academic year: a semester running from autumn into the new \
year means January dates belong to the following calendar year.

Propose nothing rather than guessing. An empty answer for a course whose \
documents say nothing about grading is the correct answer."""
