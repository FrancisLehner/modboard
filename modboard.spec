# PyInstaller build of Modboard: one executable per OS.
#
#     uv run pyinstaller modboard.spec      # -> dist/modboard (dist/modboard.exe)
#
# PyInstaller does not cross-compile; each OS builds its own, see
# .github/workflows/release.yml.

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

a = Analysis(
    ["app.py"],
    # Flask resolves these against the src.app package, so they keep their
    # place in the tree.
    datas=[
        ("src/app/templates", "src/app/templates"),
        ("src/app/static", "src/app/static"),
    ] + collect_data_files("tzdata"),
    # keyring picks its backend by entry point at runtime, which the import
    # scan cannot see.
    # zoneinfo loads timezone packages dynamically. Windows has no system
    # IANA database, so include both the packages and their timezone files.
    hiddenimports=collect_submodules("keyring.backends") + collect_submodules("tzdata"),
    excludes=["ipykernel", "IPython", "jupyter_client", "tkinter"],
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    name="modboard",
    # The console window is the app's only chrome: it shows the address and
    # closing it stops the server.
    console=True,
    upx=False,
)
