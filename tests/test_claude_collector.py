from datetime import datetime

from app.collectors.claude_collector import ClaudeUsageCostCollector
from app.time_utils import app_tz


class FakeClaudeUsageCostCollector(ClaudeUsageCostCollector):
    def _get_json(self, path, params):
        if path.endswith("/usage_report/messages"):
            return {
                "data": [
                    {
                        "model": "claude-test",
                        "input_tokens": 11,
                        "output_tokens": 6,
                        "requests": 3,
                        "ending_at": "2026-05-24",
                    }
                ]
            }
        return {
            "data": [
                {
                    "line_item": "Claude API",
                    "amount": {"value": 0.42, "currency": "usd"},
                    "ending_at": "2026-05-24",
                }
            ]
        }


def test_claude_collector_normalizes_mock_management_payloads() -> None:
    collector = FakeClaudeUsageCostCollector(
        api_key="test-key",
        organization_id="org-test",
        workspace_id="workspace-test",
    )

    rows = collector.collect(
        start_date=datetime(2026, 5, 23, tzinfo=app_tz()),
        end_date=datetime(2026, 5, 24, tzinfo=app_tz()),
    )

    assert len(rows) == 4
    assert {row["limit_type"] for row in rows} == {"input_tokens", "output_tokens", "requests", "api_cost"}
    assert rows[0]["service_provider"] == "Claude"
    assert rows[0]["source_type"] == "api_claude_management"
    assert rows[0]["organization_id"] == "org-test"
    assert rows[0]["workspace_id"] == "workspace-test"
