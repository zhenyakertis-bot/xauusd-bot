"""Bar-by-bar backtest with the live engine, gate and tracker.

    python -m app.backtest.runner --from 2025-01-01 --to 2026-10-01

At each M5 close inside a kill zone the engine sees only candles closed by then.
Signals pass the same dedup/rate gate as live, then a tracker replays M1 bars.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from app.config import StrategyConfig, get_settings, load_strategy
from app.data.candles import resample, tf_delta
from app.logging_setup import setup_logging
from app.risk.position_size import InvalidTradeError, position_size
from app.sessions import active_signal_session
from app.signals.dedup import SignalGate
from app.signals.models import Signal
from app.signals.tracker import TradeTracker
from app.strategy.smc_engine import SMCEngine

log = logging.getLogger(__name__)


@dataclass
class _Open:
    signal: Signal
    tracker: TradeTracker
    m1_pos: int  # next M1 bar to feed


@dataclass
class BacktestResult:
    trades: pd.DataFrame
    stages: Counter[str] = field(default_factory=Counter)
    blocked: Counter[str] = field(default_factory=Counter)
    start: datetime | None = None
    end: datetime | None = None


def _trade_row(o: _Open, balance: float, risk_pct: float, cfg: StrategyConfig) -> dict[str, object]:
    s, t = o.signal, o.tracker
    try:
        size = position_size(
            balance=balance,
            risk_percent=risk_pct,
            entry=s.entry,
            stop=s.stop,
            instrument=cfg.instrument,
        )
        lots, risk_usd = float(size.lots), float(size.actual_risk)
    except InvalidTradeError:
        lots, risk_usd = 0.0, 0.0
    return {
        "created_at": s.created_at,
        "session": s.session,
        "direction": s.direction.value,
        "entry_low": s.entry_zone.low,
        "entry_high": s.entry_zone.high,
        "entry": s.entry,
        "stop": s.stop,
        "tp1": s.tp1,
        "tp2": s.tp2,
        "rr1": s.rr1,
        "rr2": s.rr2,
        "sweep": f"{s.sweep.level.name} {s.sweep.level.price:.2f}",
        "tp1_source": s.tp1_source,
        "range_pos": s.range_position,
        "status": t.status.value if t.done else "OPEN",
        "triggered_at": t.triggered_at,
        "closed_at": t.closed_at,
        "result_r": t.result_r,
        "mfe_r": t.mfe_r,
        "mae_r": t.mae_r,
        "lots": lots,
        "risk_usd": risk_usd,
        "pnl_usd": (t.result_r or 0.0) * risk_usd,
    }


def run_backtest(
    m1: pd.DataFrame,
    cfg: StrategyConfig,
    start: datetime,
    end: datetime,
    balance: float = 10_000.0,
    risk_pct: float = 1.0,
) -> BacktestResult:
    etf, btf = cfg.structure.entry_timeframe, cfg.structure.bias_timeframe
    m5, m15 = resample(m1, etf), resample(m1, btf)
    d5, d15 = tf_delta(etf), tf_delta(btf)
    l5, l15 = cfg.structure.lookback_bars_entry, cfg.structure.lookback_bars_bias

    m1_t = m1.index
    m1_h, m1_l = m1["high"].to_numpy(), m1["low"].to_numpy()
    bt = cfg.backtest
    m1_s = m1["spread"].to_numpy() if bt.use_data_spread else np.full(len(m1), bt.spread)
    m1_s = np.where(m1_s > 0, m1_s, bt.spread)
    m15_close = m15.index + d15

    engine = SMCEngine(cfg)
    gate = SignalGate(cfg.signals)
    result = BacktestResult(trades=pd.DataFrame(), start=start, end=end)
    open_: list[_Open] = []
    closed: list[_Open] = []

    def advance(o: _Open, until: pd.Timestamp) -> None:
        # Feed M1 bars that have closed by `until`.
        while o.m1_pos < len(m1_t) and m1_t[o.m1_pos] + timedelta(minutes=1) <= until:
            i = o.m1_pos
            o.tracker.update(m1_t[i], float(m1_h[i]), float(m1_l[i]), float(m1_s[i]))
            o.m1_pos += 1
            if o.tracker.done:
                break

    closes = pd.DatetimeIndex(m5.index + d5)
    lo, hi = pd.Timestamp(start), pd.Timestamp(end)
    for k in np.flatnonzero((closes >= lo) & (closes < hi)):
        now = pd.Timestamp(closes[int(k)])
        for o in open_:
            advance(o, now)
        still = [o for o in open_ if not o.tracker.done]
        closed += [o for o in open_ if o.tracker.done]
        open_ = still

        if active_signal_session(cfg.sessions, now.to_pydatetime()) is None:
            continue
        j = int(np.searchsorted(m15_close, now, side="right"))
        ev = engine.evaluate(
            m5.iloc[max(0, k + 1 - l5) : k + 1],
            m15.iloc[max(0, j - l15) : j],
            now.to_pydatetime(),
            spread=None if bt.use_data_spread else bt.spread,
        )
        result.stages[ev.stage] += 1
        if ev.signal is None:
            continue
        s = ev.signal
        active = Counter(o.signal.direction for o in open_)
        reason = gate.check(s, dict(active))
        if reason is not None:
            result.blocked[reason] += 1
            continue
        gate.record(s)
        tracker = TradeTracker(
            direction=s.direction,
            entry=s.entry,
            stop=s.stop,
            tp1=s.tp1,
            tp2=s.tp2,
            expires_at=s.expires_at,
            tp1_close_fraction=cfg.trade.tp1_close_fraction,
            slippage=bt.slippage,
        )
        open_.append(_Open(s, tracker, int(np.searchsorted(m1_t, now))))

    for o in open_:  # let open trades finish on the remaining data
        advance(o, m1_t[-1] + timedelta(minutes=1))
    closed += open_
    rows = [_trade_row(o, balance, risk_pct, cfg) for o in closed]
    result.trades = (
        pd.DataFrame(rows).sort_values("created_at").reset_index(drop=True)
        if rows
        else pd.DataFrame()
    )
    return result


def apply_overrides(cfg: StrategyConfig, overrides: list[str]) -> StrategyConfig:
    """Return a copy of `cfg` with dotted `section.key=value` overrides (YAML values)."""
    data = cfg.model_dump()
    for item in overrides:
        path, _, raw = item.partition("=")
        *parents, leaf = path.split(".")
        node = data
        for key in parents:
            node = node[key]
        if leaf not in node:
            raise SystemExit(f"unknown parameter {path!r}")
        node[leaf] = yaml.safe_load(raw)
    return StrategyConfig.model_validate(data)


def main() -> None:
    from app.backtest.data import load_m1, parse_date
    from app.backtest.report import write_report

    ap = argparse.ArgumentParser(description="SMC XAUUSD backtest")
    ap.add_argument("--from", dest="start", required=True)
    ap.add_argument("--to", dest="end", required=True)
    ap.add_argument("--out", default="reports")
    ap.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="SECTION.KEY=VALUE",
        help="what-if override for this run only, e.g. --set structure.choch_sets_bias=true",
    )
    args = ap.parse_args()

    settings = get_settings()
    setup_logging(settings.log_level, None)
    cfg = apply_overrides(load_strategy(settings.strategy_config), args.set)
    start, end = parse_date(args.start), parse_date(args.end)
    # Warm-up before start for swings/previous day; a tail after end lets trades finish.
    data_end = min(end + timedelta(days=5), datetime.now(UTC))
    m1 = asyncio.run(
        load_m1(start - timedelta(days=cfg.backtest.warmup_days), data_end, progress=True)
    )
    log.info("running backtest", extra={"m1_bars": len(m1)})
    res = run_backtest(
        m1,
        cfg,
        start,
        end,
        balance=settings.paper_balance or 10_000.0,
        risk_pct=settings.risk_percent,
    )
    suffix = "".join(f"__{o.replace('=', '-')}" for o in args.set)
    out = Path(args.out) / f"backtest_{args.start}_{args.end}{suffix}"
    write_report(
        res, cfg, out, balance=settings.paper_balance or 10_000.0, risk_pct=settings.risk_percent
    )
    print((out / "report.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
