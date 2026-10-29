"""Historical M1 data for backtests (Dukascopy, cached per day as parquet).

Prefetch: python -m app.backtest.data --from 2025-01-01 --to 2026-10-28
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import UTC, datetime, timedelta

import httpx
import pandas as pd

from app.config import get_settings
from app.data.candles import ParquetCache, empty_frame, normalize
from app.data.dukascopy import DukascopyProvider
from app.logging_setup import setup_logging

log = logging.getLogger(__name__)


def parse_date(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=UTC)


async def _fetch_day(
    provider: DukascopyProvider, day: datetime, nxt: datetime, attempts: int = 6
) -> pd.DataFrame:
    """The datafeed rate-limits bulk downloads; back off for minutes, then resume."""
    for attempt in range(1, attempts + 1):
        try:
            return await provider.get_m1(day, nxt)
        except httpx.HTTPError as exc:
            if attempt == attempts:
                raise
            pause = 120 * attempt
            log.warning(
                "rate limited, pausing",
                extra={"day": day.date().isoformat(), "pause_s": pause, "error": str(exc)},
            )
            await asyncio.sleep(pause)
    raise AssertionError("unreachable")


async def load_m1(start: datetime, end: datetime, progress: bool = False) -> pd.DataFrame:
    """M1 bars in [start, end), fetched day by day (cached days are instant)."""
    provider = DukascopyProvider(ParquetCache(get_settings().cache_dir))
    frames: list[pd.DataFrame] = []
    day = start
    total = (end - start).days or 1
    try:
        while day < end:
            nxt = min(day + timedelta(days=1), end)
            df = await _fetch_day(provider, day, nxt)
            if not df.empty:
                frames.append(df)
            done = (day - start).days + 1
            if progress and (done % 20 == 0 or nxt >= end):
                log.info(
                    "history download",
                    extra={"day": day.date().isoformat(), "done": done, "total": total},
                )
            day = nxt
    finally:
        await provider.aclose()
    return normalize(pd.concat(frames)) if frames else empty_frame()


def main() -> None:
    ap = argparse.ArgumentParser(description="Prefetch Dukascopy M1 history into the cache")
    ap.add_argument("--from", dest="start", required=True)
    ap.add_argument("--to", dest="end", required=True)
    args = ap.parse_args()
    settings = get_settings()
    setup_logging(settings.log_level, None)
    m1 = asyncio.run(load_m1(parse_date(args.start), parse_date(args.end), progress=True))
    log.info(
        "history ready",
        extra={
            "bars": len(m1),
            "first": str(m1.index[0]) if len(m1) else None,
            "last": str(m1.index[-1]) if len(m1) else None,
        },
    )


if __name__ == "__main__":
    main()
