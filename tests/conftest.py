from contextlib import contextmanager

import pytest

from app.codex_rate_limits_scheduler import CodexRateLimitsScheduler
from app.codex_rate_limits_state import CodexRateLimitsController
from app.main import app


def _forbidden_fetch(**kwargs):
    raise AssertionError("real Codex fetch must never run during the test suite")


@contextmanager
def safe_codex_rate_limits_scheduler(tmp_path):
    """Swap in an isolated, safe scheduler for the duration of the `with` block;
    always restore whatever was on `app.state.codex_rate_limits_scheduler`
    beforehand on exit (including on exception).

    Plain `contextlib.contextmanager` (not a pytest fixture itself) so this
    save/replace/restore behavior can be unit tested directly and
    deterministically, independent of pytest's own fixture machinery — see
    `test_safe_scheduler_context_manager_restores_original_after_exit` in
    `tests/test_codex_rate_limits_api.py`. The actual autouse pytest fixture
    below is a thin wrapper around this.
    """
    original_scheduler = app.state.codex_rate_limits_scheduler
    app.state.codex_rate_limits_scheduler = CodexRateLimitsScheduler(
        controller=CodexRateLimitsController(),
        fetch=_forbidden_fetch,
        cache_path=tmp_path / "codex-rate-limits-scheduler-safety-net.json",
    )
    try:
        yield app.state.codex_rate_limits_scheduler
    finally:
        app.state.codex_rate_limits_scheduler = original_scheduler


@pytest.fixture(autouse=True)
def _safe_codex_rate_limits_scheduler(tmp_path):
    """Whole-suite safety net.

    `app.state.codex_rate_limits_scheduler` is a single module-level object
    wired to the real `fetch_codex_rate_limits` adapter (see `app.main`),
    and its background task auto-starts on every `TestClient(app)` __enter__
    via the FastAPI lifespan. Left as-is, any test that opens a TestClient
    would start a real periodic-refresh task; whether it could ever reach a
    real `codex app-server` launch depends on timing (~30s+ initial delay),
    which is fragile to rely on. This fixture replaces it, before every
    test, with a fresh instance (own `CodexRateLimitsController`, own
    isolated `tmp_path` cache file, own attempt/success/error state — never
    shared between tests regardless of execution order) that keeps the real
    default `enabled` behavior (so tests can still assert on the default
    `auto_refresh_*` fields) but can never actually call anything but this
    file's own fetch stub, and never touches the real on-disk cache path.

    The original object is saved and restored (see
    `safe_codex_rate_limits_scheduler` above), so the app module's real,
    production-wired scheduler is left exactly as it was once the test (and
    its TestClient, if any) is done — a test never permanently mutates
    shared module state for whatever runs after it.
    """
    with safe_codex_rate_limits_scheduler(tmp_path):
        yield
