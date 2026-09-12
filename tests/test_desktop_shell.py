"""Windows desktop shell: lifecycle, identity, paths, and endpoint regression.

No GUI is ever started here. Every collaborator that would touch a real
port, a real window, or a real clock is injected, so the whole contract --
including timeout, crash, and foreign-service paths -- runs in CI on a
machine with no pywebview installed.
"""

import ast
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main as app_main
from app.desktop import __main__ as desktop_main
from app.desktop import lifecycle
from app.desktop import paths
from app.desktop.lifecycle import (
    BIND_HOST,
    DEFAULT_PORT,
    EXPECTED_APP_NAME,
    EXPECTED_PROTOCOL_VERSION,
    HEALTH_PATH,
    IDENTITY_PATH,
    BackendOwnership,
    DesktopStartupError,
    ForeignServiceError,
    ServerHandle,
    ServiceState,
    base_url,
    dashboard_url,
    ensure_dashboard_reachable,
    prepare_backend,
    probe_service,
    resolve_port,
    shutdown_backend,
    wait_until_ready,
)
from app.main import app

PORT = 8001
OK_HEALTH = (200, json.dumps({"status": "ok"}))
OK_IDENTITY = (200, json.dumps({"app": EXPECTED_APP_NAME, "desktop_protocol": EXPECTED_PROTOCOL_VERSION}))


def make_probe(responses: dict[str, object], *, record: list[str] | None = None):
    """A scripted probe: maps a full URL to a (status, body) tuple or None."""

    def probe(url: str, timeout: float):
        if record is not None:
            record.append(url)
        return responses.get(url)

    return probe


def health_url(port: int = PORT) -> str:
    return base_url(port) + HEALTH_PATH


def identity_url(port: int = PORT) -> str:
    return base_url(port) + IDENTITY_PATH


class FakeThread:
    def __init__(self, alive: bool = True) -> None:
        self._alive = alive
        self.join_calls: list[float] = []
        self.dies_on_join = True
        self.liveness_checks = 0

    def is_alive(self) -> bool:
        self.liveness_checks += 1
        return self._alive

    def join(self, timeout: float) -> None:
        self.join_calls.append(timeout)
        if self.dies_on_join:
            self._alive = False


class FakeServer:
    def __init__(self) -> None:
        self.should_exit = False


def make_handle(alive: bool = True) -> tuple[ServerHandle, FakeServer, FakeThread]:
    server, thread = FakeServer(), FakeThread(alive=alive)
    return ServerHandle(server=server, thread=thread), server, thread


# ---------------------------------------------------------------------------
# Port contract
# ---------------------------------------------------------------------------


def test_default_port_is_the_existing_dashboard_port():
    assert resolve_port({}) == DEFAULT_PORT == 8001


@pytest.mark.parametrize("raw, expected", [("9001", 9001), (" 9001 ", 9001), ("1", 1), ("65535", 65535)])
def test_valid_port_override_is_accepted(raw, expected):
    assert resolve_port({"CLOUD_LLM_DESKTOP_PORT": raw}) == expected


@pytest.mark.parametrize("raw", ["", "   "])
def test_blank_port_override_falls_back_to_default(raw):
    assert resolve_port({"CLOUD_LLM_DESKTOP_PORT": raw}) == DEFAULT_PORT


@pytest.mark.parametrize("raw", ["abc", "80.5", "0", "-1", "65536", "8001a"])
def test_invalid_port_override_fails_fast(raw):
    with pytest.raises(DesktopStartupError):
        resolve_port({"CLOUD_LLM_DESKTOP_PORT": raw})


def test_urls_are_always_loopback():
    assert BIND_HOST == "127.0.0.1"
    assert base_url(1234) == "http://127.0.0.1:1234"
    assert dashboard_url(8001) == "http://127.0.0.1:8001/"


# ---------------------------------------------------------------------------
# Identity / service classification
# ---------------------------------------------------------------------------


def test_nothing_listening_is_absent():
    assert probe_service(PORT, probe=make_probe({})) is ServiceState.ABSENT


def test_matching_health_and_identity_is_ours():
    probe = make_probe({health_url(): OK_HEALTH, identity_url(): OK_IDENTITY})
    assert probe_service(PORT, probe=probe) is ServiceState.OURS


@pytest.mark.parametrize(
    "identity_response",
    [
        None,
        (404, "not found"),
        (200, "not-json"),
        (200, json.dumps({"app": "Something-Else", "desktop_protocol": 1})),
        (200, json.dumps({"app": EXPECTED_APP_NAME, "desktop_protocol": 2})),
        (200, json.dumps({"app": EXPECTED_APP_NAME})),
        (200, json.dumps(["not", "an", "object"])),
    ],
)
def test_health_without_matching_identity_is_foreign(identity_response):
    probe = make_probe({health_url(): OK_HEALTH, identity_url(): identity_response})
    assert probe_service(PORT, probe=probe) is ServiceState.FOREIGN


