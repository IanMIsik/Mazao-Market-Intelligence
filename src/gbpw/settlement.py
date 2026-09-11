"""
Settlement date / settlement period helpers, Europe/London.

A settlement day is a sequence of 30-minute settlement periods (SP) starting
at local midnight. It has 48 periods on a normal day, 46 on the day the
clocks go forward (lose an hour, spring), and 50 on the day the clocks go
back (gain an hour, autumn). We never hardcode those dates -- we derive the
period count from zoneinfo's own UTC offset transitions, so it stays correct
if the UK's DST rules ever change.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

LONDON = ZoneInfo("Europe/London")
UTC = ZoneInfo("UTC")
PERIOD_MINUTES = 30


def _local_midnight_utc(d: date) -> datetime:
    """UTC instant corresponding to local midnight at the start of `d`."""
    return datetime(d.year, d.month, d.day, 0, 0, tzinfo=LONDON).astimezone(UTC)


def periods_in_date(d: date) -> int:
    """Number of settlement periods in the local settlement day `d`."""
    start = _local_midnight_utc(d)
    end = _local_midnight_utc(d + timedelta(days=1))
    minutes = (end - start).total_seconds() / 60
    periods = round(minutes / PERIOD_MINUTES)
    if periods not in (46, 48, 50):
        raise ValueError(f"unexpected period count {periods} for {d} -- check DST data")
    return periods


def sp_start_utc(d: date, sp: int) -> datetime:
    """UTC start instant of settlement period `sp` (1-based) on day `d`."""
    if sp < 1:
        raise ValueError(f"settlement period must be >= 1, got {sp}")
    return _local_midnight_utc(d) + timedelta(minutes=PERIOD_MINUTES * (sp - 1))


def utc_to_settlement(dt_utc: datetime) -> tuple[date, int]:
    """Inverse of sp_start_utc: which (settlement date, period) a UTC instant falls in."""
    if dt_utc.tzinfo is None:
        raise ValueError("dt_utc must be timezone-aware")
    local = dt_utc.astimezone(LONDON)
    d = local.date()
    start = _local_midnight_utc(d)
    minutes = (dt_utc - start).total_seconds() / 60
    sp = int(minutes // PERIOD_MINUTES) + 1
    return d, sp


def local_to_settlement(local_dt: datetime) -> tuple[date, int]:
    """(settlement date, period) for a naive local (Europe/London) wall-clock instant.

    For sources that already report local civil time directly -- e.g. NESO's
    Enduring Auction Capability delivery blocks -- rather than UTC. Settlement
    period is derived straight from the local hour/minute; correct on normal
    days. On the one day a year the clocks go back, the repeated local hour
    (01:00-02:00 happening twice) can't be told apart from wall-clock time
    alone, so both occurrences land on the same nominal period -- a narrow,
    twice-a-year-at-most edge case, not handled.
    """
    sp = local_dt.hour * 2 + local_dt.minute // 30 + 1
    return local_dt.date(), sp


def week_dates(week_ending: date) -> list[date]:
    """The 7 local dates Mon..Sun for the week ending on `week_ending` (a Sunday)."""
    if week_ending.weekday() != 6:
        raise ValueError(f"week_ending must be a Sunday, got {week_ending} ({week_ending.strftime('%A')})")
    return [week_ending - timedelta(days=6 - i) for i in range(7)]
