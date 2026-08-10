"""Tests for app/github_graphql_diagnostics_export.py.

Covers:
  - safe_csv_cell's formula-injection escaping (=, +, -, @ prefixes) and its
    parity with app.exporter.csv_cell's base coercion (None -> "", datetime
    -> isoformat, else str()).
  - export_github_graphql_samples_csv / export_github_graphql_sessions_csv
    produce a UTF-8 BOM-prefixed CSV with the exact documented header row.
  - The most important test in this file: a session row whose `label` or
    `repository` contains a formula-injection payload (typed by a user via
    the POST /start request body) is safely escaped, never passed through
    raw into the CSV cell.
  - No token/secret-looking string ever appears in produced CSV output.
"""

from datetime import datetime, timezone

import pytest

from app.exporter import csv_cell
from app.github_graphql_diagnostics_export import (
    GITHUB_GRAPHQL_SAMPLE_CSV_COLUMNS,
    GITHUB_GRAPHQL_SESSION_CSV_COLUMNS,
    export_github_graphql_samples_csv,
    export_github_graphql_sessions_csv,
    rows_to_safe_csv,
    safe_csv_cell,
)


# ---------------------------------------------------------------------------
# safe_csv_cell: formula-injection escaping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "=cmd|'/c calc'!A1",
        "+HYPERLINK(\"http://evil.example\")",
        "-2+3",
        "@SUM(A1:A9)",
    ],
)
def test_safe_csv_cell_escapes_formula_trigger_prefixes(raw):
    escaped = safe_csv_cell(raw)
    assert not escaped.startswith(("=", "+", "-", "@"))
    assert escaped.startswith("'")
    # データは失われない(先頭に安全文字を足すだけ)
    assert escaped[1:] == raw


def test_safe_csv_cell_does_not_escape_normal_values():
    assert safe_csv_cell("PR #25 review") == "PR #25 review"
    assert safe_csv_cell("owner/repo") == "owner/repo"
    assert safe_csv_cell(42) == "42"
    assert safe_csv_cell(0) == "0"


def test_safe_csv_cell_none_matches_csv_cell_behavior():
    assert safe_csv_cell(None) == csv_cell(None) == ""


def test_safe_csv_cell_datetime_matches_csv_cell_isoformat_style():
    dt = datetime(2026, 8, 10, 0, 5, 0, tzinfo=timezone.utc)
    # 両方ともisoformat()ベース(app.exporter.csv_cellはnaive datetimeにapp_tzを
    # 付与するが、tz-aware datetimeでは差が出ない -- ここではtz-aware値で比較する)。
    assert safe_csv_cell(dt) == dt.isoformat() == csv_cell(dt)


def test_safe_csv_cell_plain_value_matches_csv_cell_for_non_formula_strings():
    assert safe_csv_cell("hello") == csv_cell("hello")
    assert safe_csv_cell(123) == csv_cell(123)


# ---------------------------------------------------------------------------
# rows_to_safe_csv / export_*: BOM prefix + exact header row
# ---------------------------------------------------------------------------


def test_rows_to_safe_csv_has_utf8_bom_prefix():
    csv_text = rows_to_safe_csv([{"a": 1}], ["a"])
    assert csv_text.startswith("﻿")


def test_export_samples_csv_header_matches_documented_columns():
    csv_text = export_github_graphql_samples_csv([])
    header_line = csv_text.lstrip("﻿").split("\n", 1)[0]
    assert header_line == ",".join(GITHUB_GRAPHQL_SAMPLE_CSV_COLUMNS)


def test_export_sessions_csv_header_matches_documented_columns():
    csv_text = export_github_graphql_sessions_csv([])
    header_line = csv_text.lstrip("﻿").split("\n", 1)[0]
    assert header_line == ",".join(GITHUB_GRAPHQL_SESSION_CSV_COLUMNS)


def test_export_samples_csv_columns_exact_order():
    assert GITHUB_GRAPHQL_SAMPLE_CSV_COLUMNS == [
        "id",
        "collected_at",
        "core_used",
        "graphql_used",
        "search_used",
        "graphql_limit",
        "graphql_remaining",
        "graphql_reset_at",
        "graphql_delta",
        "fetch_status",
        "attribution_status",
        "trigger_session_id",
    ]


def test_export_sessions_csv_columns_exact_order():
    assert GITHUB_GRAPHQL_SESSION_CSV_COLUMNS == [
        "id",
        "actor_type",
        "label",
        "repository",
        "pr_number",
        "started_at",
        "ended_at",
        "github_login",
        "github_user_id",
        "reset_at_start",
        "graphql_used_start",
        "graphql_used_end",
        "graphql_delta_total",
        "attribution_status",
        "status",
        "stop_reason",
    ]


# ---------------------------------------------------------------------------
# The most important test: user-input formula-injection regression
# ---------------------------------------------------------------------------


