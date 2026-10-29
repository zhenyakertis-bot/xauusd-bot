"""ATR, displacement + fair value gaps, order blocks."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from app.strategy.structure import Trend


@dataclass(frozen=True)
class Zone:
    low: float
    high: float

    @property
    def size(self) -> float:
        return self.high - self.low


def intersect(a: Zone, b: Zone) -> Zone | None:
    low, high = max(a.low, b.low), min(a.high, b.high)
    return Zone(low, high) if high > low else None


def atr(df: pd.DataFrame, period: int) -> pd.Series:
    """Wilder ATR; NaN until `period` bars are available."""
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


@dataclass(frozen=True)
class FVG:
    direction: Trend
    zone: Zone
    displacement_index: int  # middle candle
    displacement_time: pd.Timestamp
    completed_index: int  # third candle; the gap exists once it closes
    body: float
    atr: float


def find_fvgs(
    df: pd.DataFrame,
    direction: Trend,
    atr_series: pd.Series,
    atr_mult: float,
    min_size: float,
    start_index: int = 0,
) -> list[FVG]:
    """Three-candle gaps whose middle candle is a displacement in `direction`:
    body >= atr_mult * ATR (ATR as of the candle before it, so the impulse
    doesn't inflate its own yardstick)."""
    o, h, lo, c = (df[k].to_numpy() for k in ("open", "high", "low", "close"))
    a = atr_series.to_numpy()
    out: list[FVG] = []
    for i in range(max(2, start_index + 1), len(df)):
        c1, c2 = i - 2, i - 1
        body = c[c2] - o[c2] if direction == "up" else o[c2] - c[c2]
        ref = a[c1]
        if np.isnan(ref) or body <= 0 or body < atr_mult * ref:
            continue
        if direction == "up":
            zone = Zone(float(h[c1]), float(lo[i]))
        else:
            zone = Zone(float(h[i]), float(lo[c1]))
        if zone.size >= min_size and zone.size > 0:
            out.append(FVG(direction, zone, c2, df.index[c2], i, float(body), float(ref)))
    return out


def order_block(df: pd.DataFrame, fvg: FVG, max_lookback: int) -> tuple[Zone, pd.Timestamp] | None:
    """Last opposite-colour candle before the displacement (its full range)."""
    o, c = df["open"].to_numpy(), df["close"].to_numpy()
    stop = max(-1, fvg.displacement_index - 1 - max_lookback)
    for k in range(fvg.displacement_index - 1, stop, -1):
        opposite = c[k] < o[k] if fvg.direction == "up" else c[k] > o[k]
        if opposite:
            return Zone(float(df["low"].iloc[k]), float(df["high"].iloc[k])), df.index[k]
    return None
