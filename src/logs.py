"""One log file per application start.

    from src import logs
    logs.configure(context={"db": path})      # once, at startup
    log = logs.get_logger("moodle.client")    # anywhere else

``configure`` opens a fresh file under :data:`src.config.LOG_DIR`, named for
the moment the process started, and leaves it open for the life of the
process. A second call is a no-op, so importing a module that configures
logging cannot split a session across two files.

The file keeps everything the application says about itself: the Moodle
handshake, one line per web-service call, how many courses and documents each
stage retrieved, and every warning and traceback. The terminal keeps only
warnings and worse, because the loading screen polls and a chatty terminal
would bury the startup banner.

Nothing here raises. A read-only or missing log directory costs the session
its file and says so on the terminal; it never stops the application.
"""

from __future__ import annotations

import logging
import os
import platform
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from .config import (
    LOG_CONSOLE_LEVEL,
    LOG_DATE_FORMAT,
    LOG_DIR,
    LOG_FILE_LEVEL,
    LOG_FILENAME_TEMPLATE,
    LOG_FORMAT,
    LOG_KEEP_SESSIONS,
    LOG_QUIET_LOGGERS,
    LOG_ROOT_NAME,
    LOG_TIMESTAMP_FORMAT,
)

#: The file this session writes to, or ``None`` before ``configure`` has run
#: or when the directory could not be opened.
_session_path: Path | None = None

#: ``configure`` runs once per process. Guarded because the web app, the
#: notebook and a script may each try.
_configured = False
_lock = threading.Lock()


# ======================================================================
# Getting a logger
# ======================================================================


def get_logger(name: str) -> logging.Logger:
    """Return the logger for one part of the application.

    ``name`` is relative to :data:`src.config.LOG_ROOT_NAME`, so
    ``get_logger("moodle.client")`` is ``modboard.moodle.client`` and a single
    level change on ``modboard`` covers every module.
    """
    if name == LOG_ROOT_NAME or name.startswith(f"{LOG_ROOT_NAME}."):
        return logging.getLogger(name)
    return logging.getLogger(f"{LOG_ROOT_NAME}.{name}")


def session_log_path() -> Path | None:
    """The file this session is writing to, or ``None`` if there is none."""
    return _session_path


# ======================================================================
# Progress messages
# ======================================================================


def echo(message: str) -> None:
    """Default ``on_message``: print for the caller, keep it in the log.

    The pipeline classes report progress through an ``on_message`` callback so
    they need not know where their output goes. This is what they use when the
    caller does not say: a notebook or a script still sees its progress lines,
    and the session log gets them whether anyone was watching or not.
    """
    print(message)
    get_logger("pipeline").info("%s", message.strip())


def message_sink(name: str, level: int = logging.INFO) -> Callable[[str], None]:
    """Return an ``on_message`` callable that writes only to the log.

    For the background sync, where there is no terminal to print to: the user
    is watching the loading screen, and the log is where the detail behind it
    is kept.
    """
    logger = get_logger(name)
    return lambda message: logger.log(level, "%s", str(message).strip())


# ======================================================================
# Opening the session file
# ======================================================================


def configure(
    *,
    log_dir: str | Path = LOG_DIR,
    context: dict[str, Any] | None = None,
    console: bool = True,
    file_level: str | int = LOG_FILE_LEVEL,
    console_level: str | int = LOG_CONSOLE_LEVEL,
) -> Path | None:
    """Start a session log and return its path, or ``None`` if there is none.

    ``context`` is logged as the session header's last line -- the settings
    that decide what the run will do, such as the database and the site, so a
    log read weeks later says what it was a log of. Calling this a second time
    returns the first call's path and changes nothing.
    """
    global _session_path, _configured
    with _lock:
        if _configured:
            return _session_path
        _configured = True

        root = logging.getLogger()
        # The handlers filter, not the logger: the file wants DEBUG while the
        # terminal wants WARNING, and a level on the logger would cap both.
        root.setLevel(logging.DEBUG)
        formatter = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT)

        if console:
            stream = logging.StreamHandler(sys.stderr)
            stream.setLevel(console_level)
            stream.setFormatter(formatter)
            root.addHandler(stream)

        _session_path = _open_file_handler(Path(log_dir), formatter, file_level)
        for name, level in LOG_QUIET_LOGGERS.items():
            logging.getLogger(name).setLevel(level)
        _install_excepthooks()

    _log_header(context or {})
    return _session_path


