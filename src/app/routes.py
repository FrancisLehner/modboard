"""HTTP surface: one page and a small JSON API.

The page is a shell; everything in it is drawn by ``static/app.js`` from
``GET /api/dashboard``. Keeping the rendering on the client is what lets the
loading screen poll a running sync and then swap itself for the dashboard
without a reload.

Reads and writes are deliberately lopsided. Every read goes through
``view_model.dashboard`` and touches nothing but the database. The writes are
storing the API key, starting a sync, marking an activity as done or not done
and logging out, and all are POSTs, so no amount of link-following or prefetching can
spend money or open a browser window.
"""

from __future__ import annotations

import os
import re
from typing import Any

from flask import Blueprint, current_app, jsonify, render_template, request

from ..config import ENV_API_KEY_VAR, SYNC_REBUILDS_FROM_SCRATCH
from ..data_retention.session import MoodleSession
from ..extraction.screen import load_dotenv_once, resolve_api_key, store_api_key
from ..logs import get_logger
from . import view_model

#: Anthropic keys all start this way. Checked before storing so a typo is
#: caught here rather than by the first screening call minutes later.
API_KEY_PATTERN = re.compile(r"^sk-ant-[\w-]{10,}$")

bp = Blueprint("modboard", __name__)

log = get_logger("http")


def api_key_present() -> bool:
    """Whether a key can be resolved from the environment or the keyring.

    ``resolve_api_key`` answers ``None`` both when nothing is stored and when
    the environment already holds a key, since the SDK reads that itself. Only
    one of those is a reason to ask the user for one.
    """
    load_dotenv_once()
    return bool(os.environ.get(ENV_API_KEY_VAR) or resolve_api_key())


def _moodle_session() -> MoodleSession:
    """A session on the sync's site, for its token store and nothing else.

    Building one makes no request: the authenticator only talks to Moodle when
    a login starts.
    """
    return MoodleSession(_runner().site, on_message=lambda message: None)


def moodle_signed_in() -> bool:
    """Whether a Moodle token is stored for the sync's site.

    Not whether it still works -- that costs a request, and the sync finds out
    anyway. A missing token is what a logout leaves behind.
    """
    session = _moodle_session()
    try:
        return bool(session.store.load(session.site_url))
    except Exception:
        log.exception("could not read the Moodle token store")
        return False


def _store():
    """The ``CourseStore`` this app was built with."""
    return current_app.extensions["modboard"]["store"]


def _runner():
    """The ``SyncRunner`` this app was built with."""
    return current_app.extensions["modboard"]["runner"]


def app_state() -> dict[str, Any]:
    """Which screen the frontend should open on, and why.

    ``onboarding`` is the one decision the server makes about screens: no key
    means the key screen, no courses means the get-started screen, no Moodle
    token beside existing courses means the signed-out screen, and anything
    else means the dashboard. The frontend may navigate away from it afterwards.

    A first run has no token either, but its get-started screen already signs
    in, so it does not need a second screen saying so.
    """
    runner = _runner()
    courses = _store().courses()
    has_key = api_key_present()
    signed_in = moodle_signed_in()
    if not has_key:
        screen = "apikey"
    elif not courses:
        screen = "start"
    elif not signed_in and not runner.state()["running"]:
        screen = "signedout"
    else:
        screen = "overview"
    return {
        "has_api_key": has_key,
        "signed_in": signed_in,
        "course_count": len(courses),
        "screen": screen,
        "sync": runner.state(),
    }


# -- the page ----------------------------------------------------------


@bp.get("/")
def index():
    """Serve the application shell."""
    return render_template("index.html")


# -- reads -------------------------------------------------------------


@bp.get("/api/state")
def get_state():
    """Return the startup screen, the course count and the sync's progress."""
    return jsonify(app_state())


