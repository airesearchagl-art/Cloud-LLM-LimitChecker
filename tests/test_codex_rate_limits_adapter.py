import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app import codex_rate_limits_adapter as adapter

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "codex_rate_limits"


def load_fixture_result(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


def load_fixture_lines(name: str) -> list[str]:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


INIT_OK = {"id": 1, "result": {"userAgent": "fixture", "codexHome": "fixture"}}


def make_recv(messages: list[dict | None]):
    """A scripted `recv(timeout) -> dict|None` fake: pops the next pre-parsed
    message (or None for a simulated timeout/EOF) in order, ignoring `timeout`."""
    queue_ = list(messages)

    def recv(_timeout: float) -> dict | None:
        if not queue_:
            return None
        return queue_.pop(0)

    return recv


def make_send_recorder():
    sent: list[dict] = []

    def send(obj: dict) -> None:
        sent.append(obj)

    return send, sent


def rate_limits_response(result_payload: dict, response_id: int = 2) -> dict:
    return {"id": response_id, "result": result_payload}


# ---------------------------------------------------------------------------
# 1-3: initialize / initialized / account/rateLimits/read request shape
# ---------------------------------------------------------------------------


def test_initialize_request_shape():
    send, sent = make_send_recorder()
    recv = make_recv([INIT_OK, rate_limits_response(load_fixture_result("01_normal_primary_five_hour_secondary_weekly.json"))])

    adapter.run_json_rpc_session(send=send, recv=recv, now=NOW)

    init_request = sent[0]
    assert init_request["method"] == "initialize"
    assert init_request["id"] == 1
    assert init_request["params"]["clientInfo"]["name"] == "cloud-llm-limit-checker"
    assert "title" in init_request["params"]["clientInfo"]
    assert "version" in init_request["params"]["clientInfo"]


def test_initialized_notification_shape():
    send, sent = make_send_recorder()
    recv = make_recv([INIT_OK, rate_limits_response(load_fixture_result("01_normal_primary_five_hour_secondary_weekly.json"))])

    adapter.run_json_rpc_session(send=send, recv=recv, now=NOW)

    notification = sent[1]
    assert notification["method"] == "initialized"
    assert "id" not in notification


def test_rate_limits_read_request_shape():
    send, sent = make_send_recorder()
    recv = make_recv([INIT_OK, rate_limits_response(load_fixture_result("01_normal_primary_five_hour_secondary_weekly.json"))])

    adapter.run_json_rpc_session(send=send, recv=recv, now=NOW)

    rl_request = sent[2]
    assert rl_request["method"] == "account/rateLimits/read"
    assert rl_request["id"] == 2
    assert rl_request["params"] == {}


# ---------------------------------------------------------------------------
# 4-6: request id照合 / notification無視 / JSONL行単位parse (via fixtures)
# ---------------------------------------------------------------------------


def test_response_id_is_matched_ignoring_stray_ids():
    lines = load_fixture_lines("19_response_id_mismatch_lines.json")
    recv = make_recv([json.loads(line) for line in lines])
    send, _ = make_send_recorder()

    result = adapter.run_json_rpc_session(send=send, recv=recv, now=NOW)

    assert result.success is True
    assert result.windows["five_hour"]["used_percentage"] == 42.0


def test_notification_messages_are_ignored():
    lines = load_fixture_lines("18_notification_interleaved_lines.json")
    recv = make_recv([json.loads(line) for line in lines])
    send, _ = make_send_recorder()

    result = adapter.run_json_rpc_session(send=send, recv=recv, now=NOW)

    assert result.success is True
    # the notification's usedPercent (5.0) must never be picked up instead of
    # the real response's (42.0)
    assert result.windows["five_hour"]["used_percentage"] == 42.0


def test_fetch_codex_rate_limits_skips_non_json_lines(monkeypatch: pytest.MonkeyPatch):
    lines = load_fixture_lines("20_non_json_line_interleaved_lines.json")
    fake_proc = FakeProcess(stdout_lines=lines)
    monkeypatch.setattr(adapter.shutil, "which", lambda name: "codex")
    monkeypatch.setattr(adapter.subprocess, "Popen", lambda *a, **k: fake_proc)

    result = adapter.fetch_codex_rate_limits(now=NOW)

    assert result.success is True
    assert result.windows["five_hour"]["used_percentage"] == 42.0


# ---------------------------------------------------------------------------
# 7-9: primary/secondary位置非依存 / duration mapping
# ---------------------------------------------------------------------------


def test_primary_five_hour_secondary_weekly_mapping():
    send, _ = make_send_recorder()
    recv = make_recv([INIT_OK, rate_limits_response(load_fixture_result("01_normal_primary_five_hour_secondary_weekly.json"))])

    result = adapter.run_json_rpc_session(send=send, recv=recv, now=NOW)

    assert result.success is True
    assert result.windows["five_hour"]["window_duration_minutes"] == 300
    assert result.windows["weekly"]["window_duration_minutes"] == 10080


def test_swapped_primary_secondary_still_maps_correctly():
    send, _ = make_send_recorder()
    recv = make_recv([INIT_OK, rate_limits_response(load_fixture_result("02_swapped_primary_weekly_secondary_five_hour.json"))])

    result = adapter.run_json_rpc_session(send=send, recv=recv, now=NOW)

    assert result.success is True
    assert result.windows["five_hour"]["window_duration_minutes"] == 300
    assert result.windows["five_hour"]["used_percentage"] == 42.0
    assert result.windows["weekly"]["window_duration_minutes"] == 10080
    assert result.windows["weekly"]["used_percentage"] == 18.0


def test_300_minutes_maps_to_five_hour():
    send, _ = make_send_recorder()
    recv = make_recv([INIT_OK, rate_limits_response(load_fixture_result("03_five_hour_only.json"))])
    result = adapter.run_json_rpc_session(send=send, recv=recv, now=NOW)
    assert result.windows["five_hour"] is not None
    assert result.windows["weekly"] is None


def test_10080_minutes_maps_to_weekly():
    send, _ = make_send_recorder()
    recv = make_recv([INIT_OK, rate_limits_response(load_fixture_result("04_weekly_only.json"))])
    result = adapter.run_json_rpc_session(send=send, recv=recv, now=NOW)
    assert result.windows["weekly"] is not None
    assert result.windows["five_hour"] is None


# ---------------------------------------------------------------------------
# 10-11: remaining計算 / resetsAt UTC変換
# ---------------------------------------------------------------------------


def test_remaining_percentage_is_computed_from_used_percent():
    send, _ = make_send_recorder()
    recv = make_recv([INIT_OK, rate_limits_response(load_fixture_result("01_normal_primary_five_hour_secondary_weekly.json"))])
    result = adapter.run_json_rpc_session(send=send, recv=recv, now=NOW)
    assert result.windows["five_hour"]["remaining_percentage"] == 58.0
    assert result.windows["weekly"]["remaining_percentage"] == 82.0


def test_resets_at_is_converted_to_aware_utc_iso_string():
    send, _ = make_send_recorder()
    recv = make_recv([INIT_OK, rate_limits_response(load_fixture_result("01_normal_primary_five_hour_secondary_weekly.json"))])
    result = adapter.run_json_rpc_session(send=send, recv=recv, now=NOW)
    resets_at = result.windows["five_hour"]["resets_at"]
    parsed = datetime.fromisoformat(resets_at)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset().total_seconds() == 0


# ---------------------------------------------------------------------------
# 12-14: 片方欠落 / unknown duration無視 / duplicate duration拒否
# ---------------------------------------------------------------------------


def test_five_hour_only_is_partial_success():
    send, _ = make_send_recorder()
    recv = make_recv([INIT_OK, rate_limits_response(load_fixture_result("03_five_hour_only.json"))])
    result = adapter.run_json_rpc_session(send=send, recv=recv, now=NOW)
    assert result.success is True
    assert result.windows["five_hour"] is not None
    assert result.windows["weekly"] is None


def test_unknown_duration_is_dropped_not_an_error():
    send, _ = make_send_recorder()
    recv = make_recv([INIT_OK, rate_limits_response(load_fixture_result("10_duration_unknown.json"))])
    result = adapter.run_json_rpc_session(send=send, recv=recv, now=NOW)
    assert result.success is True
    assert result.windows["five_hour"] is None  # 60 minutes matches neither slot
    assert result.windows["weekly"] is not None  # 10080 still resolves


def test_duplicate_duration_is_ambiguous_response():
    send, _ = make_send_recorder()
    recv = make_recv([INIT_OK, rate_limits_response(load_fixture_result("11_duplicate_duration.json"))])
    result = adapter.run_json_rpc_session(send=send, recv=recv, now=NOW)
    assert result.success is False
    assert result.error_type == "ambiguous_response"


def test_both_windows_null_is_invalid_response():
    send, _ = make_send_recorder()
    recv = make_recv([INIT_OK, rate_limits_response(load_fixture_result("05_both_windows_null.json"))])
    result = adapter.run_json_rpc_session(send=send, recv=recv, now=NOW)
    assert result.success is False
    assert result.error_type == "invalid_response"


# ---------------------------------------------------------------------------
# 15-16: usedPercent/resetsAt不正
# ---------------------------------------------------------------------------


def test_used_percent_zero_is_accepted():
    send, _ = make_send_recorder()
    recv = make_recv([INIT_OK, rate_limits_response(load_fixture_result("06_used_percent_zero.json"))])
    result = adapter.run_json_rpc_session(send=send, recv=recv, now=NOW)
    assert result.success is True
    assert result.windows["five_hour"]["used_percentage"] == 0.0
    assert result.windows["five_hour"]["remaining_percentage"] == 100.0


def test_used_percent_hundred_is_accepted():
    send, _ = make_send_recorder()
    recv = make_recv([INIT_OK, rate_limits_response(load_fixture_result("07_used_percent_hundred.json"))])
    result = adapter.run_json_rpc_session(send=send, recv=recv, now=NOW)
    assert result.success is True
    assert result.windows["five_hour"]["used_percentage"] == 100.0
    assert result.windows["five_hour"]["remaining_percentage"] == 0.0


def test_used_percent_bool_makes_window_unusable():
    send, _ = make_send_recorder()
    recv = make_recv([INIT_OK, rate_limits_response(load_fixture_result("08_used_percent_bool.json"))])
    result = adapter.run_json_rpc_session(send=send, recv=recv, now=NOW)
    # only window present was invalid -> nothing left -> invalid_response
    assert result.success is False
    assert result.error_type == "invalid_response"


def test_used_percent_string_makes_window_unusable():
    send, _ = make_send_recorder()
    recv = make_recv([INIT_OK, rate_limits_response(load_fixture_result("09_used_percent_string.json"))])
    result = adapter.run_json_rpc_session(send=send, recv=recv, now=NOW)
    assert result.success is False
    assert result.error_type == "invalid_response"


def test_resets_at_invalid_makes_window_unusable():
    send, _ = make_send_recorder()
    recv = make_recv([INIT_OK, rate_limits_response(load_fixture_result("12_resets_at_invalid.json"))])
    result = adapter.run_json_rpc_session(send=send, recv=recv, now=NOW)
    assert result.success is False
    assert result.error_type == "invalid_response"


# ---------------------------------------------------------------------------
# 17: rateLimitResetCredits.creditsが非空object配列でもクラッシュしない
# ---------------------------------------------------------------------------


def test_nonempty_credits_object_array_does_not_crash():
    send, _ = make_send_recorder()
    recv = make_recv([INIT_OK, rate_limits_response(load_fixture_result("15_rate_limit_reset_credits_nonempty_objects.json"))])
    result = adapter.run_json_rpc_session(send=send, recv=recv, now=NOW)
    assert result.success is True
    assert result.windows["five_hour"]["used_percentage"] == 42.0


def test_empty_credits_list_does_not_crash():
    send, _ = make_send_recorder()
    recv = make_recv([INIT_OK, rate_limits_response(load_fixture_result("14_rate_limit_reset_credits_empty_list.json"))])
    result = adapter.run_json_rpc_session(send=send, recv=recv, now=NOW)
    assert result.success is True


# ---------------------------------------------------------------------------
# 18: rateLimitsByLimitIdを無視
# ---------------------------------------------------------------------------


def test_rate_limits_by_limit_id_is_ignored():
    send, _ = make_send_recorder()
    recv = make_recv([INIT_OK, rate_limits_response(load_fixture_result("13_rate_limits_by_limit_id_present.json"))])
    result = adapter.run_json_rpc_session(send=send, recv=recv, now=NOW)
    assert result.success is True
    assert result.windows["five_hour"]["used_percentage"] == 42.0


# ---------------------------------------------------------------------------
# 19: unknown fieldを無視
# ---------------------------------------------------------------------------


def test_unknown_top_level_field_is_ignored():
    send, _ = make_send_recorder()
    recv = make_recv([INIT_OK, rate_limits_response(load_fixture_result("16_unknown_field_present.json"))])
    result = adapter.run_json_rpc_session(send=send, recv=recv, now=NOW)
    assert result.success is True


# JSON-RPC error -> method_not_found classification
def test_json_rpc_error_classifies_method_not_found():
    error_response = load_fixture_result("17_json_rpc_error_response.json")
    send, _ = make_send_recorder()
    recv = make_recv([INIT_OK, error_response])
    result = adapter.run_json_rpc_session(send=send, recv=recv, now=NOW)
    assert result.success is False
    assert result.error_type == "method_not_found"


def test_json_rpc_error_message_never_surfaces_in_user_message():
    error_response = load_fixture_result("17_json_rpc_error_response.json")
    send, _ = make_send_recorder()
    recv = make_recv([INIT_OK, error_response])
    result = adapter.run_json_rpc_session(send=send, recv=recv, now=NOW)
    assert "account/rateLimits/read" not in (result.user_message or "")


def test_authentication_unavailable_classification():
    error_response = {"id": 2, "error": {"code": -32000, "message": "request requires ChatGPT authentication"}}
    send, _ = make_send_recorder()
    recv = make_recv([INIT_OK, error_response])
    result = adapter.run_json_rpc_session(send=send, recv=recv, now=NOW)
    assert result.error_type == "authentication_unavailable"


def test_generic_rpc_error_is_protocol_error():
    error_response = {"id": 2, "error": {"code": -32603, "message": "internal error"}}
    send, _ = make_send_recorder()
    recv = make_recv([INIT_OK, error_response])
    result = adapter.run_json_rpc_session(send=send, recv=recv, now=NOW)
    assert result.error_type == "protocol_error"


def test_initialize_error_response():
    send, _ = make_send_recorder()
    recv = make_recv([{"id": 1, "error": {"code": -32603, "message": "boom"}}])
    result = adapter.run_json_rpc_session(send=send, recv=recv, now=NOW)
    assert result.success is False
    assert result.error_type == "initialize_error"


def test_initialize_timeout_when_no_response():
    send, _ = make_send_recorder()
    recv = make_recv([None])
    result = adapter.run_json_rpc_session(send=send, recv=recv, now=NOW)
    assert result.success is False
    assert result.error_type == "initialize_timeout"


def test_rate_limits_timeout_when_no_response():
    send, _ = make_send_recorder()
    recv = make_recv([INIT_OK, None])
    result = adapter.run_json_rpc_session(send=send, recv=recv, now=NOW)
    assert result.success is False
    assert result.error_type == "rate_limits_timeout"


def test_invalid_response_when_result_not_a_dict():
    send, _ = make_send_recorder()
    recv = make_recv([INIT_OK, {"id": 2, "result": "not-a-dict"}])
    result = adapter.run_json_rpc_session(send=send, recv=recv, now=NOW)
    assert result.error_type == "invalid_response"


def test_invalid_response_when_rate_limits_key_missing():
    send, _ = make_send_recorder()
    recv = make_recv([INIT_OK, {"id": 2, "result": {"somethingElse": True}}])
    result = adapter.run_json_rpc_session(send=send, recv=recv, now=NOW)
    assert result.error_type == "invalid_response"


# ---------------------------------------------------------------------------
# Process-level: startup failure, executable resolution, terminate/kill,
# no residual process. `fetch_codex_rate_limits` never resolves via a
# hardcoded path — it goes through `shutil.which`.
# ---------------------------------------------------------------------------


class FakeStdin:
    def __init__(self) -> None:
        self.writes: list[str] = []
        self.closed = False

    def write(self, data: str) -> None:
        self.writes.append(data)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class FakeProcess:
    def __init__(
        self,
        stdout_lines: list[str],
        wait_raises_first: bool = False,
        terminate_raises: bool = False,
        never_exits: bool = False,
    ) -> None:
        self.stdin = FakeStdin()
        self.stdout = iter(stdout_lines)
        self._wait_raises_first = wait_raises_first
        self._wait_call_count = 0
        self._terminate_raises = terminate_raises
        self._never_exits = never_exits
        self.terminate_called = False
        self.kill_called = False
        self._exited = False

    def wait(self, timeout: float | None = None) -> int:
        self._wait_call_count += 1
        if self._never_exits and not (self.terminate_called or self.kill_called):
            raise subprocess.TimeoutExpired(cmd="codex", timeout=timeout or 0)
        if self._wait_raises_first and self._wait_call_count == 1:
            raise subprocess.TimeoutExpired(cmd="codex", timeout=timeout or 0)
        self._exited = True
        return 0

    def terminate(self) -> None:
        self.terminate_called = True
        if self._terminate_raises:
            raise OSError("cannot terminate")

    def kill(self) -> None:
        self.kill_called = True
        self._exited = True

    def poll(self) -> int | None:
        return 0 if self._exited else None


def test_executable_not_found(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(adapter.shutil, "which", lambda name: None)
    result = adapter.fetch_codex_rate_limits(now=NOW)
    assert result.success is False
    assert result.error_type == "executable_not_found"


def test_process_start_failed(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(adapter.shutil, "which", lambda name: "codex")

    def explode(*args, **kwargs):
        raise OSError("cannot start")

    monkeypatch.setattr(adapter.subprocess, "Popen", explode)
    result = adapter.fetch_codex_rate_limits(now=NOW)
    assert result.success is False
    assert result.error_type == "process_start_failed"


def test_executable_is_resolved_via_which_not_hardcoded(monkeypatch: pytest.MonkeyPatch):
    captured = {}
    monkeypatch.setattr(adapter.shutil, "which", lambda name: "/resolved/path/codex")

    def fake_popen(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return FakeProcess(
            stdout_lines=[
                json.dumps(INIT_OK),
                json.dumps(rate_limits_response(load_fixture_result("03_five_hour_only.json"))),
            ]
        )

    monkeypatch.setattr(adapter.subprocess, "Popen", fake_popen)
    adapter.fetch_codex_rate_limits(now=NOW)

    assert captured["args"][0] == "/resolved/path/codex"
    assert captured["args"][1:] == ["app-server", "--stdio"]
    assert captured["kwargs"]["shell"] is False
    assert captured["kwargs"]["stderr"] == subprocess.DEVNULL


def test_process_terminates_cleanly_on_success(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(adapter.shutil, "which", lambda name: "codex")
    fake_proc = FakeProcess(
        stdout_lines=[
            json.dumps(INIT_OK),
            json.dumps(rate_limits_response(load_fixture_result("03_five_hour_only.json"))),
        ]
    )
    monkeypatch.setattr(adapter.subprocess, "Popen", lambda *a, **k: fake_proc)

    result = adapter.fetch_codex_rate_limits(now=NOW)

    assert result.success is True
    assert fake_proc.stdin.closed is True
    assert fake_proc.poll() is not None


def test_process_falls_back_to_terminate_then_kill(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(adapter.shutil, "which", lambda name: "codex")
    fake_proc = FakeProcess(
        stdout_lines=[
            json.dumps(INIT_OK),
            json.dumps(rate_limits_response(load_fixture_result("03_five_hour_only.json"))),
        ],
        never_exits=True,
    )
    monkeypatch.setattr(adapter.subprocess, "Popen", lambda *a, **k: fake_proc)

    result = adapter.fetch_codex_rate_limits(now=NOW)

    assert fake_proc.terminate_called is True
    assert fake_proc.poll() is not None
    # data was already parsed successfully before shutdown; the extra
    # terminate/kill effort should not discard a good result.
    assert result.success is True


def test_process_exit_failed_when_process_cannot_be_confirmed_terminated(monkeypatch: pytest.MonkeyPatch):
    class StubbornProcess(FakeProcess):
        """`wait()` always times out and `kill()` never actually exits —
        simulates a process that cannot be confirmed terminated at all."""

        def wait(self, timeout: float | None = None) -> int:
            raise subprocess.TimeoutExpired(cmd="codex", timeout=timeout or 0)

        def kill(self) -> None:
            self.kill_called = True  # deliberately does NOT set `_exited`

        def poll(self) -> int | None:
            return None

    monkeypatch.setattr(adapter.shutil, "which", lambda name: "codex")
    fake_proc = StubbornProcess(
        stdout_lines=[
            json.dumps(INIT_OK),
            json.dumps(rate_limits_response(load_fixture_result("03_five_hour_only.json"))),
        ],
    )
    monkeypatch.setattr(adapter.subprocess, "Popen", lambda *a, **k: fake_proc)

    result = adapter.fetch_codex_rate_limits(now=NOW)

    assert fake_proc.terminate_called is True
    assert fake_proc.kill_called is True
    assert result.success is False
    assert result.error_type == "process_exit_failed"


def test_unexpected_exception_during_session_does_not_escape(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(adapter.shutil, "which", lambda name: "codex")

    def broken_popen(*args, **kwargs):
        fake_proc = FakeProcess(stdout_lines=[])

        def broken_write(data):
            raise RuntimeError("boom")

        fake_proc.stdin.write = broken_write
        return fake_proc

    monkeypatch.setattr(adapter.subprocess, "Popen", broken_popen)
    result = adapter.fetch_codex_rate_limits(now=NOW)
    assert result.success is False
    assert result.error_type == "unknown_error"


# ---------------------------------------------------------------------------
# stdout/stderr非保持 / 固定user_message
# ---------------------------------------------------------------------------


def test_stderr_is_always_devnull(monkeypatch: pytest.MonkeyPatch):
    captured = {}
    monkeypatch.setattr(adapter.shutil, "which", lambda name: "codex")

    def fake_popen(args, **kwargs):
        captured["kwargs"] = kwargs
        return FakeProcess(
            stdout_lines=[
                json.dumps(INIT_OK),
                json.dumps(rate_limits_response(load_fixture_result("03_five_hour_only.json"))),
            ]
        )

    monkeypatch.setattr(adapter.subprocess, "Popen", fake_popen)
    adapter.fetch_codex_rate_limits(now=NOW)
    assert captured["kwargs"]["stderr"] == subprocess.DEVNULL


def test_all_error_types_have_fixed_generic_user_messages():
    for error_type, message in adapter._USER_MESSAGES.items():
        assert isinstance(message, str)
        assert len(message) > 0
        # generic messages never look like they contain identifiers or raw JSON
        assert "{" not in message
        assert "account/rateLimits" not in message or error_type == "method_not_found"
