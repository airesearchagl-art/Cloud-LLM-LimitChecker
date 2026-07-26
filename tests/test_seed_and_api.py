from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import crud, models, schemas
from app.database import get_db
from app.main import app
from app.maintenance import remove_empty_duplicate_manual_required_limits
from app.seed import seed_from_yaml
from tests.helpers import create_limit, create_service, make_session


@pytest.fixture(autouse=True)
def default_basic_auth_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENABLE_BASIC_AUTH", "false")


def test_seed_from_yaml_is_idempotent(tmp_path: Path) -> None:
    seed_file = tmp_path / "seed.yaml"
    seed_file.write_text(
        """
services:
  - name: Test Service
    provider: TestProvider
    plan_name: Please enter manually
    account_type: web_subscription
    limits:
      - model_name: manual_model
        limit_type: messages
        max_value: null
        unit: messages
        reset_interval_type: manual
        reset_interval_value: 1
        next_reset_at: null
        warning_threshold: 70
        critical_threshold: 85
        source_type: manual_required
""",
        encoding="utf-8",
    )
    with next(make_session()) as db:
        first = seed_from_yaml(db, str(seed_file))
        second = seed_from_yaml(db, str(seed_file))
        service_count = db.scalar(select(func.count(models.Service.id)))
        limit_count = db.scalar(select(func.count(models.Limit.id)))

    assert first["services"] == 1
    assert first["limits"] == 1
    assert second["services"] == 0
    assert second["limits"] == 0
    assert service_count == 1
    assert limit_count == 1


def test_duplicate_cleanup_only_removes_safe_later_manual_required_records() -> None:
    with next(make_session()) as db:
        service = create_service(db)
        keep = create_limit(db, service, max_value=None)
        duplicate = create_limit(db, service, max_value=None)
        duplicate.source_type = "manual_required"
        unsafe = create_limit(db, service, max_value=100)
        unsafe.model_name = keep.model_name
        unsafe.limit_type = keep.limit_type
        unsafe.source_type = "manual_required"
        db.commit()
        keep_id = keep.id
        duplicate_id = duplicate.id
        unsafe_id = unsafe.id

        dry_run_count = remove_empty_duplicate_manual_required_limits(db, dry_run=True)
        removed_count = remove_empty_duplicate_manual_required_limits(db)
        remaining_ids = set(db.scalars(select(models.Limit.id)).all())

    assert dry_run_count == 1
    assert removed_count == 1
    assert duplicate_id not in remaining_ids
    assert keep_id in remaining_ids
    assert unsafe_id in remaining_ids


@pytest.fixture()
def api_client() -> tuple[TestClient, Session, int]:
    db_context = make_session()
    db = next(db_context)
    service = create_service(db)
    limit = create_limit(db, service, max_value=100)
    db.commit()

    def override_get_db():
        yield db

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)
    try:
        yield client, db, limit.id
    finally:
        app.dependency_overrides.clear()
        db.close()


def test_add_usage_mode_add_returns_200(api_client: tuple[TestClient, Session, int]) -> None:
    client, _, limit_id = api_client
    response = client.post(f"/api/limits/{limit_id}/usage", json={"used_value": 5, "mode": "add"})
    assert response.status_code == 200
    assert response.json()["used_value"] == 5


def test_add_usage_mode_add_negative_returns_error(api_client: tuple[TestClient, Session, int]) -> None:
    client, _, limit_id = api_client
    response = client.post(f"/api/limits/{limit_id}/usage", json={"used_value": -5, "mode": "add"})
    assert response.status_code in {400, 422}


