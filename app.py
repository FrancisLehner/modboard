"""Serve the Modboard dashboard.

    python app.py                  # http://127.0.0.1:5000
    python app.py --port 8000
    python app.py --db other.sqlite

Reads the database the pipeline wrote and serves it. It never syncs on its own:
that is a button on the page, and the run happens in a background thread.

``--debug`` turns on Flask's reloader. It is off by default because the
reloader restarts the process, and a restart would abandon a sync mid-run.

Every start opens a new log file under ``logs/``; see :mod:`src.logs`.
"""

from __future__ import annotations

import argparse

from src import logs
from src.app import create_app
from src.config import DEFAULT_DB_PATH, DEFAULT_SITE, LOG_DIR


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the command line."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite file to read")
    parser.add_argument("--site", default=DEFAULT_SITE, help="Moodle site a sync talks to")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable the reloader. A restart abandons a running sync.",
    )
    parser.add_argument(
        "--log-dir",
        default=str(LOG_DIR),
        help="Directory this session's log file is written to",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Build the app and serve it."""
    args = parse_args(argv)
    # First thing in the run: everything below it, and every module it
    # imports, logs into this session's file.
    log_path = logs.configure(
        log_dir=args.log_dir,
        context={
            "db": args.db,
            "site": args.site,
            "address": f"http://{args.host}:{args.port}",
            "debug": args.debug,
        },
    )
    log = logs.get_logger("app")

    app = create_app(db_path=args.db, site=args.site)
    store = app.extensions["modboard"]["store"]
    courses = store.courses()
    print(f"Modboard on http://{args.host}:{args.port}")
    print(f"  database {store.path} -- {len(courses)} course(s)")
    if log_path:
        print(f"  logging to {log_path}")
    if not courses:
        print("  empty: the page will offer to run a sync")
    log.info("serving on http://%s:%s -- %d course(s)", args.host, args.port, len(courses))
    try:
        app.run(host=args.host, port=args.port, debug=args.debug, use_reloader=args.debug)
    finally:
        log.info("server stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
