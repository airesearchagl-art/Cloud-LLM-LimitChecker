import pytest

from app.github_graphql_diagnostics_config import (
    DEFAULT_MAX_MINUTES,
    DEFAULT_SAMPLE_SECONDS,
    MIN_SAMPLE_SECONDS,
    diagnostics_enabled_from_env,
    max_minutes_from_env,
    sample_seconds_from_env,
)


# -- diagnostics_enabled_from_env --------------------------------------------


def test_enabled_default_false():
    assert diagnostics_enabled_from_env({}) is False


@pytest.mark.parametrize("raw", ["1", "true", "True", "TRUE", "yes", "YES", "on", "ON"])
def test_enabled_true_values(raw):
    assert diagnostics_enabled_from_env({"GITHUB_GRAPHQL_DIAGNOSTICS_ENABLED": raw}) is True


@pytest.mark.parametrize("raw", ["0", "false", "no", "off", "garbage", ""])
def test_enabled_false_for_other_values(raw):
    assert diagnostics_enabled_from_env({"GITHUB_GRAPHQL_DIAGNOSTICS_ENABLED": raw}) is False


# -- sample_seconds_from_env --------------------------------------------------


def test_sample_seconds_default_is_10():
    assert sample_seconds_from_env({}) == 10
    assert DEFAULT_SAMPLE_SECONDS == 10


def test_sample_seconds_env_override():
    assert sample_seconds_from_env({"GITHUB_GRAPHQL_DIAGNOSTIC_SAMPLE_SECONDS": "30"}) == 30


@pytest.mark.parametrize("raw", ["0", "1", "4"])
def test_sample_seconds_clamped_up_to_floor(raw):
    result = sample_seconds_from_env({"GITHUB_GRAPHQL_DIAGNOSTIC_SAMPLE_SECONDS": raw})
    assert result == MIN_SAMPLE_SECONDS
    assert MIN_SAMPLE_SECONDS == 5


def test_sample_seconds_at_floor_is_unchanged():
    assert sample_seconds_from_env({"GITHUB_GRAPHQL_DIAGNOSTIC_SAMPLE_SECONDS": "5"}) == 5


@pytest.mark.parametrize("raw", ["not-a-number", "", "12.5", "None"])
def test_sample_seconds_unparseable_falls_back_to_default(raw):
    assert sample_seconds_from_env({"GITHUB_GRAPHQL_DIAGNOSTIC_SAMPLE_SECONDS": raw}) == DEFAULT_SAMPLE_SECONDS


# -- max_minutes_from_env ------------------------------------------------------


def test_max_minutes_default_is_15():
    assert max_minutes_from_env({}) == 15
    assert DEFAULT_MAX_MINUTES == 15


def test_max_minutes_env_override():
    assert max_minutes_from_env({"GITHUB_GRAPHQL_DIAGNOSTIC_MAX_MINUTES": "45"}) == 45


@pytest.mark.parametrize("raw", ["0", "-1", "-100"])
def test_max_minutes_clamped_up_to_one(raw):
    assert max_minutes_from_env({"GITHUB_GRAPHQL_DIAGNOSTIC_MAX_MINUTES": raw}) == 1


@pytest.mark.parametrize("raw", ["not-a-number", "", "12.5", "None"])
def test_max_minutes_unparseable_falls_back_to_default(raw):
    assert max_minutes_from_env({"GITHUB_GRAPHQL_DIAGNOSTIC_MAX_MINUTES": raw}) == DEFAULT_MAX_MINUTES
