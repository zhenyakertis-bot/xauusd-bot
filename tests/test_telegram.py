from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.config import Settings, load_strategy
from app.risk.position_size import position_size
from app.signals.models import SignalStatus
from app.storage.db import Store, rows_to_frame
from app.telegram.bot import SignalBot
from app.telegram.demo import demo_signal
from app.telegram.formatter import (
    DISCLAIMER,
    format_daily_report,
    format_signal,
    format_stats,
    format_update,
)

CFG = load_strategy(Path(__file__).resolve().parents[1] / "config" / "strategy.yaml")


def _store_with_signal() -> tuple[Store, int]:
    store = Store("sqlite:///:memory:")
    s = demo_signal()
    sid = store.add_signal("k1", s, None, s.explanation())
    return store, sid


def test_signal_message_matches_spec_template() -> None:
    s = demo_signal()
    size = position_size(
        balance=10_000, risk_percent=1.0, entry=s.entry, stop=s.stop, instrument=CFG.instrument
    )
    text = format_signal(s, size, 1.0, CFG)
    lines = text.splitlines()
    assert lines[:4] == [
        "XAUUSD — BUY",
        "Status: PAPER SIGNAL",
        "Session: London",
        "Timeframe: M5 / M15",
    ]
    assert "Entry zone: 2651.20 – 2652.40 (limit 2652.40)" in text
    assert "Stop Loss: 2648.90" in text
    assert "Take Profit 1: 2659.80" in text
    assert "Take Profit 2: 2663.20" in text
    assert "Risk/Reward: 1:2.1 / 1:3.1" in text
    assert "Risk: 1.0% (0.28 lot = 28 oz, ≈$98)" in text
    assert "* Liquidity sweep: Asian low 2649.60 swept at 07:20 UTC / 07:20 London" in text
    assert lines[-1] == DISCLAIMER


def test_session_local_time_differs_in_summer() -> None:
    s = demo_signal("2026-07-01")
    text = format_signal(s, None, 1.0, CFG)
    assert "07:20 UTC / 08:20 London" in text  # BST
    assert "Take Profit 2: 2663.20" in text


def test_every_update_has_disclaimer() -> None:
    store, sid = _store_with_signal()
    row = store.get(sid)
    assert row is not None
    now = datetime(2026, 10, 29, 8, tzinfo=UTC)
    for st in SignalStatus:
        if st is SignalStatus.PENDING:
            continue
        text = format_update(row, st, 2652.4, now, 1.0, CFG)
        assert text.endswith(DISCLAIMER)
        assert f"#{sid}" in text
    tp1 = format_update(row, SignalStatus.TP1, 2659.8, now, None, CFG)
    assert "breakeven 2652.40" in tp1


def test_store_open_and_close() -> None:
    store, sid = _store_with_signal()
    assert [r.id for r in store.open_signals()] == [sid]
    store.update(sid, status="TP1")  # runner still open
    assert len(store.open_signals()) == 1
    store.update(sid, status="TP2", result_r=2.5, closed_at=datetime(2026, 10, 29, 9, tzinfo=UTC))
    assert store.open_signals() == []
    df = rows_to_frame(store.last(5))
    assert df.iloc[0]["status"] == "TP2" and df.iloc[0]["result_r"] == 2.5
    assert store.keys() == {"k1"}


def test_flags() -> None:
    store = Store("sqlite:///:memory:")
    assert store.get_flag("paused", False) is False
    store.set_flag("paused", True)
    assert store.get_flag("paused") is True


def test_stats_and_daily_report_text() -> None:
    store, sid = _store_with_signal()
    store.update(sid, status="SL", result_r=-1.0, closed_at=datetime(2026, 10, 29, 9, tzinfo=UTC))
    from app.backtest.report import metrics

    m = metrics(rows_to_frame(store.since(datetime(2026, 10, 1, tzinfo=UTC))))
    text = format_stats({"Last 7 days": m, "Last 30 days": {"signals": 0}})
    assert "winrate 0%" in text and "total -1.00R" in text and text.endswith(DISCLAIMER)
    rep = format_daily_report("2026-10-29", store.last(5), 2)
    assert "→ SL -1.00R" in rep and "result -1.00R" in rep


def test_bot_requires_token() -> None:
    with pytest.raises(RuntimeError):
        SignalBot(
            Settings(_env_file=None, telegram_bot_token=None), CFG, Store("sqlite:///:memory:")
        )  # type: ignore[call-arg]


def test_commands_restricted_to_allowed_chats() -> None:
    from telegram.ext import CommandHandler

    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, telegram_bot_token="123456:" + "x" * 35, telegram_admin_chat_id=42
    )
    bot = SignalBot(settings, CFG, Store("sqlite:///:memory:"))
    handlers = [
        h for group in bot.app.handlers.values() for h in group if isinstance(h, CommandHandler)
    ]
    guarded = [h for h in handlers if "stats" in h.commands]
    assert guarded and all(42 in h.filters.chat_ids for h in guarded)  # type: ignore[union-attr]
    _ = timedelta
