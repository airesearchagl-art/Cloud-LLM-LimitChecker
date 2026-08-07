import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from app.collectors.types import CollectorNormalizedRecord, normalized_record_to_dict
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
        usage_items = self._get_paginated_items(
            "/organizations/usage_report/messages",
            {**base_params, "group_by[]": ["model", "workspace_id"]},
        )
        cost_items = self._get_paginated_items(
            "/organizations/cost_report",
            {**base_params, "group_by[]": ["workspace_id", "description"]},
        )
        return self._normalize_usage(usage_items, start, end) + self._normalize_costs(cost_items, start, end)

    def _get_paginated_items(self, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        # Official pagination: response `has_more` (bool) + `next_page`
        # (opaque cursor), passed back as the `page` query param.
        all_items: list[dict[str, Any]] = []
        page_params = dict(params)
        seen_pages: set[str] = set()
        for _ in range(_MAX_PAGES):
            payload = self._get_json(path, page_params)
            all_items.extend(self._items(payload))
            if not payload.get("has_more"):
                break
            next_page = payload.get("next_page")
            if not next_page or next_page in seen_pages:
                break
            seen_pages.add(next_page)
            page_params = {**params, "page": next_page}
        return all_items

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
        except urllib.error.URLError as exc:
            raise ClaudeManagementNetworkError(f"Claude management API network error: {exc.reason}") from exc
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise ClaudeManagementAPIError("Claude management API returned invalid JSON") from exc

    def _http_error_message(self, exc: urllib.error.HTTPError) -> str:
        if exc.code in {401, 403}:
            return (
                f"Claude management API returned {exc.code}. "
                "Check that ANTHROPIC_API_KEY is an organization Admin API key "
                "(sk-ant-admin01-...) with usage/cost report permissions."
            )
        if exc.code == 429:
            return "Claude management API rate limited the request (429)."
        detail = exc.read().decode("utf-8", errors="replace").strip()
        if len(detail) > 240:
            detail = f"{detail[:240]}..."
        return f"Claude management API returned {exc.code}: {detail}"

    def _normalize_usage(
        self, items: list[dict[str, Any]], default_start: datetime, default_end: datetime
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            period_start, period_end = self._item_period(item, default_start, default_end)
            recorded_at = period_end.isoformat()
            model_name = item.get("model") or "claude_api"
            # Official field names (see docs/vendor-collector-production-readiness.md):
            # uncached_input_tokens, cache_creation_input_tokens,
            # cache_read_input_tokens, output_tokens are reported as separate
            # counters, never combined into one generic "input_tokens" figure.
            for limit_type, raw_value in (
                ("input_tokens", item.get("uncached_input_tokens")),
                ("output_tokens", item.get("output_tokens")),
                ("cache_read_tokens", item.get("cache_read_input_tokens")),
                ("cache_creation_tokens", item.get("cache_creation_input_tokens")),
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
                            workspace_id=item.get("workspace_id") or self.workspace_id,
                        )
                    )
        return rows

    def _normalize_costs(
        self, items: list[dict[str, Any]], default_start: datetime, default_end: datetime
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            period_start, period_end = self._item_period(item, default_start, default_end)
            recorded_at = period_end.isoformat()
            # Cost amounts are reported as decimal strings in the lowest
            # currency unit (cents for USD) — parsed via a plain float here
            # since UsageRecord.used_value is a Float column; a Decimal/
            # fixed-point currency type would need a schema migration (see
            # the persistence policy doc's "Missing requirements" section).
            amount_raw = item.get("amount")
            currency = str(item.get("currency") or "usd").lower()
            cents = self._safe_float(amount_raw)
            if not cents or currency != "usd":
                continue
            value = cents / 100.0
            model_name = item.get("description") or item.get("workspace_id") or "claude_api_cost"
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
                    workspace_id=item.get("workspace_id") or self.workspace_id,
                )
            )
        return rows

    def _items(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        for key in ("data", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
        return []

    def _item_period(
        self, item: dict[str, Any], default_start: datetime, default_end: datetime
    ) -> tuple[datetime, datetime]:
        start = self._parse_item_timestamp(item.get("starting_at")) or default_start
        end = self._parse_item_timestamp(item.get("ending_at")) or default_end
        if start >= end:
            end = start + timedelta(days=1)
        return start, end

    @staticmethod
    def _parse_item_timestamp(value: Any) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, int | float):
            return datetime.fromtimestamp(int(value), tz=app_tz())
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None

    @staticmethod
    def _safe_float(value: Any) -> float:
        if value is None:
            return 0.0
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

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
        workspace_id: str | None,
    ) -> dict[str, Any]:
        return normalized_record_to_dict(
            CollectorNormalizedRecord(
                vendor="claude",
                service_provider="Claude",
                model_name=model_name,
                limit_type=limit_type,
                metric_kind=metric_kind,
                used_value=used_value,
                unit=unit,
                recorded_at=recorded_at,
                period_start=period_start,
                period_end=period_end,
                bucket_width="1d",
                source_type="api_claude_management",
                organization_id=self.organization_id,
                workspace_id=workspace_id,
                metadata={
                    "organization_id": self.organization_id,
                    "workspace_id": workspace_id,
                },
            )
        )
