import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from app.collectors.types import CollectorNormalizedRecord, normalized_record_to_dict
from app.time_utils import app_tz, now_local


class GeminiCollectorConfigError(RuntimeError):
    pass


class GeminiManagementAPIError(RuntimeError):
    pass


class GeminiManagementNetworkError(RuntimeError):
    pass


@dataclass(slots=True)
class GeminiUsageCostCollector:
    api_key: str | None = None
    access_token: str | None = None
    project_id: str | None = None
    monitoring_base_url: str = "https://monitoring.googleapis.com/v3"
    service_usage_base_url: str = "https://serviceusage.googleapis.com/v1"
    timeout_seconds: float = 20.0

    def collect(self, start_date: datetime | None = None, end_date: datetime | None = None) -> list[dict[str, Any]]:
        if not self.api_key and not self.access_token:
            raise GeminiCollectorConfigError("GEMINI_API_KEY or Google Cloud management credentials are not configured")
        if not self.project_id:
            return []
        end = end_date or now_local()
        start = start_date or (end - timedelta(days=1))
        rows: list[dict[str, Any]] = []
        if self.access_token:
            usage_payload = self._get_json(
                f"{self.monitoring_base_url}/projects/{urllib.parse.quote(self.project_id)}/timeSeries",
                {
                    "filter": 'metric.type="serviceruntime.googleapis.com/api/request_count" AND resource.labels.service="generativelanguage.googleapis.com"',
                    "interval.startTime": start.isoformat(),
                    "interval.endTime": end.isoformat(),
                    "view": "FULL",
                },
                bearer_token=self.access_token,
            )
            rows.extend(self._normalize_monitoring_usage(usage_payload))
            quota_payload = self._get_json(
                f"{self.service_usage_base_url}/projects/{urllib.parse.quote(self.project_id)}/services/generativelanguage.googleapis.com/consumerQuotaMetrics",
                {},
                bearer_token=self.access_token,
            )
            rows.extend(self._normalize_quota(quota_payload))
        return rows

    def _get_json(
        self,
        url: str,
        params: dict[str, Any],
        bearer_token: str | None = None,
    ) -> dict[str, Any]:
        query = urllib.parse.urlencode(params, doseq=True)
        if self.api_key and not bearer_token:
            query = urllib.parse.urlencode({**params, "key": self.api_key}, doseq=True)
        request_url = f"{url}?{query}" if query else url
        headers = {"Accept": "application/json"}
        if bearer_token:
            headers["Authorization"] = f"Bearer {bearer_token}"
        request = urllib.request.Request(request_url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raise GeminiManagementAPIError(self._http_error_message(exc)) from exc
        except urllib.error.URLError as exc:
            raise GeminiManagementNetworkError(f"Gemini management API network error: {exc.reason}") from exc
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise GeminiManagementAPIError("Gemini management API returned invalid JSON") from exc

    def _http_error_message(self, exc: urllib.error.HTTPError) -> str:
        if exc.code in {401, 403}:
            return (
                f"Gemini management API returned {exc.code}. "
                "Check Google Cloud project permissions for usage, quota, or billing APIs."
            )
        detail = exc.read().decode("utf-8", errors="replace").strip()
        if len(detail) > 240:
            detail = f"{detail[:240]}..."
        return f"Gemini management API returned {exc.code}: {detail}"

    def _normalize_monitoring_usage(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for series in payload.get("timeSeries", []):
            metric = series.get("metric", {})
            labels = metric.get("labels", {})
            model_name = labels.get("model") or labels.get("method") or "gemini_api"
            total = 0.0
            recorded_at = now_local().isoformat()
            for point in series.get("points", []):
                recorded_at = point.get("interval", {}).get("endTime") or recorded_at
                value = point.get("value", {})
                total += float(value.get("int64Value") or value.get("doubleValue") or 0)
            if total:
                rows.append(self._row(model_name, "requests", total, "requests", recorded_at))
        return rows

    def _normalize_quota(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for metric in payload.get("metrics", []):
            model_name = metric.get("metric") or "gemini_quota"
            for limit in metric.get("consumerQuotaLimits", []):
                limit_name = limit.get("metric") or limit.get("unit") or "quota"
                for bucket in limit.get("quotaBuckets", []):
                    value = float(bucket.get("effectiveLimit") or bucket.get("defaultLimit") or 0)
                    if value:
                        rows.append(self._row(model_name, limit_name, value, limit.get("unit") or "quota", now_local().isoformat()))
        return rows

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
                vendor="gemini",
                service_provider="Gemini",
                model_name=model_name,
                limit_type=limit_type,
                used_value=used_value,
                unit=unit,
                recorded_at=recorded_at,
                source_type="api_gemini_management",
                project_id=self.project_id,
                metadata={"project_id": self.project_id},
            )
        )
