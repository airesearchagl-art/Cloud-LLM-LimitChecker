import os


KNOWN_COLLECTOR_VENDORS = {"openai", "gemini", "claude"}


class PaidModelCallBlockedError(RuntimeError):
    pass


class UnknownCollectorVendorError(ValueError):
    pass


class CollectorDailyLimitExceededError(RuntimeError):
    pass


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def allow_paid_model_calls() -> bool:
    return env_bool("ALLOW_PAID_MODEL_CALLS", False)


def vendor_collectors_enabled() -> bool:
    return env_bool("ENABLE_VENDOR_COLLECTORS", False)


def collector_enabled(vendor: str) -> bool:
    if not vendor_collectors_enabled():
        return False
    key_by_vendor = {
        "openai": "ENABLE_OPENAI_COLLECTOR",
        "gemini": "ENABLE_GEMINI_COLLECTOR",
        "claude": "ENABLE_CLAUDE_COLLECTOR",
    }
    env_name = key_by_vendor.get(vendor.strip().lower())
    if env_name is None:
        return False
    return env_bool(env_name, False)


def collector_dry_run_default() -> bool:
    return env_bool("COLLECTOR_DRY_RUN_DEFAULT", True)


def max_collector_calls_per_day() -> int:
    return env_int("MAX_COLLECTOR_CALLS_PER_DAY", 24)


def assert_paid_model_calls_allowed(operation_name: str) -> None:
    if allow_paid_model_calls():
        return
    raise PaidModelCallBlockedError(
        f"Blocked paid model operation '{operation_name}'. "
        "This project is intended to call only usage, costs, billing, quota, or rate-limit management APIs."
    )


def normalize_collector_vendor(vendor: str) -> str:
    normalized = vendor.strip().lower()
    if normalized not in KNOWN_COLLECTOR_VENDORS:
        raise UnknownCollectorVendorError(f"unknown collector vendor: {vendor}")
    return normalized


def assert_collector_daily_limit_not_exceeded(db, vendor: str) -> None:
    normalized = normalize_collector_vendor(vendor)
    from app import crud

    limit = max_collector_calls_per_day()
    count = crud.count_collector_runs_today(db, normalized)
    if count >= limit:
        raise CollectorDailyLimitExceededError(
            f"collector daily limit exceeded for {normalized}: {count}/{limit}"
        )
