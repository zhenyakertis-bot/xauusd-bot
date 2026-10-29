"""Liquidity levels (Asian range, previous day, equal highs/lows, swings) and sweeps.

A sweep is the first bar after a level formed that trades through it with its wick
and closes back on the original side. If that first breach closes beyond the level,
the level is broken, not swept.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from app.config import MarketHoursCfg, SessionWindow
from app.data.candles import tf_delta
from app.sessions import last_completed
from app.strategy.structure import Side, Swing


@dataclass(frozen=True)
class LiquidityLevel:
    name: str  # human readable, e.g. "Asian low"
    side: Side
    price: float
    formed_at: pd.Timestamp  # level is known from this time on
    sweepable: bool = True  # False: only used as a target


@dataclass(frozen=True)
class Sweep:
    level: LiquidityLevel
    tf: str
    time: pd.Timestamp  # open time of the sweeping bar
    close_time: pd.Timestamp
    extreme: float  # wick extreme beyond the level


def asian_range(df: pd.DataFrame, window: SessionWindow, now: datetime) -> list[LiquidityLevel]:
    inst = last_completed("asia", window, now)
    if inst is None:
        return []
    seg = df[(df.index >= inst.start) & (df.index < inst.end)]
    if seg.empty:
        return []
    end = pd.Timestamp(inst.end)
    return [
        LiquidityLevel("Asian high", "high", float(seg["high"].max()), end),
        LiquidityLevel("Asian low", "low", float(seg["low"].min()), end),
    ]


def trading_day(ts: pd.Timestamp, hours: MarketHoursCfg) -> pd.Timestamp:
    """Gold's trading day (tz-naive date) rolls at the daily close (17:00 New York)."""
    return pd.Timestamp(trading_days(pd.DatetimeIndex([ts]), hours)[0])


def trading_days(idx: pd.DatetimeIndex, hours: MarketHoursCfg) -> pd.DatetimeIndex:
    """Vectorised `trading_day` (tz-naive dates) for a whole index."""
    local = idx.tz_convert(ZoneInfo(hours.tz)).tz_localize(None)
    close = pd.Timedelta(hours=hours.daily_close.hour, minutes=hours.daily_close.minute)
    return (local + (pd.Timedelta(days=1) - close)).normalize()


def previous_day(
    df: pd.DataFrame, tf: str, hours: MarketHoursCfg, now: datetime
) -> list[LiquidityLevel]:
    if df.empty:
        return []
    days = trading_days(pd.DatetimeIndex(df.index), hours)
    current = trading_day(pd.Timestamp(now), hours)
    prior = days[days < current]
    if prior.empty:
        return []
    seg = df[days == prior[-1]]
    formed = seg.index[-1] + tf_delta(tf)
    return [
        LiquidityLevel("PDH", "high", float(seg["high"].max()), formed),
        LiquidityLevel("PDL", "low", float(seg["low"].min()), formed),
    ]


def _confirmed_time(df: pd.DataFrame, s: Swing, tf: str) -> pd.Timestamp:
    return pd.Timestamp(df.index[s.confirmed_index]) + tf_delta(tf)


def equal_levels(
    df: pd.DataFrame, swings: list[Swing], tf: str, tolerance: float, max_swings: int
) -> list[LiquidityLevel]:
    """Pairs of recent swing highs (lows) within `tolerance` of each other."""
    out: list[LiquidityLevel] = []
    pairs: tuple[tuple[Side, str, Callable[[float, float], float]], ...] = (
        ("high", "Equal highs", max),
        ("low", "Equal lows", min),
    )
    for side, name, pick in pairs:
        recent = [s for s in swings if s.kind == side][-max_swings:]
        for a_i, a in enumerate(recent):
            for b in recent[a_i + 1 :]:
                if abs(a.price - b.price) <= tolerance:
                    later = max(a, b, key=lambda s: s.confirmed_index)
                    out.append(
                        LiquidityLevel(
                            name, side, pick(a.price, b.price), _confirmed_time(df, later, tf)
                        )
                    )
    return out


def swing_levels(df: pd.DataFrame, swings: list[Swing], tf: str) -> list[LiquidityLevel]:
    return [
        LiquidityLevel(
            f"{tf} swing {s.kind}", s.kind, s.price, _confirmed_time(df, s, tf), sweepable=False
        )
        for s in swings
    ]


def first_breach_sweep(df: pd.DataFrame, level: LiquidityLevel, tf: str) -> Sweep | None:
    seg = df[df.index >= level.formed_at]
    if seg.empty:
        return None
    if level.side == "low":
        breached = seg[seg["low"] < level.price]
    else:
        breached = seg[seg["high"] > level.price]
    if breached.empty:
        return None
    bar = breached.iloc[0]
    t = breached.index[0]
    if level.side == "low" and bar["close"] > level.price:
        return Sweep(level, tf, t, t + tf_delta(tf), float(bar["low"]))
    if level.side == "high" and bar["close"] < level.price:
        return Sweep(level, tf, t, t + tf_delta(tf), float(bar["high"]))
    return None  # first breach closed through: level broken


def latest_sweep(
    frames: dict[str, pd.DataFrame],
    levels: list[LiquidityLevel],
    side: Side,
    now: datetime,
    max_age: timedelta,
) -> Sweep | None:
    found: list[Sweep] = []
    for tf, df in frames.items():
        for lvl in levels:
            if lvl.side != side or not lvl.sweepable:
                continue
            sw = first_breach_sweep(df, lvl, tf)
            if sw is not None and sw.close_time >= pd.Timestamp(now) - max_age:
                found.append(sw)
    if not found:
        return None
    # Most recent; among equals the deeper sweep.
    sign = 1 if side == "high" else -1
    return max(found, key=lambda s: (s.close_time, sign * s.extreme))


def is_untaken(level: LiquidityLevel, frames: list[pd.DataFrame]) -> bool:
    """No bar since the level formed has traded through it."""
    for df in frames:
        seg = df[df.index >= level.formed_at]
        if seg.empty:
            continue
        if level.side == "high" and seg["high"].max() > level.price:
            return False
        if level.side == "low" and seg["low"].min() < level.price:
            return False
    return True
