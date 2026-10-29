"""Optional high-impact news blackout (off by default: news_filter.enabled).

Source: the public ForexFactory weekly calendar JSON. If it can't be fetched the filter
fails open (signals continue) and logs a warning, so a calendar outage never stops the bot.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

import httpx
import pandas as pd

from app.config import NewsFilterCfg

log = logging.getLogger(__name__)

CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"


class NewsFilter:
    def __init__(self, cfg: NewsFilterCfg, client: httpx.AsyncClient | None = None) -> None:
        self.cfg = cfg
        self._client = client or httpx.AsyncClient(timeout=20.0)
        self._events: list[tuple[pd.Timestamp, str]] = []
        self._fetched_at: datetime | None = None

    async def aclose(self) -> None:
        await self._client.aclose()

    async def refresh(self, now: datetime) -> None:
        if self._fetched_at and now - self._fetched_at < timedelta(hours=6):
            return
        try:
            resp = await self._client.get(CALENDAR_URL)
            resp.raise_for_status()
            events = []
            for e in resp.json():
                if e.get("country") in self.cfg.currencies and e.get("impact") in self.cfg.impact:
                    events.append((pd.Timestamp(e["date"]).tz_convert("UTC"), str(e.get("title"))))
            self._events = events
            self._fetched_at = now
        except Exception as exc:
            log.warning("news calendar unavailable; filter fails open", extra={"error": str(exc)})

    async def blocked(self, now: datetime) -> str | None:
        """Reason string if `now` is inside a news window, else None."""
        if not self.cfg.enabled:
            return None
        await self.refresh(now)
        window = pd.Timedelta(minutes=self.cfg.window_minutes)
        ts = pd.Timestamp(now)
        for t, title in self._events:
            if abs(ts - t) <= window:
                return f"news: {title} at {t:%H:%M} UTC"
        return None