def test_add_usage_mode_adjust_negative_returns_200(api_client: tuple[TestClient, Session, int]) -> None:
    client, _, limit_id = api_client
    response = client.post(
        f"/api/limits/{limit_id}/usage",
        json={"used_value": -3, "mode": "adjust", "note": "誤入力補正"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["used_value"] == -3
    assert body["source_type"] == "manual_adjustment"


def test_add_usage_mode_adjust_requires_note(api_client: tuple[TestClient, Session, int]) -> None:
    client, _, limit_id = api_client
    response = client.post(f"/api/limits/{limit_id}/usage", json={"used_value": -3, "mode": "adjust"})
    assert response.status_code in {400, 422}


def test_add_usage_mode_adjust_zero_returns_error(api_client: tuple[TestClient, Session, int]) -> None:
    client, _, limit_id = api_client
    response = client.post(
        f"/api/limits/{limit_id}/usage",
        json={"used_value": 0, "mode": "adjust", "note": "ゼロ補正"},
    )
    assert response.status_code in {400, 422}


def test_add_usage_mode_set_returns_400(api_client: tuple[TestClient, Session, int]) -> None:
    client, _, limit_id = api_client
    response = client.post(f"/api/limits/{limit_id}/usage", json={"used_value": 5, "mode": "set"})
    assert response.status_code == 400


def test_add_usage_missing_limit_returns_404(api_client: tuple[TestClient, Session, int]) -> None:
    client, _, _ = api_client
    response = client.post("/api/limits/999999/usage", json={"used_value": 5, "mode": "add"})
    assert response.status_code == 404


def test_usage_records_returns_joined_limit_fields(api_client: tuple[TestClient, Session, int]) -> None:
    client, _, limit_id = api_client
    client.post(f"/api/limits/{limit_id}/usage", json={"used_value": 5, "mode": "add", "note": "通常利用"})

    response = client.get("/api/usage-records")
    assert response.status_code == 200
    first = response.json()[0]
    assert first["service_name"] == "Test"
    assert first["model_name"] == "manual_model"
    assert first["limit_type"] == "messages"
    assert first["usage_record_id"] is not None


def test_usage_records_returns_adjustment_source_type(api_client: tuple[TestClient, Session, int]) -> None:
    client, _, limit_id = api_client
    client.post(
        f"/api/limits/{limit_id}/usage",
        json={"used_value": -2, "mode": "adjust", "note": "誤入力補正"},
    )

    response = client.get("/api/usage-records")
    assert response.status_code == 200
    assert response.json()[0]["source_type"] == "manual_adjustment"


def test_usage_records_ordered_by_recorded_at_desc_then_id_desc(api_client: tuple[TestClient, Session, int]) -> None:
    client, _, limit_id = api_client
    client.post(
        f"/api/limits/{limit_id}/usage",
        json={"used_value": 1, "mode": "add", "recorded_at": "2026-01-01T09:00:00+09:00"},
    )
    client.post(
        f"/api/limits/{limit_id}/usage",
        json={"used_value": 2, "mode": "add", "recorded_at": "2026-01-02T09:00:00+09:00"},
    )
    client.post(
        f"/api/limits/{limit_id}/usage",
        json={"used_value": 3, "mode": "add", "recorded_at": "2026-01-02T09:00:00+09:00"},
    )

    response = client.get("/api/usage-records")
    assert response.status_code == 200
    rows = response.json()
    assert [row["used_value"] for row in rows[:3]] == [3, 2, 1]


def test_lifespan_health_returns_200(api_client: tuple[TestClient, Session, int]) -> None:
    client, _, _ = api_client
    response = client.get("/api/health")
    assert response.status_code == 200


def test_basic_auth_enabled_allows_health_without_credentials(api_client: tuple[TestClient, Session, int], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _, _ = api_client
    monkeypatch.setenv("ENABLE_BASIC_AUTH", "true")
    monkeypatch.setenv("BASIC_AUTH_USERNAME", "admin")
    monkeypatch.setenv("BASIC_AUTH_PASSWORD", "secret")

    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_basic_auth_enabled_requires_credentials_for_dashboard(api_client: tuple[TestClient, Session, int], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _, _ = api_client
    monkeypatch.setenv("ENABLE_BASIC_AUTH", "true")
    monkeypatch.setenv("BASIC_AUTH_USERNAME", "admin")
    monkeypatch.setenv("BASIC_AUTH_PASSWORD", "secret")

    response = client.get("/api/dashboard")

    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Basic")


def test_basic_auth_enabled_requires_credentials_for_static_ui(api_client: tuple[TestClient, Session, int], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _, _ = api_client
    monkeypatch.setenv("ENABLE_BASIC_AUTH", "true")
    monkeypatch.setenv("BASIC_AUTH_USERNAME", "admin")
    monkeypatch.setenv("BASIC_AUTH_PASSWORD", "secret")

    response = client.get("/")

    assert response.status_code == 401


def test_basic_auth_rejects_wrong_credentials_for_dashboard(api_client: tuple[TestClient, Session, int], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _, _ = api_client
    monkeypatch.setenv("ENABLE_BASIC_AUTH", "true")
    monkeypatch.setenv("BASIC_AUTH_USERNAME", "admin")
    monkeypatch.setenv("BASIC_AUTH_PASSWORD", "secret")

    response = client.get("/api/dashboard", auth=("admin", "wrong"))

    assert response.status_code == 401


def test_basic_auth_accepts_correct_credentials_for_dashboard(api_client: tuple[TestClient, Session, int], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _, _ = api_client
    monkeypatch.setenv("ENABLE_BASIC_AUTH", "true")
    monkeypatch.setenv("BASIC_AUTH_USERNAME", "admin")
    monkeypatch.setenv("BASIC_AUTH_PASSWORD", "secret")

    response = client.get("/api/dashboard", auth=("admin", "secret"))

    assert response.status_code == 200


def test_basic_auth_enabled_requires_credentials_for_usage_records_csv(api_client: tuple[TestClient, Session, int], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _, _ = api_client
    monkeypatch.setenv("ENABLE_BASIC_AUTH", "true")
    monkeypatch.setenv("BASIC_AUTH_USERNAME", "admin")
    monkeypatch.setenv("BASIC_AUTH_PASSWORD", "secret")

    response = client.get("/api/export/usage-records.csv")

    assert response.status_code == 401


def test_basic_auth_accepts_correct_credentials_for_usage_records_csv(api_client: tuple[TestClient, Session, int], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _, _ = api_client
    monkeypatch.setenv("ENABLE_BASIC_AUTH", "true")
    monkeypatch.setenv("BASIC_AUTH_USERNAME", "admin")
    monkeypatch.setenv("BASIC_AUTH_PASSWORD", "secret")

    response = client.get("/api/export/usage-records.csv", auth=("admin", "secret"))

    assert response.status_code == 200


def test_basic_auth_enabled_without_config_keeps_health_open(api_client: tuple[TestClient, Session, int], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _, _ = api_client
    monkeypatch.setenv("ENABLE_BASIC_AUTH", "true")
    monkeypatch.delenv("BASIC_AUTH_USERNAME", raising=False)
    monkeypatch.delenv("BASIC_AUTH_PASSWORD", raising=False)

    response = client.get("/api/health")

    assert response.status_code == 200


def test_basic_auth_enabled_without_config_returns_503_for_dashboard(api_client: tuple[TestClient, Session, int], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _, _ = api_client
    monkeypatch.setenv("ENABLE_BASIC_AUTH", "true")
    monkeypatch.delenv("BASIC_AUTH_USERNAME", raising=False)
    monkeypatch.delenv("BASIC_AUTH_PASSWORD", raising=False)

    response = client.get("/api/dashboard")

    assert response.status_code == 503


def test_export_limits_csv_returns_200_and_header(api_client: tuple[TestClient, Session, int]) -> None:
    client, _, _ = api_client
    response = client.get("/api/export/limits.csv")
    assert response.status_code == 200
    assert response.text.startswith("\ufeffservice_name,provider,plan_name")
    assert "usage_percent" in response.text


def test_export_usage_records_csv_returns_200_and_header(api_client: tuple[TestClient, Session, int]) -> None:
    client, _, limit_id = api_client
    client.post(f"/api/limits/{limit_id}/usage", json={"used_value": 5, "mode": "add", "note": "normal"})

    response = client.get("/api/export/usage-records.csv")

    assert response.status_code == 200
    header = response.text.splitlines()[0]
    assert "usage_record_id" in header
    assert "service_name" in header


def test_export_usage_records_csv_includes_manual_adjustment(api_client: tuple[TestClient, Session, int]) -> None:
    client, _, limit_id = api_client
    client.post(
        f"/api/limits/{limit_id}/usage",
        json={"used_value": -2, "mode": "adjust", "note": "adjustment"},
    )

    response = client.get("/api/export/usage-records.csv")

    assert response.status_code == 200
    assert "manual_adjustment" in response.text


def test_collect_openai_returns_403_when_global_collectors_disabled(
    api_client: tuple[TestClient, Session, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _, _ = api_client
    monkeypatch.setenv("ENABLE_VENDOR_COLLECTORS", "false")
    monkeypatch.setenv("ENABLE_OPENAI_COLLECTOR", "true")

    response = client.post("/api/collect/openai")

    assert response.status_code == 403


def test_collect_openai_returns_403_when_vendor_collector_disabled(
    api_client: tuple[TestClient, Session, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _, _ = api_client
    monkeypatch.setenv("ENABLE_VENDOR_COLLECTORS", "true")
    monkeypatch.setenv("ENABLE_OPENAI_COLLECTOR", "false")

    response = client.post("/api/collect/openai")

    assert response.status_code == 403


def test_collect_gemini_returns_403_when_global_collectors_disabled(
    api_client: tuple[TestClient, Session, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _, _ = api_client
    monkeypatch.setenv("ENABLE_VENDOR_COLLECTORS", "false")
    monkeypatch.setenv("ENABLE_GEMINI_COLLECTOR", "true")

    response = client.post("/api/collect/gemini")

    assert response.status_code == 403


def test_collect_gemini_returns_403_when_vendor_collector_disabled(
    api_client: tuple[TestClient, Session, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _, _ = api_client
    monkeypatch.setenv("ENABLE_VENDOR_COLLECTORS", "true")
    monkeypatch.setenv("ENABLE_GEMINI_COLLECTOR", "false")

    response = client.post("/api/collect/gemini")

    assert response.status_code == 403


def test_collect_claude_returns_403_when_global_collectors_disabled(
    api_client: tuple[TestClient, Session, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _, _ = api_client
    monkeypatch.setenv("ENABLE_VENDOR_COLLECTORS", "false")
    monkeypatch.setenv("ENABLE_CLAUDE_COLLECTOR", "true")

    response = client.post("/api/collect/claude")

    assert response.status_code == 403


def test_collect_claude_returns_403_when_vendor_collector_disabled(
    api_client: tuple[TestClient, Session, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _, _ = api_client
    monkeypatch.setenv("ENABLE_VENDOR_COLLECTORS", "true")
    monkeypatch.setenv("ENABLE_CLAUDE_COLLECTOR", "false")

    response = client.post("/api/collect/claude")

    assert response.status_code == 403


def test_collect_openai_dry_run_returns_200_and_saves_log(
    api_client: tuple[TestClient, Session, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, db, _ = api_client
    monkeypatch.setenv("ENABLE_VENDOR_COLLECTORS", "true")
    monkeypatch.setenv("ENABLE_OPENAI_COLLECTOR", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    class FakeOpenAICollector:
        def __init__(self, api_key: str) -> None:
            self.api_key = api_key

        def collect(self):
            return [{"service_provider": "OpenAI"}]

    monkeypatch.setattr("app.main.OpenAIUsageCostCollector", FakeOpenAICollector)

    response = client.post("/api/collect/openai?dry_run=true")

    assert response.status_code == 200
    body = response.json()
    assert body["vendor"] == "openai"
    assert body["dry_run"] is True
    assert body["status"] == "success"
    assert body["records_found"] == 1
    assert body["records_saved"] == 0
    assert db.scalar(select(func.count(models.CollectorRun.id))) == 1


def test_collect_openai_dry_run_does_not_save_usage_records(
    api_client: tuple[TestClient, Session, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, db, _ = api_client
    monkeypatch.setenv("ENABLE_VENDOR_COLLECTORS", "true")
    monkeypatch.setenv("ENABLE_OPENAI_COLLECTOR", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    class FakeOpenAICollector:
        def __init__(self, api_key: str) -> None:
            pass

        def collect(self):
            return [
                {
                    "vendor": "openai",
                    "service_provider": "OpenAI",
                    "model_name": "openai_api",
                    "limit_type": "requests",
                    "used_value": 1.0,
                    "unit": "requests",
                    "recorded_at": "2026-05-24T12:00:00+09:00",
                    "source_type": "api_openai_management",
                }
            ]

    monkeypatch.setattr("app.main.OpenAIUsageCostCollector", FakeOpenAICollector)

    response = client.post("/api/collect/openai?dry_run=true")
    usage_count = db.scalar(select(func.count(models.UsageRecord.id)))

    assert response.status_code == 200
    assert response.json()["records_found"] == 1
    assert response.json()["records_saved"] == 0
    assert usage_count == 0


def test_collect_openai_dry_run_false_saves_usage_records(
    api_client: tuple[TestClient, Session, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, db, _ = api_client
    monkeypatch.setenv("ENABLE_VENDOR_COLLECTORS", "true")
    monkeypatch.setenv("ENABLE_OPENAI_COLLECTOR", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    class FakeOpenAICollector:
        def __init__(self, api_key: str) -> None:
            pass

        def collect(self):
            return [
                {
                    "vendor": "openai",
                    "service_provider": "OpenAI",
                    "model_name": "openai_api",
                    "limit_type": "requests",
                    "used_value": 1.0,
                    "unit": "requests",
                    "recorded_at": "2026-05-24",
                    "source_type": "api_openai_management",
                    "project_id": "project-test",
                }
            ]

    monkeypatch.setattr("app.main.OpenAIUsageCostCollector", FakeOpenAICollector)

    response = client.post("/api/collect/openai?dry_run=false")
    usage_count = db.scalar(select(func.count(models.UsageRecord.id)))
    service = db.scalar(select(models.Service).where(models.Service.name == "OpenAI API"))
    limit = db.scalar(select(models.Limit).where(models.Limit.model_name == "openai_api"))

    assert response.status_code == 200
    assert response.json()["records_found"] == 1
    assert response.json()["records_saved"] == 1
    assert usage_count == 1
    assert service is not None
    assert service.account_type == "api"
    assert limit is not None


def test_collect_openai_dry_run_false_skips_duplicate_records(
    api_client: tuple[TestClient, Session, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, db, _ = api_client
    monkeypatch.setenv("ENABLE_VENDOR_COLLECTORS", "true")
    monkeypatch.setenv("ENABLE_OPENAI_COLLECTOR", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("MAX_COLLECTOR_CALLS_PER_DAY", "10")

    class FakeOpenAICollector:
        def __init__(self, api_key: str) -> None:
            pass

        def collect(self):
            return [
                {
                    "vendor": "openai",
                    "service_provider": "OpenAI",
                    "model_name": "openai_api",
                    "limit_type": "requests",
                    "used_value": 1.0,
                    "unit": "requests",
                    "recorded_at": "2026-05-24T12:00:00+09:00",
                    "source_type": "api_openai_management",
                }
            ]

    monkeypatch.setattr("app.main.OpenAIUsageCostCollector", FakeOpenAICollector)

    first = client.post("/api/collect/openai?dry_run=false")
    second = client.post("/api/collect/openai?dry_run=false")
    usage_count = db.scalar(select(func.count(models.UsageRecord.id)))

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["records_saved"] == 1
    assert second.json()["records_saved"] == 0
    assert usage_count == 1


def test_collector_runs_returns_history(api_client: tuple[TestClient, Session, int], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _, _ = api_client
    monkeypatch.setenv("ENABLE_VENDOR_COLLECTORS", "true")
    monkeypatch.setenv("ENABLE_OPENAI_COLLECTOR", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr("app.main.OpenAIUsageCostCollector", lambda api_key: type("Fake", (), {"collect": lambda self: []})())
    client.post("/api/collect/openai?dry_run=true")

    response = client.get("/api/collector-runs")

    assert response.status_code == 200
    rows = response.json()
    assert rows[0]["vendor"] == "openai"
    assert rows[0]["status"] == "success"


def test_collect_daily_limit_exceeded_returns_429(
    api_client: tuple[TestClient, Session, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _, _ = api_client
    monkeypatch.setenv("ENABLE_VENDOR_COLLECTORS", "true")
    monkeypatch.setenv("ENABLE_OPENAI_COLLECTOR", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("MAX_COLLECTOR_CALLS_PER_DAY", "1")
    monkeypatch.setattr("app.main.OpenAIUsageCostCollector", lambda api_key: type("Fake", (), {"collect": lambda self: []})())

    first = client.post("/api/collect/openai?dry_run=true")
    second = client.post("/api/collect/openai?dry_run=true")

    assert first.status_code == 200
    assert second.status_code == 429


def test_collect_unknown_vendor_returns_400(api_client: tuple[TestClient, Session, int], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _, _ = api_client
    monkeypatch.setenv("ENABLE_VENDOR_COLLECTORS", "true")

    response = client.post("/api/collect/unknown")

    assert response.status_code == 400


def test_collect_stub_does_not_call_external_api(api_client: tuple[TestClient, Session, int], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _, _ = api_client
    monkeypatch.setenv("ENABLE_VENDOR_COLLECTORS", "true")
    monkeypatch.setenv("ENABLE_OPENAI_COLLECTOR", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr("app.main.OpenAIUsageCostCollector", lambda api_key: type("Fake", (), {"collect": lambda self: []})())

    response = client.post("/api/collect/openai?dry_run=true")

    assert response.status_code == 200
    assert response.json()["records_found"] == 0
    assert response.json()["records_saved"] == 0


def test_collect_openai_without_api_key_returns_400(
    api_client: tuple[TestClient, Session, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _, _ = api_client
    monkeypatch.setenv("ENABLE_VENDOR_COLLECTORS", "true")
    monkeypatch.setenv("ENABLE_OPENAI_COLLECTOR", "true")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    response = client.post("/api/collect/openai?dry_run=true")

    assert response.status_code == 400


def test_collect_gemini_without_api_key_returns_400(
    api_client: tuple[TestClient, Session, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _, _ = api_client
    monkeypatch.setenv("ENABLE_VENDOR_COLLECTORS", "true")
    monkeypatch.setenv("ENABLE_GEMINI_COLLECTOR", "true")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_ACCESS_TOKEN", raising=False)

    response = client.post("/api/collect/gemini?dry_run=true")

    assert response.status_code == 400


def test_collect_claude_without_api_key_returns_400(
    api_client: tuple[TestClient, Session, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _, _ = api_client
    monkeypatch.setenv("ENABLE_VENDOR_COLLECTORS", "true")
    monkeypatch.setenv("ENABLE_CLAUDE_COLLECTOR", "true")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    response = client.post("/api/collect/claude?dry_run=true")

    assert response.status_code == 400


def test_collect_claude_dry_run_returns_200_and_saves_log(
    api_client: tuple[TestClient, Session, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, db, _ = api_client
    monkeypatch.setenv("ENABLE_VENDOR_COLLECTORS", "true")
    monkeypatch.setenv("ENABLE_CLAUDE_COLLECTOR", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    class FakeClaudeCollector:
        def __init__(self, api_key: str, organization_id=None, workspace_id=None) -> None:
            self.api_key = api_key

        def collect(self):
            return [{"service_provider": "Claude"}, {"service_provider": "Claude"}]

    monkeypatch.setattr("app.main.ClaudeUsageCostCollector", FakeClaudeCollector)

    response = client.post("/api/collect/claude?dry_run=true")
    run = db.scalar(select(models.CollectorRun).order_by(models.CollectorRun.id.desc()))

    assert response.status_code == 200
    body = response.json()
    assert body["vendor"] == "claude"
    assert body["dry_run"] is True
    assert body["status"] == "success"
    assert body["records_found"] == 2
    assert body["records_saved"] == 0
    assert run is not None
    assert run.status == "success"


def test_collect_claude_management_api_error_returns_502(
    api_client: tuple[TestClient, Session, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.collectors.claude_collector import ClaudeManagementAPIError

    client, _, _ = api_client
    monkeypatch.setenv("ENABLE_VENDOR_COLLECTORS", "true")
    monkeypatch.setenv("ENABLE_CLAUDE_COLLECTOR", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    class FakeClaudeCollector:
        def __init__(self, api_key: str, organization_id=None, workspace_id=None) -> None:
            pass

        def collect(self):
            raise ClaudeManagementAPIError("bad upstream")

    monkeypatch.setattr("app.main.ClaudeUsageCostCollector", FakeClaudeCollector)

    response = client.post("/api/collect/claude?dry_run=true")

    assert response.status_code == 502


def test_collect_claude_network_error_returns_503(
    api_client: tuple[TestClient, Session, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.collectors.claude_collector import ClaudeManagementNetworkError

    client, _, _ = api_client
    monkeypatch.setenv("ENABLE_VENDOR_COLLECTORS", "true")
    monkeypatch.setenv("ENABLE_CLAUDE_COLLECTOR", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    class FakeClaudeCollector:
        def __init__(self, api_key: str, organization_id=None, workspace_id=None) -> None:
            pass

        def collect(self):
            raise ClaudeManagementNetworkError("network unavailable")

    monkeypatch.setattr("app.main.ClaudeUsageCostCollector", FakeClaudeCollector)

    response = client.post("/api/collect/claude?dry_run=true")

    assert response.status_code == 503


def test_collect_gemini_dry_run_returns_200_and_saves_log(
    api_client: tuple[TestClient, Session, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, db, _ = api_client
    monkeypatch.setenv("ENABLE_VENDOR_COLLECTORS", "true")
    monkeypatch.setenv("ENABLE_GEMINI_COLLECTOR", "true")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    class FakeGeminiCollector:
        def __init__(self, api_key=None, access_token=None, project_id=None) -> None:
            self.api_key = api_key

        def collect(self):
            return [{"service_provider": "Gemini"}, {"service_provider": "Gemini"}]

    monkeypatch.setattr("app.main.GeminiUsageCostCollector", FakeGeminiCollector)

    response = client.post("/api/collect/gemini?dry_run=true")
    run = db.scalar(select(models.CollectorRun).order_by(models.CollectorRun.id.desc()))

    assert response.status_code == 200
    body = response.json()
    assert body["vendor"] == "gemini"
    assert body["dry_run"] is True
    assert body["status"] == "success"
    assert body["records_found"] == 2
    assert body["records_saved"] == 0
    assert run is not None
    assert run.status == "success"


def test_collect_gemini_management_api_error_returns_502(
    api_client: tuple[TestClient, Session, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.collectors.gemini_collector import GeminiManagementAPIError

    client, _, _ = api_client
    monkeypatch.setenv("ENABLE_VENDOR_COLLECTORS", "true")
    monkeypatch.setenv("ENABLE_GEMINI_COLLECTOR", "true")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    class FakeGeminiCollector:
        def __init__(self, api_key=None, access_token=None, project_id=None) -> None:
            pass

        def collect(self):
            raise GeminiManagementAPIError("bad upstream")

    monkeypatch.setattr("app.main.GeminiUsageCostCollector", FakeGeminiCollector)

    response = client.post("/api/collect/gemini?dry_run=true")

    assert response.status_code == 502


def test_collect_gemini_network_error_returns_503(
    api_client: tuple[TestClient, Session, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.collectors.gemini_collector import GeminiManagementNetworkError

    client, _, _ = api_client
    monkeypatch.setenv("ENABLE_VENDOR_COLLECTORS", "true")
    monkeypatch.setenv("ENABLE_GEMINI_COLLECTOR", "true")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    class FakeGeminiCollector:
        def __init__(self, api_key=None, access_token=None, project_id=None) -> None:
            pass

        def collect(self):
            raise GeminiManagementNetworkError("network unavailable")

    monkeypatch.setattr("app.main.GeminiUsageCostCollector", FakeGeminiCollector)

    response = client.post("/api/collect/gemini?dry_run=true")

    assert response.status_code == 503


def test_collect_openai_management_api_error_returns_502(
    api_client: tuple[TestClient, Session, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.collectors.openai_collector import OpenAIManagementAPIError

    client, _, _ = api_client
    monkeypatch.setenv("ENABLE_VENDOR_COLLECTORS", "true")
    monkeypatch.setenv("ENABLE_OPENAI_COLLECTOR", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    class FakeOpenAICollector:
        def __init__(self, api_key: str) -> None:
            pass

        def collect(self):
            raise OpenAIManagementAPIError("bad upstream")

    monkeypatch.setattr("app.main.OpenAIUsageCostCollector", FakeOpenAICollector)

    response = client.post("/api/collect/openai?dry_run=true")

    assert response.status_code == 502


def test_collect_openai_permission_error_is_logged(
    api_client: tuple[TestClient, Session, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.collectors.openai_collector import OpenAIManagementAPIError

    client, db, _ = api_client
    monkeypatch.setenv("ENABLE_VENDOR_COLLECTORS", "true")
    monkeypatch.setenv("ENABLE_OPENAI_COLLECTOR", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    class FakeOpenAICollector:
        def __init__(self, api_key: str) -> None:
            pass

        def collect(self):
            raise OpenAIManagementAPIError(
                "OpenAI management API returned 403. "
                "Check organization/project permissions for usage/costs APIs."
            )

    monkeypatch.setattr("app.main.OpenAIUsageCostCollector", FakeOpenAICollector)

    response = client.post("/api/collect/openai?dry_run=true")
    run = db.scalar(select(models.CollectorRun).order_by(models.CollectorRun.id.desc()))

    assert response.status_code == 502
    assert run is not None
    assert run.status == "failed"
    assert "403" in (run.error_message or "")
    assert "permissions" in (run.error_message or "")
    assert run.records_saved == 0


def test_collect_openai_network_error_returns_503(
    api_client: tuple[TestClient, Session, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.collectors.openai_collector import OpenAIManagementNetworkError

    client, _, _ = api_client
    monkeypatch.setenv("ENABLE_VENDOR_COLLECTORS", "true")
    monkeypatch.setenv("ENABLE_OPENAI_COLLECTOR", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    class FakeOpenAICollector:
        def __init__(self, api_key: str) -> None:
            pass

        def collect(self):
            raise OpenAIManagementNetworkError("network unavailable")

    monkeypatch.setattr("app.main.OpenAIUsageCostCollector", FakeOpenAICollector)

    response = client.post("/api/collect/openai?dry_run=true")

    assert response.status_code == 503


def test_seed_api_disabled_returns_403(api_client: tuple[TestClient, Session, int], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _, _ = api_client
    monkeypatch.setenv("ENABLE_SEED_API", "false")
    response = client.post("/api/seed")
    assert response.status_code == 403


def test_seed_api_enabled_returns_200(api_client: tuple[TestClient, Session, int], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _, _ = api_client
    monkeypatch.setenv("ENABLE_SEED_API", "true")
    response = client.post("/api/seed")
    assert response.status_code == 200
    assert {"services", "limits", "normalized", "removed"}.issubset(response.json().keys())


def test_update_limit_returns_200_and_updates_fields(api_client: tuple[TestClient, Session, int]) -> None:
    client, _, limit_id = api_client
    response = client.put(
        f"/api/limits/{limit_id}",
        json={
            "model_name": "renamed_model",
            "max_value": 200,
            "unit": "requests",
            "reset_interval_type": "weeks",
            "next_reset_at": "2026-02-01T00:00:00+09:00",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["model_name"] == "renamed_model"
    assert body["max_value"] == 200
    assert body["unit"] == "requests"
    assert body["reset_interval_type"] == "weeks"


def test_update_limit_missing_limit_returns_404(api_client: tuple[TestClient, Session, int]) -> None:
    client, _, _ = api_client
    response = client.put(
        "/api/limits/999999",
        json={"model_name": "x", "max_value": None, "unit": "messages", "reset_interval_type": "manual"},
    )
    assert response.status_code == 404


def test_update_limit_rejects_negative_max_value(api_client: tuple[TestClient, Session, int]) -> None:
    client, _, limit_id = api_client
    response = client.put(
        f"/api/limits/{limit_id}",
        json={"model_name": "x", "max_value": -1, "unit": "messages", "reset_interval_type": "manual"},
    )
    assert response.status_code == 422


def test_update_limit_rejects_empty_unit(api_client: tuple[TestClient, Session, int]) -> None:
    client, _, limit_id = api_client
    response = client.put(
        f"/api/limits/{limit_id}",
        json={"model_name": "x", "max_value": None, "unit": "", "reset_interval_type": "manual"},
    )
    assert response.status_code == 422


def test_update_limit_rejects_empty_model_name(api_client: tuple[TestClient, Session, int]) -> None:
    client, _, limit_id = api_client
    response = client.put(
        f"/api/limits/{limit_id}",
        json={"model_name": "   ", "max_value": None, "unit": "messages", "reset_interval_type": "manual"},
    )
    assert response.status_code == 422


def test_update_limit_ignores_id_and_service_id_fields(api_client: tuple[TestClient, Session, int]) -> None:
    client, _, limit_id = api_client
    response = client.put(
        f"/api/limits/{limit_id}",
        json={
            "id": 999999,
            "service_id": 999999,
            "model_name": "keep_service",
            "max_value": None,
            "unit": "messages",
            "reset_interval_type": "manual",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == limit_id
    assert body["service_id"] != 999999


def test_update_limit_does_not_affect_other_limits(api_client: tuple[TestClient, Session, int]) -> None:
    client, db, limit_id = api_client
    existing_limit = db.get(models.Limit, limit_id)
    service = db.get(models.Service, existing_limit.service_id)
    other_limit = create_limit(db, service, max_value=50)
    db.commit()

    response = client.put(
        f"/api/limits/{limit_id}",
        json={"model_name": "only_this_one", "max_value": 300, "unit": "requests", "reset_interval_type": "manual"},
    )
    assert response.status_code == 200

    other_response = client.get("/api/limits", params={"service_id": service.id})
    other_body = next(item for item in other_response.json() if item["id"] == other_limit.id)
    assert other_body["model_name"] == "manual_model"
    assert other_body["max_value"] == 50


def test_update_limit_get_reflects_new_values(api_client: tuple[TestClient, Session, int]) -> None:
    client, _, limit_id = api_client
    client.put(
        f"/api/limits/{limit_id}",
        json={"model_name": "after_update", "max_value": 42, "unit": "tokens", "reset_interval_type": "hours"},
    )
    response = client.get("/api/limits")
    body = next(item for item in response.json() if item["id"] == limit_id)
    assert body["model_name"] == "after_update"
    assert body["max_value"] == 42
    assert body["unit"] == "tokens"
    assert body["reset_interval_type"] == "hours"


def test_update_limit_rolls_back_on_commit_failure() -> None:
    with next(make_session()) as db:
        service = create_service(db)
        limit = create_limit(db, service, max_value=100)
        db.commit()

        original_commit = db.commit
        calls = {"count": 0}

        def failing_commit() -> None:
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("commit failed")
            original_commit()

        db.commit = failing_commit
        payload = schemas.LimitUpdate(model_name="broken_update", max_value=999, unit="messages", reset_interval_type="manual")
        with pytest.raises(RuntimeError):
            crud.update_limit(db, limit.id, payload)

        db.commit = original_commit
        refreshed = db.get(models.Limit, limit.id)
        db.refresh(refreshed)

    assert refreshed.model_name == "manual_model"
    assert refreshed.max_value == 100


def test_update_limit_session_reusable_after_commit_failure() -> None:
    with next(make_session()) as db:
        service = create_service(db)
        limit = create_limit(db, service, max_value=100)
        db.commit()

        original_commit = db.commit
        calls = {"count": 0}

        def failing_commit() -> None:
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("commit failed")
            original_commit()

        db.commit = failing_commit
        bad_payload = schemas.LimitUpdate(model_name="broken", max_value=1, unit="messages", reset_interval_type="manual")
        with pytest.raises(RuntimeError):
            crud.update_limit(db, limit.id, bad_payload)

        db.commit = original_commit
        good_payload = schemas.LimitUpdate(model_name="fixed", max_value=5, unit="messages", reset_interval_type="manual")
        updated = crud.update_limit(db, limit.id, good_payload)

    assert updated.model_name == "fixed"
    assert updated.max_value == 5