def _open_file_handler(
    log_dir: Path, formatter: logging.Formatter, level: str | int
) -> Path | None:
    """Attach a handler writing to a new file in ``log_dir``; return its path."""
    started = datetime.now().strftime(LOG_TIMESTAMP_FORMAT)
    path = log_dir / LOG_FILENAME_TEMPLATE.format(started=started, pid=os.getpid())
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        _prune(log_dir, keep=LOG_KEEP_SESSIONS)
        handler = logging.FileHandler(path, encoding="utf-8")
    except OSError as exc:
        # Worth a warning rather than a crash: the application works fine
        # without a log, and the terminal handler is already attached.
        logging.getLogger().warning("No session log (%s): %s", path, exc)
        return None
    handler.setLevel(level)
    handler.setFormatter(formatter)
    logging.getLogger().addHandler(handler)
    return path


def _prune(log_dir: Path, *, keep: int | None) -> None:
    """Delete the oldest session logs, leaving room for ``keep`` including this one.

    Sorting by name sorts by time: the name starts with a fixed-width
    timestamp. Reading the directory is enough -- no index to keep in step.
    """
    if keep is None or keep <= 0:
        return
    existing = sorted(_session_files(log_dir))
    for stale in existing[: max(0, len(existing) - keep + 1)]:
        try:
            stale.unlink()
        except OSError:
            pass  # someone else's file, or already gone


def _session_files(log_dir: Path) -> Iterable[Path]:
    """Every session log in ``log_dir``, matched on the configured name."""
    pattern = LOG_FILENAME_TEMPLATE.format(started="*", pid="*")
    return (path for path in log_dir.glob(pattern) if path.is_file())


# ======================================================================
# The header, and crashes nobody caught
# ======================================================================


def _log_header(context: dict[str, Any]) -> None:
    """Write the lines that say what this session is."""
    log = get_logger("session")
    log.info("=" * 68)
    log.info("session started %s", datetime.now().astimezone().isoformat(timespec="seconds"))
    log.info(
        "python %s on %s (pid %s)",
        platform.python_version(),
        platform.platform(),
        os.getpid(),
    )
    log.info("log file %s", _session_path or "none -- terminal only")
    if context:
        log.info("%s", " ".join(f"{key}={value}" for key, value in context.items()))
    log.info("=" * 68)


def _install_excepthooks() -> None:
    """Record exceptions that reach the top of the main thread or a worker.

    The sync runs in a thread. Without this, a failure outside the stages'
    own ``try`` blocks would print to a terminal nobody is reading and leave
    the log with no trace of why the run stopped.
    """
    log = get_logger("session")

    previous = sys.excepthook

    def on_exception(exc_type, exc, tb) -> None:
        """Log an uncaught exception, then hand it to the previous hook."""
        if not issubclass(exc_type, KeyboardInterrupt):
            log.critical("uncaught exception", exc_info=(exc_type, exc, tb))
        previous(exc_type, exc, tb)

    sys.excepthook = on_exception

    def on_thread_exception(args: Any) -> None:
        """Log an exception that killed a thread."""
        if args.exc_type is not None and not issubclass(args.exc_type, SystemExit):
            log.critical(
                "uncaught exception in thread %s",
                getattr(args.thread, "name", "?"),
                exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
            )

    threading.excepthook = on_thread_exception
