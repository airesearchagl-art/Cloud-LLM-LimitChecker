import pytest

from app.codex_rate_limits_scheduler import CodexRateLimitsScheduler
from app.codex_rate_limits_state import CodexRateLimitsController
from app.main import app


def _forbidden_fetch(**kwargs):
    raise AssertionError("real Codex fetch must never run during the test suite")


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
    test, with an isolated instance that keeps the real default `enabled`
    behavior (so tests can still assert on the default `auto_refresh_*`
    fields) but can never actually call anything but this file's own
    fetch stub, and never touches the real on-disk cache path.
    """
    app.state.codex_rate_limits_scheduler = CodexRateLimitsScheduler(
        controller=CodexRateLimitsController(),
        fetch=_forbidden_fetch,
        cache_path=tmp_path / "codex-rate-limits-scheduler-safety-net.json",
    )
    yield
