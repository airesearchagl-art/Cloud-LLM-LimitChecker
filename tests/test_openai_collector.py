from datetime import datetime

from app.collectors.openai_collector import OpenAIUsageCostCollector
from app.time_utils import app_tz


class FakeOpenAIUsageCostCollector(OpenAIUsageCostCollector):
    def _get_json(self, path, params):
        if path.endswith("/usage/completions"):
            return {
                "data": [
                    {
                        "start_time": 1763895600,
                        "end_time": 1763982000,
                        "results": [
                            {
                                "model": "gpt-test",
                                "input_tokens": 10,
                                "output_tokens": 5,
                                "num_model_requests": 2,
                                "project_id": "proj_test",
                            }
                        ],
                    }
                ]
            }
        return {
            "data": [
                {
                    "start_time": 1763895600,
                    "end_time": 1763982000,
                    "results": [
                        {
                            "amount": {"value": 0.12, "currency": "usd"},
                            "line_item": "Test line item",
                            "project_id": "proj_test",
                        }
                    ],
                }
            ]
        }


def test_openai_collector_normalizes_mock_usage_and_cost_payloads() -> None:
    collector = FakeOpenAIUsageCostCollector(api_key="test-key")

    rows = collector.collect(
        start_date=datetime(2026, 5, 23, tzinfo=app_tz()),
        end_date=datetime(2026, 5, 24, tzinfo=app_tz()),
    )

    assert len(rows) == 4
    assert {row["limit_type"] for row in rows} == {"input_tokens", "output_tokens", "requests", "api_cost"}
    assert rows[0]["source_type"] == "api_openai_management"
    assert rows[-1]["unit"] == "usd"
