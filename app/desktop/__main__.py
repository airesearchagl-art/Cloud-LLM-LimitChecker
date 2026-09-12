"""Desktop entry point: prepare environment, own a backend, show one window.

Run with `python -m app.desktop` during development; the packaged executable
calls `main()` through the same path, so there is no separate packaged-only
code branch to keep in sync.

Two things here exist purely because of how a packaged windowed build
behaves, and both are load-bearing:

- A windowed PyInstaller build has `sys.stdout` and `sys.stderr` set to
  None. uvicorn builds its log formatter with `sys.stdout.isatty()`, so a
  None stream makes the backend fail to start at all. `ensure_std_streams`
  gives the process real streams before anything else runs.
- With no console, a printed error message goes nowhere. Fatal startup
  problems are therefore written to a log file *and* shown in a native
  dialog, so a double-clicked app that cannot start says why.

The GUI import lives inside `open_window`: everything above it -- streams,
environment, port, identity, readiness -- is exercised in CI with no
pywebview installed and no window on screen.
"""

from __future__ import annotations

import os
import sys
import threading
import traceback
from pathlib import Path

from app.desktop import paths
from app.desktop.lifecycle import (
    BackendOwnership,
    DesktopStartupError,
    ServerHandle,
    dashboard_url,
    ensure_dashboard_reachable,
    prepare_backend,
    resolve_port,
    shutdown_backend,
)

WINDOW_TITLE = "Cloud LLM Limit Checker"
WINDOW_WIDTH = 1280
WINDOW_HEIGHT = 900
BACKEND_WATCHDOG_INTERVAL_SECONDS = 1.0

#: Win32 MessageBox flags: OK button, error icon, foreground.
_MB_ICONERROR = 0x10
_MB_SETFOREGROUND = 0x10000


#: One launch's worth of output is tiny; this only stops an install that
#: runs for years from growing an unbounded file. Oldest content is dropped
#: wholesale rather than rotated into numbered files.
MAX_LOG_BYTES = 1_000_000


def _open_log_stream(path: Path):
    """A line-buffered, size-capped log file; a null sink if it is unwritable."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        mode = "w" if path.exists() and path.stat().st_size > MAX_LOG_BYTES else "a"
        return open(path, mode, encoding="utf-8", buffering=1)
    except OSError:
        return open(os.devnull, "w", encoding="utf-8")


def ensure_std_streams(*, log_path: Path | None = None, opener=_open_log_stream) -> Path | None:
    """Replace absent stdout/stderr with a log file. Returns the path used, if any.

    A source run already has both streams and is left untouched. This runs
    before the main try block, so it must not raise: if the log cannot be
    opened at all, the streams still get a null sink and startup continues.
    """
    if sys.stdout is not None and sys.stderr is not None:
        return None
    target = paths.desktop_log_path() if log_path is None else log_path
    try:
        stream = opener(target)
    except OSError:
        stream = open(os.devnull, "w", encoding="utf-8")
        target = None
    if getattr(stream, "name", None) == os.devnull:
        # The opener degraded to a null sink. Returning the path anyway would
        # make the failure dialog point at a log file that does not exist.
        target = None
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream
    return target


def report_fatal_error(message: str, *, log_path: Path | None = None, message_box=None) -> None:
    """Make a startup failure visible even when there is no console.

    The message is the adapter's own generic text -- never an environment
    value, a credential, or an OS error string. The log path is appended
    only when a log is actually being written, so the dialog never sends the
    user looking for a file that does not exist.
    """
    print(f"Cloud LLM Limit Checker could not start: {message}", file=sys.stderr)
    if log_path is not None:
        detail = f"{message}\n\nDetails: {log_path}"
    else:
        detail = message
    if message_box is None:  # pragma: no cover - real Win32 dialog
        if sys.platform != "win32":
            return
        import ctypes

        message_box = ctypes.windll.user32.MessageBoxW  # type: ignore[attr-defined]
    message_box(None, detail, WINDOW_TITLE, _MB_ICONERROR | _MB_SETFOREGROUND)


def open_window(url: str, *, handle: ServerHandle | None, webview_module=None) -> None:
    """Show the dashboard and block until the user closes it.

    `webview_module` is injectable so the surrounding flow can be tested
    against a fake; in production it is imported here and nowhere else.
    """
    if webview_module is None:  # pragma: no cover - real GUI path
        import webview as webview_module  # type: ignore[no-redef]

    window = webview_module.create_window(
        WINDOW_TITLE,
        url,
        width=WINDOW_WIDTH,
        height=WINDOW_HEIGHT,
    )

    stop_watchdog = threading.Event()
    if handle is not None:
        def watch_backend() -> None:
            # If the backend thread dies while the window is open, close the
            # window instead of leaving a dashboard that can no longer load.
            while not stop_watchdog.wait(BACKEND_WATCHDOG_INTERVAL_SECONDS):
                if not handle.is_alive():
                    # The user may have closed the window in the same instant
                    # the backend went away; re-check before touching it.
                    if stop_watchdog.is_set():
                        return
                    try:
                        window.destroy()
                    except Exception:
                        pass
                    return

        threading.Thread(target=watch_backend, name="cloud-llm-backend-watchdog", daemon=True).start()

    try:
        # debug=False keeps devtools off in a packaged build.
        webview_module.start(debug=False)
    finally:
        stop_watchdog.set()


def main(argv: list[str] | None = None) -> int:
    """Returns a process exit code; never raises for an expected failure."""
    del argv  # no command line surface yet

    log_path = ensure_std_streams()

    ownership: BackendOwnership | None = None
    handle: ServerHandle | None = None
    try:
        paths.configure_environment()
        port = resolve_port(os.environ)
        ownership, handle = prepare_backend(port)
        ensure_dashboard_reachable(port)
        open_window(dashboard_url(port), handle=handle)
    except DesktopStartupError as error:
        report_fatal_error(str(error), log_path=log_path)
        return 1
    except Exception:
        # Unexpected, but a packaged build must still say something rather
        # than disappearing: full traceback to the log, generic text on screen.
        traceback.print_exc()
        report_fatal_error("an unexpected error occurred during startup", log_path=log_path)
        return 1
    finally:
        if ownership is not None and not shutdown_backend(ownership, handle):
            print("Warning: the backend did not stop cleanly.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
