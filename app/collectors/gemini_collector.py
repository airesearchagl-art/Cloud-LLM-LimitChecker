import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from app.collectors.types import CollectorNormalizedRecord, normalized_record_to_dict
from app.time_utils import now_local


class GeminiCollectorConfigError(RuntimeError):
    pass


class GeminiManagementAPIError(RuntimeError):
    pass


class GeminiManagementNetworkError(RuntimeError):
    pass


# Security/auth decision (see docs/vendor-collector-production-readiness.md,
# "Gemini Security" section): official docs confirm neither Cloud Monitoring
# (monitoring.googleapis.com) nor Service Usage/Consumer Quota
# (serviceusage.googleapis.com) accept API-key auth at all — both require
# OAuth2/Application Default Credentials (docs.cloud.google.com/monitoring/api/authentication,
# docs.cloud.google.com/docs/quotas/view-manage). A Google AI Studio Gemini
# API key is scoped only to the generative Gemini API and cannot authenticate
# to either endpoint. This collector therefore never places an API key in a
# URL query string (or anywhere else) for these endpoints, and treats an
# API-key-only configuration as unconfigured rather than silently returning
# zero rows. Only an OAuth2/ADC access token is accepted for actually calling
# these APIs.
@dataclass(slots=True)
class GeminiUsageCostCollector:
    access_token: str | None = None
    project_id: str | None = None
    monitoring_base_url: str = "https://monitoring.googleapis.com/v3"
    service_usage_base_url: str = "https://serviceusage.googleapis.com/v1"
    timeout_seconds: float = 20.0

    def collect(self, start_date: datetime | None = None, end_date: datetime | None = None) -> list[dict[str, Any]]:
        if not self.access_token:
            raise GeminiCollectorConfigError(
                "Google Cloud OAuth2/ADC access token is not configured. "
                "Cloud Monitoring and Service Usage APIs require OAuth2 credentials "
                "(a Gemini/AI Studio API key alone cannot authenticate to these management endpoints)."
            )
        if not self.project_id:
            raise GeminiCollectorConfigError("GOOGLE_CLOUD_PROJECT (or GEMINI_PROJECT_ID) is not configured")
        end = end_date or now_local()
        start = start_date or (end - timedelta(days=1))
        rows: list[dict[str, Any]] = []
        usage_payload = self._get_json(
            f"{self.monitoring_base_url}/projects/{urllib.parse.quote(self.project_id)}/timeSeries",
            {
                "filter": 'metric.type="serviceruntime.googleapis.com/api/request_count" AND resource.labels.service="generativelanguage.googleapis.com"',
                "interval.startTime": start.isoformat(),
                "interval.endTime": end.isoformat(),
                "view": "FULL",
            },
        )
        rows.extend(self._normalize_monitoring_usage(usage_payload, start, end))
        quota_payload = self._get_json(
            f"{self.service_usage_base_url}/projects/{urllib.parse.quote(self.project_id)}/services/generativelanguage.googleapis.com/consumerQuotaMetrics",
            {},
        )
        rows.extend(self._normalize_quota(quota_payload, start, end))
        return rows

    def _get_json(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        # Only ever OAuth2 bearer-token auth — no API key is ever placed in a
        # query string or header here. See the module docstring above.
        query = urllib.parse.urlencode(params, doseq=True)
        request_url = f"{url}?{query}" if query else url
        headers = {"Accept": "application/json", "Authorization": f"Bearer {self.access_token}"}
        request = urllib.request.Request(request_url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raise GeminiManagementAPIError(self._http_error_message(exc)) from exc
        except urllib.error.URLError as exc:
            raise GeminiManagementNetworkError("Gemini management API network error") from exc
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise GeminiManagementAPIError("Gemini management API returned invalid JSON") from exc

    def _http_error_message(self, exc: urllib.error.HTTPError) -> str:
        # Never echo the request URL, headers, or raw response body — both
        # could plausibly (via a misconfigured proxy/redirect or a verbose
        # error page) include auth material, and the URL for these two
        # endpoints only ever varies by project_id anyway.
        if exc.code in {401, 403}:
            return (
                f"Gemini management API returned {exc.code}. "
                "Check Google Cloud project IAM permissions (Monitoring Viewer, "
                "Service Usage Consumer) for the OAuth2 access token in use."
            )
        if exc.code == 429:
            return "Gemini management API rate limited the request (429)."
        return f"Gemini management API returned {exc.code}."

    def _normalize_monitoring_usage(
        self, payload: dict[str, Any], start: datetime, end: datetime
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for series in payload.get("timeSeries", []):
            metric = series.get("metric", {})
            labels = metric.get("labels", {})
            model_name = labels.get("model") or labels.get("method") or "gemini_api"
            total = 0.0
            period_end = end
            for point in series.get("points", []):
                interval = point.get("interval", {})
                end_time = interval.get("endTime")
                if end_time:
                    parsed = self._parse_timestamp(end_time)
                    if parsed:
                        period_end = parsed
                value = point.get("value", {})
                total += self._safe_float(value.get("int64Value"))
                total += self._safe_float(value.get("doubleValue"))
            if total:
                rows.append(
                    self._row(
                        model_name=model_name,
                        limit_type="requests",
                        metric_kind="usage",
                        used_value=total,
                        unit="requests",
                        period_start=start,
                        period_end=max(period_end, start + timedelta(seconds=1)),
                    )
                )
        return rows

    def _normalize_quota(self, payload: dict[str, Any], start: datetime, end: datetime) -> list[dict[str, Any]]:
        # Quota rows (a configured ceiling, not consumption history) are
        # tagged metric_kind="quota" and are never persisted as UsageRecord —
        # see PERSISTABLE_METRIC_KINDS in app/collectors/types.py and the
        # persistence policy in docs/vendor-collector-production-readiness.md.
        rows: list[dict[str, Any]] = []
        for metric in payload.get("metrics", []):
            metric_name = metric.get("metric") or "gemini_quota"
            for limit in metric.get("consumerQuotaLimits", []):
                limit_name = limit.get("metric") or metric_name
                for bucket in limit.get("quotaBuckets", []):
                    value = self._safe_float(bucket.get("effectiveLimit"))
                    if not value:
                        value = self._safe_float(bucket.get("defaultLimit"))
                    if not value:
                        continue
                    rows.append(
                        self._row(
                            model_name=metric_name,
                            limit_type=limit_name,
                            metric_kind="quota",
                            used_value=value,
                            unit="quota_count",
                            period_start=start,
                            period_end=max(end, start + timedelta(seconds=1)),
                            metadata_extra={"raw_unit": limit.get("unit")},
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

    @staticmethod
    def _parse_timestamp(value: str) -> datetime | None:
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None

    def _row(
        self,
        *,
        model_name: str,
        limit_type: str,
        metric_kind: str,
        used_value: float,
        unit: str,
        period_start: datetime,
        period_end: datetime,
        metadata_extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        metadata = {"project_id": self.project_id}
        if metadata_extra:
            metadata.update(metadata_extra)
        return normalized_record_to_dict(
            CollectorNormalizedRecord(
                vendor="gemini",
                service_provider="Gemini",
                model_name=model_name,
                limit_type=limit_type,
                metric_kind=metric_kind,
                used_value=used_value,
                unit=unit,
                recorded_at=period_end.isoformat(),
                period_start=period_start,
                period_end=period_end,
                source_type="api_gemini_management",
                project_id=self.project_id,
                metadata=metadata,
            )
        )