@pytest.mark.parametrize(
    "health_response",
    [(200, json.dumps({"status": "degraded"})), (200, "not-json"), (500, "boom"), (200, json.dumps("ok"))],
)
def test_foreign_health_payload_is_foreign(health_response):
    probe = make_probe({health_url(): health_response, identity_url(): OK_IDENTITY})
    assert probe_service(PORT, probe=probe) is ServiceState.FOREIGN


# ---------------------------------------------------------------------------
# Backend ownership
# ---------------------------------------------------------------------------


def test_already_running_same_app_attaches_without_starting_a_backend():
    probe = make_probe({health_url(): OK_HEALTH, identity_url(): OK_IDENTITY})
    started: list[int] = []

    ownership, handle = prepare_backend(PORT, probe=probe, starter=lambda port: started.append(port))

    assert ownership is BackendOwnership.ATTACHED
    assert handle is None
    assert started == []


def test_foreign_occupant_refuses_to_start_or_attach():
    probe = make_probe({health_url(): (200, json.dumps({"status": "ok"})), identity_url(): (404, "nope")})
    started: list[int] = []

    with pytest.raises(ForeignServiceError):
        prepare_backend(PORT, probe=probe, starter=lambda port: started.append(port))

    assert started == []


def test_free_port_starts_an_owned_backend():
    handle, _, _ = make_handle()
    waited: list[int] = []

    ownership, returned = prepare_backend(
        PORT,
        probe=make_probe({}),
        starter=lambda port: handle,
        waiter=lambda port, **kwargs: waited.append(port),
    )

    assert ownership is BackendOwnership.OWNED
    assert returned is handle
    assert waited == [PORT]


def test_failed_readiness_stops_the_backend_it_started():
    handle, server, thread = make_handle()

    def failing_waiter(port, **kwargs):
        raise DesktopStartupError("timeout")

    with pytest.raises(DesktopStartupError):
        prepare_backend(PORT, probe=make_probe({}), starter=lambda port: handle, waiter=failing_waiter)

    assert server.should_exit is True
    assert thread.join_calls  # joined, so no thread is left behind


# ---------------------------------------------------------------------------
# Readiness
# ---------------------------------------------------------------------------


def test_readiness_returns_once_our_identity_answers():
    responses: dict[str, object] = {}
    calls = {"n": 0}

    def probe(url: str, timeout: float):
        calls["n"] += 1
        if calls["n"] > 2:  # backend finishes starting after the first poll
            responses[health_url()] = OK_HEALTH
            responses[identity_url()] = OK_IDENTITY
        return responses.get(url)

    slept: list[float] = []
    wait_until_ready(PORT, probe=probe, sleep=slept.append, monotonic=lambda: 0.0)

    assert slept  # it actually polled instead of returning immediately


def test_readiness_times_out_without_a_window():
    clock = iter([0.0, 5.0, 10.0, 15.0, 20.0, 25.0])

    with pytest.raises(DesktopStartupError):
        wait_until_ready(
            PORT,
            probe=make_probe({}),
            timeout=20.0,
            sleep=lambda _seconds: None,
            monotonic=lambda: next(clock),
        )


def test_readiness_fails_immediately_when_the_backend_thread_dies():
    with pytest.raises(DesktopStartupError):
        wait_until_ready(
            PORT,
            probe=make_probe({}),
            sleep=lambda _seconds: None,
            monotonic=lambda: 0.0,
            is_alive=lambda: False,
        )


def test_readiness_fails_closed_when_a_foreign_service_answers():
    probe = make_probe({health_url(): OK_HEALTH, identity_url(): (200, json.dumps({"app": "Other"}))})

    with pytest.raises(ForeignServiceError):
        wait_until_ready(PORT, probe=probe, sleep=lambda _s: None, monotonic=lambda: 0.0)


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------


def test_owned_backend_is_stopped_and_joined():
    handle, server, thread = make_handle()

    assert shutdown_backend(BackendOwnership.OWNED, handle) is True
    assert server.should_exit is True
    assert thread.join_calls


def test_attached_backend_is_never_stopped():
    handle, server, thread = make_handle()

    assert shutdown_backend(BackendOwnership.ATTACHED, handle) is True
    assert server.should_exit is False
    assert thread.join_calls == []


def test_shutdown_reports_a_backend_that_refuses_to_stop():
    handle, _, thread = make_handle()
    thread.dies_on_join = False

    assert shutdown_backend(BackendOwnership.OWNED, handle) is False


