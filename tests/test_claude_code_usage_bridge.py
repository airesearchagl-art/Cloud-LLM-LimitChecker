import io
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.claude_code_usage_bridge import (
    FALLBACK_STATUS_LINE,
    extract_usage_record,
    format_status_line,
    main,
)
from app.claude_code_usage_cache import SCHEMA_VERSION, SOURCE_NAME

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def valid_payload(**overrides) -> dict:
    payload = {
        "rate_limits": {
            "five_hour": {"used_percentage": 42, "resets_at": 1798700000},
            "seven_day": {"used_percentage": 18, "resets_at": 1798900000},
        }
    }
    payload.update(overrides)
    return payload


# 1. five_hour / seven_day正常抽出
def test_extract_usage_record_normal_case() -> None:
    record = extract_usage_record(valid_payload(), now=NOW)
    assert record["schema_version"] == SCHEMA_VERSION
    assert record["source"] == SOURCE_NAME
    assert record["five_hour"]["used_percentage"] == 42.0
    assert record["seven_day"]["used_percentage"] == 18.0


# 2. remaining_percentage計算
def test_remaining_percentage_is_complement_of_used() -> None:
    record = extract_usage_record(valid_payload(), now=NOW)
    assert record["five_hour"]["remaining_percentage"] == 58.0
    assert record["seven_day"]["remaining_percentage"] == 82.0


# 3. 片方欠落
def test_extract_usage_record_one_window_missing() -> None:
    payload = {"rate_limits": {"five_hour": {"used_percentage": 42, "resets_at": 1798700000}}}
    record = extract_usage_record(payload, now=NOW)
    assert record["five_hour"] is not None
    assert record["seven_day"] is None


# 4. rate_limits全体欠落
def test_extract_usage_record_rate_limits_absent() -> None:
    record = extract_usage_record({}, now=NOW)
    assert record["five_hour"] is None
    assert record["seven_day"] is None


# 5. 最初のAPI応答前(rate_limitsキー自体がまだ存在しない典型的なペイロード形状)
def test_extract_usage_record_before_first_response() -> None:
    payload = {"model": {"display_name": "Claude"}, "workspace": {"current_dir": "/tmp"}}
    record = extract_usage_record(payload, now=NOW)
    assert record["five_hour"] is None
    assert record["seven_day"] is None
    assert format_status_line(record) == FALLBACK_STATUS_LINE


# 6. used_percentageが0
def test_used_percentage_zero_is_valid() -> None:
    payload = valid_payload()
    payload["rate_limits"]["five_hour"]["used_percentage"] = 0
    record = extract_usage_record(payload, now=NOW)
    assert record["five_hour"]["used_percentage"] == 0.0
    assert record["five_hour"]["remaining_percentage"] == 100.0


# 7. used_percentageが100
def test_used_percentage_hundred_is_valid() -> None:
    payload = valid_payload()
    payload["rate_limits"]["five_hour"]["used_percentage"] = 100
    record = extract_usage_record(payload, now=NOW)
    assert record["five_hour"]["used_percentage"] == 100.0
    assert record["five_hour"]["remaining_percentage"] == 0.0


# 8. 範囲外値
@pytest.mark.parametrize("bad_value", [-1, 101, 1000, -0.01])
def test_used_percentage_out_of_range_is_rejected(bad_value: float) -> None:
    payload = valid_payload()
    payload["rate_limits"]["five_hour"]["used_percentage"] = bad_value
    record = extract_usage_record(payload, now=NOW)
    assert record["five_hour"] is None


# 9. bool拒否
@pytest.mark.parametrize("bad_value", [True, False])
def test_used_percentage_bool_is_rejected(bad_value: bool) -> None:
    payload = valid_payload()
    payload["rate_limits"]["five_hour"]["used_percentage"] = bad_value
    record = extract_usage_record(payload, now=NOW)
    assert record["five_hour"] is None


# 10. resets_at不正
@pytest.mark.parametrize("bad_value", ["not-a-number", None, True, [], {}])
def test_resets_at_invalid_is_rejected(bad_value) -> None:
    payload = valid_payload()
    payload["rate_limits"]["five_hour"]["resets_at"] = bad_value
    record = extract_usage_record(payload, now=NOW)
    assert record["five_hour"] is None


