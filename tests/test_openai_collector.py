import io
import json
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any

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
        if path.endswith("/organization/spend_limit"):
            return {}
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


def test_openai_collector_bucket_missing_period_produces_unvalidated_row() -> None:
    # The collector never fabricates a period — a bucket missing start_time/
    # end_time passes through with period_start/period_end=None rather than
    # being backfilled with now()-1d. Validation/rejection is
    # app.collectors.types.CollectorNormalizedRecord's job, not the
    # collector's.
    class FakeMissingPeriod(OpenAIUsageCostCollector):
        def _get_json(self, path, params):
            if path.endswith("/usage/completions"):
                return {"data": [{"results": [{"model": "gpt-test", "num_model_requests": 1}]}]}
            return {"data": []}

    rows = FakeMissingPeriod(api_key="test-key").collect()

    assert len(rows) == 1
    assert rows[0]["period_start"] is None
    assert rows[0]["period_end"] is None
    assert rows[0]["recorded_at"] == ""


def test_openai_collector_ignores_nan_and_infinity_values() -> None:
    class FakeNonFinite(OpenAIUsageCostCollector):
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
                                    "input_tokens": "NaN",
                                    "output_tokens": "Infinity",
                                    "num_model_requests": "-Infinity",
                                }
                            ],
                        }
                    ]
                }
            return {"data": []}

    rows = FakeNonFinite(api_key="test-key").collect()

    assert rows == []