def test_shutdown_without_a_handle_is_a_no_op():
    assert shutdown_backend(BackendOwnership.OWNED, None) is True


# ---------------------------------------------------------------------------
# Paths: app data, database precedence, dotenv candidates
# ---------------------------------------------------------------------------


def test_app_data_dir_uses_localappdata_when_present(tmp_path):
    assert paths.app_data_dir({"LOCALAPPDATA": str(tmp_path)}) == tmp_path / "Cloud-LLM-LimitChecker"


def test_app_data_dir_falls_back_to_xdg(tmp_path):
    assert paths.app_data_dir({"XDG_DATA_HOME": str(tmp_path)}) == tmp_path / "Cloud-LLM-LimitChecker"


def test_default_database_url_points_at_app_data(tmp_path):
    url = paths.default_database_url({"LOCALAPPDATA": str(tmp_path)})
    assert url.startswith("sqlite:///")
    assert url.endswith("Cloud-LLM-LimitChecker/limit_checker.db")


def test_existing_database_url_is_preserved(tmp_path):
    env = {"APP_DB_URL": "sqlite:///./limit_checker.db", "LOCALAPPDATA": str(tmp_path)}
    assert paths.resolve_database_url(env, frozen=True) is None


def test_source_run_keeps_the_historical_database_default(tmp_path):
    assert paths.resolve_database_url({"LOCALAPPDATA": str(tmp_path)}, frozen=False) is None


def test_packaged_run_defaults_to_app_data_database(tmp_path):
    resolved = paths.resolve_database_url({"LOCALAPPDATA": str(tmp_path)}, frozen=True)
    assert resolved == paths.default_database_url({"LOCALAPPDATA": str(tmp_path)})


def test_packaged_dotenv_candidates_start_next_to_the_executable(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "executable", str(tmp_path / "app.exe"), raising=False)
    candidates = paths.dotenv_candidates({"LOCALAPPDATA": str(tmp_path)}, frozen=True)
    assert candidates[0] == tmp_path / ".env"
    assert candidates[1] == tmp_path / "Cloud-LLM-LimitChecker" / ".env"


def test_source_dotenv_candidates_start_at_the_repository_root(tmp_path):
    candidates = paths.dotenv_candidates({"LOCALAPPDATA": str(tmp_path)}, frozen=False)
    assert candidates[0] == paths.resource_dir() / ".env"


def test_configure_environment_never_overrides_the_process_environment(tmp_path):
    env = {"LOCALAPPDATA": str(tmp_path), "APP_DB_URL": "sqlite:///./existing.db"}
    # Written into a real candidate location, so the loader is genuinely called.
    app_data = paths.app_data_dir(env)
    app_data.mkdir(parents=True)
    (app_data / ".env").write_text("EXAMPLE=1\n", encoding="utf-8")
    seen: list[dict] = []

    summary = paths.configure_environment(
        env,
        frozen=True,
        dotenv_loader=lambda dotenv_path, override: seen.append({"path": dotenv_path, "override": override}),
        chdir=lambda _path: None,
    )

    assert [entry["path"] for entry in seen] == [str(app_data / ".env")]
    assert all(entry["override"] is False for entry in seen)
    assert env["APP_DB_URL"] == "sqlite:///./existing.db"
    assert summary["database_url_source"] == "preexisting"
    assert summary["dotenv_files_loaded"] == [str(app_data / ".env")]


def test_configure_environment_sets_app_data_database_only_when_packaged(tmp_path):
    env = {"LOCALAPPDATA": str(tmp_path)}

    summary = paths.configure_environment(
        env, frozen=True, dotenv_loader=lambda **kwargs: None, chdir=lambda _path: None
    )

    assert env["APP_DB_URL"] == paths.default_database_url({"LOCALAPPDATA": str(tmp_path)})
    assert summary["database_url_source"] == "app_data_default"


def test_configure_environment_summary_carries_no_values(tmp_path):
    env = {"LOCALAPPDATA": str(tmp_path)}
    app_data = paths.app_data_dir(env)
    app_data.mkdir(parents=True)
    (app_data / ".env").write_text("SECRET_TOKEN=super-secret-value\n", encoding="utf-8")

    summary = paths.configure_environment(
        env, frozen=True, dotenv_loader=lambda **kwargs: None, chdir=lambda _path: None
    )

    assert "super-secret-value" not in json.dumps(summary)


def test_packaged_run_pins_the_working_directory_to_the_bundle_root(tmp_path):
    # `config/seed.yaml` is read as a relative path and a missing file is
    # silently treated as "nothing to seed", so a packaged launch must not
    # depend on wherever the shortcut was invoked from.
    changed: list[Path] = []

    summary = paths.configure_environment(
        {"LOCALAPPDATA": str(tmp_path)},
        frozen=True,
        dotenv_loader=lambda **kwargs: None,
        chdir=changed.append,
    )

    assert changed == [paths.resource_dir()]
    assert summary["working_directory_pinned"] == str(paths.resource_dir())


