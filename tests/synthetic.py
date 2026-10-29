"""Builders for synthetic candle frames with known answers."""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import pandas as pd

from app.data.candles import normalize

OHLC = tuple[float, float, float, float]


def bars(start: str, ohlc: Sequence[OHLC], minutes: int = 15, spread: float = 0.3) -> pd.DataFrame:
    idx = pd.date_range(start, periods=len(ohlc), freq=f"{minutes}min", tz="UTC")
    arr = np.array(ohlc, dtype=float)
    return normalize(
        pd.DataFrame(
            {
                "open": arr[:, 0],
                "high": arr[:, 1],
                "low": arr[:, 2],
                "close": arr[:, 3],
                "volume": 1.0,
                "spread": spread,
            },
            index=idx,
        )
    )


def path_bars(
    points: Sequence[tuple[str, float]], minutes: int = 5, wick: float = 0.1, spread: float = 0.3
) -> pd.DataFrame:
    """Bars along a piecewise-linear price path. Each bar: open = p(t), close = p(t + tf),
    high/low = max/min(open, close) +/- wick. Monotonic legs produce no swings;
    swings appear exactly at the turning points."""
    times = pd.DatetimeIndex([pd.Timestamp(t, tz="UTC") for t, _ in points])
    prices = np.array([p for _, p in points])
    secs = (times - times[0]).total_seconds().to_numpy()
    idx = pd.date_range(times[0], times[-1], freq=f"{minutes}min", inclusive="left")
    t0 = (idx - times[0]).total_seconds().to_numpy()
    o = np.interp(t0, secs, prices)
    c = np.interp(t0 + minutes * 60, secs, prices)
    df = pd.DataFrame(
        {
            "open": o,
            "high": np.maximum(o, c) + wick,
            "low": np.minimum(o, c) - wick,
            "close": c,
            "volume": 1.0,
            "spread": spread,
        },
        index=idx,
    )
    return normalize(df)
