"""Coverage for the Claude collector against the OFFICIAL nested response shape:

{
  "data": [
    {"starting_at": ..., "ending_at": ..., "results": [ {...usage/cost fields...}, ... ]},
    ...
  ],
  "has_more": bool,
  "next_page": <cursor>
}

Usage/cost values live in bucket["results"], never directly on the bucket —
fixtures here intentionally mirror that nesting rather than a flattened shape.
"""

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


def _usage_result(**overrides) -> dict:
    result = {
        "model": "claude-test",
        "uncached_input_tokens": 11,
        "output_tokens": 6,
        "cache_read_input_tokens": 2,
        "cache_creation_input_tokens": 1,
        "workspace_id": "workspace-test",
    }
    result.update(overrides)
    return result


def _usage_bucket(results=None, starting_at="2026-05-23T00:00:00Z", ending_at="2026-05-24T00:00:00Z") -> dict:
    return {
        "starting_at": starting_at,
        "ending_at": ending_at,
        "results": [_usage_result()] if results is None else results,
    }


def _cost_result(**overrides) -> dict:
    result = {
        "description": "Claude API",
        "amount": "42",
        "currency": "usd",
        "workspace_id": "workspace-test",
    }
    result.update(overrides)
    return result


def _cost_bucket(results=None, starting_at="2026-05-23T00:00:00Z", ending_at="2026-05-24T00:00:00Z") -> dict:
    return {
        "starting_at": starting_at,
        "ending_at": ending_at,
        "results": [_cost_result()] if results is None else results,
    }


class FakeClaudeUsageCostCollector(ClaudeUsageCostCollector):
    def _get_json(self, path, params):
        if path.endswith("/usage_report/messages"):
            return {"data": [_usage_bucket()], "has_more": False}
        return {"data": [_cost_bucket()], "has_more": False}


def test_claude_collector_normalizes_mock_usage_and_cost_bucket_results() -> None:
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
    assert usage_row["period_start"] == "2026-05-23T00:00:00Z"
    assert usage_row["period_end"] == "2026-05-24T00:00:00Z"


def test_claude_collector_handles_multiple_results_in_one_bucket() -> None:
    class FakeMultiResult(ClaudeUsageCostCollector):
        def _get_json(self, path, params):
            if path.endswith("/usage_report/messages"):
                return {
                    "data": [
                        _usage_bucket(
                            results=[
                                _usage_result(model="claude-a", output_tokens=1),
                                _usage_result(model="claude-b", output_tokens=2),
                            ]
                        )
                    ],
                    "has_more": False,
                }
            return {"data": [], "has_more": False}

    rows = FakeMultiResult(api_key="test-key").collect()

    assert {row["model_name"] for row in rows} == {"claude-a", "claude-b"}


def test_claude_collector_handles_multiple_buckets() -> None:
    class FakeMultiBucket(ClaudeUsageCostCollector):
        def _get_json(self, path, params):
            if path.endswith("/usage_report/messages"):
                return {
                    "data": [
                        _usage_bucket(
                            results=[_usage_result(model="claude-day-1", output_tokens=1)],
                            starting_at="2026-05-23T00:00:00Z",
                            ending_at="2026-05-24T00:00:00Z",
                        ),
                        _usage_bucket(
                            results=[_usage_result(model="claude-day-2", output_tokens=1)],
                            starting_at="2026-05-24T00:00:00Z",
                            ending_at="2026-05-25T00:00:00Z",
                        ),
                    ],
                    "has_more": False,
                }
            return {"data": [], "has_more": False}

    rows = FakeMultiBucket(api_key="test-key").collect()

    periods = {(row["model_name"], row["period_start"], row["period_end"]) for row in rows}
    assert periods == {
        ("claude-day-1", "2026-05-23T00:00:00Z", "2026-05-24T00:00:00Z"),
        ("claude-day-2", "2026-05-24T00:00:00Z", "2026-05-25T00:00:00Z"),
    }


def test_claude_collector_handles_empty_results_bucket() -> None:
    class FakeEmptyResults(ClaudeUsageCostCollector):
        def _get_json(self, path, params):
            if path.endswith("/usage_report/messages"):
                return {"data": [_usage_bucket(results=[])], "has_more": False}
            return {"data": [], "has_more": False}

    rows = FakeEmptyResults(api_key="test-key").collect()

    assert rows == []


def test_claude_collector_ignores_malformed_bucket() -> None:
    class FakeMalformedBucket(ClaudeUsageCostCollector):
        def _get_json(self, path, params):
            if path.endswith("/usage_report/messages"):
                return {"data": ["not-a-dict", _usage_bucket()], "has_more": False}
            return {"data": [], "has_more": False}

    rows = FakeMalformedBucket(api_key="test-key").collect()

    # the malformed entry is skipped; the valid bucket alongside it still produces rows
    assert len(rows) == 4


