"""Telegram bot: admin-only commands, signal messages and threaded updates.

CLI helpers:
    python -m app.telegram.bot whoami        # list chats that pressed /start (find your chat id)
    python -m app.telegram.bot test-signal   # send a demo signal to TELEGRAM_ADMIN_CHAT_ID
    python -m app.telegram.bot run           # answer commands only (no signal generation)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes, filters

from app.backtest.report import metrics
from app.config import Settings, StrategyConfig, get_settings, load_strategy
from app.logging_setup import setup_logging
from app.storage.db import Store, rows_to_frame
from app.telegram.formatter import DISCLAIMER, format_row_short, format_stats

log = logging.getLogger(__name__)

Handler = Callable[[Update, ContextTypes.DEFAULT_TYPE], Coroutine[Any, Any, None]]


class StatusSource(Protocol):
    async def snapshot(self) -> dict[str, str]: ...


class SignalBot:
    def __init__(
        self,
        settings: Settings,
        cfg: StrategyConfig,
        store: Store,
        status: StatusSource | None = None,
    ) -> None:
        if settings.telegram_bot_token is None:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
        self.settings, self.cfg, self.store, self.status = settings, cfg, store, status
        self.admin = settings.telegram_admin_chat_id
        self.app = (
            Application.builder().token(settings.telegram_bot_token.get_secret_value()).build()
        )

        allowed = sorted(settings.allowed_chat_ids)
        commands: dict[str, Handler] = {
            "start": self._start,
            "status": self._status,
            "active": self._active,
            "stats": self._stats,
            "last": self._last,
            "pause": self._pause,
            "resume": self._resume,
            "settings": self._settings,
            "help": self._start,
        }
        if allowed:
            only_admin = filters.Chat(chat_id=allowed)
            for name, fn in commands.items():
                self.app.add_handler(CommandHandler(name, fn, filters=only_admin))
        # Anyone else: /start only reveals their own chat id until an admin is configured.
        self.app.add_handler(CommandHandler("start", self._start_stranger))

    @property
    def bot(self) -> Bot:
        return self.app.bot

    # --- lifecycle -----------------------------------------------------------------

    async def start(self) -> None:
        await self.app.initialize()
        await self.app.start()
        assert self.app.updater is not None
        await self.app.updater.start_polling(drop_pending_updates=True)
        log.info("telegram polling started", extra={"admin_configured": self.admin is not None})

    async def stop(self) -> None:
        if self.app.updater is not None and self.app.updater.running:
            await self.app.updater.stop()
        if self.app.running:
            await self.app.stop()
        await self.app.shutdown()

    # --- outgoing ------------------------------------------------------------------

    async def send(self, text: str, reply_to: int | None = None) -> int | None:
        """Send to the admin chat; returns the message id (None if no admin is set)."""
        if self.admin is None:
            log.warning("no TELEGRAM_ADMIN_CHAT_ID; message not sent")
            return None
        for attempt in range(4):
            try:
                msg = await self.bot.send_message(
                    chat_id=self.admin,
                    text=text,
                    reply_to_message_id=reply_to,
                    allow_sending_without_reply=True,
                    disable_web_page_preview=True,
                )
                return int(msg.message_id)
            except Exception as exc:  # network errors, flood control
                log.warning("telegram send failed", extra={"attempt": attempt, "error": str(exc)})
                await asyncio.sleep(2**attempt)
        log.error("telegram send gave up")
        return None

    # --- commands ------------------------------------------------------------------

    async def _reply(self, update: Update, text: str) -> None:
        if update.effective_message is not None:
            await update.effective_message.reply_text(text, disable_web_page_preview=True)

    async def _start_stranger(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        if chat is None or chat.id in self.settings.allowed_chat_ids:
            return
        log.warning("command from unauthorized chat", extra={"chat_id": chat.id})
        if self.admin is None:
            await self._reply(
                update,
                f"Your chat id is {chat.id}.\n"
                "Set TELEGRAM_ADMIN_CHAT_ID to this value and restart the bot to get access.",
            )

    async def _start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await self._reply(
            update,
            "XAUUSD SMC paper-signal bot.\n"
            "Commands: /status /active /stats /last /pause /resume /settings\n\n" + DISCLAIMER,
        )
        await self._status(update, ctx)

    async def _status(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        snap: dict[str, str] = {}
        if self.status is not None:
            try:
                snap = await self.status.snapshot()
            except Exception as exc:
                snap = {"error": str(exc)}
        paused = bool(self.store.get_flag("paused", False))
        lines = [
            f"Bot: {'PAUSED' if paused else 'running'}",
            f"Open signals: {len(self.store.open_signals())}",
            *[f"{k}: {v}" for k, v in snap.items()],
        ]
        await self._reply(update, "\n".join(lines))

    async def _active(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        rows = self.store.open_signals()
        prec = self.cfg.instrument.price_precision
        text = "\n".join(format_row_short(r, prec) for r in rows) if rows else "No active signals."
        await self._reply(update, text)

    async def _stats(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        now = datetime.now(UTC)
        out: dict[str, dict[str, Any]] = {}
        for days in (7, 30):
            rows = self.store.since(now - timedelta(days=days))
            out[f"Last {days} days"] = metrics(rows_to_frame(rows)) if rows else {"signals": 0}
        await self._reply(update, format_stats(out))

    async def _last(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        rows = self.store.last(5)
        prec = self.cfg.instrument.price_precision
        text = "\n".join(format_row_short(r, prec) for r in rows) if rows else "No signals yet."
        await self._reply(update, text)

    async def _pause(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        self.store.set_flag("paused", True)
        log.info("signal generation paused via telegram")
        await self._reply(update, "Paused: no new signals. Open signals are still tracked.")

    async def _resume(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        self.store.set_flag("paused", False)
        log.info("signal generation resumed via telegram")
        await self._reply(update, "Resumed: generating signals in London / New York kill zones.")

    async def _settings(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        c = self.cfg
        sess = ", ".join(
            f"{w.label or n} {w.start:%H:%M}-{w.end:%H:%M} {w.tz}"
            for n, w in c.sessions.items()
            if w.signals
        )
        lines = [
            f"Sessions: {sess}",
            f"Risk: {self.settings.risk_percent:g}% of {self.settings.paper_balance or 0:,.0f} "
            f"{self.settings.account_currency}",
            f"Swings: M15 {c.structure.swing_lookback} / "
            f"M5 {c.structure.entry_swing_lookback} bars; "
            f"CHoCH sets bias: {c.structure.choch_sets_bias}; "
            f"premium/discount filter: {c.structure.require_premium_discount}",
            f"Liquidity: Asia {c.liquidity.use_asian_range}, "
            f"prev day {c.liquidity.use_previous_day}, "
            f"equal ±{c.liquidity.eq_tolerance}; sweep ≤{c.liquidity.sweep_max_age_minutes} min",
            f"Displacement ≥{c.confirmation.displacement_atr_mult}×ATR{c.confirmation.atr_period}, "
            f"FVG ≥{c.confirmation.min_fvg_size}",
            f"Min R:R TP1 {c.trade.min_rr_tp1}, TP2 {c.trade.tp2_r}R, "
            f"expiry {c.trade.entry_expiry_minutes} min, entry {c.trade.entry_mode}",
            f"Limits: cooldown {c.signals.signal_cooldown_minutes} min, "
            f"≤{c.signals.max_signals_per_session}/session, "
            f"≤{c.signals.max_active_per_direction} active per direction",
            f"News filter: {'on' if c.news_filter.enabled else 'off'}",
        ]
        await self._reply(update, "\n".join(lines))


# --- CLI ---------------------------------------------------------------------------


async def _whoami(settings: Settings) -> None:
    assert settings.telegram_bot_token is not None
    bot = Bot(settings.telegram_bot_token.get_secret_value())
    async with bot:
        me = await bot.get_me()
        print(f"Bot: @{me.username}")
        updates = await bot.get_updates(timeout=5)
        chats = {u.effective_chat.id: u.effective_chat for u in updates if u.effective_chat}
        if not chats:
            print("No messages yet: open the bot in Telegram, press Start, then run this again.")
        for cid, chat in chats.items():
            name = chat.username or chat.title or chat.full_name or ""
            print(f"chat id {cid}  ({chat.type}, {name})")


async def _test_signal(settings: Settings, cfg: StrategyConfig) -> None:
    from app.telegram.demo import demo_message

    store = Store("sqlite:///:memory:")
    bot = SignalBot(settings, cfg, store)
    await bot.app.initialize()
    try:
        msg_id = await bot.send(demo_message(cfg, settings))
        if msg_id is None:
            print("Not sent: set TELEGRAM_ADMIN_CHAT_ID (see `whoami`).")
            return
        await bot.send(
            "XAUUSD BUY #0 — TRIGGERED\nEntry triggered (demo update).\n\n" + DISCLAIMER, msg_id
        )
        print(f"Sent demo signal (message {msg_id}) and a threaded update.")
    finally:
        await bot.app.shutdown()


async def _run(settings: Settings, cfg: StrategyConfig) -> None:
    bot = SignalBot(settings, cfg, Store(settings.database_url))
    await bot.start()
    try:
        await asyncio.Event().wait()
    finally:
        await bot.stop()


def main() -> None:
    ap = argparse.ArgumentParser(description="Telegram helpers")
    ap.add_argument("command", choices=["whoami", "test-signal", "run"])
    args = ap.parse_args()
    settings = get_settings()
    setup_logging(settings.log_level, None)
    if settings.telegram_bot_token is None:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set")
    cfg = load_strategy(settings.strategy_config)
    if args.command == "whoami":
        asyncio.run(_whoami(settings))
    elif args.command == "test-signal":
        asyncio.run(_test_signal(settings, cfg))
    else:
        asyncio.run(_run(settings, cfg))


if __name__ == "__main__":
    main()
