"""Telegram message templates (plain text, no parse mode)."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from app.config import StrategyConfig
from app.risk.position_size import PositionSize
from app.signals.models import Signal, SignalStatus
from app.storage.db import SignalRow, utc

DISCLAIMER = "Paper signal. Not financial advice."
TRADINGVIEW = "https://www.tradingview.com/chart/?symbol=OANDA%3AXAUUSD"


def _p(x: float | None, precision: int) -> str:
    return "—" if x is None else f"{x:.{precision}f}"


def _time(ts: pd.Timestamp | datetime, tz: str | None = None, label: str = "") -> str:
    t = pd.Timestamp(ts)
    t = t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")
    out = t.strftime("%H:%M UTC")
    if tz:
        out += f" ({t.tz_convert(ZoneInfo(tz)).strftime('%H:%M')} {label})".replace(" )", ")")
    return out


def format_signal(
    s: Signal, size: PositionSize | None, risk_pct: float, cfg: StrategyConfig
) -> str:
    prec = cfg.instrument.price_precision
    sess = cfg.sessions[s.session]
    rr2 = f" / 1:{s.rr2:.1f}" if s.rr2 is not None else ""
    if size is not None:
        risk = (
            f"Risk: {risk_pct:.1f}% ({float(size.lots):.2f} lot = {float(size.ounces):g} oz, "
            f"≈${float(size.actual_risk):.0f})"
        )
    else:
        risk = f"Risk: {risk_pct:.1f}% (size below minimum lot)"
    lines = [
        f"{cfg.instrument.symbol} — {s.direction.value}",
        "Status: PAPER SIGNAL",
        f"Session: {s.session_label}",
        "Timeframe: M5 / M15",
        "",
        f"Entry zone: {_p(s.entry_zone.low, prec)} – {_p(s.entry_zone.high, prec)}"
        f" (limit {_p(s.entry, prec)})",
        f"Stop Loss: {_p(s.stop, prec)}",
        f"Take Profit 1: {_p(s.tp1, prec)}",
        f"Take Profit 2: {_p(s.tp2, prec)}",
        f"Risk/Reward: 1:{s.rr1:.1f}{rr2}",
        risk,
        f"Valid until: {_time(s.expires_at, sess.tz, s.session_label)}",
        "",
        "Setup:",
        *[f"* {line}" for line in s.explanation(prec, sess.tz, s.session_label)],
        "",
        f"Chart: {TRADINGVIEW}",
        "",
        DISCLAIMER,
    ]
    return "\n".join(lines)


UPDATE_TEXT = {
    SignalStatus.TRIGGERED: "Entry triggered at {price}",
    SignalStatus.TP1: "TP1 hit at {price} ({r}). Consider moving SL to breakeven {entry}.",
    SignalStatus.TP2: "TP2 hit at {price}. Trade closed: {r}",
    SignalStatus.SL: "SL hit at {price}. Trade closed: {r}",
    SignalStatus.BREAKEVEN: "Stopped at breakeven {price}. Trade closed: {r}",
    SignalStatus.EXPIRED: "Expired: entry zone not reached in time. No trade.",
    SignalStatus.INVALIDATED: "Invalidated: price hit the stop level before entry. No trade.",
}


def format_update(
    row: SignalRow,
    status: SignalStatus,
    price: float,
    when: datetime,
    result_r: float | None,
    cfg: StrategyConfig,
    final: bool = False,
) -> str:
    prec = cfg.instrument.price_precision
    if status is SignalStatus.TP1 and not final:
        frac = cfg.trade.tp1_close_fraction
        r = f"+{row.rr1 * frac:.2f}R booked on {frac:.0%}"
    else:
        r = "—" if result_r is None else f"{result_r:+.2f}R"
    text = UPDATE_TEXT[status].format(price=_p(price, prec), r=r, entry=_p(row.entry, prec))
    if status is SignalStatus.TP1 and final:
        text = f"TP1 hit at {_p(price, prec)}. Trade closed: {r}"
    return "\n".join(
        [
            f"{cfg.instrument.symbol} {row.direction} #{row.id} — {status.value}",
            f"{text} ({_time(when)})",
            "",
            DISCLAIMER,
        ]
    )


def format_row_short(row: SignalRow, prec: int) -> str:
    created = utc(row.created_at)
    assert created is not None
    res = "" if row.result_r is None else f" {row.result_r:+.2f}R"
    status = row.status if row.closed_at is not None or row.status != "TP1" else "TP1 (running)"
    return (
        f"#{row.id} {created:%m-%d %H:%M} {row.direction} {row.session} "
        f"entry {_p(row.entry, prec)} SL {_p(row.stop, prec)} TP1 {_p(row.tp1, prec)} "
        f"→ {status}{res}"
    )


def format_stats(period_metrics: dict[str, dict[str, Any]]) -> str:
    lines = ["XAUUSD paper signals — statistics", ""]
    for label, m in period_metrics.items():
        lines.append(f"{label}:")
        if not m.get("trades"):
            lines.append(f"  signals {m.get('signals', 0)}, no closed trades")
        else:
            lines += [
                f"  signals {m['signals']} (filled {m['trades']}, expired {m['expired']})",
                f"  winrate {m['winrate']:.0%}, avg {m['expectancy_r']:+.2f}R, "
                f"total {m['total_r']:+.2f}R",
                f"  max drawdown {m['max_drawdown_r']:.2f}R",
            ]
        lines.append("")
    lines.append(DISCLAIMER)
    return "\n".join(lines)


def format_daily_report(day: str, rows: list[SignalRow], prec: int) -> str:
    closed_r = [r.result_r for r in rows if r.closed_at is not None and r.result_r is not None]
    lines = [f"XAUUSD daily report — {day}", ""]
    if not rows:
        lines.append("No signals today.")
    else:
        lines += [format_row_short(r, prec) for r in rows]
        lines += ["", f"Closed: {len(closed_r)}, result {sum(closed_r):+.2f}R"]
    lines += ["", DISCLAIMER]
    return "\n".join(lines)
