import io
import json
import urllib.error
import urllib.request
from datetime import datetime

import pytest

from app.collectors.openai_collector import OpenAIManagementAPIError, OpenAIUsageCostCollector
from app.time_utils import app_tz


class FakeOpenAIUsageCostCollector(OpenAIUsageCostCollector):
    def _get_json(self, path, params):
        if path.endswith("/usage/completions"):
            return {
                "data": [
                    {
                        "start_time": 1763895600,
                        "end_time": 1763982000,
                        "results": [
                            {
                                "model": "gpt-test",
                                "input_tokens": 10,
                                "output_tokens": 5,
                                "num_model_requests": 2,
                                "project_id": "proj_test",
                            }
                        ],
                    }
                ]
            }
        return {
            "data": [
                {
                    "start_time": 1763895600,
                    "end_time": 1763982000,
                    "results": [
                        {
                            "amount": {"value": 0.12, "currency": "usd"},
                            "line_item": "Test line item",
                            "project_id": "proj_test",
                        }
                    ],
                }
            ]
        }


def test_openai_collector_normalizes_mock_usage_and_cost_payloads() -> None:
    collector = FakeOpenAIUsageCostCollector(api_key="test-key")

    rows = collector.collect(
        start_date=datetime(2026, 5, 23, tzinfo=app_tz()),
        end_date=datetime(2026, 5, 24, tzinfo=app_tz()),
    )

    assert len(rows) == 4
    assert {row["limit_type"] for row in rows} == {"input_tokens", "output_tokens", "requests", "api_cost"}
    assert rows[0]["source_type"] == "api_openai_management"
    assert rows[-1]["unit"] == "usd"
    assert rows[0]["metric_kind"] == "usage"
    assert rows[-1]["metric_kind"] == "cost"
    assert all(row["period_start"] < row["period_end"] for row in rows)


def test_openai_collector_extracts_cache_read_tokens() -> None:
    class FakeWithCache(OpenAIUsageCostCollector):
        def _get_json(self, path, params):
            if path.endswith("/usage/completions"):
                return {
                    "data": [
                        {
                            "start_time": 1763895600,
                            "end_time": 1763982000,
                            "results": [
                                {
                                    "model": "gpt-test",
                                    "input_tokens": 10,
                                    "output_tokens": 5,
                                    "input_cached_tokens": 3,
                                    "num_model_requests": 2,
                                    "project_id": "proj_test",
                                }
                            ],
                        }
                    ]
                }
            return {"data": []}

    rows = FakeWithCache(api_key="test-key").collect()

    cache_row = next(row for row in rows if row["limit_type"] == "cache_read_tokens")
    assert cache_row["used_value"] == 3.0
    assert cache_row["unit"] == "cache_read_tokens"
    assert cache_row["metric_kind"] == "usage"


def test_openai_collector_paginates_and_merges_pages() -> None:
    class FakePaginated(OpenAIUsageCostCollector):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.calls = 0

        def _get_json(self, path, params):
            if not path.endswith("/usage/completions"):
                return {"data": []}
            self.calls += 1
            if params.get("page") is None:
                return {
                    "data": [
                        {
                            "start_time": 1763895600,
                            "end_time": 1763982000,
                            "results": [{"model": "gpt-page-1", "num_model_requests": 1}],
                        }
                    ],
                    "next_page": "cursor-2",
                }
            return {
                "data": [
                    {
                        "start_time": 1763982000,
                        "end_time": 1764068400,
                        "results": [{"model": "gpt-page-2", "num_model_requests": 1}],
                    }
                ]
            }

    collector = FakePaginated(api_key="test-key")
    rows = collector.collect()

    assert collector.calls == 2
    assert {row["model_name"] for row in rows} == {"gpt-page-1", "gpt-page-2"}


def test_openai_collector_pagination_stops_on_repeated_cursor() -> None:
    # A vendor response that echoes back the same next_page cursor forever
    # must not loop indefinitely.
    class FakeLoopingPagination(OpenAIUsageCostCollector):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.calls = 0

        def _get_json(self, path, params):
            if not path.endswith("/usage/completions"):
                return {"data": []}
            self.calls += 1
            return {
                "data": [
                    {
                        "start_time": 1763895600,
                        "end_time": 1763982000,
                        "results": [{"model": "gpt-test", "num_model_requests": 1}],
                    }
                ],
                "next_page": "same-cursor-forever",
            }

    collector = FakeLoopingPagination(api_key="test-key")
    collector.collect()

    assert collector.calls == 2  # first page + one repeat, then the guard breaks


def test_openai_collector_rejects_non_usd_cost_currency() -> None:
    class FakeNonUsd(OpenAIUsageCostCollector):
        def _get_json(self, path, params):
            if path.endswith("/usage/completions"):
                return {"data": []}
            return {
                "data": [
                    {
                        "start_time": 1763895600,
                        "end_time": 1763982000,
                        "results": [{"amount": {"value": 1.0, "currency": "eur"}, "line_item": "test"}],
                    }
                ]
            }

    rows = FakeNonUsd(api_key="test-key").collect()

    assert rows == []


def test_openai_collector_ignores_malformed_result_entries() -> None:
    class FakeMalformed(OpenAIUsageCostCollector):
        def _get_json(self, path, params):
            if path.endswith("/usage/completions"):
                return {
                    "data": [
                        {
                            "start_time": 1763895600,
                            "end_time": 1763982000,
                            "results": ["not-a-dict", {"model": "gpt-test", "num_model_requests": "not-a-number"}],
                        }
                    ]
                }
            return {"data": []}

    rows = FakeMalformed(api_key="test-key").collect()

    assert rows == []


def test_openai_collector_403_error_message_is_generic_and_actionable(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", None, io.BytesIO(b""))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    collector = OpenAIUsageCostCollector(api_key="test-key")
    with pytest.raises(OpenAIManagementAPIError) as exc_info:
        collector.collect()

    assert "403" in str(exc_info.value)
    assert "Admin" in str(exc_info.value)


def test_openai_collector_rate_limit_error_returns_safe_message(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 429, "Too Many Requests", None, io.BytesIO(b""))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    collector = OpenAIUsageCostCollector(api_key="test-key")
    with pytest.raises(OpenAIManagementAPIError) as exc_info:
        collector.collect()

    assert "429" in str(exc_info.value)


def test_openai_collector_api_key_never_appears_in_request_url(monkeypatch: pytest.MonkeyPatch) -> None:
    secret_key = "sk-SECRET-SHOULD-NEVER-APPEAR-IN-URL"
    captured_urls: list[str] = []

    def fake_urlopen(request, timeout=None):
        captured_urls.append(request.full_url)
        assert request.get_header("Authorization") == f"Bearer {secret_key}"
        return _FakeResponse(json.dumps({"data": []}).encode("utf-8"))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    OpenAIUsageCostCollector(api_key=secret_key).collect()

    assert captured_urls
    for url in captured_urls:
        assert secret_key not in url


def test_openai_collector_invalid_json_raises_management_api_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(request, timeout=None):
        return _FakeResponse(b"{not valid json")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    collector = OpenAIUsageCostCollector(api_key="test-key")
    with pytest.raises(OpenAIManagementAPIError):
        collector.collect()


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *args: object) -> bool:
        return False
