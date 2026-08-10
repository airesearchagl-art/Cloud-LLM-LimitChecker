"""Static content checks for start_dashboard.bat.

start_dashboard.bat is cp932-encoded with CRLF line endings (a Windows BAT
file), not UTF-8. It must be read as bytes and decoded with encoding="cp932"
-- reading it as plain text with the default encoding will raise or mangle
the embedded Japanese text. These tests never modify the file; they only
assert that the fix already applied by the Lead (conditionally adding
`--env-file .env` to the uvicorn invocation only when `.env` exists) is
present and that nothing else regressed.
"""

import subprocess
from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _bat_path() -> Path:
    return _repo_root() / "start_dashboard.bat"


def _bat_bytes() -> bytes:
    return _bat_path().read_bytes()


def _bat_text() -> str:
    return _bat_bytes().decode("cp932")


def test_env_file_arg_variable_is_declared() -> None:
    """The ENV_FILE_ARG variable must start empty (no --env-file by default)."""
    text = _bat_text()
    assert 'set "ENV_FILE_ARG="' in text


def test_env_file_arg_set_when_env_exists() -> None:
    """When .env exists, ENV_FILE_ARG is set to --env-file .env.

    The `if exist ".env" (` block must set ENV_FILE_ARG within a few lines
    so that uvicorn actually loads the .env file into the process
    environment when one is present.
    """
    text = _bat_text()
    marker = 'if exist ".env" ('
    assert marker in text
    idx = text.index(marker)
    nearby = text[idx : idx + 200]
    assert 'set "ENV_FILE_ARG=--env-file .env"' in nearby


def test_uvicorn_line_appends_env_file_arg_after_port() -> None:
    """The final uvicorn invocation must end with %ENV_FILE_ARG% after --port 8001."""
    text = _bat_text()
    uvicorn_lines = [
        line
        for line in text.splitlines()
        if "-m uvicorn app.main:app" in line
    ]
    assert len(uvicorn_lines) == 1
    uvicorn_line = uvicorn_lines[0].strip()
    assert uvicorn_line.endswith("--port 8001 %ENV_FILE_ARG%")


def test_missing_env_warning_block_preserved() -> None:
    """.env absent: the warning block must still exist, and the ASCII
    [WARNING] marker (not the Japanese wording) is asserted for encoding
    safety within this test file itself.
    """
    text = _bat_text()
    assert 'if not exist ".env" (' in text
    assert "[WARNING]" in text


def test_env_file_arg_does_not_hard_require_env() -> None:
    """Without .env, ENV_FILE_ARG stays empty ("set "ENV_FILE_ARG="" from
    test_env_file_arg_variable_is_declared), so %ENV_FILE_ARG% expands to
    nothing and the uvicorn command line is byte-for-byte identical to the
    pre-fix command line. This is what preserves .env-less startup as a
    supported (non-hard-required) mode -- the only thing conditionally
    appended is the flag itself, never a requirement that .env exist.
    """
    text = _bat_text()
    uvicorn_lines = [
        line
        for line in text.splitlines()
        if "-m uvicorn app.main:app" in line
    ]
    assert len(uvicorn_lines) == 1
    assert "%ENV_FILE_ARG%" in uvicorn_lines[0]
    # The flag is only ever produced through the conditional ENV_FILE_ARG
    # variable -- there is no unconditional "--env-file" text elsewhere.
    assert text.count("--env-file") == 1


def test_localhost_bind_preserved() -> None:
    text = _bat_text()
    assert "--host 127.0.0.1" in text
    assert "--port 8001" in text
    assert text.count("127.0.0.1:8001") >= 2


def test_no_reload_flag_added() -> None:
    """start_dashboard.bat is the non-reload 'daily use' launcher per
    README's dev-time section; --reload here would be a regression.
    """
    text = _bat_text()
    assert "--reload" not in text


def test_existing_server_detection_preserved() -> None:
    text = _bat_text()
    assert text.count("/api/health") >= 2
    assert "exit /b 0" in text


def test_venv_guard_preserved() -> None:
    text = _bat_text()
    assert ".venv\\Scripts\\python.exe" in text
    assert "[ERROR]" in text


def test_file_decodes_as_cp932() -> None:
    raw = _bat_bytes()
    # Must not raise.
    decoded = raw.decode("cp932")
    assert decoded


def test_file_uses_crlf_line_endings() -> None:
    raw = _bat_bytes()
    assert b"\r\n" in raw
    text = raw.decode("cp932")
    lines = text.split("\n")
    # Every line except a possible trailing blank line should carry a
    # trailing \r before the split on \n (i.e. no lone-LF endings mixed in).
    for line in lines[:-1]:
        assert line.endswith("\r")


def test_env_is_gitignored() -> None:
    """Regression guard: .env must stay git-ignored so credentials are
    never accidentally tracked. Read-only confirmation of existing
    .gitignore behavior -- this test does not modify .gitignore.
    """
    result = subprocess.run(
        ["git", "check-ignore", ".env"],
        cwd=_repo_root(),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0


def test_env_example_is_tracked() -> None:
    """Regression guard: .env.example must remain a tracked file so the
    documented `Copy-Item .env.example .env` setup flow keeps working.
    """
    result = subprocess.run(
        ["git", "ls-files", ".env.example"],
        cwd=_repo_root(),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert result.stdout.strip() != ""
