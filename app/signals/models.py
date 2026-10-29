"""Signal domain model."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from zoneinfo import ZoneInfo

import pandas as pd

from app.strategy.liquidity import LiquidityLevel, Sweep
from app.strategy.structure import StructureEvent
from app.strategy.zones import FVG, Zone


class Direction(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class SignalStatus(StrEnum):
    PENDING = "PENDING"
    TRIGGERED = "TRIGGERED"
    TP1 = "TP1"
    TP2 = "TP2"
    SL = "SL"
    BREAKEVEN = "BREAKEVEN"
    EXPIRED = "EXPIRED"
    INVALIDATED = "INVALIDATED"


@dataclass
class Signal:
    direction: Direction
    session: str
    session_label: str
    created_at: pd.Timestamp
    expires_at: pd.Timestamp
    entry_zone: Zone
    entry: float
    stop: float
    tp1: float
    tp1_source: str
    tp2: float | None
    tp2_source: str | None
    rr1: float
    rr2: float | None
    bias_event: StructureEvent
    sweep: Sweep
    confirm_event: StructureEvent
    fvg: FVG
    order_block: Zone | None
    range_position: float
    targets_considered: list[LiquidityLevel] = field(default_factory=list)
    status: SignalStatus = SignalStatus.PENDING

    def explanation(
        self, precision: int = 2, tz: str | None = None, tz_label: str = ""
    ) -> list[str]:
        """Human-readable setup. Times in UTC, plus local session time when `tz` is given."""
        p = f"{{:.{precision}f}}".format

        def t(ts: pd.Timestamp) -> str:
            out = ts.tz_convert("UTC").strftime("%H:%M UTC")
            if tz:
                out += f" / {ts.tz_convert(ZoneInfo(tz)).strftime('%H:%M')} {tz_label}".rstrip()
            return out

        bias = "bullish" if self.direction is Direction.BUY else "bearish"
        b = self.bias_event
        lines = [
            f"M15 bias: {bias} ({b.kind} {'above' if b.direction == 'up' else 'below'} "
            f"{p(b.level)} at {t(b.time)})",
            f"Liquidity sweep: {self.sweep.level.name} {p(self.sweep.level.price)} swept at "
            f"{t(self.sweep.time)} ({self.sweep.tf}, wick {p(self.sweep.extreme)})",
            f"M5 {self.confirm_event.kind} confirmed at {t(self.confirm_event.time)} "
            f"(close beyond {p(self.confirm_event.level)})",
            f"Displacement + FVG {p(self.fvg.zone.low)}–{p(self.fvg.zone.high)} "
            f"(body {self.fvg.body / self.fvg.atr:.1f}× ATR)",
        ]
        if self.order_block is not None:
            lines.append(f"Order block {p(self.order_block.low)}–{p(self.order_block.high)}")
        where = "discount" if self.range_position < 0.5 else "premium"
        lines.append(f"Price in {where} ({self.range_position * 100:.0f}% of range)")
        lines.append(
            f"TP1 at {self.tp1_source}" + (f", TP2 at {self.tp2_source}" if self.tp2_source else "")
        )
        return lines
