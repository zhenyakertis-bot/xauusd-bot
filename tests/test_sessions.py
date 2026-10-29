"""Session windows across DST transitions.

2026 transitions: US (America/New_York) Mar 8 and Nov 1; UK (Europe/London) Mar 29 and Oct 25.
Between them, the London/NY offset is 4h instead of 5h.
"""

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from app.config import MarketHoursCfg, load_strategy
from app.sessions import (
    active_signal_session,
    is_market_open,
    last_completed,
    session_at,
    session_on,
)

CFG = load_strategy(Path(__file__).resolve().parents[1] / "config" / "strategy.yaml")
S = CFG.sessions


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)  # type: ignore[misc]


@pytest.mark.parametrize(
    ("name", "day", "start_h", "end_h"),
    [
        ("london", date(2026, 3, 27), 7, 10),  # GMT, last Friday before UK switch
        ("london", date(2026, 3, 30), 6, 9),  # BST
        ("london", date(2026, 10, 23), 6, 9),  # BST, last Friday before UK switch back
        ("london", date(2026, 10, 26), 7, 10),  # GMT
        ("new_york", date(2026, 3, 6), 13, 16),  # EST
        ("new_york", date(2026, 3, 9), 12, 15),  # EDT
        ("new_york", date(2026, 10, 26), 12, 15),  # still EDT while UK is on GMT
        ("new_york", date(2026, 11, 2), 13, 16),  # EST
        ("asia", date(2026, 7, 1), 0, 6),  # Tokyo has no DST
        ("asia", date(2026, 12, 1), 0, 6),
    ],
)
def test_session_utc_bounds(name: str, day: date, start_h: int, end_h: int) -> None:
    inst = session_on(name, S[name], day)
    assert inst.start == utc(day.year, day.month, day.day, start_h)
    assert inst.end == utc(day.year, day.month, day.day, end_h)


def test_session_boundaries_start_inclusive_end_exclusive() -> None:
    w = S["new_york"]
    assert session_at("new_york", w, utc(2026, 10, 29, 12, 0)) is not None
    assert session_at("new_york", w, utc(2026, 10, 29, 14, 59)) is not None
    assert session_at("new_york", w, utc(2026, 10, 29, 15, 0)) is None
    assert session_at("new_york", w, utc(2026, 10, 29, 11, 59)) is None


@pytest.mark.parametrize(
    ("ts", "expected"),
    [
        (utc(2026, 10, 29, 7, 30), "london"),
        (utc(2026, 10, 29, 11, 0), None),
        (utc(2026, 10, 29, 13, 30), "new_york"),
        (utc(2026, 10, 29, 3, 0), None),  # Asia window never produces signals
        (utc(2026, 7, 1, 6, 0), "london"),  # summer: London opens 06:00 UTC
        (utc(2026, 7, 1, 9, 0), None),
    ],
)
def test_active_signal_session(ts: datetime, expected: str | None) -> None:
    inst = active_signal_session(S, ts)
    assert (inst.name if inst else None) == expected


def test_label_from_config() -> None:
    inst = active_signal_session(S, utc(2026, 10, 29, 13, 0))
    assert inst is not None and inst.label == "New York"


def test_last_completed_asia() -> None:
    inst = last_completed("asia", S["asia"], utc(2026, 10, 29, 7, 0))
    assert inst is not None and inst.start == utc(2026, 10, 29, 0, 0)
    inst = last_completed("asia", S["asia"], utc(2026, 10, 29, 5, 0))  # today's not done yet
    assert inst is not None and inst.start == utc(2026, 10, 28, 0, 0)


def test_naive_timestamp_rejected() -> None:
    with pytest.raises(ValueError):
        session_at("london", S["london"], datetime(2026, 10, 29, 8))


HOURS = MarketHoursCfg()


@pytest.mark.parametrize(
    ("ts", "is_open"),
    [
        (utc(2026, 10, 30, 20, 59), True),  # Fri 16:59 EDT
        (utc(2026, 10, 30, 21, 0), False),  # Fri 17:00 EDT: weekend close
        (utc(2026, 10, 31, 12, 0), False),  # Saturday
        (utc(2026, 11, 1, 22, 59), False),  # Sun 17:59 EST (US switched that morning)
        (utc(2026, 11, 1, 23, 0), True),  # Sun 18:00 EST: weekly open
        (utc(2026, 10, 27, 21, 30), False),  # Tue daily break (EDT)
        (utc(2026, 10, 27, 22, 0), True),
        (utc(2026, 11, 3, 22, 30), False),  # Tue daily break (EST) moved an hour
        (utc(2026, 11, 3, 23, 0), True),
    ],
)
def test_market_hours(ts: datetime, is_open: bool) -> None:
    assert is_market_open(ts, HOURS) is is_open
