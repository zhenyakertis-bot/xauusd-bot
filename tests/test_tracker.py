import pandas as pd
import pytest

from app.signals.models import Direction, SignalStatus
from app.signals.tracker import TradeTracker

T0 = pd.Timestamp("2026-10-29 08:00", tz="UTC")


def t(i: int) -> pd.Timestamp:
    return T0 + pd.Timedelta(minutes=i)


def buy(tp2: float | None = 106.0, slippage: float = 0.0) -> TradeTracker:
    # Entry 100, SL 98 (risk 2), TP1 104 (2R), TP2 106 (3R).
    return TradeTracker(
        Direction.BUY, 100.0, 98.0, 104.0, tp2, T0 + pd.Timedelta(minutes=90), slippage=slippage
    )


def test_fill_tp1_then_breakeven() -> None:
    tr = buy()
    assert tr.update(t(0), 101, 100.5, 0) == []
    assert [e.status for e in tr.update(t(1), 101, 99.9, 0)] == [SignalStatus.TRIGGERED]
    assert [e.status for e in tr.update(t(2), 104.2, 101, 0)] == [SignalStatus.TP1]
    assert not tr.done
    assert [e.status for e in tr.update(t(3), 103, 99.9, 0)] == [SignalStatus.BREAKEVEN]
    assert tr.done and tr.result_r == pytest.approx(1.0)  # 50% at 2R


def test_fill_tp1_then_tp2() -> None:
    tr = buy()
    tr.update(t(0), 101, 99.9, 0)
    tr.update(t(1), 104.2, 101, 0)
    tr.update(t(2), 106.1, 103, 0)
    assert tr.status is SignalStatus.TP2
    assert tr.result_r == pytest.approx(2.5)  # 0.5 * 2R + 0.5 * 3R
    assert tr.mfe_r == pytest.approx(3.05)


def test_sl_and_tp_in_same_bar_counts_as_sl() -> None:
    tr = buy(slippage=0.05)
    tr.update(t(0), 101, 99.9, 0)
    tr.update(t(1), 104.5, 97.5, 0)
    assert tr.status is SignalStatus.SL
    assert tr.result_r == pytest.approx(-1.025)


def test_fill_and_stop_in_same_bar_is_sl() -> None:
    tr = buy()
    evs = tr.update(t(0), 101, 97.9, 0)
    assert [e.status for e in evs] == [SignalStatus.TRIGGERED, SignalStatus.SL]
    assert tr.result_r == pytest.approx(-1.0)


def test_tp_not_credited_on_fill_bar() -> None:
    tr = buy()
    tr.update(t(0), 104.5, 99.9, 0)  # wick through entry and TP1 in one bar
    assert tr.status is SignalStatus.TRIGGERED


def test_expiry() -> None:
    tr = buy()
    tr.update(t(10), 101, 100.5, 0)
    assert tr.update(t(90), 101, 100.5, 0)[0].status is SignalStatus.EXPIRED
    assert tr.done and tr.result_r == 0.0


def test_spread_buy_fills_on_ask_exits_on_bid() -> None:
    tr = buy()
    tr.update(t(0), 101, 100.15, 0.4)  # ask low 100.35 > entry: no fill
    assert tr.status is SignalStatus.PENDING
    tr.update(t(1), 101, 99.8, 0.4)  # ask low 100.0: filled
    assert tr.status is SignalStatus.TRIGGERED
    tr.update(t(2), 104.1, 101, 0.4)  # bid high 103.9: TP1 not reached
    assert tr.status is SignalStatus.TRIGGERED
    tr.update(t(3), 104.2, 101, 0.4)  # bid high 104.0
    assert tr.status is SignalStatus.TP1


def test_single_target_closes_fully_at_tp1() -> None:
    tr = buy(tp2=None)
    tr.update(t(0), 101, 99.9, 0)
    tr.update(t(1), 104.1, 101, 0)
    assert tr.done and tr.status is SignalStatus.TP1 and tr.result_r == pytest.approx(2.0)


def test_sell_mirror() -> None:
    tr = TradeTracker(Direction.SELL, 100.0, 102.0, 96.0, None, T0 + pd.Timedelta(hours=1))
    tr.update(t(0), 100.1, 99.5, 0)
    assert tr.status is SignalStatus.TRIGGERED
    tr.update(t(1), 99.0, 95.9, 0)
    assert tr.status is SignalStatus.TP1 and tr.result_r == pytest.approx(2.0)


def test_gap_through_stop_without_fill_invalidates() -> None:
    tr = TradeTracker(Direction.SELL, 100.0, 102.0, 96.0, None, T0 + pd.Timedelta(hours=1))
    # SELL limit needs bid >= 100; a bar with high 99 but spread 7 has ask high 102.5 >= SL.
    tr.update(t(0), 99.0, 98.0, 7.0)
    assert tr.status is SignalStatus.INVALIDATED
