"""Process-wide cache in front of ``keyring``.

On macOS every Keychain read by a program the item's access list does not
name asks for the login password. The app reads its secrets on every state
poll and every dashboard redraw, so without a cache one click could cost a
prompt. Each entry is read from the OS store at most once per process; writes
and deletes go through and update the cache.

All access to the credential store goes through here, so the cache cannot go
stale behind its back.
"""

from __future__ import annotations

import threading

_lock = threading.Lock()
_cache: dict[tuple[str, str], str | None] = {}


def _keyring():
    """Import ``keyring`` lazily; raise if it is unavailable."""
    try:
        import keyring
    except ImportError as exc:  # pragma: no cover - depends on env
        raise RuntimeError(
            "keyring is not installed, so secrets cannot be stored securely."
        ) from exc
    return keyring


def get_password(service: str, username: str) -> str | None:
    """Return the stored secret, or ``None``. Asks the OS store once per entry."""
    key = (service, username)
    with _lock:
        if key not in _cache:
            _cache[key] = _keyring().get_password(service, username)
        return _cache[key]


def set_password(service: str, username: str, password: str) -> None:
    """Store a secret, unless the cache already holds that exact value."""
    key = (service, username)
    with _lock:
        if _cache.get(key) == password:
            return
        _keyring().set_password(service, username, password)
        _cache[key] = password


def delete_password(service: str, username: str) -> bool:
    """Delete a secret. True when one existed."""
    key = (service, username)
    with _lock:
        existed = (
            _cache[key] if key in _cache else _keyring().get_password(service, username)
        ) is not None
        if existed:
            _keyring().delete_password(service, username)
        _cache[key] = None
        return existed