def _sample_session_row(**overrides):
    row = {
        "id": 1,
        "actor_type": "claude_code",
        "label": "PR #25 review",
        "repository": "owner/repo",
        "pr_number": 25,
        "started_at": "2026-08-10T00:00:00+00:00",
        "ended_at": None,
        "github_login": "octocat",
        "github_user_id": 123,
        "reset_at_start": "2026-08-10T01:00:00+00:00",
        "graphql_used_start": 500,
        "graphql_used_end": None,
        "graphql_delta_total": None,
        "attribution_status": "SINGLE_ACTIVITY_CORRELATION",
        "status": "ACTIVE",
        "stop_reason": None,
    }
    row.update(overrides)
    return row


def test_session_row_with_formula_injection_label_is_escaped():
    row = _sample_session_row(label="=cmd|'/c calc'!A1")
    csv_text = export_github_graphql_sessions_csv([row])
    lines = csv_text.split("\n")
    data_line = lines[1]
    assert "'=cmd|" in data_line
    # 生の"=cmd|"がescapeされずセルの先頭に来ることはない
    assert ",=cmd|" not in data_line
    assert not data_line.startswith("=cmd|")


def test_session_row_with_formula_injection_repository_is_escaped():
    row = _sample_session_row(repository="+HYPERLINK(\"http://evil.example\",\"click\")")
    csv_text = export_github_graphql_sessions_csv([row])
    data_line = csv_text.split("\n")[1]
    assert "'+HYPERLINK(" in data_line
    assert ",+HYPERLINK(" not in data_line


def test_session_row_with_at_and_minus_prefixed_label_is_escaped():
    row_at = _sample_session_row(label="@SUM(1;2)")
    row_minus = _sample_session_row(label="-2+3+cmd|calc")
    for row in (row_at, row_minus):
        csv_text = export_github_graphql_sessions_csv([row])
        data_line = csv_text.split("\n")[1]
        cell = data_line.split(",")[2]  # id, actor_type, label
        assert cell.startswith("'")


def test_session_row_normal_label_and_repository_pass_through_unescaped():
    row = _sample_session_row(label="PR #25 review", repository="owner/repo")
    csv_text = export_github_graphql_sessions_csv([row])
    data_line = csv_text.split("\n")[1]
    assert "PR #25 review" in data_line
    assert "owner/repo" in data_line
    assert "'PR" not in data_line


def _sample_sample_row(**overrides):
    row = {
        "id": 10,
        "collected_at": "2026-08-10T00:05:00+00:00",
        "core_used": 20,
        "graphql_used": 530,
        "search_used": 0,
        "graphql_limit": 5000,
        "graphql_remaining": 4470,
        "graphql_reset_at": "2026-08-10T01:00:00+00:00",
        "graphql_delta": 30,
        "fetch_status": "ok",
        "attribution_status": "SINGLE_ACTIVITY_CORRELATION",
        "trigger_session_id": None,
    }
    row.update(overrides)
    return row


def test_sample_row_null_delta_and_trigger_session_render_as_empty_cell():
    row = _sample_sample_row(graphql_delta=None, trigger_session_id=None)
    csv_text = export_github_graphql_samples_csv([row])
    header, data_line = csv_text.lstrip("﻿").split("\n")[:2]
    columns = header.split(",")
    values = data_line.split(",")
    delta_idx = columns.index("graphql_delta")
    trigger_idx = columns.index("trigger_session_id")
    assert values[delta_idx] == ""
    assert values[trigger_idx] == ""


# ---------------------------------------------------------------------------
# No secrets/tokens ever appear in produced CSV output
# ---------------------------------------------------------------------------


def test_no_secret_looking_string_in_sample_csv():
    row = _sample_sample_row(fetch_status="ok")
    csv_text = export_github_graphql_samples_csv([row])
    for forbidden in ("ghp_", "gho_", "github_pat_", "Bearer ", "SECRET", "Traceback"):
        assert forbidden not in csv_text


def test_no_secret_looking_string_in_session_csv():
    row = _sample_session_row()
    csv_text = export_github_graphql_sessions_csv([row])
    for forbidden in ("ghp_", "gho_", "github_pat_", "Bearer ", "SECRET", "Traceback"):
        assert forbidden not in csv_text


def test_extraneous_row_keys_are_ignored_not_leaked():
    # extrasaction="ignore"であることを確認する -- rowにtoken等の余計なkeyが
    # 紛れ込んでいても、宣言済みcolumns以外は絶対に出力へ含まれない。
    row = _sample_session_row()
    row["access_token"] = "ghp_SECRETVALUE1234567890"
    csv_text = export_github_graphql_sessions_csv([row])
    assert "ghp_SECRETVALUE" not in csv_text
    assert "access_token" not in csv_text
