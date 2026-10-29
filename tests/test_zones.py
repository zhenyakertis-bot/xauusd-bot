import pytest

from app.strategy.zones import Zone, atr, find_fvgs, intersect, order_block
from tests.synthetic import bars

# 20 quiet bars (range 1.0), then: bearish candle (OB), displacement up, gap candle.
QUIET = [(100, 100.5, 99.5, 100.2)] * 20
UP_IMPULSE = [
    *QUIET,
    (100.2, 100.4, 99.0, 99.2),  # 20: bearish -> order block 99.0-100.4
    (99.2, 105.0, 99.1, 104.8),  # 21: displacement, body 5.6
    (104.8, 106.0, 102.0, 105.5),  # 22: low 102.0 > high[20] 100.4 -> FVG 100.4-102.0
]


def test_atr_constant_range() -> None:
    df = bars("2026-10-29", QUIET)
    a = atr(df, 14)
    assert a.iloc[:13].isna().all()
    assert a.iloc[-1] == pytest.approx(1.0)


def test_bullish_fvg_and_order_block() -> None:
    df = bars("2026-10-29", UP_IMPULSE, minutes=5)
    fvgs = find_fvgs(df, "up", atr(df, 14), atr_mult=1.5, min_size=0.5)
    assert len(fvgs) == 1
    f = fvgs[0]
    assert (f.zone.low, f.zone.high) == (100.4, 102.0)
    assert (f.displacement_index, f.completed_index) == (21, 22)
    ob = order_block(df, f, max_lookback=10)
    assert ob is not None and ob[0] == Zone(99.0, 100.4)


def test_no_fvg_without_displacement() -> None:
    df = bars("2026-10-29", UP_IMPULSE, minutes=5)
    assert find_fvgs(df, "up", atr(df, 14), atr_mult=10.0, min_size=0.5) == []


def test_no_fvg_when_gap_too_small() -> None:
    df = bars("2026-10-29", UP_IMPULSE, minutes=5)
    assert find_fvgs(df, "up", atr(df, 14), atr_mult=1.5, min_size=2.0) == []


def test_bearish_fvg_mirror() -> None:
    mirrored = [(200 - o, 200 - lo, 200 - h, 200 - c) for o, h, lo, c in UP_IMPULSE]
    df = bars("2026-10-29", mirrored, minutes=5)
    fvgs = find_fvgs(df, "down", atr(df, 14), atr_mult=1.5, min_size=0.5)
    assert len(fvgs) == 1
    assert (fvgs[0].zone.low, fvgs[0].zone.high) == pytest.approx((98.0, 99.6))
    ob = order_block(df, fvgs[0], 10)
    assert ob is not None and ob[0] == Zone(pytest.approx(99.6), pytest.approx(101.0))  # type: ignore[arg-type]
    assert find_fvgs(df, "up", atr(df, 14), 1.5, 0.5) == []


def test_intersect() -> None:
    assert intersect(Zone(100, 102), Zone(101, 103)) == Zone(101, 102)
    assert intersect(Zone(100, 101), Zone(101, 103)) is None
