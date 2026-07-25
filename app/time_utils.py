import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dateutil.relativedelta import relativedelta


def app_tz() -> ZoneInfo:
    return ZoneInfo(os.getenv("APP_TIMEZONE", "Asia/Tokyo"))


def now_local() -> datetime:
    return datetime.now(app_tz())


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=app_tz())
    return parsed.astimezone(app_tz())


def reset_delta(interval_type: str, interval_value: int) -> timedelta | relativedelta | None:
    value = max(interval_value, 1)
    if interval_type == "hours":
        return timedelta(hours=value)
    if interval_type == "days":
        return timedelta(days=value)
    if interval_type == "weeks":
        return timedelta(weeks=value)
    if interval_type == "months":
        return relativedelta(months=value)
    return None


def add_reset_interval(base: datetime, interval_type: str, interval_value: int, multiplier: int = 1) -> datetime:
    value = max(interval_value, 1) * multiplier
    if interval_type == "months":
        return base + relativedelta(months=value)
    delta = reset_delta(interval_type, abs(value))
    if delta is None:
        return base
    return base + (delta if multiplier >= 0 else -delta)


def advance_next_reset(
    next_reset_at: datetime | None,
    interval_type: str,
    interval_value: int,
    reference_now: datetime | None = None,
) -> datetime | None:
    delta = reset_delta(interval_type, interval_value)
    if next_reset_at is None or delta is None:
        return next_reset_at
    current = next_reset_at.astimezone(app_tz()) if next_reset_at.tzinfo else next_reset_at.replace(tzinfo=app_tz())
    now = reference_now or now_local()
    if now.tzinfo is None:
        now = now.replace(tzinfo=app_tz())
    else:
        now = now.astimezone(app_tz())
    while current <= now:
        current = add_reset_interval(current, interval_type, interval_value)
    return current


def current_period_start(
    next_reset_at: datetime | None,
    interval_type: str,
    interval_value: int,
    reference_now: datetime | None = None,
) -> datetime | None:
    delta = reset_delta(interval_type, interval_value)
    if next_reset_at is None or delta is None:
        return None
    next_reset = advance_next_reset(next_reset_at, interval_type, interval_value, reference_now=reference_now)
    if next_reset is None:
        return None
    return add_reset_interval(next_reset, interval_type, interval_value, multiplier=-1)
