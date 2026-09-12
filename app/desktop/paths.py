"""Filesystem and environment contract for the desktop shell.

Source runs and packaged runs disagree about what "here" means, so every
path the shell needs is derived explicitly rather than from the working
directory. Two rules shape this module:

- Nothing is migrated or copied. A packaged build never moves, imports, or
  duplicates an existing development database; it simply defaults to its own
  app-data location, and `APP_DB_URL` overrides that if the user wants the
  old file.
- The process environment always wins over any `.env` file, and no value
  read from one is ever logged or returned.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path

from app.desktop.lifecycle import DesktopStartupError

#: Same directory name the existing cache modules already use, so a desktop
#: install keeps all of this app's local state in one place.
APP_DIR_NAME = "Cloud-LLM-LimitChecker"

DATABASE_URL_ENV_VAR = "APP_DB_URL"
DATABASE_FILE_NAME = "limit_checker.db"
DOTENV_FILE_NAME = ".env"
LOG_FILE_NAME = "desktop.log"

#: Shown verbatim in the failure dialog: no path, no OS message. The errno
#: and strerror behind it go to the log instead.
APP_DATA_DIR_ERROR = "Desktop application data directory could not be prepared."


def is_frozen() -> bool:
    """True when running from a PyInstaller build rather than the source tree."""
    return bool(getattr(sys, "frozen", False))


def app_data_dir(env: Mapping[str, str] | None = None) -> Path:
    """Per-user writable directory, mirroring the existing cache modules.

    `env` is injectable so tests never touch the real %LOCALAPPDATA%.
    """
    environ = env if env is not None else os.environ
    local_app_data = environ.get("LOCALAPPDATA")
    if local_app_data:
        base = Path(local_app_data)
    else:
        base = Path(environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share")))
    return base / APP_DIR_NAME


def executable_dir(frozen: bool | None = None) -> Path:
    """Directory the user launched: the executable's own folder when packaged.

    `frozen` is injectable so the packaged layout can be exercised from a
    source checkout without pretending the interpreter is frozen.
    """
    is_packaged = is_frozen() if frozen is None else frozen
    if is_packaged:
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent.parent


def resource_dir() -> Path:
    """Root that holds `static/`, matching `app.main.STATIC_DIR` in both modes."""
    return Path(__file__).resolve().parent.parent.parent


def ensure_app_data_dir(env: Mapping[str, str] | None = None, *, mkdir=None) -> Path:
    """Create the per-user app-data directory, or fail fast with sanitized text.

    Only the packaged default database needs this: SQLite will not create a
    missing parent directory, and finding out at `create_all` time surfaces
    as a dead backend thread rather than an explainable startup error.
    Creating it here also removes the accident that made it work so far --
    the log file happening to be opened first.
    """
    target = app_data_dir(env)
    maker = mkdir if mkdir is not None else (lambda path: path.mkdir(parents=True, exist_ok=True))
    try:
        maker(target)
    except OSError as error:
        # Split deliberately: the dialog gets a constant sentence, while the
        # part that actually explains the failure goes to the log. Without
        # this the user would be told something went wrong and nothing else.
        print(
            f"app data directory could not be created (errno {error.errno}: {error.strerror})",
            file=sys.stderr,
        )
        raise DesktopStartupError(APP_DATA_DIR_ERROR) from None
    return target


def desktop_log_path(env: Mapping[str, str] | None = None) -> Path:
    """Where a packaged run sends stdout/stderr, so a failure leaves a trace."""
    return app_data_dir(env) / LOG_FILE_NAME


def default_database_url(env: Mapping[str, str] | None = None) -> str:
    """The app-data SQLite URL a packaged build falls back to."""
    return f"sqlite:///{(app_data_dir(env) / DATABASE_FILE_NAME).as_posix()}"


def dotenv_candidates(env: Mapping[str, str] | None = None, *, frozen: bool | None = None) -> list[Path]:
    """`.env` files to consider, most specific first.

    A packaged build looks next to the executable and then in app-data; it
    never carries a bundled `.env`. A source run keeps the repository's own
    `.env` first so existing development behavior is unchanged.
    """
    is_packaged = is_frozen() if frozen is None else frozen
    if is_packaged:
        return [executable_dir(is_packaged) / DOTENV_FILE_NAME, app_data_dir(env) / DOTENV_FILE_NAME]
    return [resource_dir() / DOTENV_FILE_NAME, app_data_dir(env) / DOTENV_FILE_NAME]


def resolve_database_url(env: Mapping[str, str], *, frozen: bool | None = None) -> str | None:
    """The value to put in `APP_DB_URL`, or None to leave the app's default alone.

    Precedence, deliberately conservative:

    1. `APP_DB_URL` already set -> preserved untouched.
    2. Source/dev run -> None, so `app.database`'s historical
       `sqlite:///./limit_checker.db` keeps working exactly as before.
    3. Packaged run with nothing set -> this user's app-data file.

    A packaged build therefore starts with an empty database instead of
    silently adopting or copying a development one; pointing it at existing
    history is an explicit `APP_DB_URL` decision by the user.
    """
    existing = env.get(DATABASE_URL_ENV_VAR, "").strip()
    if existing:
        return None
    is_packaged = is_frozen() if frozen is None else frozen
    if not is_packaged:
        return None
    return default_database_url(env)


def configure_environment(
    env: dict[str, str] | None = None,
    *,
    frozen: bool | None = None,
    dotenv_loader=None,
    chdir=None,
    mkdir=None,
) -> dict[str, object]:
    """Prepare process environment before `app.main` is imported.

    Returns a summary for diagnostics that names files and decisions only --
    never a variable value, so nothing here can leak a credential into a log
    or a window title.
    """
    environ = os.environ if env is None else env
    is_packaged = is_frozen() if frozen is None else frozen
    if dotenv_loader is None:  # pragma: no cover - exercised via injection in tests
        from dotenv import load_dotenv as dotenv_loader  # type: ignore[assignment]

    loaded: list[str] = []
    for candidate in dotenv_candidates(environ, frozen=frozen):
        if candidate.is_file():
            # override=False: an explicitly exported variable always wins.
            dotenv_loader(dotenv_path=str(candidate), override=False)
            loaded.append(str(candidate))

    database_url = resolve_database_url(environ, frozen=frozen)
    if database_url is not None:
        # Only when *we* chose the app-data default. An explicitly configured
        # APP_DB_URL is someone else's path: preserve it and create nothing.
        ensure_app_data_dir(environ, mkdir=mkdir)
        environ[DATABASE_URL_ENV_VAR] = database_url

    # A packaged launch inherits whatever directory the shortcut was invoked
    # from, while parts of the app still resolve relative paths (the seed
    # config is read as `config/seed.yaml`, and a missing file is silently
    # treated as "nothing to seed"). Pinning the working directory to the
    # bundle root keeps those lookups pointing at the bundled files instead
    # of failing quietly. A source run is left alone so existing developer
    # behavior is unchanged.
    working_directory: str | None = None
    if is_packaged:
        target = resource_dir()
        (chdir or os.chdir)(target)
        working_directory = str(target)

    return {
        "dotenv_files_loaded": loaded,
        "database_url_source": "preexisting" if database_url is None else "app_data_default",
        "packaged": is_packaged,
        "working_directory_pinned": working_directory,
    }
