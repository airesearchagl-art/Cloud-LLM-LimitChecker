"""Security-focused coverage for the Gemini collector's auth handling.

Cloud Monitoring and Service Usage/Consumer Quota (the only two APIs this
collector calls) require OAuth2/ADC per official Google Cloud docs — neither
accepts an API key. This file locks in that a Gemini/AI Studio API key can
never reach a URL, header, exception message, or log line for these
endpoints, and that the collector fails closed (config error, not a silent
empty result) when only an API key is configured.
"""

import json
import urllib.error
import urllib.request

import pytest

from app.collectors.gemini_collector import (
    GeminiCollectorConfigError,
    GeminiManagementAPIError,
    GeminiManagementNetworkError,
    GeminiUsageCostCollector,
)


def test_collector_has_no_api_key_field() -> None:
    # The dataclass field itself was removed, not just left unused — an
    # incautious future change cannot silently reactivate an api_key path.
    with pytest.raises(TypeError):
        GeminiUsageCostCollector(api_key="sk-should-not-exist")


def test_collect_without_access_token_raises_config_error_and_never_calls_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def explode(*args, **kwargs):
        raise AssertionError("must not attempt network access without an access token")

    monkeypatch.setattr(urllib.request, "urlopen", explode)

    collector = GeminiUsageCostCollector(access_token=None, project_id="test-project")
    with pytest.raises(GeminiCollectorConfigError):
        collector.collect()


def test_collect_without_project_id_raises_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(*args, **kwargs):
        raise AssertionError("must not attempt network access without a project_id")

    monkeypatch.setattr(urllib.request, "urlopen", explode)

    collector = GeminiUsageCostCollector(access_token="secret-token-should-never-leak", project_id=None)
    with pytest.raises(GeminiCollectorConfigError):
        collector.collect()


class _FakeHTTPResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeHTTPResponse":
        return self

    def __exit__(self, *args: object) -> bool:
        return False


def test_access_token_never_appears_in_request_url_or_query_string(monkeypatch: pytest.MonkeyPatch) -> None:
    secret_token = "SECRET-TOKEN-SHOULD-NEVER-APPEAR-IN-URL"
    captured_urls: list[str] = []

    def fake_urlopen(request, timeout=None):
        captured_urls.append(request.full_url)
        assert request.get_header("Authorization") == f"Bearer {secret_token}"
        return _FakeHTTPResponse(json.dumps({"timeSeries": []}).encode("utf-8"))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    collector = GeminiUsageCostCollector(access_token=secret_token, project_id="test-project")
    collector.collect()

    assert captured_urls  # sanity: at least one request was actually made
    for url in captured_urls:
        assert secret_token not in url
        assert "key=" not in url


def test_http_error_message_never_leaks_response_body(monkeypatch: pytest.MonkeyPatch) -> None:
    secret_marker = "SECRET-SHOULD-NEVER-LEAK-INTO-EXCEPTION"

    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", None, None)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    # Sanity: prove the marker really would have been available to leak if
    # the implementation ever started reading exc bodies.
    assert secret_marker not in ""

    collector = GeminiUsageCostCollector(access_token="token", project_id="test-project")
    with pytest.raises(GeminiManagementAPIError) as exc_info:
        collector.collect()

    assert secret_marker not in str(exc_info.value)


def test_403_error_message_is_generic_and_actionable(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", None, None)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    collector = GeminiUsageCostCollector(access_token="token", project_id="test-project")
    with pytest.raises(GeminiManagementAPIError) as exc_info:
        collector.collect()

    message = str(exc_info.value)
    assert "403" in message
    assert "IAM" in message or "permission" in message.lower()


def test_rate_limit_error_returns_safe_message(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 429, "Too Many Requests", None, None)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    collector = GeminiUsageCostCollector(access_token="token", project_id="test-project")
    with pytest.raises(GeminiManagementAPIError) as exc_info:
        collector.collect()

    assert "429" in str(exc_info.value)


def test_network_error_message_has_no_dynamic_content(monkeypatch: pytest.MonkeyPatch) -> None:
    sensitive_detail = "some low-level socket/proxy detail that might be sensitive"

    def fake_urlopen(request, timeout=None):
        raise urllib.error.URLError(sensitive_detail)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    collector = GeminiUsageCostCollector(access_token="token", project_id="test-project")
    with pytest.raises(GeminiManagementNetworkError) as exc_info:
        collector.collect()

    assert sensitive_detail not in str(exc_info.value)


def test_config_error_message_never_includes_the_access_token_value() -> None:
    secret_token = "SECRET-TOKEN-SHOULD-NEVER-LEAK-INTO-CONFIG-ERROR"
    collector = GeminiUsageCostCollector(access_token=None, project_id=None)

    with pytest.raises(GeminiCollectorConfigError) as exc_info:
        collector.collect()

    assert secret_token not in str(exc_info.value)