def test_source_run_never_changes_the_working_directory(tmp_path):
    changed: list[Path] = []

    summary = paths.configure_environment(
        {"LOCALAPPDATA": str(tmp_path)},
        frozen=False,
        dotenv_loader=lambda **kwargs: None,
        chdir=changed.append,
    )

    assert changed == []
    assert summary["working_directory_pinned"] is None


def test_seed_config_is_reachable_from_the_pinned_working_directory():
    assert (paths.resource_dir() / "config" / "seed.yaml").is_file()


# ---------------------------------------------------------------------------
# Static resources and endpoint regression
# ---------------------------------------------------------------------------


def test_static_dir_is_resolved_from_the_module_not_the_working_directory():
    assert app_main.STATIC_DIR.is_absolute()
    assert app_main.STATIC_DIR.name == "static"
    assert (app_main.STATIC_DIR / "index.html").is_file()
    assert (app_main.STATIC_DIR / "compact.html").is_file()


def test_static_dir_matches_the_packaged_resource_layout():
    # The desktop package and the web app must derive the same directory
    # independently; a packaged build preserves this relative layout, which
    # itself is checked by the manual packaged smoke, not by CI.
    assert app_main.STATIC_DIR == paths.resource_dir() / "static"


def test_static_dir_does_not_move_with_the_working_directory(tmp_path, monkeypatch):
    # The regression this guards: `StaticFiles(directory="static")` resolved
    # against the process CWD, which a packaged launch does not control.
    monkeypatch.chdir(tmp_path)
    assert (app_main.STATIC_DIR / "index.html").is_file()
    assert app_main.STATIC_DIR.is_absolute()


def test_static_serving_uses_one_directory_for_both_surfaces():
    source = Path(app_main.__file__).read_text(encoding="utf-8")
    assert 'FileResponse(STATIC_DIR / "compact.html")' in source
    assert "StaticFiles(directory=STATIC_DIR" in source

    # No packaged-only branch is allowed to creep into the web app. Parsed
    # rather than grepped, so a comment mentioning _MEIPASS is not mistaken
    # for a code path that reads it.
    tree = ast.parse(source)
    meipass_reads = [
        node
        for node in ast.walk(tree)
        if (isinstance(node, ast.Attribute) and node.attr == "_MEIPASS")
        or (isinstance(node, ast.Constant) and node.value == "_MEIPASS")  # getattr(sys, "_MEIPASS")
    ]
    assert meipass_reads == []


def test_dashboard_and_compact_pages_still_serve(monkeypatch):
    monkeypatch.setenv("ENABLE_BASIC_AUTH", "false")
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        assert client.get("/compact").status_code == 200
        assert client.get("/api/health").json() == {"status": "ok"}


def test_identity_endpoint_returns_the_exact_contract(monkeypatch):
    monkeypatch.setenv("ENABLE_BASIC_AUTH", "false")
    with TestClient(app) as client:
        response = client.get("/api/desktop/identity")

    assert response.status_code == 200
    assert response.json() == {"app": "Cloud-LLM-LimitChecker", "desktop_protocol": 1}


def test_identity_endpoint_is_usable_for_preflight_under_basic_auth(monkeypatch):
    monkeypatch.setenv("ENABLE_BASIC_AUTH", "true")
    monkeypatch.setenv("BASIC_AUTH_USERNAME", "user")
    monkeypatch.setenv("BASIC_AUTH_PASSWORD", "pass")

    with TestClient(app) as client:
        assert client.get("/api/desktop/identity").status_code == 200
        assert client.get("/api/health").status_code == 200
        # everything else still requires credentials
        assert client.get("/api/limits").status_code == 401


def test_identity_endpoint_exposes_nothing_beyond_the_contract(monkeypatch):
    monkeypatch.setenv("ENABLE_BASIC_AUTH", "false")
    with TestClient(app) as client:
        payload = client.get("/api/desktop/identity").json()

    assert set(payload) == {"app", "desktop_protocol"}


def test_entry_point_module_imports_without_a_gui_library():
    # Importing the launcher must not require pywebview to be installed.
    module = __import__("app.desktop.__main__", fromlist=["main"])
    assert callable(module.main)
    assert "webview" not in sys.modules


# ---------------------------------------------------------------------------
# Real probe classification (what urllib actually raises)
# ---------------------------------------------------------------------------


