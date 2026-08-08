import json
import math
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from app.time_utils import app_tz, now_local


class ClaudeCollectorConfigError(RuntimeError):
    pass


class ClaudeManagementAPIError(RuntimeError):
    pass


class ClaudeManagementNetworkError(RuntimeError):
    pass


_MAX_PAGES = 50


@dataclass(slots=True)
class ClaudeUsageCostCollector:
    # Must be an Admin API key (prefix sk-ant-admin01-, created under
    # Console -> Settings -> Admin keys by an org member with the admin
    # role), not a regular API key — the Usage/Cost Admin API requires it.
    # See docs/vendor-collector-production-readiness.md for the source.
    api_key: str
    organization_id: str | None = None
    workspace_id: str | None = None
    base_url: str = "https://api.anthropic.com/v1"
    anthropic_version: str = "2023-06-01"
    timeout_seconds: float = 20.0

    def collect(self, start_date: datetime | None = None, end_date: datetime | None = None) -> list[dict[str, Any]]:
        if not self.api_key:
            raise ClaudeCollectorConfigError("ANTHROPIC_API_KEY is not configured")
        end = end_date or now_local()
        start = start_date or (end - timedelta(days=1))
        base_params = {
            "starting_at": start.astimezone(app_tz()).isoformat(),
            "ending_at": end.astimezone(app_tz()).isoformat(),
            "bucket_width": "1d",
            "limit": "31",
        }
        usage_buckets = self._get_paginated_buckets(
            "/organizations/usage_report/messages",
            {**base_params, "group_by[]": ["model", "workspace_id"]},
        )
        cost_buckets = self._get_paginated_buckets(
            "/organizations/cost_report",
            {**base_params, "group_by[]": ["workspace_id", "description"]},
        )
        return self._normalize_usage(usage_buckets) + self._normalize_costs(cost_buckets)

    def _get_paginated_buckets(self, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        # Official response shape: {"data": [<bucket>, ...], "has_more": bool,
        # "next_page": <cursor>}. Each bucket is {"starting_at", "ending_at",
        # "results": [...]} — the actual usage/cost values live in
        # bucket["results"], never directly on the bucket. Pagination is
        # cursor-based: response `has_more` (bool) + `next_page` (opaque
        # cursor), passed back as the `page` query param.
        all_buckets: list[dict[str, Any]] = []
        page_params = dict(params)
        seen_pages: set[str] = set()
        for _ in range(_MAX_PAGES):
            payload = self._get_json(path, page_params)
            data = payload.get("data")
            if isinstance(data, list):
                all_buckets.extend(bucket for bucket in data if isinstance(bucket, dict))
            if not payload.get("has_more"):
                break
            next_page = payload.get("next_page")
            if not next_page or next_page in seen_pages:
                break
            seen_pages.add(next_page)
            page_params = {**params, "page": next_page}
        return all_buckets

    def _get_json(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base_url}{path}?{urllib.parse.urlencode(params, doseq=True)}"
        request = urllib.request.Request(
            url,
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": self.anthropic_version,
                "Accept": "application/json",
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raise ClaudeManagementAPIError(self._http_error_message(exc)) from exc
        except urllib.error.URLError:
            # Never include exc.reason — it can carry proxy/DNS/socket detail
            # from the local network stack that has no reason to be exposed
            # in an API response or collector_run.error_message.
            raise ClaudeManagementNetworkError("Claude management API network error")
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise ClaudeManagementAPIError("Claude management API returned invalid JSON") from exc

    def _http_error_message(self, exc: urllib.error.HTTPError) -> str:
        # Never echo the response body, request URL, or headers — a proxy or
        # a verbose vendor error page could plausibly include auth material
        # or otherwise sensitive detail. Only a fixed, generic message per
        # status code.
        if exc.code in {401, 403}:
            return (
                f"Claude management API returned {exc.code}. "
                "Check that ANTHROPIC_API_KEY is an organization Admin API key "
                "(sk-ant-admin01-...) with usage/cost report permissions."
            )
        if exc.code == 429:
            return "Claude management API rate limited the request (429)."
        return f"Claude management API returned {exc.code}."

    def _normalize_usage(self, buckets: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for bucket in buckets:
            period_start = bucket.get("starting_at")
            period_end = bucket.get("ending_at")
            recorded_at = self._recorded_at_label(bucket)
            results = bucket.get("results")
            if not isinstance(results, list):
                continue
            for result in results:
                if not isinstance(result, dict):
                    continue
                model_name = result.get("model") or "claude_api"
                # Official field names (see docs/vendor-collector-production-readiness.md):
                # uncached_input_tokens, cache_creation_input_tokens,
                # cache_read_input_tokens, output_tokens are reported as
                # separate counters, never combined into one generic
                # "input_tokens" figure.
                for limit_type, raw_value in (
                    ("input_tokens", result.get("uncached_input_tokens")),
                    ("output_tokens", result.get("output_tokens")),
                    ("cache_read_tokens", result.get("cache_read_input_tokens")),
                    ("cache_creation_tokens", result.get("cache_creation_input_tokens")),
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
                                workspace_id=result.get("workspace_id") or self.workspace_id,
                            )
                        )
        return rows

    def _normalize_costs(self, buckets: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for bucket in buckets:
            period_start = bucket.get("starting_at")
            period_end = bucket.get("ending_at")
            recorded_at = self._recorded_at_label(bucket)
            results = bucket.get("results")
            if not isinstance(results, list):
                continue
            for result in results:
                if not isinstance(result, dict):
                    continue
                # Cost amounts are reported as decimal strings in the lowest
                # currency unit (cents for USD) — parsed via a plain float
                # here since UsageRecord.used_value is a Float column; a
                # Decimal/fixed-point currency type would need a schema
                # migration (see the persistence policy doc's "Missing
                # requirements" section). Float is therefore an approximation
                # for local monitoring use, not an accounting source of truth.
                amount_raw = result.get("amount")
                currency = str(result.get("currency") or "usd").lower()
                cents = self._safe_finite_float(amount_raw)
                if not cents or currency != "usd":
                    continue
                value = cents / 100.0
                model_name = result.get("description") or result.get("workspace_id") or "claude_api_cost"
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
                        workspace_id=result.get("workspace_id") or self.workspace_id,
                    )
                )
        return rows

    @staticmethod
    def _recorded_at_label(bucket: dict[str, Any]) -> str:
        value = bucket.get("ending_at") or bucket.get("starting_at")
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
        workspace_id: str | None,
    ) -> dict[str, Any]:
        # Returns a plain dict, not a validated CollectorNormalizedRecord —
        # period_start/period_end are passed through exactly as the vendor
        # response provided them (a string, a unix timestamp, or None if the
        # bucket omitted them), never fabricated or corrected here. A
        # missing/unparseable/naive/reversed period is deliberately left for
        # app.collectors.types.CollectorNormalizedRecord's own validation to
        # reject downstream (surfaced as an "invalid_record" import outcome
        # for dry_run, or the existing whole-batch rollback for a real
        # import) — never silently substituted with a fabricated window.
        return {
            "vendor": "claude",
            "service_provider": "Claude",
            "model_name": model_name,
            "limit_type": limit_type,
            "metric_kind": metric_kind,
            "used_value": used_value,
            "unit": unit,
            "recorded_at": recorded_at,
            "period_start": period_start,
            "period_end": period_end,
            "bucket_width": "1d",
            "source_type": "api_claude_management",
            "organization_id": self.organization_id,
            "workspace_id": workspace_id,
            "metadata": {
                "organization_id": self.organization_id,
                "workspace_id": workspace_id,
            },
        }
