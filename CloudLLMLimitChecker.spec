# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build definition for the Windows desktop shell (onedir).

Tracked on purpose: the build must be reproducible from the repository, and
what is - and is not - bundled is part of the security contract.

Bundled: the application package, the static dashboard, and the seed config.
Never bundled: `.env`, any database file, any cache file. Those belong to the
machine the app runs on, not to the artifact.

onedir rather than onefile: the app serves `static/` and opens a SQLite file,
and a plain folder keeps both predictable (no per-launch temporary
extraction). onefile stays a later decision.
"""

from PyInstaller.utils.hooks import collect_submodules

datas = [
    ("static", "static"),
    ("config", "config"),
]

hiddenimports = [
    *collect_submodules("app"),
    *collect_submodules("uvicorn"),
]

a = Analysis(
    ["app/desktop/__main__.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "pytest"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="CloudLLMLimitChecker",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="CloudLLMLimitChecker",
)
