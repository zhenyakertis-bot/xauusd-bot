"""Session windows (kill zones) and market hours, DST-aware via zoneinfo.

All functions take and return tz-aware UTC datetimes; local session times are
converted per calendar date, so a window like 07:00-10:00 Europe/London is
06:00-09:00 UTC in summer and 07:00-10:00 UTC in winter.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.config import MarketHoursCfg, SessionWindow


@dataclass(frozen=True)
class SessionInstance:
    name: str
    label: str
    start: datetime  # UTC, inclusive
    end: datetime  # UTC, exclusive

    def contains(self, ts: datetime) -> bool:
        return self.start <= ts < self.end


def _require_aware(ts: datetime) -> None:
    if ts.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")


def _local_to_utc(d: date, t: time, tz: ZoneInfo) -> datetime:
    return datetime.combine(d, t, tzinfo=tz).astimezone(UTC)


def session_on(name: str, window: SessionWindow, local_date: date) -> SessionInstance:
    """The session `name` on a given date in the session's own timezone."""
    tz = ZoneInfo(window.tz)
    return SessionInstance(
        name=name,
        label=window.label or name,
        start=_local_to_utc(local_date, window.start, tz),
        end=_local_to_utc(local_date, window.end, tz),
    )


def session_at(name: str, window: SessionWindow, ts: datetime) -> SessionInstance | None:
    """The instance of session `name` containing `ts`, if any."""
    _require_aware(ts)
    local_date = ts.astimezone(ZoneInfo(window.tz)).date()
    inst = session_on(name, window, local_date)
    return inst if inst.contains(ts) else None


def active_signal_session(
    sessions: dict[str, SessionWindow], ts: datetime
) -> SessionInstance | None:
    """First signal-generating session (in config order) that contains `ts`."""
    for name, window in sessions.items():
        if not window.signals:
            continue
        inst = session_at(name, window, ts)
        if inst is not None:
            return inst
    return None


def last_completed(name: str, window: SessionWindow, ts: datetime) -> SessionInstance | None:
    """Most recent instance of session `name` that ended at or before `ts`."""
    _require_aware(ts)
    local_date = ts.astimezone(ZoneInfo(window.tz)).date()
    for back in range(0, 8):
        inst = session_on(name, window, local_date - timedelta(days=back))
        if inst.end <= ts:
            return inst
    return None


def is_market_open(ts: datetime, hours: MarketHoursCfg) -> bool:
    """Spot gold: Sun open -> Fri close, with a daily break, all in `hours.tz`."""
    _require_aware(ts)
    local = ts.astimezone(ZoneInfo(hours.tz))
    wd, t = local.weekday(), local.time()  # Mon=0 .. Sun=6
    if wd == 5:  # Saturday
        return False
    if wd == 6:  # Sunday: opens at daily_open
        return t >= hours.daily_open
    if wd == 4:  # Friday: closes at daily_close for the weekend
        return t < hours.daily_close
    return not (hours.daily_close <= t < hours.daily_open)
