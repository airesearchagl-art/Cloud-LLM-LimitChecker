"""Backend lifecycle for the desktop shell: port, identity, readiness, shutdown.

Deliberately free of any GUI import. Everything here is a pure function or
takes its collaborators as arguments, so the whole contract -- including
timeout, crash and foreign-service paths -- is testable in CI without ever
opening a window or binding a real port.

Two ownership modes, and the difference matters at close time:

- OWNED: this process started the backend, so closing the window stops it.
- ATTACHED: a matching instance was already running (the browser launcher,
  or a second desktop window), so closing this window leaves it running.

A port that answers but does not present this app's identity is a foreign
service: the shell refuses it rather than showing whatever is there.
"""

from __future__ import annotations

import http.client
import json
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum

BIND_HOST = "127.0.0.1"
DEFAULT_PORT = 8001
PORT_ENV_VAR = "CLOUD_LLM_DESKTOP_PORT"

HEALTH_PATH = "/api/health"
IDENTITY_PATH = "/api/desktop/identity"
EXPECTED_APP_NAME = "Cloud-LLM-LimitChecker"
EXPECTED_PROTOCOL_VERSION = 1

STARTUP_TIMEOUT_SECONDS = 20.0
PROBE_TIMEOUT_SECONDS = 2.0
POLL_INTERVAL_SECONDS = 0.25
SHUTDOWN_JOIN_TIMEOUT_SECONDS = 10.0

#: (status_code, body) on a completed HTTP response, None when unreachable.
ProbeResult = tuple[int, str] | None
Probe = Callable[[str, float], ProbeResult]


class DesktopStartupError(RuntimeError):
    """The shell cannot reach a usable backend. Message is always generic."""


class ForeignServiceError(DesktopStartupError):
    """The port is taken by something that is not this application."""


class ServiceState(str, Enum):
    ABSENT = "absent"
    OURS = "ours"
    FOREIGN = "foreign"


class BackendOwnership(str, Enum):
    OWNED = "owned"
    ATTACHED = "attached"


def resolve_port(env: Mapping[str, str]) -> int:
    """Desktop-only port override. Invalid values fail fast rather than drift."""
    raw = env.get(PORT_ENV_VAR)
    if raw is None or not raw.strip():
        return DEFAULT_PORT
    text = raw.strip()
    # Plain ASCII digits only: `int()` would also accept "+8001", "8_001" and
    # full-width digits, which are far more likely to be a typo than intent.
    if not (text.isascii() and text.isdigit()):
        raise DesktopStartupError(f"{PORT_ENV_VAR} must be an integer between 1 and 65535")
    port = int(text)
    if not 1 <= port <= 65535:
        raise DesktopStartupError(f"{PORT_ENV_VAR} must be an integer between 1 and 65535")
    return port


def base_url(port: int) -> str:
    return f"http://{BIND_HOST}:{port}"


def dashboard_url(port: int) -> str:
    """The only URL the window is ever pointed at."""
    return f"{base_url(port)}/"


def _http_probe(url: str, timeout: float) -> ProbeResult:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 - fixed localhost URL
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        # A 4xx/5xx is still a real HTTP response, and a service answering
        # this port with one is emphatically not us. Returning it (rather
        # than None) is what keeps that case classified as FOREIGN instead
        # of "nothing is listening".
        try:
            body = error.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        return error.code, body
    except http.client.HTTPException:
        # The connection was accepted but the peer does not speak HTTP. That
        # is a foreign occupant, not a free port: reporting it as "nothing is
        # listening" would send us on to bind a port that is already taken.
        return 0, ""
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _json_payload(result: ProbeResult) -> dict | None:
    if result is None:
        return None
    status, body = result
    if status != 200:
        return None
    try:
        payload = json.loads(body)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def probe_service(port: int, *, probe: Probe = _http_probe, timeout: float = PROBE_TIMEOUT_SECONDS) -> ServiceState:
    """Classify whatever is listening on `port`.

    Health alone is not enough: any local service could answer it, and one
    that does not is not necessarily hostile -- it is simply not ours. Both
    checks must pass with the exact expected identity to return OURS.
    """
    raw_health = probe(base_url(port) + HEALTH_PATH, timeout)
    if raw_health is None:
        # Nothing is listening: the port is free for us to take.
        return ServiceState.ABSENT
    health = _json_payload(raw_health)
    if health is None or health.get("status") != "ok":
        # Something answered, but not with this app's health contract.
        return ServiceState.FOREIGN

    identity = _json_payload(probe(base_url(port) + IDENTITY_PATH, timeout))
    if identity is None:
        return ServiceState.FOREIGN
    if identity.get("app") != EXPECTED_APP_NAME:
        return ServiceState.FOREIGN
    if identity.get("desktop_protocol") != EXPECTED_PROTOCOL_VERSION:
        return ServiceState.FOREIGN
    return ServiceState.OURS


