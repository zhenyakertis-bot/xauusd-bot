"""Live loop: every M5 close fetch data, track open signals, look for new ones.

`LiveRunner.cycle(now)` is a plain coroutine with an explicit clock, so the same path is
exercised by the scheduler and by replay tests on historical data.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

import pandas as pd

from app.config import Settings, StrategyConfig
from app.data.base import MarketDataProvider
from app.data.candles import tf_delta
from app.news import NewsFilter
from app.risk.position_size import InvalidTradeError, position_size
from app.sessions import active_signal_session, is_market_open
from app.signals.dedup import Emitted, SignalGate, signal_key
from app.signals.models import Direction, SignalStatus
from app.signals.tracker import TradeTracker
from app.storage.db import SignalRow, Store, utc
from app.strategy.smc_engine import SMCEngine
from app.strategy.structure import bias_from_events, find_swings, structure_events
from app.telegram.formatter import format_daily_report, format_signal, format_update

log = logging.getLogger(__name__)


class Messenger(Protocol):
    async def send(self, text: str, reply_to: int | None = None) -> int | None: ...


class LogMessenger:
    """Fallback when Telegram isn't configured: messages go to the log."""

    async def send(self, text: str, reply_to: int | None = None) -> int | None:
        log.info("message (telegram not configured)", extra={"text": text, "reply_to": reply_to})
        return None


@dataclass
class _Tracked:
    row_id: int
    tracker: TradeTracker


