"""IST time helpers, used instead of datetime.now() everywhere so behavior
doesn't depend on the host container's timezone (Railway runs UTC)."""
from datetime import datetime, timedelta, timezone

IST_OFFSET = timedelta(hours=5, minutes=30)


def now_ist() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None) + IST_OFFSET


def candle_time_str(timestamp) -> str:
    """Angel SmartAPI candle timestamps look like '2024-01-15T09:15:00+05:30'."""
    return str(timestamp)[11:16]
