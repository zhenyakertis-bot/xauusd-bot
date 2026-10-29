"""Assembles the SMC rules into a signal. Same code runs live and in the backtest.

`evaluate` is a pure function of closed candles up to `now`. It returns either a
Signal or the stage at which the setup was rejected and why.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from app.config import StrategyConfig
from app.data.candles import drop_incomplete
from app.risk.position_size import (
    InvalidTradeError,
    entry_price,
    reward_risk,
    sl_buffer,
    target_at_r,
)
from app.sessions import active_signal_session
from app.signals.models import Direction, Signal
from app.strategy import liquidity as liq
from app.strategy.structure import (
    Swing,
    bias_from_events,
    dealing_range,
    find_swings,
    structure_events,
)
from app.strategy.zones import atr, find_fvgs, intersect, order_block

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Evaluation:
    signal: Signal | None
    stage: str  # last stage reached: data, session, bias, sweep, confirmation, ...
    reason: str

    @property
    def ok(self) -> bool:
        return self.signal is not None


def _reject(stage: str, reason: str) -> Evaluation:
    return Evaluation(None, stage, reason)


class SMCEngine:
    def __init__(self, cfg: StrategyConfig) -> None:
        self.cfg = cfg

    def evaluate(
        self,
        entry_df: pd.DataFrame,
        bias_df: pd.DataFrame,
        now: datetime,
        spread: float | None = None,
    ) -> Evaluation:
        cfg = self.cfg
        etf, btf = cfg.structure.entry_timeframe, cfg.structure.bias_timeframe
        now_ts = pd.Timestamp(now)

        # Only closed candles, whatever the caller passed.
        m5 = drop_incomplete(entry_df, etf, now).tail(cfg.structure.lookback_bars_entry)
        m15 = drop_incomplete(bias_df, btf, now).tail(cfg.structure.lookback_bars_bias)
        if len(m5) < cfg.confirmation.atr_period + 5 or len(m15) < 4 * cfg.structure.swing_lookback:
            return _reject("data", "not enough closed candles")

        # 1. Kill zone
        session = active_signal_session(cfg.sessions, now)
        if session is None:
            return _reject("session", "outside London/New York kill zones")

        # 2. M15 bias
        sw15 = find_swings(m15, cfg.structure.swing_lookback)
        ev15 = structure_events(m15, sw15)
        bias, bias_ev = bias_from_events(ev15, cfg.structure.choch_sets_bias)
        if bias is None or bias_ev is None:
            last = f"last event {bias_ev.kind} {bias_ev.direction}" if bias_ev else "no breaks"
            return _reject("bias", f"no M15 BOS bias ({last})")
        direction = Direction.BUY if bias == "up" else Direction.SELL
        buy = direction is Direction.BUY

        # 3. Liquidity sweep against the bias
        levels = self._levels(m15, sw15, btf, now)
        frames = {tf: (m5 if tf == etf else m15) for tf in cfg.liquidity.sweep_timeframes}
        sweep = liq.latest_sweep(
            frames,
            levels,
            "low" if buy else "high",
            now,
            timedelta(minutes=cfg.liquidity.sweep_max_age_minutes),
        )
        if sweep is None:
            side = "sell-side (below)" if buy else "buy-side (above)"
            return _reject("sweep", f"no recent {side} liquidity sweep")

        # 4. M5 confirmation after the sweep
        after = np.flatnonzero(m5.index >= sweep.time)
        if after.size == 0:
            return _reject("confirmation", "no M5 bars after sweep")
        sweep_i = int(after[0])
        sw5 = find_swings(m5, cfg.structure.entry_swing_lookback)
        breaks = [
            e for e in structure_events(m5, sw5) if e.direction == bias and e.index >= sweep_i
        ]
        if not breaks:
            return _reject("confirmation", "no M5 structure break after sweep")

        # The break and the displacement belong to the same impulse: the FVG's middle
        # candle is within `impulse_max_gap_bars` of the break (either side).
        atr5 = atr(m5, cfg.confirmation.atr_period)
        all_fvgs = find_fvgs(
            m5,
            bias,
            atr5,
            cfg.confirmation.displacement_atr_mult,
            cfg.confirmation.min_fvg_size,
            start_index=sweep_i,
        )
        gap = cfg.confirmation.impulse_max_gap_bars
        pairs = [
            (br, f)
            for f in all_fvgs
            for br in breaks
            if abs(f.displacement_index - br.index) <= gap
        ]
        if not pairs:
            return _reject("confirmation", "no displacement FVG in the impulse")
        confirm, fvg = max(pairs, key=lambda p: (max(p[0].index, p[1].completed_index), p[0].index))

        last_i = len(m5) - 1
        if last_i - max(confirm.index, fvg.completed_index) >= cfg.confirmation.max_age_bars:
            return _reject("confirmation", "setup completed too long ago")

        later = m5.iloc[fvg.completed_index + 1 :]
        if not later.empty and (
            (buy and later["low"].min() < fvg.zone.low)
            or (not buy and later["high"].max() > fvg.zone.high)
        ):
            return _reject("confirmation", "FVG already traded through")

        ob = order_block(m5, fvg, cfg.confirmation.ob_max_lookback)
        ob_zone = ob[0] if ob else None
        zone = (intersect(fvg.zone, ob_zone) if ob_zone else None) or fvg.zone
        entry = entry_price(direction.value, zone.low, zone.high, cfg.trade.entry_mode)

        # 5. Premium / discount on the M15 leg
        rng = dealing_range(m15, sw15, bias_ev)
        if rng is None:
            return _reject("premium_discount", "no M15 dealing range")
        pos = rng.position(entry)
        eq = cfg.structure.equilibrium
        if cfg.structure.require_premium_discount and (
            np.isnan(pos) or (buy and pos > eq) or (not buy and pos < eq)
        ):
            where = "discount" if buy else "premium"
            return _reject("premium_discount", f"entry not in {where} ({pos * 100:.0f}% of range)")

        # 6. Stop loss beyond the sweep extreme
        if spread is None or spread <= 0:
            feed_spread = float(m5["spread"].iloc[-1])
            spread = feed_spread if feed_spread > 0 else cfg.trade.assumed_spread
        a = float(atr5.iloc[-1])
        buf = sl_buffer(spread, a, cfg.trade.sl_spread_mult, cfg.trade.sl_atr_mult)
        seg = m5.iloc[sweep_i:]
        if buy:
            stop = min(float(seg["low"].min()), sweep.extreme) - buf
        else:
            stop = max(float(seg["high"].max()), sweep.extreme) + buf
        last_close = float(m5["close"].iloc[-1])
        if (buy and last_close <= stop) or (not buy and last_close >= stop):
            return _reject("levels", "price already beyond stop")

        # 7. Targets: opposite liquidity that is still untaken
        tgt_side = "high" if buy else "low"
        targets = [
            lv
            for lv in levels
            if lv.side == tgt_side
            and ((lv.price > entry) if buy else (lv.price < entry))
            and liq.is_untaken(lv, [m15, m5])
        ]
        targets.sort(key=lambda lv: abs(lv.price - entry))
        try:
            if targets:
                tp1, tp1_src = targets[0].price, f"{targets[0].name} {targets[0].price:.2f}"
            else:
                tp1 = target_at_r(direction.value, entry, stop, cfg.trade.min_rr_tp1)
                tp1_src = f"{cfg.trade.min_rr_tp1:g}R (no liquidity target)"
            rr1 = reward_risk(direction.value, entry, stop, tp1)
        except InvalidTradeError as exc:
            return _reject("levels", str(exc))
        if rr1 < cfg.trade.min_rr_tp1:
            return _reject("rr", f"R:R to TP1 ({tp1_src}) is {rr1:.2f} < {cfg.trade.min_rr_tp1}")

        tp2: float | None = None
        tp2_src: str | None = None
        beyond = [
            lv
            for lv in targets
            if abs(lv.price - tp1) > cfg.liquidity.eq_tolerance
            and ((lv.price > tp1) if buy else (lv.price < tp1))
        ]
        if beyond:
            tp2, tp2_src = beyond[0].price, f"{beyond[0].name} {beyond[0].price:.2f}"
        else:
            r_level = target_at_r(direction.value, entry, stop, cfg.trade.tp2_r)
            if (r_level > tp1) if buy else (r_level < tp1):
                tp2, tp2_src = r_level, f"{cfg.trade.tp2_r:g}R"
        rr2 = reward_risk(direction.value, entry, stop, tp2) if tp2 is not None else None

        signal = Signal(
            direction=direction,
            session=session.name,
            session_label=session.label,
            created_at=now_ts,
            expires_at=now_ts + timedelta(minutes=cfg.trade.entry_expiry_minutes),
            entry_zone=zone,
            entry=entry,
            stop=stop,
            tp1=tp1,
            tp1_source=tp1_src,
            tp2=tp2,
            tp2_source=tp2_src,
            rr1=rr1,
            rr2=rr2,
            bias_event=bias_ev,
            sweep=sweep,
            confirm_event=confirm,
            fvg=fvg,
            order_block=ob_zone,
            range_position=pos,
            targets_considered=targets,
        )
        return Evaluation(signal, "ok", "signal")

    def _levels(
        self, m15: pd.DataFrame, sw15: list[Swing], tf: str, now: datetime
    ) -> list[liq.LiquidityLevel]:
        lc = self.cfg.liquidity
        levels: list[liq.LiquidityLevel] = []
        if lc.use_asian_range and "asia" in self.cfg.sessions:
            levels += liq.asian_range(m15, self.cfg.sessions["asia"], now)
        if lc.use_previous_day:
            levels += liq.previous_day(m15, tf, self.cfg.market_hours, now)
        if lc.use_equal_levels:
            levels += liq.equal_levels(m15, sw15, tf, lc.eq_tolerance, lc.eq_lookback_swings)
        if lc.use_swing_targets:
            levels += liq.swing_levels(m15, sw15, tf)
        return levels
