"""Modboard's web application: a Flask app over the course database.

    from src.app import create_app
    create_app().run(port=5000)

The store and the sync runner are built once per app and kept in
``app.extensions["modboard"]``, so a test can hand in its own database without
the routes knowing where it came from.
"""

from __future__ import annotations

from pathlib import Path

from flask import Flask

from ..config import DEFAULT_DB_PATH, DEFAULT_SITE
from ..logs import get_logger
from ..storage.sqlite import CourseStore
from .jobs import SyncRunner

log = get_logger("app")


def create_app(
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    site: str = DEFAULT_SITE,
    runner: SyncRunner | None = None,
) -> Flask:
    """Build the application against ``db_path``.

    The store is opened with ``fresh=False``: only a sync may drop the
    database, and the web app never does. Opening it here also creates the
    schema, so a first run with no database serves the onboarding screens
    instead of a stack trace.
    """
    app = Flask(__name__)
    app.json.sort_keys = False
    # Jinja caches a compiled template until the process restarts unless this
    # is on, and a restart would abandon a running sync.
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    store = CourseStore(db_path, fresh=False)
    app.extensions["modboard"] = {
        "store": store,
        "runner": runner or SyncRunner(site=site, db=str(db_path)),
    }
    log.info("app built: database %s, sync target %s", store.path, site)

    from .routes import bp

    app.register_blueprint(bp)
    return app
