import json
import math
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

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
# zero rows.
#
# Auth scope actually implemented: a caller-supplied OAuth2 bearer access
# token (`access_token`) only. This collector does NOT implement Application
# Default Credentials discovery, service-account impersonation, or token
# refresh — the caller (app/main.py, reading GOOGLE_CLOUD_ACCESS_TOKEN) is
# responsible for obtaining and refreshing a valid access token before
# calling collect(). Full ADC support (e.g. via google-auth's credential
# discovery chain) is a future candidate and would add a new dependency,
# which this PR deliberately does not introduce.
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
                "A Google Cloud OAuth2 access token is not configured (GOOGLE_CLOUD_ACCESS_TOKEN). "
                "Cloud Monitoring and Service Usage APIs require OAuth2 credentials "
                "(a Gemini/AI Studio API key alone cannot authenticate to these management endpoints). "
                "Application Default Credentials discovery is not implemented; a bearer token must be "
                "supplied directly and kept fresh by the caller."
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
        rows.extend(self._normalize_monitoring_usage(usage_payload))
        quota_payload = self._get_json(
            f"{self.service_usage_base_url}/projects/{urllib.parse.quote(self.project_id)}/services/generativelanguage.googleapis.com/consumerQuotaMetrics",
            {},
        )
        rows.extend(self._normalize_quota(quota_payload))
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
        except urllib.error.URLError:
            raise GeminiManagementNetworkError("Gemini management API network error")
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

    def _normalize_monitoring_usage(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        # One row per point, using that point's OWN interval as the period —
        # never the outer request's start/end window. A Cloud Monitoring
        # `timeSeries.list` response reports each point with its own
        # `interval.{startTime,endTime}`; substituting the query window for a
        # missing/invalid point interval would misrepresent an aggregated
        # value as if it belonged to a specific bucket it was never reported
        # against.
        rows: list[dict[str, Any]] = []
        for series in payload.get("timeSeries", []):
            if not isinstance(series, dict):
                continue
            metric = series.get("metric") or {}
            labels = metric.get("labels") or {}
            model_name = labels.get("model") or labels.get("method") or "gemini_api"
            points = series.get("points")
            if not isinstance(points, list):
                continue
            for point in points:
                if not isinstance(point, dict):
                    continue
                interval = point.get("interval") or {}
                period_start = interval.get("startTime") if isinstance(interval, dict) else None
                period_end = interval.get("endTime") if isinstance(interval, dict) else None
                value_obj = point.get("value") or {}
                total = self._safe_finite_float(value_obj.get("int64Value"))
                total += self._safe_finite_float(value_obj.get("doubleValue"))
                if total:
                    rows.append(
                        self._row(
                            model_name=model_name,
                            limit_type="requests",
                            metric_kind="usage",
                            used_value=total,
                            unit="requests",
                            period_start=period_start,
                            period_end=period_end,
                        )
                    )
        return rows

    def _normalize_quota(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        # Quota rows (a configured ceiling, not consumption history) are
        # tagged metric_kind="quota" and are never persisted as UsageRecord —
        # see PERSISTABLE_METRIC_KINDS in app/collectors/types.py and the
        # persistence policy in docs/vendor-collector-production-readiness.md.
        # consumerQuotaMetrics has no bucket/period concept in the API at all
        # (it is a point-in-time configuration snapshot, not a time series),
        # so there is no vendor-provided period to fabricate away from; using
        # the actual collection instant (not the request's start/end window)
        # as a zero-width-avoiding [now-1us, now) pair honestly represents
        # "this is the limit as observed right now" rather than disguising a
        # missing bucket as a real one.
        rows: list[dict[str, Any]] = []
        observed_at = now_local()
        period_start = observed_at - timedelta(microseconds=1)
        period_end = observed_at
        for metric in payload.get("metrics", []):
            if not isinstance(metric, dict):
                continue
            metric_name = metric.get("metric") or "gemini_quota"
            limits = metric.get("consumerQuotaLimits")
            if not isinstance(limits, list):
                continue
            for limit in limits:
                if not isinstance(limit, dict):
                    continue
                limit_name = limit.get("metric") or metric_name
                buckets = limit.get("quotaBuckets")
                if not isinstance(buckets, list):
                    continue
                for bucket in buckets:
                    if not isinstance(bucket, dict):
                        continue
                    value = self._safe_finite_float(bucket.get("effectiveLimit"))
                    if not value:
                        value = self._safe_finite_float(bucket.get("defaultLimit"))
                    if not value:
                        continue
                    rows.append(
                        self._row(
                            model_name=metric_name,
                            limit_type=limit_name,
                            metric_kind="quota",
                            used_value=value,
                            unit="quota_count",
                            period_start=period_start,
                            period_end=period_end,
                            metadata_extra={"raw_unit": limit.get("unit")},
                        )
                    )
        return rows

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
        period_start: Any,
        period_end: Any,
        metadata_extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # Returns a plain dict, not a validated CollectorNormalizedRecord —
        # period_start/period_end are passed through exactly as obtained
        # (a string, a datetime, or None), never fabricated or corrected
        # here. A missing/unparseable/naive/reversed period is left for
        # app.collectors.types.CollectorNormalizedRecord's own validation to
        # reject downstream.
        metadata = {"project_id": self.project_id}
        if metadata_extra:
            metadata.update(metadata_extra)
        recorded_at = period_end if isinstance(period_end, str) else (str(period_end) if period_end is not None else "")
        return {
            "vendor": "gemini",
            "service_provider": "Gemini",
            "model_name": model_name,
            "limit_type": limit_type,
            "metric_kind": metric_kind,
            "used_value": used_value,
            "unit": unit,
            "recorded_at": recorded_at,
            "period_start": period_start,
            "period_end": period_end,
            "source_type": "api_gemini_management",
            "project_id": self.project_id,
            "metadata": metadata,
        }
