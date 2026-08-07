import io
import urllib.error
import urllib.request
from datetime import datetime

import pytest

from app.collectors.claude_collector import (
    ClaudeManagementAPIError,
    ClaudeUsageCostCollector,
)
from app.time_utils import app_tz


class FakeClaudeUsageCostCollector(ClaudeUsageCostCollector):
    def _get_json(self, path, params):
        if path.endswith("/usage_report/messages"):
            return {
                "data": [
                    {
                        "model": "claude-test",
                        "uncached_input_tokens": 11,
                        "output_tokens": 6,
                        "cache_read_input_tokens": 2,
                        "cache_creation_input_tokens": 1,
                        "ending_at": "2026-05-24T00:00:00Z",
                        "workspace_id": "workspace-test",
                    }
                ],
                "has_more": False,
            }
        return {
            "data": [
                {
                    "description": "Claude API",
                    "amount": "42",
                    "currency": "usd",
                    "ending_at": "2026-05-24T00:00:00Z",
                    "workspace_id": "workspace-test",
                }
            ],
            "has_more": False,
        }


def test_claude_collector_normalizes_mock_management_payloads() -> None:
    collector = FakeClaudeUsageCostCollector(
        api_key="test-key",
        organization_id="org-test",
        workspace_id="workspace-test",
    )

    rows = collector.collect(
        start_date=datetime(2026, 5, 23, tzinfo=app_tz()),
        end_date=datetime(2026, 5, 24, tzinfo=app_tz()),
    )

    assert len(rows) == 5
    assert {row["limit_type"] for row in rows} == {
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_creation_tokens",
        "api_cost",
    }
    assert rows[0]["service_provider"] == "Claude"
    assert rows[0]["source_type"] == "api_claude_management"
    assert rows[0]["organization_id"] == "org-test"
    assert rows[0]["workspace_id"] == "workspace-test"

    cost_row = next(row for row in rows if row["limit_type"] == "api_cost")
    assert cost_row["unit"] == "usd"
    assert cost_row["used_value"] == 0.42
    assert cost_row["metric_kind"] == "cost"

    usage_row = next(row for row in rows if row["limit_type"] == "input_tokens")
    assert usage_row["metric_kind"] == "usage"
    assert usage_row["period_start"] < usage_row["period_end"]


def test_claude_collector_paginates_and_merges_pages() -> None:
    class FakePaginated(ClaudeUsageCostCollector):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.calls = 0

        def _get_json(self, path, params):
            if not path.endswith("/usage_report/messages"):
                return {"data": [], "has_more": False}
            self.calls += 1
            if params.get("page") is None:
                return {
                    "data": [{"model": "claude-page-1", "output_tokens": 1, "ending_at": "2026-05-24T00:00:00Z"}],
                    "has_more": True,
                    "next_page": "cursor-2",
                }
            return {
                "data": [{"model": "claude-page-2", "output_tokens": 1, "ending_at": "2026-05-24T00:00:00Z"}],
                "has_more": False,
            }

    collector = FakePaginated(api_key="test-key")
    rows = collector.collect()

    assert collector.calls == 2
    assert {row["model_name"] for row in rows} == {"claude-page-1", "claude-page-2"}


def test_claude_collector_pagination_stops_on_repeated_cursor() -> None:
    class FakeLoopingPagination(ClaudeUsageCostCollector):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.calls = 0

        def _get_json(self, path, params):
            if not path.endswith("/usage_report/messages"):
                return {"data": [], "has_more": False}
            self.calls += 1
            return {
                "data": [{"model": "claude-test", "output_tokens": 1, "ending_at": "2026-05-24T00:00:00Z"}],
                "has_more": True,
                "next_page": "same-cursor-forever",
            }

    collector = FakeLoopingPagination(api_key="test-key")
    collector.collect()

    assert collector.calls == 2  # first page + one repeat, then the guard breaks


def test_claude_collector_rejects_non_usd_cost_currency() -> None:
    class FakeNonUsd(ClaudeUsageCostCollector):
        def _get_json(self, path, params):
            if path.endswith("/usage_report/messages"):
                return {"data": [], "has_more": False}
            return {
                "data": [{"description": "test", "amount": "100", "currency": "eur", "ending_at": "2026-05-24T00:00:00Z"}],
                "has_more": False,
            }

    rows = FakeNonUsd(api_key="test-key").collect()

    assert rows == []


def test_claude_collector_ignores_malformed_items() -> None:
    class FakeMalformed(ClaudeUsageCostCollector):
        def _get_json(self, path, params):
            if path.endswith("/usage_report/messages"):
                return {"data": ["not-a-dict", {"model": "claude-test", "output_tokens": "not-a-number"}], "has_more": False}
            return {"data": [], "has_more": False}

    rows = FakeMalformed(api_key="test-key").collect()

    assert rows == []


def test_claude_collector_403_error_message_is_generic_and_actionable(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", None, io.BytesIO(b""))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    collector = ClaudeUsageCostCollector(api_key="test-key")
    with pytest.raises(ClaudeManagementAPIError) as exc_info:
        collector.collect()

    assert "403" in str(exc_info.value)
    assert "Admin" in str(exc_info.value)


def test_claude_collector_api_key_never_appears_in_request_url(monkeypatch: pytest.MonkeyPatch) -> None:
    secret_key = "sk-ant-admin01-SECRET-SHOULD-NEVER-APPEAR-IN-URL"
    captured_urls: list[str] = []

    def fake_urlopen(request, timeout=None):
        captured_urls.append(request.full_url)
        assert request.get_header("X-api-key") == secret_key
        return _FakeResponse(b'{"data": [], "has_more": false}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    ClaudeUsageCostCollector(api_key=secret_key).collect()

    assert captured_urls
    for url in captured_urls:
        assert secret_key not in url


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *args: object) -> bool:
        return False
