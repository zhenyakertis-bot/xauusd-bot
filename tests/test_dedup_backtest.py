from dataclasses import replace
from datetime import UTC, datetime

import pandas as pd
import pytest

from app.backtest.report import metrics, write_report
from app.backtest.runner import run_backtest
from app.data.candles import normalize
from app.signals.dedup import SignalGate, signal_key
from app.signals.models import Direction, Signal
from app.strategy.smc_engine import SMCEngine
from app.strategy.zones import Zone
from tests.synthetic import bars
from tests.test_smc_engine import CFG, NOW, scenario


@pytest.fixture(scope="module")
def sig() -> Signal:
    m5, m15 = scenario()
    s = SMCEngine(CFG).evaluate(m5, m15, NOW).signal
    assert s is not None
    return s


def test_key_stable_under_tiny_price_noise(sig: Signal) -> None:
    other = replace(sig, entry_zone=Zone(sig.entry_zone.low + 0.01, sig.entry_zone.high))
    assert signal_key(sig, 0.10) == signal_key(other, 0.10)
    moved = replace(sig, entry_zone=Zone(sig.entry_zone.low + 1, sig.entry_zone.high + 1))
    assert signal_key(sig, 0.10) != signal_key(moved, 0.10)


def test_gate_rules(sig: Signal) -> None:
    gate = SignalGate(CFG.signals)
    assert gate.check(sig, {}) is None
    gate.record(sig)
    assert gate.check(sig, {}) == "duplicate"

    later = pd.Timedelta(minutes=10)
    s2 = replace(sig, created_at=sig.created_at + later, entry_zone=Zone(2600, 2601))
    assert gate.check(s2, {Direction.BUY: 1}).startswith("already 1 active")  # type: ignore[union-attr]
    assert gate.check(s2, {}) == "cooldown"

    s3 = replace(s2, created_at=sig.created_at + pd.Timedelta(minutes=40))
    assert gate.check(s3, {}) is None
    gate.record(s3)
    s4 = replace(
        s3, created_at=sig.created_at + pd.Timedelta(minutes=80), entry_zone=Zone(2590, 2591)
    )
    assert gate.check(s4, {}) == "session limit"  # max 2 per session


def to_m1(m5: pd.DataFrame) -> pd.DataFrame:
    """Split each M5 bar into 5 M1 bars that resample back to the same OHLC."""
    rows = []
    for t, r in m5.iterrows():
        rows.append((t, r.open, r.high, r.low, r.close, r.spread))
        rows += [
            (t + pd.Timedelta(minutes=i), r.close, r.close, r.close, r.close, r.spread)
            for i in range(1, 5)
        ]
    df = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "spread"]).set_index(
        "time"
    )
    df["volume"] = 1.0
    return normalize(df)


def test_backtest_end_to_end(tmp_path) -> None:  # type: ignore[no-untyped-def]
    m5, _ = scenario()
    # After the signal at 07:50: retrace fills the 2634.50 limit, then rally through PDH 2678.10.
    after = bars(
        "2026-10-29 07:50",
        [
            (2641.5, 2641.6, 2634.0, 2635.0),
            *[(2635 + 4 * i, 2639 + 4 * i, 2634.5 + 4 * i, 2639 + 4 * i) for i in range(12)],
        ],
        minutes=5,
    )
    m1 = to_m1(normalize(pd.concat([m5, after])))
    res = run_backtest(
        m1, CFG, datetime(2026, 10, 29, 7, tzinfo=UTC), datetime(2026, 10, 29, 10, tzinfo=UTC)
    )
    trades = res.trades
    assert len(trades) >= 1
    first = trades.iloc[0]
    assert first["created_at"] == pd.Timestamp(NOW)
    assert first["direction"] == "BUY"
    assert first["status"] == "TP1"  # single target: closed in full at PDH
    assert first["result_r"] == pytest.approx(first["rr1"])
    assert first["lots"] == pytest.approx(0.14)

    m = metrics(trades)
    assert m["trades"] >= 1 and m["wins"] >= 1
    summary = write_report(res, CFG, tmp_path, balance=10_000, risk_pct=1.0)
    assert (tmp_path / "equity.png").stat().st_size > 1000
    assert (tmp_path / "trades.csv").exists()
    assert "Walk-forward" in (tmp_path / "report.md").read_text()
    assert summary["overall"]["signals"] == len(trades)


def test_apply_overrides() -> None:
    from app.backtest.runner import apply_overrides

    cfg = apply_overrides(CFG, ["structure.choch_sets_bias=true", "trade.min_rr_tp1=1.5"])
    assert cfg.structure.choch_sets_bias is True and cfg.trade.min_rr_tp1 == 1.5
    assert CFG.structure.choch_sets_bias is False  # original untouched
    with pytest.raises(SystemExit):
        apply_overrides(CFG, ["trade.nope=1"])
