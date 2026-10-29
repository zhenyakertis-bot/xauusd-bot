"""Entry point: Telegram bot + live loop on a schedule (APScheduler)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import get_settings, load_strategy
from app.data.factory import make_provider
from app.live import LiveRunner, LogMessenger, Messenger
from app.logging_setup import setup_logging
from app.news import NewsFilter
from app.storage.db import Store
from app.telegram.bot import SignalBot

log = logging.getLogger("app.main")


def bar_close(now: datetime | None = None) -> datetime:
    """Latest M5 boundary (the close time of the bar that just finished)."""
    now = now or datetime.now(UTC)
    return now.replace(minute=now.minute - now.minute % 5, second=0, microsecond=0)


async def run() -> None:
    settings = get_settings()
    setup_logging(settings.log_level, settings.log_dir)
    cfg = load_strategy(settings.strategy_config)
    store = Store(settings.database_url)
    provider = make_provider(settings)
    news = NewsFilter(cfg.news_filter) if cfg.news_filter.enabled else None

    bot = SignalBot(settings, cfg, store) if settings.telegram_bot_token else None
    if bot is not None:
        try:
            await bot.start()
        except Exception:
            # Bad token / Telegram unreachable: keep generating and tracking, log messages.
            log.exception("telegram unavailable; continuing without it")
            bot = None
    messenger: Messenger = bot if bot is not None else LogMessenger()
    runner = LiveRunner(settings, cfg, provider, store, messenger, news)
    if bot is not None:
        bot.status = runner

    async def cycle_job() -> None:
        await runner.cycle(bar_close())
        with contextlib.suppress(OSError):
            settings.health_file.parent.mkdir(parents=True, exist_ok=True)
            settings.health_file.write_text(datetime.now(UTC).isoformat())

    async def report_job() -> None:
        await runner.daily_report(datetime.now(UTC))

    scheduler = AsyncIOScheduler(timezone=UTC)
    # 20 s after each M5 close so the data provider has finalised the bar.
    scheduler.add_job(
        cycle_job,
        CronTrigger(minute="*/5", second=20, timezone=UTC),
        max_instances=1,
        coalesce=True,
        misfire_grace_time=120,
    )
    at = cfg.monitoring.daily_report_after
    scheduler.add_job(
        report_job,
        CronTrigger(
            day_of_week="mon-fri",
            hour=at.hour,
            minute=at.minute,
            timezone=ZoneInfo(cfg.market_hours.tz),
        ),
    )
    scheduler.start()
    log.info(
        "bot started",
        extra={
            "data_provider": provider.name,
            "telegram": bot is not None,
            "admin_chat": settings.telegram_admin_chat_id is not None,
            "open_signals": len(runner.tracked),
        },
    )
    await messenger.send(
        f"Bot started. Data: {provider.name}. Open signals: {len(runner.tracked)}. /status"
    )
    await cycle_job()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()

    log.info("bot stopping")
    scheduler.shutdown(wait=False)
    if bot is not None:
        await bot.stop()
    if news is not None:
        await news.aclose()
    await provider.aclose()
    log.info("bot stopped")


def main() -> None:
    try:
        asyncio.run(run())
    except Exception:
        # Route crashes through logging so the traceback is redacted.
        log.exception("bot crashed")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
