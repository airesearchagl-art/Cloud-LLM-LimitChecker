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


@dataclass(slots=True)
class OpenAIUsageCostCollector:
    api_key: str
    base_url: str = "https://api.openai.com/v1"
    timeout_seconds: float = 20.0

    def collect(self, start_date: datetime | None = None, end_date: datetime | None = None) -> list[dict[str, Any]]:
        if not self.api_key:
            raise OpenAICollectorConfigError("OPENAI_API_KEY is not configured")
        end = end_date or now_local()
        start = start_date or (end - timedelta(days=1))
        usage_payload = self._get_json(
            "/organization/usage/completions",
            {
                "start_time": str(int(start.timestamp())),
                "end_time": str(int(end.timestamp())),
                "bucket_width": "1d",
                "limit": "31",
                "group_by": ["model", "project_id"],
            },
        )
        costs_payload = self._get_json(
            "/organization/costs",
            {
                "start_time": str(int(start.timestamp())),
                "end_time": str(int(end.timestamp())),
                "bucket_width": "1d",
                "limit": "31",
                "group_by": ["project_id", "line_item"],
            },
        )
        return self._normalize_usage(usage_payload) + self._normalize_costs(costs_payload)

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
                "Check organization/project permissions for usage/costs APIs."
            )
        detail = exc.read().decode("utf-8", errors="replace").strip()
        if len(detail) > 240:
            detail = f"{detail[:240]}..."
        return f"OpenAI management API returned {exc.code}: {detail}"

    def _normalize_usage(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for bucket in payload.get("data", []):
            recorded_at = self._bucket_recorded_at(bucket)
            for result in bucket.get("results", []):
                model_name = result.get("model") or "openai_api"
                input_tokens = float(result.get("input_tokens") or 0)
                output_tokens = float(result.get("output_tokens") or 0)
                requests = float(result.get("num_model_requests") or 0)
                project_id = result.get("project_id")
                if input_tokens:
                    rows.append(self._row(model_name, "input_tokens", input_tokens, "tokens", recorded_at, project_id))
                if output_tokens:
                    rows.append(self._row(model_name, "output_tokens", output_tokens, "tokens", recorded_at, project_id))
                if requests:
                    rows.append(self._row(model_name, "requests", requests, "requests", recorded_at, project_id))
        return rows

    def _normalize_costs(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for bucket in payload.get("data", []):
            recorded_at = self._bucket_recorded_at(bucket)
            for result in bucket.get("results", []):
                amount = result.get("amount") or {}
                value = float(amount.get("value") or 0)
                currency = amount.get("currency") or "usd"
                if not value:
                    continue
                model_name = result.get("line_item") or "openai_api_cost"
                rows.append(self._row(model_name, "api_cost", value, currency, recorded_at, result.get("project_id")))
        return rows

    def _bucket_recorded_at(self, bucket: dict[str, Any]) -> str:
        timestamp = bucket.get("end_time") or bucket.get("start_time")
        if timestamp is None:
            return now_local().isoformat()
        return datetime.fromtimestamp(int(timestamp), tz=app_tz()).isoformat()

    def _row(
        self,
        model_name: str,
        limit_type: str,
        used_value: float,
        unit: str,
        recorded_at: str,
        project_id: str | None,
    ) -> dict[str, Any]:
        return normalized_record_to_dict(
            CollectorNormalizedRecord(
                vendor="openai",
                service_provider="OpenAI",
                model_name=model_name,
                limit_type=limit_type,
                used_value=used_value,
                unit=unit,
                recorded_at=recorded_at,
                source_type="api_openai_management",
                project_id=project_id,
                metadata={"project_id": project_id},
            )
        )