def test_http_error_response_is_a_response_not_an_absent_service(monkeypatch):
    import urllib.error

    def raise_http_error(url, timeout):
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

    monkeypatch.setattr(lifecycle.urllib.request, "urlopen", raise_http_error)
    # A 404 is a foreign service answering, never "nothing is listening".
    assert lifecycle._http_probe("http://127.0.0.1:8001/api/health", 1.0) == (404, "")
    assert probe_service(PORT, probe=lifecycle._http_probe) is ServiceState.FOREIGN


def test_non_http_speaking_occupant_is_foreign_not_absent(monkeypatch):
    import http.client

    def raise_bad_status(url, timeout):
        raise http.client.BadStatusLine("garbage")

    monkeypatch.setattr(lifecycle.urllib.request, "urlopen", raise_bad_status)
    # The connection succeeded, so the port is taken: treating it as free
    # would make us try to bind it and fail with a misleading reason.
    assert lifecycle._http_probe("http://127.0.0.1:8001/api/health", 1.0) == (0, "")
    assert probe_service(PORT, probe=lifecycle._http_probe) is ServiceState.FOREIGN


def test_refused_connection_is_absent(monkeypatch):
    def refuse(url, timeout):
        raise ConnectionRefusedError(61, "connection refused")

    monkeypatch.setattr(lifecycle.urllib.request, "urlopen", refuse)
    assert lifecycle._http_probe("http://127.0.0.1:8001/api/health", 1.0) is None
    assert probe_service(PORT, probe=lifecycle._http_probe) is ServiceState.ABSENT


@pytest.mark.parametrize("raw", ["+8001", "8_001", "８００１", " 80 01 "])
def test_port_override_rejects_non_plain_integers(raw):
    with pytest.raises(DesktopStartupError):
        resolve_port({"CLOUD_LLM_DESKTOP_PORT": raw})


def test_a_backend_that_will_not_stop_is_reported_not_swallowed():
    handle, _, thread = make_handle()
    thread.dies_on_join = False

    def failing_waiter(port, **kwargs):
        raise DesktopStartupError("did not become ready")

    with pytest.raises(DesktopStartupError, match="did not stop cleanly"):
        prepare_backend(PORT, probe=make_probe({}), starter=lambda port: handle, waiter=failing_waiter)


# ---------------------------------------------------------------------------
# App-data directory: created for the packaged default database only
# ---------------------------------------------------------------------------


def test_packaged_default_database_directory_is_created_without_opening_a_log(tmp_path):
    # SQLite will not create a missing parent, and nothing here opens the log
    # file -- the directory must exist purely because the environment
    # preparation created it.
    env = {"LOCALAPPDATA": str(tmp_path / "missing")}
    app_data = paths.app_data_dir(env)
    assert not app_data.exists()

    paths.configure_environment(
        env, frozen=True, dotenv_loader=lambda **kwargs: None, chdir=lambda _path: None
    )

    assert app_data.is_dir()
    assert env["APP_DB_URL"] == paths.default_database_url(env)
    assert not (app_data / paths.LOG_FILE_NAME).exists()


def test_sqlite_can_open_the_prepared_packaged_database(tmp_path):
    import sqlite3

    env = {"LOCALAPPDATA": str(tmp_path / "missing")}
    paths.configure_environment(
        env, frozen=True, dotenv_loader=lambda **kwargs: None, chdir=lambda _path: None
    )

    # The whole point of the directory: this is what app.database does next.
    database_file = paths.app_data_dir(env) / paths.DATABASE_FILE_NAME
    sqlite3.connect(database_file).close()

    assert database_file.exists()


def test_preset_database_url_creates_no_directory(tmp_path):
    env = {"LOCALAPPDATA": str(tmp_path / "missing"), "APP_DB_URL": "sqlite:///./existing.db"}

    paths.configure_environment(
        env, frozen=True, dotenv_loader=lambda **kwargs: None, chdir=lambda _path: None
    )

    assert env["APP_DB_URL"] == "sqlite:///./existing.db"
    assert not paths.app_data_dir(env).exists()


def test_source_run_creates_no_directory_and_sets_no_database_url(tmp_path):
    env = {"LOCALAPPDATA": str(tmp_path / "missing")}
    made: list[Path] = []

    summary = paths.configure_environment(
        env,
        frozen=False,
        dotenv_loader=lambda **kwargs: None,
        chdir=lambda _path: None,
        mkdir=made.append,
    )

    assert made == []
    assert "APP_DB_URL" not in env
    assert summary["database_url_source"] == "preexisting"
    assert not paths.app_data_dir(env).exists()


