"""Coverage for the network-free vendor config preflight (app/collectors/preflight.py
and GET /api/collector-preflight). Never makes a network call; never returns
any part of a credential value."""

import pytest
from fastapi.testclient import TestClient

from app.collectors.preflight import (
    all_vendor_preflight_statuses,
    claude_preflight,
    gemini_preflight,
    openai_preflight,
)
from app.main import app


@pytest.fixture(autouse=True)
def default_basic_auth_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENABLE_BASIC_AUTH", "false")


@pytest.fixture(autouse=True)
def clear_vendor_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "OPENAI_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_CLOUD_ACCESS_TOKEN",
        "GOOGLE_CLOUD_PROJECT",
        "GEMINI_PROJECT_ID",
        "ANTHROPIC_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------------------
# openai_preflight
# ---------------------------------------------------------------------------


def test_openai_preflight_reports_missing_when_unset() -> None:
    status = openai_preflight()

    assert status.configured is False
    assert status.production_ready is False
    assert "OPENAI_API_KEY" in status.missing_requirements


def test_openai_preflight_reports_configured_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-should-never-appear-in-output")

    status = openai_preflight()

    assert status.configured is True
    assert status.production_ready is True
    assert status.missing_requirements == []
    assert status.auth_mode == "organization_admin_api_key"


def test_openai_preflight_never_echoes_configured_key_value(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "sk-SECRET-SHOULD-NEVER-APPEAR-ANYWHERE-IN-PREFLIGHT"
    monkeypatch.setenv("OPENAI_API_KEY", secret)

    status = openai_preflight()

    blob = " ".join([status.auth_mode, *status.missing_requirements, *status.notes])
    assert secret not in blob


# ---------------------------------------------------------------------------
# gemini_preflight
# ---------------------------------------------------------------------------


def test_gemini_preflight_reports_missing_when_unset() -> None:
    status = gemini_preflight()

    assert status.configured is False
    assert status.production_ready is False
    assert "GOOGLE_CLOUD_ACCESS_TOKEN" in status.missing_requirements
    assert any("GOOGLE_CLOUD_PROJECT" in item for item in status.missing_requirements)


def test_gemini_preflight_api_key_alone_is_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    # The core security assertion: GEMINI_API_KEY alone must never be
    # reported as sufficient/production_ready for the management APIs.
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-should-not-be-sufficient")

    status = gemini_preflight()

    assert status.configured is False
    assert status.production_ready is False
    assert "GOOGLE_CLOUD_ACCESS_TOKEN" in status.missing_requirements


def test_gemini_preflight_api_key_alone_adds_explanatory_note(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    status = gemini_preflight()

    assert any("GEMINI_API_KEY" in note for note in status.notes)


def test_gemini_preflight_configured_when_oauth_and_project_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_CLOUD_ACCESS_TOKEN", "test-token")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")

    status = gemini_preflight()

    assert status.configured is True
    assert status.production_ready is True
    assert status.missing_requirements == []
    assert status.auth_mode == "oauth2_access_token"


def test_gemini_preflight_never_echoes_configured_token_value(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "SECRET-ACCESS-TOKEN-SHOULD-NEVER-APPEAR-ANYWHERE"
    monkeypatch.setenv("GOOGLE_CLOUD_ACCESS_TOKEN", secret)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")

    status = gemini_preflight()

    blob = " ".join([status.auth_mode, *status.missing_requirements, *status.notes])
    assert secret not in blob


# ---------------------------------------------------------------------------
# claude_preflight
# ---------------------------------------------------------------------------


def test_claude_preflight_reports_missing_when_unset() -> None:
    status = claude_preflight()

    assert status.configured is False
    assert status.production_ready is False
    assert "ANTHROPIC_API_KEY" in status.missing_requirements


def test_claude_preflight_configured_with_admin_key_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-admin01-should-never-appear-in-output")

    status = claude_preflight()

    assert status.configured is True
    assert status.production_ready is True
    assert not any("expected Admin API key prefix" in note for note in status.notes)


def test_claude_preflight_warns_on_unexpected_key_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-a-regular-key-not-admin")

    status = claude_preflight()

    # Soft signal only — never a hard block on a guessable prefix pattern.
    assert status.configured is True
    assert any("expected Admin API key prefix" in note for note in status.notes)


def test_claude_preflight_never_echoes_configured_key_value(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "sk-ant-api03-SECRET-SHOULD-NEVER-APPEAR-ANYWHERE"
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)

    status = claude_preflight()

    blob = " ".join([status.auth_mode, *status.missing_requirements, *status.notes])
    assert secret not in blob


# ---------------------------------------------------------------------------
# all_vendor_preflight_statuses / GET /api/collector-preflight
# ---------------------------------------------------------------------------


def test_all_vendor_preflight_statuses_returns_all_three_vendors() -> None:
    statuses = all_vendor_preflight_statuses()

    assert {status.vendor for status in statuses} == {"openai", "gemini", "claude"}


def test_collector_preflight_endpoint_never_makes_network_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.request

    def explode(*args, **kwargs):
        raise AssertionError("GET /api/collector-preflight must never make a network call")

    monkeypatch.setattr(urllib.request, "urlopen", explode)

    with TestClient(app) as client:
        response = client.get("/api/collector-preflight")

    assert response.status_code == 200


def test_collector_preflight_endpoint_returns_all_vendors() -> None:
    with TestClient(app) as client:
        response = client.get("/api/collector-preflight")

    assert response.status_code == 200
    body = response.json()
    assert {item["vendor"] for item in body} == {"openai", "gemini", "claude"}
    for item in body:
        assert set(item.keys()) == {
            "vendor",
            "configured",
            "auth_mode",
            "production_ready",
            "missing_requirements",
            "notes",
        }


def test_collector_preflight_endpoint_never_leaks_configured_secret_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secrets = {
        "OPENAI_API_KEY": "sk-OPENAI-SECRET-SHOULD-NEVER-LEAK",
        "GOOGLE_CLOUD_ACCESS_TOKEN": "GOOGLE-SECRET-SHOULD-NEVER-LEAK",
        "GOOGLE_CLOUD_PROJECT": "secret-project-should-not-be-treated-as-sensitive-but-check-anyway",
        "ANTHROPIC_API_KEY": "sk-ant-admin01-ANTHROPIC-SECRET-SHOULD-NEVER-LEAK",
    }
    for name, value in secrets.items():
        monkeypatch.setenv(name, value)

    with TestClient(app) as client:
        response = client.get("/api/collector-preflight")

    assert response.status_code == 200
    for secret in ("sk-OPENAI-SECRET-SHOULD-NEVER-LEAK", "GOOGLE-SECRET-SHOULD-NEVER-LEAK", "ANTHROPIC-SECRET-SHOULD-NEVER-LEAK"):
        assert secret not in response.text
