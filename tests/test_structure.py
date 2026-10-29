import pandas as pd
import pytest

from app.strategy.structure import (
    bias_from_events,
    dealing_range,
    find_swings,
    structure_events,
)
from tests.synthetic import bars

# lookback 1. Swing high 13 (bar 1), swing low 8 (bar 3); bar 5 closes above 13 -> BOS up;
# swing high 15 (bar 6); bar 8 closes below 8 -> CHoCH down.
SEQ = bars(
    "2026-10-28 00:00",
    [
        (10, 11, 9, 10),
        (10, 13, 10, 12),
        (12, 12.5, 11, 11),
        (11, 11.5, 8, 9),
        (9, 10, 8.5, 10),
        (10, 14, 10, 13.5),
        (13.5, 15, 13, 14.5),
        (14.5, 14.6, 12, 12.5),
        (12.5, 13, 7, 7.5),
    ],
)


def test_find_swings_known_answer() -> None:
    sw = find_swings(SEQ, 1)
    highs = [(s.index, s.price, s.confirmed_index) for s in sw if s.kind == "high"]
    lows = [(s.index, s.price, s.confirmed_index) for s in sw if s.kind == "low"]
    assert highs == [(1, 13.0, 2), (6, 15.0, 7)]
    assert lows == [(3, 8.0, 4)]


def test_swings_need_right_side_bars() -> None:
    # The last `lookback` bars can never be swings yet (no lookahead).
    sw = find_swings(SEQ, 1)
    assert all(s.index <= len(SEQ) - 2 for s in sw)
    rising = bars("2026-10-28", [(i, i + 1, i - 1, i + 0.5) for i in range(10)])
    assert [s for s in find_swings(rising, 2) if s.kind == "high"] == []


def test_equal_highs_first_one_wins() -> None:
    df = bars("2026-10-28", [(1, 2, 0, 1), (1, 5, 1, 2), (2, 5, 1, 2), (2, 3, 1, 2), (2, 2, 1, 1)])
    highs = [s.index for s in find_swings(df, 1) if s.kind == "high"]
    assert highs == [1]


def test_bos_then_choch() -> None:
    ev = structure_events(SEQ, find_swings(SEQ, 1))
    assert [(e.kind, e.direction, e.level, e.index) for e in ev] == [
        ("BOS", "up", 13.0, 5),
        ("CHoCH", "down", 8.0, 8),
    ]


def test_wick_through_is_not_a_break() -> None:
    df = SEQ.copy()
    df.iloc[5] = [10, 14, 10, 12.9, 1, 0.3]  # wick above 13 but close below
    ev = structure_events(df, find_swings(df, 1))
    assert all(not (e.direction == "up" and e.index == 5) for e in ev)


def test_bias_rules() -> None:
    ev = structure_events(SEQ, find_swings(SEQ, 1))
    assert bias_from_events(ev[:1])[0] == "up"
    bias, last = bias_from_events(ev)
    assert bias is None and last is not None and last.kind == "CHoCH"
    assert bias_from_events(ev, choch_sets_bias=True)[0] == "down"
    assert bias_from_events([]) == (None, None)


def test_dealing_range_for_bullish_break() -> None:
    sw = find_swings(SEQ, 1)
    ev = structure_events(SEQ, sw)
    rng = dealing_range(SEQ.iloc[:8], sw, ev[0])
    assert rng is not None
    # From swing low 8 (confirmed before the break) to the highest high since (15).
    assert (rng.low, rng.high) == (8.0, 15.0)
    assert rng.position(11.5) == pytest.approx(0.5)
    assert rng.position(9.75) == pytest.approx(0.25)


def test_index_is_utc() -> None:
    assert SEQ.index.tz is not None and str(SEQ.index.tz) == "UTC"
    assert isinstance(SEQ.index[0], pd.Timestamp)