class LiveRunner:
    def __init__(
        self,
        settings: Settings,
        cfg: StrategyConfig,
        provider: MarketDataProvider,
        store: Store,
        messenger: Messenger,
        news: NewsFilter | None = None,
    ) -> None:
        self.settings, self.cfg, self.provider, self.store = settings, cfg, provider, store
        self.messenger = messenger
        self.news = news
        self.engine = SMCEngine(cfg)
        self.gate = SignalGate(cfg.signals)
        self.tracked: dict[int, _Tracked] = {}
        self.m5 = pd.DataFrame()
        self.m15 = pd.DataFrame()
        self.last_price: float | None = None
        self.last_data_at: pd.Timestamp | None = None
        self.last_stage: str = "-"
        self.stage_counts: Counter[str] = Counter()
        self.stale_alerted = False
        self.failures = 0
        self._restore()

    # --- state -----------------------------------------------------------------------

    def _restore(self) -> None:
        """Rebuild trackers and the dedup gate from the database after a restart."""
        for row in self.store.open_signals():
            self.tracked[row.id] = _Tracked(row.id, self._tracker_from_row(row))
        recent = self.store.since(pd.Timestamp.now(tz="UTC").to_pydatetime() - timedelta(days=2))
        for r in recent:
            created = utc(r.created_at)
            assert created is not None
            self.gate.history.append(
                Emitted(r.key, Direction(r.direction), f"{r.session}:{created.date()}", created)
            )
        if self.tracked:
            log.info("restored open signals", extra={"ids": list(self.tracked)})

    def _tracker_from_row(self, row: SignalRow) -> TradeTracker:
        expires, created = utc(row.expires_at), utc(row.created_at)
        assert expires is not None and created is not None
        # Bars before the signal was created are history, not trade management.
        last_bar = utc(row.tracked_until) or created - timedelta(minutes=1)
        tr = TradeTracker(
            direction=Direction(row.direction),
            entry=row.entry,
            stop=row.stop,
            tp1=row.tp1,
            tp2=row.tp2,
            expires_at=expires,
            tp1_close_fraction=self.cfg.trade.tp1_close_fraction,
            slippage=self.cfg.backtest.slippage,
            status=SignalStatus(row.status),
            triggered_at=utc(row.triggered_at),
            mfe_r=row.mfe_r,
            mae_r=row.mae_r,
            last_bar=last_bar,
        )
        tr.restore_booked()
        return tr

    @property
    def paused(self) -> bool:
        return bool(self.store.get_flag("paused", False))

    # --- one cycle -----------------------------------------------------------------------

    async def cycle(self, now: datetime) -> None:
        if not is_market_open(now, self.cfg.market_hours):
            return  # weekend / daily break: quiet, no data calls, no alerts
        try:
            session = active_signal_session(self.cfg.sessions, now)
            if self.tracked:
                await self._track(now)
            if session is not None:
                await self._refresh_frames(now)
                if not self.paused:
                    await self._look_for_signal(now)
            elif not self.tracked and now.minute % 15 == 0:
                self.last_price = await self.provider.get_price()
                self.last_data_at = pd.Timestamp(now)
            self.failures = 0
        except Exception as exc:
            self.failures += 1
            log.exception("cycle failed", extra={"failures": self.failures})
            if self.failures == 3:
                await self.messenger.send(f"Bot alert: data cycle failing ({exc}). Retrying.")
        await self._heartbeat(now)

    async def _refresh_frames(self, now: datetime) -> None:
        sc = self.cfg.structure
        self.m5 = await self.provider.get_latest(sc.entry_timeframe, sc.lookback_bars_entry)
        if self.m15.empty or now.minute % 15 == 0:
            self.m15 = await self.provider.get_latest(sc.bias_timeframe, sc.lookback_bars_bias)
        if not self.m5.empty:
            self.last_price = float(self.m5["close"].iloc[-1])
            self.last_data_at = self.m5.index[-1] + tf_delta(sc.entry_timeframe)

    async def _look_for_signal(self, now: datetime) -> None:
        ev = self.engine.evaluate(self.m5, self.m15, now)
        self.last_stage = f"{ev.stage}: {ev.reason}"
        self.stage_counts[ev.stage] += 1
        if ev.signal is None:
            log.debug("no signal", extra={"stage": ev.stage, "reason": ev.reason})
            return
        s = ev.signal
        if self.news is not None and (reason := await self.news.blocked(now)):
            log.info("signal skipped", extra={"reason": reason})
            return
        active = Counter(Direction(t.tracker.direction) for t in self.tracked.values())
        blocked = self.gate.check(s, dict(active))
        if blocked is not None:
            log.info("signal blocked", extra={"reason": blocked, "direction": s.direction.value})
            return
        self.gate.record(s)

        try:
            size = position_size(
                balance=self.settings.paper_balance or 10_000.0,
                risk_percent=self.settings.risk_percent,
                entry=s.entry,
                stop=s.stop,
                instrument=self.cfg.instrument,
            )
        except InvalidTradeError as exc:
            log.info("signal without valid size", extra={"error": str(exc)})
            size = None
        prec = self.cfg.instrument.price_precision
        sess = self.cfg.sessions[s.session]
        explanation = s.explanation(prec, sess.tz, s.session_label)
        row_id = self.store.add_signal(
            signal_key(s, self.cfg.signals.price_rounding), s, size, explanation
        )
        msg_id = await self.messenger.send(
            format_signal(s, size, self.settings.risk_percent, self.cfg)
        )
        self.store.update(row_id, message_id=msg_id, chat_id=self.settings.telegram_admin_chat_id)
        row = self.store.get(row_id)
        assert row is not None
        self.tracked[row_id] = _Tracked(row_id, self._tracker_from_row(row))
        log.info(
            "signal sent",
            extra={"id": row_id, "direction": s.direction.value, "entry": s.entry, "stop": s.stop},
        )

    async def _track(self, now: datetime) -> None:
        since = min(
            (t.tracker.last_bar for t in self.tracked.values() if t.tracker.last_bar is not None),
            default=pd.Timestamp(now) - timedelta(hours=2),
        )
        m1 = await self.provider.get_candles("M1", since.to_pydatetime(), now)
        if m1.empty:
            return
        self.last_price = float(m1["close"].iloc[-1])
        self.last_data_at = m1.index[-1] + timedelta(minutes=1)
        spread_default = self.cfg.trade.assumed_spread
        for tid, t in list(self.tracked.items()):
            row = self.store.get(tid)
            assert row is not None
            times = pd.DatetimeIndex(m1.index)
            highs, lows, spreads = (m1[c].to_numpy() for c in ("high", "low", "spread"))
            for i, ts in enumerate(times):
                spread = float(spreads[i]) if spreads[i] > 0 else spread_default
                for e in t.tracker.update(ts, float(highs[i]), float(lows[i]), spread):
                    await self._on_event(row, t.tracker, e.status, e.price, e.time)
                if t.tracker.done:
                    break
            tr = t.tracker
            self.store.update(
                tid,
                status=tr.status.value,
                triggered_at=tr.triggered_at,
                closed_at=tr.closed_at,
                result_r=tr.result_r,
                mfe_r=tr.mfe_r,
                mae_r=tr.mae_r,
                tracked_until=tr.last_bar,
            )
            if tr.done:
                del self.tracked[tid]

    async def _on_event(
        self,
        row: SignalRow,
        tr: TradeTracker,
        status: SignalStatus,
        price: float,
        when: pd.Timestamp,
    ) -> None:
        text = format_update(row, status, price, when, tr.result_r, self.cfg, final=tr.done)
        await self.messenger.send(text, reply_to=row.message_id)
        log.info("signal update", extra={"id": row.id, "status": status.value, "price": price})

    async def _heartbeat(self, now: datetime) -> None:
        if self.last_data_at is None or not is_market_open(now, self.cfg.market_hours):
            return
        age = pd.Timestamp(now) - self.last_data_at
        stale = age > timedelta(minutes=self.cfg.monitoring.heartbeat_stale_minutes)
        if stale and not self.stale_alerted:
            self.stale_alerted = True
            await self.messenger.send(
                f"Bot alert: no new market data for {int(age.total_seconds() // 60)} min "
                "during trading hours."
            )
        elif not stale and self.stale_alerted:
            self.stale_alerted = False
            await self.messenger.send("Bot: market data is flowing again.")

    # --- reports / status --------------------------------------------------------------

    async def daily_report(self, now: datetime) -> None:
        ny = pd.Timestamp(now).tz_convert(self.cfg.market_hours.tz)
        start = ny.normalize().tz_convert("UTC").to_pydatetime()
        rows = self.store.since(start)
        await self.messenger.send(
            format_daily_report(f"{ny:%Y-%m-%d}", rows, self.cfg.instrument.price_precision)
        )

    async def snapshot(self) -> dict[str, str]:
        now = pd.Timestamp.now(tz="UTC")
        sess = active_signal_session(self.cfg.sessions, now.to_pydatetime())
        bias = "-"
        if not self.m15.empty:
            sw = find_swings(self.m15, self.cfg.structure.swing_lookback)
            b, ev = bias_from_events(
                structure_events(self.m15, sw), self.cfg.structure.choch_sets_bias
            )
            bias = f"{b or 'neutral'}" + (
                f" (last {ev.kind} {ev.direction} {ev.level:.2f})" if ev else ""
            )
        prec = self.cfg.instrument.price_precision
        return {
            "Market": "open"
            if is_market_open(now.to_pydatetime(), self.cfg.market_hours)
            else "closed",
            "Session": sess.label if sess else "outside kill zones",
            "Last price": "-" if self.last_price is None else f"{self.last_price:.{prec}f}",
            "Data as of": "-"
            if self.last_data_at is None
            else f"{self.last_data_at:%Y-%m-%d %H:%M} UTC",
            "M15 bias": bias,
            "Last check": self.last_stage,
            "Data provider": self.provider.name,
        }