def test_unpreparable_app_data_directory_fails_fast_without_leaking_paths(tmp_path):
    env = {"LOCALAPPDATA": str(tmp_path / "missing")}

    def refuse(path):
        raise OSError(13, f"Access is denied: {path}")

    with pytest.raises(DesktopStartupError) as failure:
        paths.configure_environment(
            env,
            frozen=True,
            dotenv_loader=lambda **kwargs: None,
            chdir=lambda _path: None,
            mkdir=refuse,
        )

    message = str(failure.value)
    assert message == paths.APP_DATA_DIR_ERROR
    assert str(tmp_path) not in message
    assert "Access is denied" not in message
    # Fails before the URL is published, so nothing downstream sees a path
    # it cannot use.
    assert "APP_DB_URL" not in env


def test_app_data_failure_reason_is_logged_while_the_dialog_stays_generic(tmp_path, capsys):
    # The dialog must not carry the OS message, but the reason has to survive
    # somewhere or the user is told "it failed" and nothing more.
    def refuse(path):
        raise OSError(13, "Access is denied")

    with pytest.raises(DesktopStartupError) as failure:
        paths.ensure_app_data_dir({"LOCALAPPDATA": str(tmp_path)}, mkdir=refuse)

    assert str(failure.value) == paths.APP_DATA_DIR_ERROR
    logged = capsys.readouterr().err
    assert "errno 13" in logged
    assert "Access is denied" in logged


def test_dialog_points_at_no_log_when_none_is_being_written(tmp_path):
    shown: list[str] = []

    desktop_main.report_fatal_error(
        paths.APP_DATA_DIR_ERROR,
        log_path=None,
        message_box=lambda handle, text, title, flags: shown.append(text),
    )

    assert shown == [paths.APP_DATA_DIR_ERROR]
    assert "Details" not in shown[0]
    assert str(tmp_path) not in shown[0]


def test_null_sink_is_never_advertised_as_a_log_file(tmp_path, monkeypatch):
    # %LOCALAPPDATA% unwritable: the streams still work, but there is no file
    # to send anyone to.
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)

    used = desktop_main.ensure_std_streams(
        log_path=tmp_path / "desktop.log",
        opener=lambda path: open(os.devnull, "w", encoding="utf-8"),
    )

    assert used is None
    assert sys.stdout is not None
    sys.stdout.close()


def test_main_reports_environment_preparation_failure_before_starting_a_backend(monkeypatch):
    started: list[int] = []
    reported: list[str] = []

    def refuse():
        raise DesktopStartupError(paths.APP_DATA_DIR_ERROR)

    monkeypatch.setattr(desktop_main.paths, "configure_environment", refuse)
    monkeypatch.setattr(desktop_main, "prepare_backend", lambda port: started.append(port))
    monkeypatch.setattr(desktop_main, "report_fatal_error", lambda message, **kwargs: reported.append(message))

    assert desktop_main.main([]) == 1
    assert started == []
    assert reported == [paths.APP_DATA_DIR_ERROR]


# ---------------------------------------------------------------------------
# The dashboard must be usable before a window appears
# ---------------------------------------------------------------------------


def test_authenticated_dashboard_is_refused_rather_than_shown_as_401():
    # Basic auth exempts health and identity, so readiness passes while the
    # dashboard itself returns 401 -- and a native window cannot log in.
    probe = make_probe({dashboard_url(PORT): (401, "unauthorized")})

    with pytest.raises(DesktopStartupError, match="basic authentication"):
        ensure_dashboard_reachable(PORT, probe=probe)


def test_reachable_dashboard_passes():
    probe = make_probe({dashboard_url(PORT): (200, "<html></html>")})

    ensure_dashboard_reachable(PORT, probe=probe)


@pytest.mark.parametrize("response", [None, (500, "boom"), (404, "missing")])
def test_unusable_dashboard_is_refused(response):
    probe = make_probe({dashboard_url(PORT): response})

    with pytest.raises(DesktopStartupError):
        ensure_dashboard_reachable(PORT, probe=probe)


# ---------------------------------------------------------------------------
# Packaged-mode streams and error reporting
# ---------------------------------------------------------------------------


def test_absent_std_streams_are_replaced_before_the_backend_starts(tmp_path, monkeypatch):
    # uvicorn builds its formatter with sys.stdout.isatty(); a windowed
    # PyInstaller build has None there and the backend cannot start at all.
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    log_file = tmp_path / "desktop.log"

    used = desktop_main.ensure_std_streams(log_path=log_file)

    assert used == log_file
    assert sys.stdout is not None and sys.stderr is not None
    sys.stdout.write("probe\n")
    sys.stdout.flush()
    assert "probe" in log_file.read_text(encoding="utf-8")
    sys.stdout.close()