def test_openai_collector_generic_error_never_echoes_response_body(monkeypatch: pytest.MonkeyPatch) -> None:
    secret_marker = "SECRET-SHOULD-NEVER-LEAK-INTO-EXCEPTION"

    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(
            request.full_url, 500, "Internal Server Error", None, io.BytesIO(secret_marker.encode("utf-8"))
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    collector = OpenAIUsageCostCollector(api_key="test-key")
    with pytest.raises(OpenAIManagementAPIError) as exc_info:
        collector.collect()

    assert secret_marker not in str(exc_info.value)
    assert "500" in str(exc_info.value)


def test_openai_collector_network_error_has_no_dynamic_content(monkeypatch: pytest.MonkeyPatch) -> None:
    sensitive_detail = "some low-level socket/proxy detail that might be sensitive"

    def fake_urlopen(request, timeout=None):
        raise urllib.error.URLError(sensitive_detail)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    collector = OpenAIUsageCostCollector(api_key="test-key")
    with pytest.raises(Exception) as exc_info:
        collector.collect()

    assert sensitive_detail not in str(exc_info.value)


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


def test_openai_collector_spend_limit_uses_get_and_bearer_header_only(monkeypatch: pytest.MonkeyPatch) -> None:
    secret_key = "sk-SECRET-SHOULD-NEVER-APPEAR-IN-URL"
    captured_requests: list[urllib.request.Request] = []

    def fake_urlopen(request, timeout=None):
        captured_requests.append(request)
        return _FakeResponse(json.dumps({"data": []}).encode("utf-8"))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    OpenAIUsageCostCollector(api_key=secret_key).collect()

    spend_limit_requests = [r for r in captured_requests if r.full_url.endswith("/organization/spend_limit")]
    assert len(spend_limit_requests) == 1
    request = spend_limit_requests[0]
    assert request.get_method() == "GET"
    assert request.get_header("Authorization") == f"Bearer {secret_key}"
    assert secret_key not in request.full_url
    assert "?" not in request.full_url


_VALID_SPEND_LIMIT_PAYLOAD: dict[str, Any] = {
    "object": "organization.spend_limit",
    "threshold_amount": 10000,
    "currency": "USD",
    "interval": "month",
    "enforcement": {"status": "enforcing"},
}


def test_openai_collector_spend_limit_converts_cents_to_usd() -> None:
    class FakeSpendLimit(OpenAIUsageCostCollector):
        def _get_json(self, path, params):
            if path.endswith("/organization/spend_limit"):
                return dict(_VALID_SPEND_LIMIT_PAYLOAD)
            return {"data": []}

    rows = FakeSpendLimit(api_key="test-key").collect()

    assert len(rows) == 1
    assert rows[0]["used_value"] == 100.0


def test_openai_collector_spend_limit_accepts_float_threshold_amount() -> None:
    # A genuine JSON number (not a numeric string) is accepted whether it
    # arrives as an int or a float.
    class FakeSpendLimit(OpenAIUsageCostCollector):
        def _get_json(self, path, params):
            if path.endswith("/organization/spend_limit"):
                return {**_VALID_SPEND_LIMIT_PAYLOAD, "threshold_amount": 10050.0}
            return {"data": []}

    rows = FakeSpendLimit(api_key="test-key").collect()

    assert len(rows) == 1
    assert rows[0]["used_value"] == 100.5


def test_openai_collector_spend_limit_row_shape() -> None:
    class FakeSpendLimit(OpenAIUsageCostCollector):
        def _get_json(self, path, params):
            if path.endswith("/organization/spend_limit"):
                return {**_VALID_SPEND_LIMIT_PAYLOAD, "threshold_amount": 5000, "currency": "USD"}
            return {"data": []}

    rows = FakeSpendLimit(api_key="test-key").collect()

    assert len(rows) == 1
    row = rows[0]
    assert row["metric_kind"] == "budget"
    assert row["unit"] == "usd"
    assert row["vendor"] == "openai"
    assert row["source_type"] == "api_openai_management"
    assert row["bucket_width"] is None


def test_openai_collector_spend_limit_interval_goes_to_metadata_not_bucket_width() -> None:
    # bucket_width means "vendor-reported time-series bucket size" (e.g.
    # 1d/1h/1m for usage/costs). spend_limit's `interval` is the evaluation
    # period for the hard threshold, a different concept entirely — it must
    # never be placed into bucket_width, only into metadata["interval"].
    class FakeSpendLimit(OpenAIUsageCostCollector):
        def _get_json(self, path, params):
            if path.endswith("/organization/spend_limit"):
                return dict(_VALID_SPEND_LIMIT_PAYLOAD)
            return {"data": []}

    rows = FakeSpendLimit(api_key="test-key").collect()

    assert len(rows) == 1
    assert rows[0]["bucket_width"] is None
    assert rows[0]["metadata"]["interval"] == "month"


def test_openai_collector_spend_limit_enforcement_status_enforcing_is_valid_row() -> None:
    class FakeSpendLimit(OpenAIUsageCostCollector):
        def _get_json(self, path, params):
            if path.endswith("/organization/spend_limit"):
                return {**_VALID_SPEND_LIMIT_PAYLOAD, "enforcement": {"status": "enforcing"}}
            return {"data": []}

    rows = FakeSpendLimit(api_key="test-key").collect()

    assert len(rows) == 1
    assert rows[0]["metadata"]["enforcement_status"] == "enforcing"


def test_openai_collector_spend_limit_enforcement_status_inactive_is_valid_row() -> None:
    # Current OpenAI API contract (Returns schema of the "Retrieve
    # organization spend limit" API Reference) explicitly documents
    # enforcement.status as one of "inactive" or "enforcing".
    class FakeSpendLimit(OpenAIUsageCostCollector):
        def _get_json(self, path, params):
            if path.endswith("/organization/spend_limit"):
                return {**_VALID_SPEND_LIMIT_PAYLOAD, "enforcement": {"status": "inactive"}}
            return {"data": []}

    rows = FakeSpendLimit(api_key="test-key").collect()

    assert len(rows) == 1
    assert rows[0]["metadata"]["enforcement_status"] == "inactive"


@pytest.mark.parametrize(
    "payload",
    [
        "not-a-dict",
        [],
        None,
        {},
        {**_VALID_SPEND_LIMIT_PAYLOAD, "threshold_amount": None},
        {**_VALID_SPEND_LIMIT_PAYLOAD, "threshold_amount": "not-a-number"},
        {**_VALID_SPEND_LIMIT_PAYLOAD, "threshold_amount": "NaN"},
        {**_VALID_SPEND_LIMIT_PAYLOAD, "threshold_amount": "Infinity"},
        {**_VALID_SPEND_LIMIT_PAYLOAD, "threshold_amount": 0},
        {**_VALID_SPEND_LIMIT_PAYLOAD, "threshold_amount": -100},
        {**_VALID_SPEND_LIMIT_PAYLOAD, "threshold_amount": "10000"},
        {**_VALID_SPEND_LIMIT_PAYLOAD, "threshold_amount": True},
        {**_VALID_SPEND_LIMIT_PAYLOAD, "threshold_amount": False},
        {**_VALID_SPEND_LIMIT_PAYLOAD, "currency": "eur"},
        {**_VALID_SPEND_LIMIT_PAYLOAD, "currency": "usd"},
        {k: v for k, v in _VALID_SPEND_LIMIT_PAYLOAD.items() if k != "currency"},
        {**_VALID_SPEND_LIMIT_PAYLOAD, "object": "wrong_object"},
        {k: v for k, v in _VALID_SPEND_LIMIT_PAYLOAD.items() if k != "object"},
        {**_VALID_SPEND_LIMIT_PAYLOAD, "interval": "day"},
        {k: v for k, v in _VALID_SPEND_LIMIT_PAYLOAD.items() if k != "interval"},
        {**_VALID_SPEND_LIMIT_PAYLOAD, "enforcement": {"status": "something_unknown"}},
        {k: v for k, v in _VALID_SPEND_LIMIT_PAYLOAD.items() if k != "enforcement"},
    ],
)
def test_openai_collector_spend_limit_malformed_payload_produces_no_row(payload: object) -> None:
    # Fail-closed: object != "organization.spend_limit", interval != "month",
    # enforcement.status not in {"inactive", "enforcing"}, currency != "USD"
    # (exact case -- "usd" is rejected, no implicit normalization), and
    # missing/non-finite/<=0 threshold_amount are all treated as contract
    # drift -- no row is produced (never fabricated or passed through).
    # threshold_amount must be a genuine JSON number: a numeric-looking
    # string ("10000") or a bool (True/False, an int subclass in Python)
    # must also be rejected, not coerced.
    class FakeSpendLimit(OpenAIUsageCostCollector):
        def _get_json(self, path, params):
            if path.endswith("/organization/spend_limit"):
                return payload
            return {"data": []}

    rows = FakeSpendLimit(api_key="test-key").collect()

    assert rows == []


@pytest.mark.parametrize("enforcement", ["not-a-dict", None, [], 123])
def test_openai_collector_spend_limit_malformed_enforcement_produces_no_row(enforcement: object) -> None:
    # A malformed (non-dict) `enforcement` field fails the fail-closed
    # enforcement.status check -- no row is produced. Must never crash.
    class FakeSpendLimit(OpenAIUsageCostCollector):
        def _get_json(self, path, params):
            if path.endswith("/organization/spend_limit"):
                return {**_VALID_SPEND_LIMIT_PAYLOAD, "enforcement": enforcement}
            return {"data": []}

    rows = FakeSpendLimit(api_key="test-key").collect()

    assert rows == []


def test_openai_collector_spend_limit_period_start_before_period_end() -> None:
    class FakeSpendLimit(OpenAIUsageCostCollector):
        def _get_json(self, path, params):
            if path.endswith("/organization/spend_limit"):
                return dict(_VALID_SPEND_LIMIT_PAYLOAD)
            return {"data": []}

    rows = FakeSpendLimit(api_key="test-key").collect()

    assert len(rows) == 1
    assert rows[0]["period_start"] < rows[0]["period_end"]


def test_openai_collector_spend_limit_401_error_message_is_generic(monkeypatch: pytest.MonkeyPatch) -> None:
    call_count = {"n": 0}

    def fake_urlopen(request, timeout=None):
        call_count["n"] += 1
        if request.full_url.endswith("/organization/spend_limit"):
            raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", None, io.BytesIO(b""))
        return _FakeResponse(json.dumps({"data": []}).encode("utf-8"))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    collector = OpenAIUsageCostCollector(api_key="test-key")
    with pytest.raises(OpenAIManagementAPIError) as exc_info:
        collector.collect()

    assert "401" in str(exc_info.value)
    assert "Admin" in str(exc_info.value)


def test_openai_collector_spend_limit_network_error_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    sensitive_detail = "some low-level socket/proxy detail that might be sensitive"

    def fake_urlopen(request, timeout=None):
        if request.full_url.endswith("/organization/spend_limit"):
            raise urllib.error.URLError(sensitive_detail)
        return _FakeResponse(json.dumps({"data": []}).encode("utf-8"))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    collector = OpenAIUsageCostCollector(api_key="test-key")
    with pytest.raises(Exception) as exc_info:
        collector.collect()

    assert sensitive_detail not in str(exc_info.value)


def test_openai_collector_never_calls_inference_endpoints_for_spend_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    # Regression guard alongside tests/test_no_paid_model_calls.py's static
    # scan: confirm at runtime that adding spend_limit support did not
    # introduce a call to any inference/generation endpoint.
    forbidden_fragments = ("/chat/completions", "/images/", "/audio/", "/embeddings", "/moderations")
    captured_urls: list[str] = []

    def fake_urlopen(request, timeout=None):
        captured_urls.append(request.full_url)
        return _FakeResponse(json.dumps({"data": []}).encode("utf-8"))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    OpenAIUsageCostCollector(api_key="test-key").collect()

    assert captured_urls
    for url in captured_urls:
        assert not any(fragment in url for fragment in forbidden_fragments)


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *args: object) -> bool:
        return False
