from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd

from app.config import MarketHoursCfg, load_strategy
from app.strategy.liquidity import (
    LiquidityLevel,
    asian_range,
    equal_levels,
    first_breach_sweep,
    is_untaken,
    latest_sweep,
    previous_day,
)
from app.strategy.structure import Swing
from tests.synthetic import bars, path_bars

CFG = load_strategy(Path(__file__).resolve().parents[1] / "config" / "strategy.yaml")


def ts(s: str) -> pd.Timestamp:
    return pd.Timestamp(s, tz="UTC")


def test_asian_range() -> None:
    # Asia window = 00:00-06:00 UTC (09:00-15:00 Tokyo).
    df = path_bars(
        [
            ("2026-10-28 22:00", 2600),
            ("2026-10-29 01:00", 2650),  # Asian high 2650.1
            ("2026-10-29 03:00", 2630),  # Asian low 2629.9
            ("2026-10-29 06:00", 2640),
            ("2026-10-29 07:00", 2700),  # after the window: ignored
        ],
        minutes=15,
    )
    lv = {
        x.name: x
        for x in asian_range(df, CFG.sessions["asia"], datetime(2026, 10, 29, 7, tzinfo=UTC))
    }
    assert lv["Asian high"].price == 2650.1
    assert lv["Asian low"].price == 2629.9
    assert lv["Asian low"].formed_at == ts("2026-10-29 06:00")


def test_asian_range_not_before_it_completes() -> None:
    df = path_bars([("2026-10-28 22:00", 2600), ("2026-10-29 05:00", 2650)], minutes=15)
    lv = asian_range(df, CFG.sessions["asia"], datetime(2026, 10, 29, 5, tzinfo=UTC))
    # Today's window is still open -> uses yesterday's, which has no data here.
    assert lv == []


def test_previous_day_rolls_at_ny_close() -> None:
    # Oct 2026, EDT: trading day rolls at 21:00 UTC.
    df = path_bars(
        [
            ("2026-10-27 21:00", 2600),
            ("2026-10-28 10:00", 2700),  # PDH 2700.1
            ("2026-10-28 20:00", 2590),  # PDL 2589.9 (bar 19:55)
            ("2026-10-28 21:00", 2595),
            ("2026-10-29 03:00", 2580),  # today: lower, must not count
        ],
        minutes=15,
    )
    lv = {
        x.name: x
        for x in previous_day(df, "M15", MarketHoursCfg(), datetime(2026, 10, 29, 8, tzinfo=UTC))
    }
    assert lv["PDH"].price == 2700.1
    assert lv["PDL"].price == 2589.9
    assert lv["PDH"].formed_at == ts("2026-10-28 21:00")


def _swing(kind: str, i: int, price: float) -> Swing:
    return Swing(kind, i, ts("2026-10-29") + timedelta(minutes=15 * i), price, i + 3)  # type: ignore[arg-type]


def test_equal_levels() -> None:
    df = bars("2026-10-29", [(1, 1, 1, 1)] * 20)
    swings = [_swing("high", 2, 100.0), _swing("high", 6, 100.2), _swing("high", 10, 105.0)]
    out = equal_levels(df, swings, "M15", tolerance=0.3, max_swings=10)
    assert [(x.name, x.price) for x in out] == [("Equal highs", 100.2)]
    assert out[0].formed_at == ts("2026-10-29 02:30")  # bar 9 close
    assert equal_levels(df, swings, "M15", tolerance=0.1, max_swings=10) == []


LOW = LiquidityLevel("Asian low", "low", 100.0, ts("2026-10-29 06:00"))


def test_sweep_wick_through_close_back() -> None:
    df = bars("2026-10-29 06:00", [(101, 102, 100.5, 101.5), (101.5, 102, 99.4, 100.6)], minutes=5)
    sw = first_breach_sweep(df, LOW, "M5")
    assert sw is not None
    assert sw.time == ts("2026-10-29 06:05") and sw.extreme == 99.4
    assert sw.close_time == ts("2026-10-29 06:10")


def test_close_through_is_a_break_not_a_sweep() -> None:
    df = bars(
        "2026-10-29 06:00",
        [(101, 102, 99.5, 99.8), (99.8, 101, 99.0, 100.5)],  # first breach closes below
        minutes=5,
    )
    assert first_breach_sweep(df, LOW, "M5") is None


def test_bars_before_level_formed_are_ignored() -> None:
    df = bars("2026-10-29 05:50", [(101, 101, 99, 100.5), (101, 101, 100.5, 101)], minutes=5)
    assert first_breach_sweep(df, LOW, "M5") is None


def test_latest_sweep_respects_max_age() -> None:
    df = bars(
        "2026-10-29 06:00", [(101, 102, 99.4, 100.6)] + [(101, 102, 100.5, 101)] * 30, minutes=5
    )
    now = datetime(2026, 10, 29, 8, 35, tzinfo=UTC)
    assert latest_sweep({"M5": df}, [LOW], "low", now, timedelta(minutes=120)) is None
    assert latest_sweep({"M5": df}, [LOW], "low", now, timedelta(minutes=160)) is not None
    assert latest_sweep({"M5": df}, [LOW], "high", now, timedelta(minutes=160)) is None


def test_is_untaken() -> None:
    hi = LiquidityLevel("PDH", "high", 105.0, ts("2026-10-29 06:00"))
    df = bars("2026-10-29 06:00", [(101, 104.9, 100, 101)], minutes=5)
    assert is_untaken(hi, [df])
    df2 = bars("2026-10-29 06:00", [(101, 105.1, 100, 101)], minutes=5)
    assert not is_untaken(hi, [df, df2])


def test_trading_day_across_dst() -> None:
    from app.strategy.liquidity import trading_day

    h = MarketHoursCfg()
    # Sun Nov 1 2026 (US falls back): 17:30 EST = 22:30 UTC belongs to Monday's session.
    assert trading_day(ts("2026-11-01 22:30"), h) == pd.Timestamp("2026-11-02")
    assert trading_day(ts("2026-10-28 20:59"), h) == pd.Timestamp("2026-10-28")
    assert trading_day(ts("2026-10-28 21:00"), h) == pd.Timestamp("2026-10-29")