def test_oversized_log_is_truncated_rather_than_grown_forever(tmp_path):
    log_file = tmp_path / "desktop.log"
    log_file.write_text("x" * (desktop_main.MAX_LOG_BYTES + 1), encoding="utf-8")

    stream = desktop_main._open_log_stream(log_file)
    stream.write("fresh\n")
    stream.close()

    assert log_file.read_text(encoding="utf-8") == "fresh\n"


def test_small_log_is_appended_to(tmp_path):
    log_file = tmp_path / "desktop.log"
    log_file.write_text("earlier\n", encoding="utf-8")

    stream = desktop_main._open_log_stream(log_file)
    stream.write("later\n")
    stream.close()

    assert log_file.read_text(encoding="utf-8") == "earlier\nlater\n"


def test_existing_std_streams_are_left_alone(tmp_path):
    before_out, before_err = sys.stdout, sys.stderr

    assert desktop_main.ensure_std_streams(log_path=tmp_path / "unused.log") is None

    assert sys.stdout is before_out and sys.stderr is before_err
    assert not (tmp_path / "unused.log").exists()


def test_unwritable_log_falls_back_instead_of_failing_startup(tmp_path, monkeypatch):
    # This runs before main()'s try block, so it must never raise: a startup
    # that cannot write a log still has to start.
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)

    def refuse(path):
        raise OSError("read-only")

    used = desktop_main.ensure_std_streams(log_path=tmp_path / "x.log", opener=refuse)

    assert used is None
    assert sys.stdout is not None and sys.stderr is not None
    sys.stdout.write("discarded")
    sys.stdout.close()


def test_real_opener_degrades_to_a_null_sink(tmp_path, monkeypatch):
    def explode(*args, **kwargs):
        raise OSError("no such volume")

    monkeypatch.setattr(Path, "mkdir", explode)

    stream = desktop_main._open_log_stream(tmp_path / "nope" / "desktop.log")

    assert stream.name == os.devnull
    stream.close()


def test_fatal_error_is_reported_on_screen_without_leaking_values(tmp_path, monkeypatch):
    monkeypatch.setenv("BASIC_AUTH_PASSWORD", "super-secret-value")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
    log_file = tmp_path / "desktop.log"
    shown: list[tuple] = []

    desktop_main.report_fatal_error(
        "port 8001 is in use by another service",
        log_path=log_file,
        message_box=lambda handle, text, title, flags: shown.append((text, title)),
    )

    (text, title) = shown[0]
    # Exactly the adapter's own message plus where to look -- nothing else.
    assert text == f"port 8001 is in use by another service\n\nDetails: {log_file}"
    assert title == desktop_main.WINDOW_TITLE
    assert "super-secret-value" not in text
    assert "sk-secret" not in text


# ---------------------------------------------------------------------------
# Window and entry point, against a fake webview
# ---------------------------------------------------------------------------


class FakeWindow:
    def __init__(self) -> None:
        self.destroyed = 0

    def destroy(self) -> None:
        self.destroyed += 1


class FakeWebview:
    def __init__(self, *, on_start=None) -> None:
        self.window = FakeWindow()
        self.created: list[tuple] = []
        self.started_with: list[dict] = []
        self._on_start = on_start

    def create_window(self, title, url, **kwargs):
        self.created.append((title, url, kwargs))
        return self.window

    def start(self, **kwargs):
        self.started_with.append(kwargs)
        if self._on_start is not None:
            self._on_start()


def test_window_is_created_with_devtools_off_and_the_given_url():
    fake = FakeWebview()

    desktop_main.open_window("http://127.0.0.1:8001/", handle=None, webview_module=fake)

    (title, url, kwargs) = fake.created[0]
    assert title == desktop_main.WINDOW_TITLE
    assert url == "http://127.0.0.1:8001/"
    assert kwargs["width"] and kwargs["height"]
    assert fake.started_with == [{"debug": False}]


def test_window_closes_itself_when_the_backend_dies_while_open(monkeypatch):
    monkeypatch.setattr(desktop_main, "BACKEND_WATCHDOG_INTERVAL_SECONDS", 0.01)
    handle, _, thread = make_handle()

    def die_then_wait():
        thread._alive = False
        for _ in range(200):
            if fake.window.destroyed:
                return
            time.sleep(0.01)

    fake = FakeWebview(on_start=die_then_wait)
    desktop_main.open_window("http://127.0.0.1:8001/", handle=handle, webview_module=fake)

    assert fake.window.destroyed == 1


