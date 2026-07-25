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


@dataclass(slots=True)
class ClaudeUsageCostCollector:
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
        params = {
            "starting_at": start.date().isoformat(),
            "ending_at": end.date().isoformat(),
        }
        usage_payload = self._get_json("/organizations/usage_report/messages", params)
        cost_payload = self._get_json("/organizations/cost_report", params)
        return self._normalize_usage(usage_payload) + self._normalize_costs(cost_payload)

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
                "Check Anthropic organization/workspace permissions for usage or billing APIs."
            )
        detail = exc.read().decode("utf-8", errors="replace").strip()
        if len(detail) > 240:
            detail = f"{detail[:240]}..."
        return f"Claude management API returned {exc.code}: {detail}"

    def _normalize_usage(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for item in self._items(payload):
            recorded_at = self._recorded_at(item)
            model_name = item.get("model") or item.get("model_name") or "claude_api"
            input_tokens = float(item.get("input_tokens") or item.get("input_token_count") or 0)
            output_tokens = float(item.get("output_tokens") or item.get("output_token_count") or 0)
            requests = float(item.get("requests") or item.get("request_count") or 0)
            if input_tokens:
                rows.append(self._row(model_name, "input_tokens", input_tokens, "tokens", recorded_at))
            if output_tokens:
                rows.append(self._row(model_name, "output_tokens", output_tokens, "tokens", recorded_at))
            if requests:
                rows.append(self._row(model_name, "requests", requests, "requests", recorded_at))
        return rows

    def _normalize_costs(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for item in self._items(payload):
            recorded_at = self._recorded_at(item)
            amount = item.get("amount") or item.get("cost") or item.get("total_cost") or {}
            if isinstance(amount, dict):
                value = float(amount.get("value") or amount.get("amount") or 0)
                currency = amount.get("currency") or "usd"
            else:
                value = float(amount or 0)
                currency = item.get("currency") or "usd"
            if value:
                model_name = item.get("line_item") or item.get("model") or "claude_api_cost"
                rows.append(self._row(model_name, "api_cost", value, currency, recorded_at))
        return rows

    def _items(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        for key in ("data", "results", "usage", "costs"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
        return []

    def _recorded_at(self, item: dict[str, Any]) -> str:
        value = item.get("ending_at") or item.get("date") or item.get("timestamp")
        if not value:
            return now_local().isoformat()
        if isinstance(value, int | float):
            return datetime.fromtimestamp(int(value), tz=app_tz()).isoformat()
        return str(value)

    def _row(
        self,
        model_name: str,
        limit_type: str,
        used_value: float,
        unit: str,
        recorded_at: str,
    ) -> dict[str, Any]:
        return normalized_record_to_dict(
            CollectorNormalizedRecord(
                vendor="claude",
                service_provider="Claude",
                model_name=model_name,
                limit_type=limit_type,
                used_value=used_value,
                unit=unit,
                recorded_at=recorded_at,
                source_type="api_claude_management",
                organization_id=self.organization_id,
                workspace_id=self.workspace_id,
                metadata={
                    "organization_id": self.organization_id,
                    "workspace_id": self.workspace_id,
                },
            )
        )
