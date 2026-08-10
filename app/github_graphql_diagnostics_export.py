"""CSV export helpers for the GitHub GraphQL Consumption Diagnostics feature.

This module has NO database dependency -- it only formats plain dict rows it
is given (fetched elsewhere) into CSV text. It mirrors app.exporter's
csv_cell/rows_to_csv pattern (do NOT modify app.exporter itself) but adds
Excel/Google Sheets formula-injection escaping, because two of the row
fields handled here (``label`` and ``repository``) originate from
user-supplied input (the POST /start request body) and must never reach a
spreadsheet cell unescaped.
"""

import csv
from io import StringIO
from typing import Any

_FORMULA_TRIGGER_PREFIXES = ("=", "+", "-", "@")


def safe_csv_cell(value: Any) -> str:
    """Coerce ``value`` to a CSV-safe string.

    Same base coercion as app.exporter.csv_cell: ``None`` -> ``""``,
    datetime-like values -> ``isoformat()``, everything else -> ``str()``.
    On top of that, if the resulting string would start with ``=``, ``+``,
    ``-``, or ``@`` (any of which a spreadsheet application may interpret as
    the start of a formula), a leading ``'`` is prefixed so the cell is
    always opened as plain text. This only ever escapes -- it never
    truncates or drops data.
    """
    if value is None:
        text = ""
    elif hasattr(value, "isoformat"):
        text = value.isoformat()
    else:
        text = str(value)

    if text.startswith(_FORMULA_TRIGGER_PREFIXES):
        return "'" + text
    return text


def rows_to_safe_csv(rows: list[dict], columns: list[str]) -> str:
    """Same shape as app.exporter.rows_to_csv, but using safe_csv_cell.

    DictWriter with extrasaction="ignore", "\\n" line endings, and a UTF-8
    BOM prefix for Excel compatibility -- matches the existing convention in
    app.exporter.rows_to_csv exactly, aside from the cell formatter used.
    """
    output = StringIO()
    writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({column: safe_csv_cell(row.get(column)) for column in columns})
    return "﻿" + output.getvalue()


GITHUB_GRAPHQL_SAMPLE_CSV_COLUMNS = [
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

GITHUB_GRAPHQL_SESSION_CSV_COLUMNS = [
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


def export_github_graphql_samples_csv(rows: list[dict]) -> str:
    """rows: plain dicts already fetched elsewhere (this module has no DB access)."""
    return rows_to_safe_csv(rows, GITHUB_GRAPHQL_SAMPLE_CSV_COLUMNS)


def export_github_graphql_sessions_csv(rows: list[dict]) -> str:
    """rows: plain dicts already fetched elsewhere (this module has no DB access).

    ``label``/``repository`` are user-supplied strings and are the primary
    reason safe_csv_cell (not app.exporter.csv_cell) is used here.
    """
    return rows_to_safe_csv(rows, GITHUB_GRAPHQL_SESSION_CSV_COLUMNS)