def test_watchdog_stops_with_the_window_and_never_touches_it_afterwards(monkeypatch):
    # main() stops an OWNED backend right after the window closes, so the
    # watchdog must already be gone by then: a surviving one would call
    # destroy() on a window that no longer exists.
    monkeypatch.setattr(desktop_main, "BACKEND_WATCHDOG_INTERVAL_SECONDS", 0.01)
    handle, _, thread = make_handle()
    fake = FakeWebview()

    desktop_main.open_window("http://127.0.0.1:8001/", handle=handle, webview_module=fake)
    checks_at_close = thread.liveness_checks
    thread._alive = False  # the backend goes away after the window is gone
    time.sleep(0.1)  # several watchdog intervals

    assert fake.window.destroyed == 0
    assert thread.liveness_checks == checks_at_close  # the watchdog is no longer polling
    assert not any(
        candidate.name == "cloud-llm-backend-watchdog" and candidate.is_alive()
        for candidate in threading.enumerate()
    )


def test_main_returns_zero_and_stops_only_what_it_owns(monkeypatch):
    handle, _, _ = make_handle()
    stopped: list[tuple] = []
    opened: list[str] = []
    monkeypatch.setattr(desktop_main.paths, "configure_environment", lambda: {})
    monkeypatch.setattr(desktop_main, "resolve_port", lambda env: 8001)
    monkeypatch.setattr(desktop_main, "prepare_backend", lambda port: (BackendOwnership.OWNED, handle))
    monkeypatch.setattr(desktop_main, "ensure_dashboard_reachable", lambda port: None)
    monkeypatch.setattr(desktop_main, "open_window", lambda url, handle: opened.append(url))
    monkeypatch.setattr(
        desktop_main, "shutdown_backend", lambda ownership, h: stopped.append((ownership, h)) or True
    )

    assert desktop_main.main([]) == 0
    assert stopped == [(BackendOwnership.OWNED, handle)]
    # The window is only ever pointed at the verified loopback dashboard.
    assert opened == ["http://127.0.0.1:8001/"]


def test_main_refuses_to_open_a_window_the_user_cannot_use(monkeypatch):
    handle, _, _ = make_handle()
    reported: list[str] = []
    opened: list[str] = []
    monkeypatch.setattr(desktop_main.paths, "configure_environment", lambda: {})
    monkeypatch.setattr(desktop_main, "resolve_port", lambda env: 8001)
    monkeypatch.setattr(desktop_main, "prepare_backend", lambda port: (BackendOwnership.OWNED, handle))
    monkeypatch.setattr(desktop_main, "open_window", lambda url, handle: opened.append(url))
    monkeypatch.setattr(desktop_main, "shutdown_backend", lambda ownership, h: True)
    monkeypatch.setattr(desktop_main, "report_fatal_error", lambda message, **kwargs: reported.append(message))

    def unauthorized(port):
        raise DesktopStartupError("the dashboard requires basic authentication")

    monkeypatch.setattr(desktop_main, "ensure_dashboard_reachable", unauthorized)

    assert desktop_main.main([]) == 1
    assert opened == []
    assert "basic authentication" in reported[0]


def test_main_reports_a_startup_failure_and_exits_nonzero(monkeypatch):
    reported: list[str] = []
    monkeypatch.setattr(desktop_main.paths, "configure_environment", lambda: {})
    monkeypatch.setattr(desktop_main, "resolve_port", lambda env: 8001)

    def refuse(port):
        raise ForeignServiceError("port 8001 is in use by another service")

    monkeypatch.setattr(desktop_main, "prepare_backend", refuse)
    monkeypatch.setattr(desktop_main, "report_fatal_error", lambda message, **kwargs: reported.append(message))

    assert desktop_main.main([]) == 1
    assert reported == ["port 8001 is in use by another service"]


def test_main_never_lets_an_unexpected_error_vanish(monkeypatch):
    reported: list[str] = []
    monkeypatch.setattr(desktop_main.paths, "configure_environment", lambda: {})
    monkeypatch.setattr(desktop_main, "resolve_port", lambda env: 8001)
    monkeypatch.setattr(desktop_main, "prepare_backend", lambda port: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(desktop_main, "report_fatal_error", lambda message, **kwargs: reported.append(message))

    assert desktop_main.main([]) == 1
    assert reported and "unexpected" in reported[0]


def test_main_does_not_stop_an_attached_backend(monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(desktop_main.paths, "configure_environment", lambda: {})
    monkeypatch.setattr(desktop_main, "resolve_port", lambda env: 8001)
    monkeypatch.setattr(desktop_main, "prepare_backend", lambda port: (BackendOwnership.ATTACHED, None))
    monkeypatch.setattr(desktop_main, "ensure_dashboard_reachable", lambda port: None)
    monkeypatch.setattr(desktop_main, "open_window", lambda url, handle: None)
    monkeypatch.setattr(desktop_main, "shutdown_backend", lambda ownership, h: calls.append(ownership) or True)

    assert desktop_main.main([]) == 0
    assert calls == [BackendOwnership.ATTACHED]
