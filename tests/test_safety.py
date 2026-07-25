import pytest

from app import crud
from app.safety import (
    CollectorDailyLimitExceededError,
    PaidModelCallBlockedError,
    UnknownCollectorVendorError,
    assert_collector_daily_limit_not_exceeded,
    allow_paid_model_calls,
    assert_paid_model_calls_allowed,
    collector_dry_run_default,
    collector_enabled,
    max_collector_calls_per_day,
    normalize_collector_vendor,
    vendor_collectors_enabled,
)
from tests.helpers import make_session


def test_allow_paid_model_calls_defaults_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALLOW_PAID_MODEL_CALLS", raising=False)
    assert allow_paid_model_calls() is False


def test_paid_model_call_blocked_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALLOW_PAID_MODEL_CALLS", "false")
    with pytest.raises(PaidModelCallBlockedError):
        assert_paid_model_calls_allowed("responses.create")


def test_paid_model_call_allowed_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALLOW_PAID_MODEL_CALLS", "true")
    assert_paid_model_calls_allowed("responses.create")


def test_vendor_collectors_default_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ENABLE_VENDOR_COLLECTORS", raising=False)
    assert vendor_collectors_enabled() is False


def test_collector_disabled_when_global_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENABLE_VENDOR_COLLECTORS", "false")
    monkeypatch.setenv("ENABLE_OPENAI_COLLECTOR", "true")
    assert collector_enabled("openai") is False


def test_collector_enabled_when_global_and_vendor_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENABLE_VENDOR_COLLECTORS", "true")
    monkeypatch.setenv("ENABLE_OPENAI_COLLECTOR", "true")
    assert collector_enabled("openai") is True


def test_unknown_vendor_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENABLE_VENDOR_COLLECTORS", "true")
    assert collector_enabled("unknown") is False


def test_collector_dry_run_default_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COLLECTOR_DRY_RUN_DEFAULT", raising=False)
    assert collector_dry_run_default() is True


def test_max_collector_calls_per_day_default_24(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MAX_COLLECTOR_CALLS_PER_DAY", raising=False)
    assert max_collector_calls_per_day() == 24


def test_daily_limit_check_blocks_when_count_reaches_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAX_COLLECTOR_CALLS_PER_DAY", "1")
    with next(make_session()) as db:
        crud.create_collector_run(db, "openai", dry_run=True)

        with pytest.raises(CollectorDailyLimitExceededError):
            assert_collector_daily_limit_not_exceeded(db, "openai")


def test_daily_limit_check_rejects_unknown_vendor() -> None:
    with next(make_session()) as db:
        with pytest.raises(UnknownCollectorVendorError):
            assert_collector_daily_limit_not_exceeded(db, "unknown")


def test_normalize_collector_vendor() -> None:
    assert normalize_collector_vendor(" OpenAI ") == "openai"