@dataclass(slots=True)
class ServerHandle:
    """A uvicorn server running on a background thread of this same process.

    No subprocess: there is no child to outlive us, so the classic orphaned
    backend cannot happen. Stopping is cooperative (`should_exit`), which
    lets the existing FastAPI lifespan run its scheduler and diagnostics
    shutdown exactly as it does under Ctrl+C.
    """

    server: object
    thread: threading.Thread

    def is_alive(self) -> bool:
        return self.thread.is_alive()

    def request_stop(self) -> None:
        self.server.should_exit = True  # type: ignore[attr-defined]

    def join(self, timeout: float = SHUTDOWN_JOIN_TIMEOUT_SECONDS) -> bool:
        self.thread.join(timeout)
        return not self.thread.is_alive()


def start_backend(port: int) -> ServerHandle:
    """Start uvicorn on 127.0.0.1 in a daemon thread. Never binds elsewhere."""
    import uvicorn  # imported lazily so this module stays import-cheap

    from app.main import app

    config = uvicorn.Config(app, host=BIND_HOST, port=port, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="cloud-llm-backend", daemon=True)
    thread.start()
    return ServerHandle(server=server, thread=thread)


def wait_until_ready(
    port: int,
    *,
    probe: Probe = _http_probe,
    timeout: float = STARTUP_TIMEOUT_SECONDS,
    poll_interval: float = POLL_INTERVAL_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    is_alive: Callable[[], bool] | None = None,
) -> None:
    """Block until our own backend answers, or fail closed.

    Three distinct failures, none of which end with a window on screen: the
    backend thread died, the deadline passed, or something answered that is
    not this application.
    """
    deadline = monotonic() + timeout
    while True:
        if is_alive is not None and not is_alive():
            raise DesktopStartupError("the backend stopped before it became ready")
        state = probe_service(port, probe=probe)
        if state is ServiceState.OURS:
            return
        if state is ServiceState.FOREIGN:
            raise ForeignServiceError(f"port {port} is in use by another service")
        if monotonic() >= deadline:
            raise DesktopStartupError(f"the backend did not become ready within {timeout:.0f}s")
        sleep(poll_interval)


def prepare_backend(
    port: int,
    *,
    probe: Probe = _http_probe,
    starter: Callable[[int], ServerHandle] = start_backend,
    waiter: Callable[..., None] = wait_until_ready,
) -> tuple[BackendOwnership, ServerHandle | None]:
    """Attach to a matching instance, or start our own. Never touch a stranger."""
    state = probe_service(port, probe=probe)
    if state is ServiceState.OURS:
        return BackendOwnership.ATTACHED, None
    if state is ServiceState.FOREIGN:
        raise ForeignServiceError(f"port {port} is in use by another service")

    handle = starter(port)
    try:
        waiter(port, probe=probe, is_alive=handle.is_alive)
    except DesktopStartupError as error:
        handle.request_stop()
        if not handle.join():
            # Losing this would leave a half-started backend running with
            # nothing on screen to explain it.
            raise DesktopStartupError(f"{error}; the backend also did not stop cleanly") from error
        raise
    return BackendOwnership.OWNED, handle


def ensure_dashboard_reachable(
    port: int,
    *,
    probe: Probe = _http_probe,
    timeout: float = PROBE_TIMEOUT_SECONDS,
) -> None:
    """Refuse to open a window on a page the user cannot actually use.

    Readiness only proves the health and identity endpoints answer, and both
    are exempt from basic auth. With `ENABLE_BASIC_AUTH=true` the dashboard
    itself still returns 401, and a native window has nowhere to type
    credentials -- so fail closed with an explanation instead of showing an
    error page in a chromeless window.
    """
    result = probe(dashboard_url(port), timeout)
    if result is None:
        raise DesktopStartupError("the dashboard did not respond")
    status, _ = result
    if status == 401:
        raise DesktopStartupError(
            "the dashboard requires basic authentication, which the desktop window cannot supply; "
            "open it in a browser or disable ENABLE_BASIC_AUTH"
        )
    if status >= 400:
        raise DesktopStartupError(f"the dashboard responded with HTTP {status}")


def shutdown_backend(
    ownership: BackendOwnership,
    handle: ServerHandle | None,
    *,
    timeout: float = SHUTDOWN_JOIN_TIMEOUT_SECONDS,
) -> bool:
    """Stop the backend only if we started it. Returns True once it is gone.

    An ATTACHED backend belongs to whoever launched it -- closing this
    window must not take down the browser dashboard someone else is using.
    """
    if ownership is BackendOwnership.ATTACHED or handle is None:
        return True
    handle.request_stop()
    return handle.join(timeout)