@bp.get("/api/dashboard")
def get_dashboard():
    """Return every course in the shape the frontend renders."""
    payload = view_model.dashboard(_store())
    payload["state"] = app_state()
    return jsonify(payload)


@bp.get("/api/courses/<int:course_id>")
def get_course(course_id: int):
    """Return one course, for a deep link that does not need the whole set."""
    document = _store().load(course_id)
    if document is None:
        log.warning("GET /api/courses/%s: not in the database", course_id)
        return jsonify({"error": f"no course {course_id} in the database"}), 404
    return jsonify(view_model.course_view(document, _store().manual_status(course_id)))


# -- writes ------------------------------------------------------------


@bp.post("/api/key")
def post_key():
    """Store an Anthropic API key in the OS credential store."""
    api_key = str((request.get_json(silent=True) or {}).get("api_key") or "").strip()
    if not API_KEY_PATTERN.match(api_key):
        # The key itself is never logged, here or anywhere else.
        log.warning("POST /api/key rejected: not shaped like an Anthropic key")
        return jsonify({"error": "That does not look like an Anthropic key."}), 400
    try:
        store_api_key(api_key)
    except Exception as exc:
        log.exception("POST /api/key: the credential store refused the write")
        return jsonify({"error": f"Could not reach the credential store: {exc}"}), 500
    log.info("Anthropic API key stored in the credential store")
    return jsonify(app_state())


@bp.post("/api/courses/<int:course_id>/status")
def post_status(course_id: int):
    """Mark one activity as done or not done, for what Moodle cannot see.

    ``key`` is the activity's ``key`` from the dashboard payload. Returns the
    whole dashboard again, so the page redraws from one consistent answer.
    """
    body = request.get_json(silent=True) or {}
    key = str(body.get("key") or "").strip()
    store = _store()
    if store.load(course_id) is None:
        log.warning("POST /api/courses/%s/status: not in the database", course_id)
        return jsonify({"error": f"no course {course_id} in the database"}), 404
    if not key or not isinstance(body.get("done"), bool):
        log.warning("POST /api/courses/%s/status rejected: %r", course_id, body)
        return jsonify({"error": "Expected a key and done: true or false."}), 400
    store.set_manual_status(course_id, key, body["done"])
    return get_dashboard()


@bp.post("/api/logout")
def post_logout():
    """Delete the stored Moodle token, so the next sync has to sign in again.

    The course database is left alone: it is what the student already pulled,
    and signing in again refreshes it rather than starting over. Refused while
    a sync runs, which holds the token and would store it again on the way.
    """
    if _runner().state()["running"]:
        log.warning("POST /api/logout refused: a sync is running")
        return jsonify({"error": "Wait for the sync to finish before logging out."}), 409
    removed = _moodle_session().logout()
    log.info("POST /api/logout: %s", ", ".join(removed) or "nothing was stored")
    return jsonify(app_state())


@bp.post("/api/sync")
def post_sync():
    """Start a sync, unless one is already running.

    ``full_rebuild`` pulls and reads everything again instead of only what
    Moodle changed since the last sync.
    """
    body = request.get_json(silent=True) or {}
    use_model = bool(body.get("use_model", True))
    full_rebuild = bool(body.get("full_rebuild", SYNC_REBUILDS_FROM_SCRATCH))
    if use_model and not api_key_present():
        log.warning("POST /api/sync refused: no Anthropic API key is stored")
        return jsonify({"error": "No Anthropic API key is stored."}), 400
    started, state = _runner().start(use_model=use_model, full_rebuild=full_rebuild)
    log.info(
        "POST /api/sync: %s (model stages %s, %s)",
        "started" if started else "refused, one is already running",
        "on" if use_model else "off",
        "full rebuild" if full_rebuild else "new and changed only",
    )
    return jsonify({"started": started, "sync": state}), (202 if started else 409)


@bp.get("/api/sync")
def get_sync():
    """Return the current or most recent sync's progress."""
    return jsonify(_runner().state())
