from datetime import datetime

from app.collectors.gemini_collector import GeminiUsageCostCollector
from app.time_utils import app_tz


class FakeGeminiUsageCostCollector(GeminiUsageCostCollector):
    def _get_json(self, url, params, bearer_token=None):
        if url.endswith("/timeSeries"):
            return {
                "timeSeries": [
                    {
                        "metric": {"labels": {"model": "gemini-test"}},
                        "points": [
                            {
                                "interval": {"endTime": "2026-05-24T00:00:00+09:00"},
                                "value": {"int64Value": "7"},
                            }
                        ],
                    }
                ]
            }
        return {
            "metrics": [
                {
                    "metric": "gemini_quota_metric",
                    "consumerQuotaLimits": [
                        {
                            "metric": "requests_per_day",
                            "unit": "1/d",
                            "quotaBuckets": [{"effectiveLimit": "1000"}],
                        }
                    ],
                }
            ]
        }


def test_gemini_collector_normalizes_mock_management_payloads() -> None:
    collector = FakeGeminiUsageCostCollector(
        api_key="test-key",
        access_token="test-token",
        project_id="test-project",
    )

    rows = collector.collect(
        start_date=datetime(2026, 5, 23, tzinfo=app_tz()),
        end_date=datetime(2026, 5, 24, tzinfo=app_tz()),
    )

    assert len(rows) == 2
    assert {row["limit_type"] for row in rows} == {"requests", "requests_per_day"}
    assert rows[0]["service_provider"] == "Gemini"
    assert rows[0]["source_type"] == "api_gemini_management"
    assert rows[0]["project_id"] == "test-project"