def test_claude_collector_ignores_malformed_result_within_bucket() -> None:
    malformed_result = _usage_result(
        uncached_input_tokens="not-a-number",
        output_tokens="not-a-number",
        cache_read_input_tokens="not-a-number",
        cache_creation_input_tokens="not-a-number",
    )

    class FakeMalformedResult(ClaudeUsageCostCollector):
        def _get_json(self, path, params):
            if path.endswith("/usage_report/messages"):
                return {
                    "data": [_usage_bucket(results=["not-a-dict", malformed_result])],
                    "has_more": False,
                }
            return {"data": [], "has_more": False}

    rows = FakeMalformedResult(api_key="test-key").collect()

    assert rows == []


def test_claude_collector_partially_malformed_result_still_reports_valid_fields() -> None:
    # Only output_tokens is malformed; the other three token fields in the
    # same result are still valid and must still produce rows.
    partially_malformed = _usage_result(output_tokens="not-a-number")

    class FakePartiallyMalformed(ClaudeUsageCostCollector):
        def _get_json(self, path, params):
            if path.endswith("/usage_report/messages"):
                return {"data": [_usage_bucket(results=[partially_malformed])], "has_more": False}
            return {"data": [], "has_more": False}

    rows = FakePartiallyMalformed(api_key="test-key").collect()

    assert {row["limit_type"] for row in rows} == {"input_tokens", "cache_read_tokens", "cache_creation_tokens"}


def test_claude_collector_bucket_missing_period_produces_unvalidated_row() -> None:
    # The collector itself never fabricates a period — it passes through
    # whatever (or nothing) the bucket provided. Validation/rejection of a
    # missing period is app.collectors.types.CollectorNormalizedRecord's job,
    # not the collector's.
    class FakeMissingPeriod(ClaudeUsageCostCollector):
        def _get_json(self, path, params):
            if path.endswith("/usage_report/messages"):
                bucket = _usage_bucket()
                del bucket["starting_at"]
                del bucket["ending_at"]
                return {"data": [bucket], "has_more": False}
            return {"data": [], "has_more": False}

    rows = FakeMissingPeriod(api_key="test-key").collect()

    assert len(rows) == 4
    assert all(row["period_start"] is None and row["period_end"] is None for row in rows)


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
                    "data": [_usage_bucket(results=[_usage_result(model="claude-page-1", output_tokens=1)])],
                    "has_more": True,
                    "next_page": "cursor-2",
                }
            return {
                "data": [_usage_bucket(results=[_usage_result(model="claude-page-2", output_tokens=1)])],
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
                "data": [_usage_bucket()],
                "has_more": True,
                "next_page": "same-cursor-forever",
            }

    collector = FakeLoopingPagination(api_key="test-key")
    collector.collect()

    assert collector.calls == 2  # first page + one repeat, then the guard breaks


def test_claude_collector_ignores_nan_and_infinity_values() -> None:
    class FakeNonFinite(ClaudeUsageCostCollector):
        def _get_json(self, path, params):
            if path.endswith("/usage_report/messages"):
                return {
                    "data": [
                        _usage_bucket(
                            results=[
                                _usage_result(
                                    uncached_input_tokens="NaN",
                                    output_tokens="Infinity",
                                    cache_read_input_tokens="-Infinity",
                                    cache_creation_input_tokens=float("nan"),
                                )
                            ]
                        )
                    ],
                    "has_more": False,
                }
            return {"data": [], "has_more": False}

    rows = FakeNonFinite(api_key="test-key").collect()

    assert rows == []


def test_claude_collector_rejects_non_usd_cost_currency() -> None:
    class FakeNonUsd(ClaudeUsageCostCollector):
        def _get_json(self, path, params):
            if path.endswith("/usage_report/messages"):
                return {"data": [], "has_more": False}
            return {"data": [_cost_bucket(results=[_cost_result(currency="eur")])], "has_more": False}

    rows = FakeNonUsd(api_key="test-key").collect()

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


def test_claude_collector_generic_error_never_echoes_response_body(monkeypatch: pytest.MonkeyPatch) -> None:
    secret_marker = "SECRET-SHOULD-NEVER-LEAK-INTO-EXCEPTION"

    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(
            request.full_url, 500, "Internal Server Error", None, io.BytesIO(secret_marker.encode("utf-8"))
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    collector = ClaudeUsageCostCollector(api_key="test-key")
    with pytest.raises(ClaudeManagementAPIError) as exc_info:
        collector.collect()

    assert secret_marker not in str(exc_info.value)
    assert "500" in str(exc_info.value)


def test_claude_collector_network_error_has_no_dynamic_content(monkeypatch: pytest.MonkeyPatch) -> None:
    sensitive_detail = "some low-level socket/proxy detail that might be sensitive"

    def fake_urlopen(request, timeout=None):
        raise urllib.error.URLError(sensitive_detail)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    collector = ClaudeUsageCostCollector(api_key="test-key")
    with pytest.raises(Exception) as exc_info:
        collector.collect()

    assert sensitive_detail not in str(exc_info.value)


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
