import json
import math
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from app.time_utils import now_local


class OpenAICollectorConfigError(RuntimeError):
    pass


class OpenAIManagementAPIError(RuntimeError):
    pass


class OpenAIManagementNetworkError(RuntimeError):
    pass


# Maximum pages fetched per endpoint per collect() call. Bounds worst-case
# request count if a vendor response's next_page cursor is malformed/looping;
# paired with the seen-pages guard in _get_paginated_data.
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
            data = payload.get("data")
            if isinstance(data, list):
                all_data.extend(bucket for bucket in data if isinstance(bucket, dict))
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
            raise OpenAIManagementAPIError(self._http_error_message(exc)) from exc
        except urllib.error.URLError:
            # Never include exc.reason — it can carry proxy/DNS/socket detail
            # from the local network stack that has no reason to be exposed
            # in an API response or collector_run.error_message.
            raise OpenAIManagementNetworkError("OpenAI management API network error")
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise OpenAIManagementAPIError("OpenAI management API returned invalid JSON") from exc

    def _http_error_message(self, exc: urllib.error.HTTPError) -> str:
        # Never echo the response body, request URL, or headers — a proxy or
        # a verbose vendor error page could plausibly include auth material
        # or otherwise sensitive detail. Only a fixed, generic message per
        # status code.
        if exc.code in {401, 403}:
            return (
                f"OpenAI management API returned {exc.code}. "
                "Check that OPENAI_API_KEY is an Organization Admin key with usage/costs permissions."
            )
        if exc.code == 429:
            return "OpenAI management API rate limited the request (429)."
        return f"OpenAI management API returned {exc.code}."

    def _normalize_usage(self, buckets: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for bucket in buckets:
            period_start = bucket.get("start_time")
            period_end = bucket.get("end_time")
            recorded_at = self._recorded_at_label(bucket)
            results = bucket.get("results")
            if not isinstance(results, list):
                continue
            for result in results:
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
                    value = self._safe_finite_float(raw_value)
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
            period_start = bucket.get("start_time")
            period_end = bucket.get("end_time")
            recorded_at = self._recorded_at_label(bucket)
            results = bucket.get("results")
            if not isinstance(results, list):
                continue
            for result in results:
                if not isinstance(result, dict):
                    continue
                amount = result.get("amount")
                if not isinstance(amount, dict):
                    continue
                value = self._safe_finite_float(amount.get("value"))
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
    def _recorded_at_label(bucket: dict[str, Any]) -> str:
        value = bucket.get("end_time")
        if value is None:
            value = bucket.get("start_time")
        return str(value) if value is not None else ""

    @staticmethod
    def _safe_finite_float(value: Any) -> float:
        if value is None:
            return 0.0
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return 0.0
        if not math.isfinite(parsed):
            return 0.0
        return parsed

    def _row(
        self,
        *,
        model_name: str,
        limit_type: str,
        metric_kind: str,
        used_value: float,
        unit: str,
        recorded_at: str,
        period_start: Any,
        period_end: Any,
        project_id: str | None,
    ) -> dict[str, Any]:
        # Returns a plain dict, not a validated CollectorNormalizedRecord —
        # period_start/period_end (unix timestamps per the official response
        # shape, or None if the bucket omitted them) are passed through
        # exactly as received, never fabricated or corrected here. A
        # missing/unparseable/reversed period is left for
        # app.collectors.types.CollectorNormalizedRecord's own validation to
        # reject downstream (surfaced as an "invalid_record" import outcome
        # for dry_run, or the existing whole-batch rollback for a real
        # import).
        return {
            "vendor": "openai",
            "service_provider": "OpenAI",
            "model_name": model_name,
            "limit_type": limit_type,
            "metric_kind": metric_kind,
            "used_value": used_value,
            "unit": unit,
            "recorded_at": recorded_at,
            "period_start": period_start,
            "period_end": period_end,
            "bucket_width": "1d",
            "source_type": "api_openai_management",
            "project_id": project_id,
            "metadata": {"project_id": project_id},
        }
