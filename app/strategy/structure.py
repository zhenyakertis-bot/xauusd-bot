"""Market structure: fractal swings, BOS / CHoCH, bias and dealing range.

No lookahead: a swing at bar i with lookback n is only known once bar i+n has closed,
and structure breaks only use swings confirmed before the breaking bar.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np
import pandas as pd

Side = Literal["high", "low"]
Trend = Literal["up", "down"]


@dataclass(frozen=True)
class Swing:
    kind: Side
    index: int  # position in the frame
    time: pd.Timestamp  # open time of the swing bar
    price: float
    confirmed_index: int  # bar whose close confirms the swing


@dataclass(frozen=True)
class StructureEvent:
    kind: Literal["BOS", "CHoCH"]
    direction: Trend
    level: float  # broken swing price
    swing_time: pd.Timestamp
    index: int  # breaking bar position
    time: pd.Timestamp  # breaking bar open time


def find_swings(df: pd.DataFrame, lookback: int) -> list[Swing]:
    """Fractal swings: the bar's high is the window max and strictly above the left side
    (so the first of equal highs wins). Same for lows."""
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    times = df.index
    n = len(df)
    swings: list[Swing] = []
    for i in range(lookback, n - lookback):
        left_h, right_h = highs[i - lookback : i], highs[i + 1 : i + lookback + 1]
        if highs[i] > left_h.max() and highs[i] >= right_h.max():
            swings.append(Swing("high", i, times[i], float(highs[i]), i + lookback))
        left_l, right_l = lows[i - lookback : i], lows[i + 1 : i + lookback + 1]
        if lows[i] < left_l.min() and lows[i] <= right_l.min():
            swings.append(Swing("low", i, times[i], float(lows[i]), i + lookback))
    swings.sort(key=lambda s: (s.confirmed_index, s.index))
    return swings


def structure_events(df: pd.DataFrame, swings: list[Swing]) -> list[StructureEvent]:
    """Walk bars in order; a candle *close* beyond the latest confirmed swing is a break.

    Break with the trend (or with no trend yet) = BOS; against the trend = CHoCH.
    Each swing can be broken once.
    """
    closes = df["close"].to_numpy()
    times = df.index
    events: list[StructureEvent] = []
    last_high: Swing | None = None
    last_low: Swing | None = None
    high_broken = low_broken = False
    trend: Trend | None = None
    ptr = 0
    for j in range(len(df)):
        while ptr < len(swings) and swings[ptr].confirmed_index < j:
            s = swings[ptr]
            if s.kind == "high":
                last_high, high_broken = s, False
            else:
                last_low, low_broken = s, False
            ptr += 1
        c = closes[j]
        if last_high is not None and not high_broken and c > last_high.price:
            kind: Literal["BOS", "CHoCH"] = "CHoCH" if trend == "down" else "BOS"
            events.append(StructureEvent(kind, "up", last_high.price, last_high.time, j, times[j]))
            trend, high_broken = "up", True
        elif last_low is not None and not low_broken and c < last_low.price:
            kind = "CHoCH" if trend == "up" else "BOS"
            events.append(StructureEvent(kind, "down", last_low.price, last_low.time, j, times[j]))
            trend, low_broken = "down", True
    return events


def bias_from_events(
    events: list[StructureEvent], choch_sets_bias: bool = False
) -> tuple[Trend | None, StructureEvent | None]:
    """Bias = direction of the last BOS. A trailing CHoCH means a possible reversal:
    bias is neutral unless `choch_sets_bias`."""
    if not events:
        return None, None
    last = events[-1]
    if last.kind == "BOS" or choch_sets_bias:
        return last.direction, last
    return None, last


@dataclass(frozen=True)
class DealingRange:
    low: float
    high: float

    def position(self, price: float) -> float:
        """0 = range low, 1 = range high (can fall outside [0, 1])."""
        span = self.high - self.low
        return float("nan") if span <= 0 else (price - self.low) / span


def dealing_range(
    df: pd.DataFrame, swings: list[Swing], event: StructureEvent
) -> DealingRange | None:
    """Leg that produced the break: for an up-break, from the last swing low confirmed
    before the break to the highest high since; mirrored for a down-break."""
    want: Side = "low" if event.direction == "up" else "high"
    anchors = [s for s in swings if s.kind == want and s.confirmed_index < event.index]
    if not anchors:
        return None
    anchor = anchors[-1]
    seg = df.iloc[anchor.index :]
    if event.direction == "up":
        return DealingRange(low=anchor.price, high=float(np.max(seg["high"].to_numpy())))
    return DealingRange(low=float(np.min(seg["low"].to_numpy())), high=anchor.price)
