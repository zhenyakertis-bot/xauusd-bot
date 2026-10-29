"""Duplicate protection and signal rate limits, shared by live and backtest."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import timedelta

import pandas as pd

from app.config import SignalsCfg
from app.signals.models import Direction, Signal


def _round(x: float, step: float) -> str:
    return f"{round(x / step) * step:.2f}"


def signal_key(s: Signal, step: float) -> str:
    """Direction + session + date + sweep level + entry zone (rounded)."""
    raw = "|".join(
        [
            s.direction.value,
            s.session,
            s.created_at.date().isoformat(),
            _round(s.sweep.level.price, step),
            _round(s.entry_zone.low, step),
            _round(s.entry_zone.high, step),
        ]
    )
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


@dataclass
class Emitted:
    key: str
    direction: Direction
    session_id: str  # session name + date
    time: pd.Timestamp


@dataclass
class SignalGate:
    cfg: SignalsCfg
    history: list[Emitted] = field(default_factory=list)

    def check(self, s: Signal, active: dict[Direction, int]) -> str | None:
        """None if the signal may be sent, otherwise the reason it is blocked.
        `active`: currently open (pending/triggered) signals per direction."""
        key = signal_key(s, self.cfg.price_rounding)
        if any(e.key == key for e in self.history):
            return "duplicate"
        if active.get(s.direction, 0) >= self.cfg.max_active_per_direction:
            return f"already {active[s.direction]} active {s.direction.value}"
        if self.history:
            last = self.history[-1].time
            if s.created_at - last < timedelta(minutes=self.cfg.signal_cooldown_minutes):
                return "cooldown"
        sid = f"{s.session}:{s.created_at.date()}"
        if sum(e.session_id == sid for e in self.history) >= self.cfg.max_signals_per_session:
            return "session limit"
        return None

    def record(self, s: Signal) -> None:
        self.history.append(
            Emitted(
                signal_key(s, self.cfg.price_rounding),
                s.direction,
                f"{s.session}:{s.created_at.date()}",
                s.created_at,
            )
        )