# 11. stdin JSON不正
def test_main_handles_invalid_json_stdin(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    stdin = io.StringIO("{not valid json")
    exit_code = main(stdin=stdin, now=NOW, cache_path=tmp_path / "cache.json")
    assert exit_code == 0
    out = capsys.readouterr().out.strip()
    assert out == FALLBACK_STATUS_LINE


# 12. 必要外フィールドを保存しない
def test_record_excludes_fields_not_in_official_schema() -> None:
    payload = valid_payload(
        session_id="sess-abc123",
        transcript_path="/Users/me/.claude/transcripts/abc.jsonl",
        cwd="/Users/me/project",
        model={"display_name": "Claude Opus"},
        cost={"total_cost_usd": 1.23},
        context_window={"used_tokens": 12345},
    )
    record = extract_usage_record(payload, now=NOW)
    assert set(record.keys()) == {"schema_version", "source", "observed_at", "five_hour", "seven_day"}
    assert set(record["five_hour"].keys()) == {"used_percentage", "remaining_percentage", "resets_at"}


# 13. session_idを保存しない
def test_record_never_contains_session_id() -> None:
    payload = valid_payload(session_id="sess-super-secret-id")
    record = extract_usage_record(payload, now=NOW)
    serialized = json.dumps(record)
    assert "session_id" not in serialized
    assert "sess-super-secret-id" not in serialized


# 14. transcript_pathを保存しない
def test_record_never_contains_transcript_path() -> None:
    payload = valid_payload(transcript_path="/Users/me/.claude/transcripts/abc.jsonl")
    record = extract_usage_record(payload, now=NOW)
    serialized = json.dumps(record)
    assert "transcript_path" not in serialized
    assert "transcripts" not in serialized


# 15. token風文字列を保存しない
def test_record_never_contains_token_like_strings() -> None:
    payload = valid_payload(
        auth={"token": "sk-ant-api03-FAKESECRETVALUE1234567890"},
        api_key="sk-ant-api03-FAKESECRETVALUE1234567890",
    )
    record = extract_usage_record(payload, now=NOW)
    serialized = json.dumps(record)
    assert "sk-ant-api03-FAKESECRETVALUE1234567890" not in serialized
    assert "token" not in serialized
    assert "api_key" not in serialized


# 16. 原子的置換
def test_write_cache_atomic_replaces_file_and_leaves_no_temp_files(tmp_path: Path) -> None:
    from app.claude_code_usage_cache import write_cache_atomic

    cache_path = tmp_path / "nested" / "claude-code-usage.json"
    record_one = extract_usage_record(valid_payload(), now=NOW)
    write_cache_atomic(record_one, cache_path)

    payload_two = valid_payload()
    payload_two["rate_limits"]["five_hour"]["used_percentage"] = 99
    record_two = extract_usage_record(payload_two, now=NOW)
    write_cache_atomic(record_two, cache_path)

    assert cache_path.exists()
    on_disk = json.loads(cache_path.read_text(encoding="utf-8"))
    assert on_disk["five_hour"]["used_percentage"] == 99.0
    leftover_tmp_files = list(cache_path.parent.glob(".tmp-claude-code-usage-*"))
    assert leftover_tmp_files == []


# 17. 書き込み失敗
def test_main_does_not_raise_when_cache_write_fails(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    def explode(record, path):
        raise OSError("disk full (simulated)")

    monkeypatch.setattr("app.claude_code_usage_bridge.write_cache_atomic", explode)
    stdin = io.StringIO(json.dumps(valid_payload()))
    exit_code = main(stdin=stdin, now=NOW, cache_path=Path("unused"))
    assert exit_code == 0
    out = capsys.readouterr().out.strip()
    assert out == "Claude 5h: 42% used | 7d: 18% used"


# 22. observed_atがUTC aware
def test_observed_at_is_utc_aware_isoformat() -> None:
    record = extract_usage_record(valid_payload(), now=NOW)
    assert record["observed_at"] == "2026-01-01T12:00:00+00:00"


# 28. stdin原文をstdout/stderrへ出さない
def test_main_never_prints_raw_stdin(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    secret_marker = "sk-ant-api03-THIS-SHOULD-NEVER-BE-PRINTED"
    stdin = io.StringIO(f"not json at all, contains secret {secret_marker}")
    exit_code = main(stdin=stdin, now=NOW, cache_path=tmp_path / "cache.json")
    assert exit_code == 0
    captured = capsys.readouterr()
    assert secret_marker not in captured.out
    assert secret_marker not in captured.err


def test_format_status_line_examples() -> None:
    both = extract_usage_record(valid_payload(), now=NOW)
    assert format_status_line(both) == "Claude 5h: 42% used | 7d: 18% used"

    none_observed = extract_usage_record({}, now=NOW)
    assert format_status_line(none_observed) == FALLBACK_STATUS_LINE
