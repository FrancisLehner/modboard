"""Find the user's default browser, for the login window to open in.

Playwright drives Chromium-family browsers through their own executable, so
the default is usable when it is one of those. Safari and Firefox are not:
:func:`default_browser` still names them, so the caller can say why it fell
back to Chrome or Edge.
"""

from __future__ import annotations

import plistlib
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path

from ..logs import get_logger

log = get_logger("moodle.auth")

#: macOS bundle ids of Chromium-family browsers, lowercased as LaunchServices
#: stores them, with the name to show.
_MAC_CHROMIUM = {
    "com.google.chrome": "Google Chrome",
    "com.google.chrome.beta": "Google Chrome Beta",
    "com.google.chrome.dev": "Google Chrome Dev",
    "com.google.chrome.canary": "Google Chrome Canary",
    "com.microsoft.edgemac": "Microsoft Edge",
    "com.microsoft.edgemac.beta": "Microsoft Edge Beta",
    "com.microsoft.edgemac.dev": "Microsoft Edge Dev",
    "com.brave.browser": "Brave Browser",
    "com.vivaldi.vivaldi": "Vivaldi",
    "org.chromium.chromium": "Chromium",
}

#: Windows executables of Chromium-family browsers, with the name to show.
_WIN_CHROMIUM = {
    "chrome.exe": "Google Chrome",
    "msedge.exe": "Microsoft Edge",
    "brave.exe": "Brave",
    "vivaldi.exe": "Vivaldi",
}


@dataclass(frozen=True)
class DefaultBrowser:
    """The user's default browser. ``executable`` is set only if Playwright can drive it."""

    name: str
    executable: Path | None = None


def default_browser() -> DefaultBrowser | None:
    """The default browser, or ``None`` if it cannot be determined."""
    try:
        if sys.platform == "darwin":
            return _mac_default()
        if sys.platform == "win32":
            return _windows_default()
    except Exception as exc:  # registry or plist layout we do not know
        log.warning("could not read the default browser: %s", exc)
    return None


def _mac_default() -> DefaultBrowser:
    """Read the ``https`` handler LaunchServices records for this user."""
    plist = (
        Path.home()
        / "Library/Preferences/com.apple.LaunchServices/com.apple.launchservices.secure.plist"
    )
    bundle_id = None
    if plist.exists():
        for handler in plistlib.loads(plist.read_bytes()).get("LSHandlers", []):
            if handler.get("LSHandlerURLScheme") == "https":
                bundle_id = (handler.get("LSHandlerRoleAll") or "").lower()
                break
    # No entry means the user never changed it: Safari.
    if not bundle_id or bundle_id == "com.apple.safari":
        return DefaultBrowser("Safari")
    name = _MAC_CHROMIUM.get(bundle_id)
    if name is None:
        return DefaultBrowser(bundle_id)
    for apps in (Path("/Applications"), Path.home() / "Applications"):
        bundle = apps / f"{name}.app"
        info = bundle / "Contents" / "Info.plist"
        if info.exists():
            executable = plistlib.loads(info.read_bytes())["CFBundleExecutable"]
            return DefaultBrowser(name, bundle / "Contents" / "MacOS" / executable)
    return DefaultBrowser(name)


def _windows_default() -> DefaultBrowser:
    """Follow the ``https`` UserChoice ProgId to the command that opens it."""
    import winreg

    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER,
        r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice",
    ) as key:
        prog_id = winreg.QueryValueEx(key, "ProgId")[0]
    with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, rf"{prog_id}\shell\open\command") as key:
        command = winreg.QueryValueEx(key, "")[0]
    executable = Path(shlex.split(command, posix=False)[0].strip('"'))
    name = _WIN_CHROMIUM.get(executable.name.lower())
    if name is None:
        return DefaultBrowser(executable.stem)
    return DefaultBrowser(name, executable if executable.exists() else None)
