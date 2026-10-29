"""A fixed demo signal (the example from the spec) for checking the Telegram format."""

from __future__ import annotations

import pandas as pd

from app.config import Settings, StrategyConfig
from app.risk.position_size import InvalidTradeError, position_size
from app.signals.models import Direction, Signal
from app.strategy.liquidity import LiquidityLevel, Sweep
from app.strategy.structure import StructureEvent
from app.strategy.zones import FVG, Zone
from app.telegram.formatter import format_signal


def demo_signal(day: str = "2026-10-29") -> Signal:
    def t(hm: str) -> pd.Timestamp:
        return pd.Timestamp(f"{day} {hm}", tz="UTC")

    level = LiquidityLevel("Asian low", "low", 2649.60, t("06:00"))
    return Signal(
        direction=Direction.BUY,
        session="london",
        session_label="London",
        created_at=t("07:40"),
        expires_at=t("09:10"),
        entry_zone=Zone(2651.20, 2652.40),
        entry=2652.40,
        stop=2648.90,
        tp1=2659.80,
        tp1_source="PDH 2659.80",
        tp2=2663.20,
        tp2_source="Equal highs 2663.20",
        rr1=(2659.80 - 2652.40) / 3.5,
        rr2=(2663.20 - 2652.40) / 3.5,
        bias_event=StructureEvent("BOS", "up", 2655.80, t("05:30"), 0, t("06:45")),
        sweep=Sweep(level, "M5", t("07:20"), t("07:25"), 2649.10),
        confirm_event=StructureEvent("CHoCH", "up", 2651.90, t("07:25"), 0, t("07:35")),
        fvg=FVG("up", Zone(2651.20, 2652.40), 0, t("07:30"), 0, 4.2, 1.6),
        order_block=Zone(2650.60, 2652.90),
        range_position=0.38,
    )


def demo_message(cfg: StrategyConfig, settings: Settings) -> str:
    s = demo_signal()
    try:
        size = position_size(
            balance=settings.paper_balance or 10_000,
            risk_percent=settings.risk_percent,
            entry=s.entry,
            stop=s.stop,
            instrument=cfg.instrument,
        )
    except InvalidTradeError:
        size = None
    text = format_signal(s, size, settings.risk_percent, cfg)
    return "[TEST — demo data, not a market signal]\n" + text
