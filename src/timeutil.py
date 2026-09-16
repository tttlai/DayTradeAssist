"""IST time helpers, used instead of datetime.now() everywhere so behavior
doesn't depend on the host container's timezone (Railway runs UTC)."""
import calendar
from datetime import date, datetime, timedelta, timezone

IST_OFFSET = timedelta(hours=5, minutes=30)


def now_ist() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None) + IST_OFFSET


def candle_time_str(timestamp) -> str:
    """Angel SmartAPI candle timestamps look like '2024-01-15T09:15:00+05:30'."""
    return str(timestamp)[11:16]


def is_fo_expiry_day(d: date) -> bool:
    """True if `d` is the last Thursday of its month -- the standard
    monthly expiry for stock futures/options on NSE (unlike index weekly
    expiry days, which have changed exchange-to-exchange rules multiple
    times in recent years, monthly stock F&O expiry on the last Thursday
    has been stable for a long time).

    Does NOT account for exchange holidays shifting expiry a day earlier
    (NSE does this a few times a year, e.g. around Diwali) -- that would
    need a maintained holiday calendar. Good enough for a caution flag,
    not precise enough to rely on for anything that needs to be exact.
    """
    last_day = calendar.monthrange(d.year, d.month)[1]
    last_date = date(d.year, d.month, last_day)
    offset = (last_date.weekday() - 3) % 7  # Thursday == 3
    last_thursday = last_date - timedelta(days=offset)
    return d == last_thursday
