import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from app.collectors.types import CollectorNormalizedRecord, normalized_record_to_dict
from app.time_utils import app_tz, now_local


class OpenAICollectorConfigError(RuntimeError):
    pass


class OpenAIManagementAPIError(RuntimeError):
    pass


class OpenAIManagementNetworkError(RuntimeError):
    pass


# Maximum pages fetched per endpoint per collect() call. Bounds worst-case
# request count if a vendor response's next_page cursor is malformed/looping;
# paired with the seen-pages guard in _get_paginated_json.
_MAX_PAGES = 50


@dataclass(slots=True)
class OpenAIUsageCostCollector:
    # Must be an Organization Admin API key (created under Organization ->
    # Admin keys), not a regular/project API key — the official Usage and
    # Costs endpoints below require it. See
    # docs/vendor-collector-production-readiness.md for the source.
    api_key: str
    base_url: str = "https://api.openai.com/v1"
    timeout_seconds: float = 20.0

    def collect(self, start_date: datetime | None = None, end_date: datetime | None = None) -> list[dict[str, Any]]:
        if not self.api_key:
            raise OpenAICollectorConfigError("OPENAI_API_KEY is not configured")
        end = end_date or now_local()
        start = start_date or (end - timedelta(days=1))
        usage_buckets = self._get_paginated_data(
            "/organization/usage/completions",
            {
                "start_time": str(int(start.timestamp())),
                "end_time": str(int(end.timestamp())),
                "bucket_width": "1d",
                "limit": "31",
                "group_by": ["model", "project_id"],
            },
        )
        costs_buckets = self._get_paginated_data(
            "/organization/costs",
            {
                "start_time": str(int(start.timestamp())),
                "end_time": str(int(end.timestamp())),
                "bucket_width": "1d",
                "limit": "31",
                "group_by": ["project_id", "line_item"],
            },
        )
        return self._normalize_usage(usage_buckets) + self._normalize_costs(costs_buckets)

    def _get_paginated_data(self, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        # Official pagination: request param `page`, response field
        # `next_page` (cursor-based, per the Usage/Costs API docs). The
        # seen_pages guard prevents an infinite loop if a vendor response ever
        # echoes back the same cursor (a "duplicate page" response).
        all_data: list[dict[str, Any]] = []
        page_params = dict(params)
        seen_pages: set[str] = set()
        for _ in range(_MAX_PAGES):
            payload = self._get_json(path, page_params)
            all_data.extend(payload.get("data", []))
            next_page = payload.get("next_page")
            if not next_page or next_page in seen_pages:
                break
            seen_pages.add(next_page)
            page_params = {**params, "page": next_page}
        return all_data

    def _get_json(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base_url}{path}?{urllib.parse.urlencode(params, doseq=True)}"
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "application/json",
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = self._http_error_message(exc)
            raise OpenAIManagementAPIError(detail) from exc
        except urllib.error.URLError as exc:
            raise OpenAIManagementNetworkError(f"OpenAI management API network error: {exc.reason}") from exc
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise OpenAIManagementAPIError("OpenAI management API returned invalid JSON") from exc

    def _http_error_message(self, exc: urllib.error.HTTPError) -> str:
        if exc.code in {401, 403}:
            return (
                f"OpenAI management API returned {exc.code}. "
                "Check that OPENAI_API_KEY is an Organization Admin key with usage/costs permissions."
            )
        if exc.code == 429:
            return "OpenAI management API rate limited the request (429)."
        detail = exc.read().decode("utf-8", errors="replace").strip()
        if len(detail) > 240:
            detail = f"{detail[:240]}..."
        return f"OpenAI management API returned {exc.code}: {detail}"

    def _normalize_usage(self, buckets: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for bucket in buckets:
            recorded_at, period_start, period_end = self._bucket_window(bucket)
            for result in bucket.get("results", []):
                if not isinstance(result, dict):
                    continue
                model_name = result.get("model") or "openai_api"
                project_id = result.get("project_id")
                for limit_type, raw_value in (
                    ("input_tokens", result.get("input_tokens")),
                    ("output_tokens", result.get("output_tokens")),
                    ("cache_read_tokens", result.get("input_cached_tokens")),
                    ("requests", result.get("num_model_requests")),
                ):
                    value = self._safe_float(raw_value)
                    if value:
                        rows.append(
                            self._row(
                                model_name=model_name,
                                limit_type=limit_type,
                                metric_kind="usage",
                                used_value=value,
                                unit=limit_type,
                                recorded_at=recorded_at,
                                period_start=period_start,
                                period_end=period_end,
                                project_id=project_id,
                            )
                        )
        return rows

    def _normalize_costs(self, buckets: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for bucket in buckets:
            recorded_at, period_start, period_end = self._bucket_window(bucket)
            for result in bucket.get("results", []):
                if not isinstance(result, dict):
                    continue
                amount = result.get("amount")
                if not isinstance(amount, dict):
                    continue
                value = self._safe_float(amount.get("value"))
                # OpenAI's Costs API is USD-only today; a non-usd currency in
                # the response is unexpected and treated as unparseable rather
                # than silently mislabeled.
                currency = (amount.get("currency") or "usd").lower()
                if not value or currency != "usd":
                    continue
                model_name = result.get("line_item") or "openai_api_cost"
                rows.append(
                    self._row(
                        model_name=model_name,
                        limit_type="api_cost",
                        metric_kind="cost",
                        used_value=value,
                        unit="usd",
                        recorded_at=recorded_at,
                        period_start=period_start,
                        period_end=period_end,
                        project_id=result.get("project_id"),
                    )
                )
        return rows

    @staticmethod
    def _safe_float(value: Any) -> float:
        if value is None:
            return 0.0
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    def _bucket_window(self, bucket: dict[str, Any]) -> tuple[str, datetime, datetime]:
        start_ts = bucket.get("start_time")
        end_ts = bucket.get("end_time")
        if start_ts is None or end_ts is None:
            end_dt = now_local()
            return end_dt.isoformat(), end_dt - timedelta(days=1), end_dt
        try:
            period_start = datetime.fromtimestamp(int(start_ts), tz=app_tz())
            period_end = datetime.fromtimestamp(int(end_ts), tz=app_tz())
        except (TypeError, ValueError, OSError):
            end_dt = now_local()
            return end_dt.isoformat(), end_dt - timedelta(days=1), end_dt
        if period_start >= period_end:
            period_end = period_start + timedelta(seconds=1)
        return period_end.isoformat(), period_start, period_end

    def _row(
        self,
        *,
        model_name: str,
        limit_type: str,
        metric_kind: str,
        used_value: float,
        unit: str,
        recorded_at: str,
        period_start: datetime,
        period_end: datetime,
        project_id: str | None,
    ) -> dict[str, Any]:
        return normalized_record_to_dict(
            CollectorNormalizedRecord(
                vendor="openai",
                service_provider="OpenAI",
                model_name=model_name,
                limit_type=limit_type,
                metric_kind=metric_kind,
                used_value=used_value,
                unit=unit,
                recorded_at=recorded_at,
                period_start=period_start,
                period_end=period_end,
                bucket_width="1d",
                source_type="api_openai_management",
                project_id=project_id,
                metadata={"project_id": project_id},
            )
        )
