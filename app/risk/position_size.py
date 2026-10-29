"""Position sizing, stop buffer and reward:risk.

Sizing for a USD-quoted metal (XAUUSD): P&L per ounce = price change in USD, so
    ounces = risk_amount_usd / |entry - stop|
    lots   = ounces / contract_size, floored to lot_step (never rounds risk up).
Decimal arithmetic avoids float floor errors (0.30 -> 0.29).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal
from typing import Literal

from app.config import InstrumentCfg

Direction = Literal["BUY", "SELL"]


class InvalidTradeError(ValueError):
    pass


@dataclass(frozen=True)
class PositionSize:
    lots: Decimal
    ounces: Decimal
    risk_amount: Decimal  # intended risk, account currency
    actual_risk: Decimal  # risk after lot rounding, account currency
    stop_distance: Decimal


def _d(x: float | Decimal) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(str(x))


def position_size(
    *,
    balance: float,
    risk_percent: float,
    entry: float,
    stop: float,
    instrument: InstrumentCfg,
    usd_to_account: float = 1.0,
) -> PositionSize:
    """Size so that hitting `stop` loses at most `risk_percent` of `balance`.

    `usd_to_account`: units of account currency per 1 USD (1.0 for a USD account).
    Raises InvalidTradeError if the stop is degenerate or the size is below min_lot.
    """
    if balance <= 0 or risk_percent <= 0 or usd_to_account <= 0:
        raise InvalidTradeError("balance, risk_percent and usd_to_account must be positive")
    distance = abs(_d(entry) - _d(stop))
    if distance == 0:
        raise InvalidTradeError("entry equals stop")

    risk_amount = _d(balance) * _d(risk_percent) / 100
    risk_usd = risk_amount / _d(usd_to_account)
    raw_lots = risk_usd / distance / _d(instrument.contract_size)

    step = _d(instrument.lot_step)
    lots = (raw_lots / step).to_integral_value(rounding=ROUND_FLOOR) * step
    if lots < _d(instrument.min_lot):
        raise InvalidTradeError(
            f"size {raw_lots:.4f} lots is below min_lot {instrument.min_lot} "
            f"(stop distance {distance} too wide for the risk budget)"
        )
    ounces = lots * _d(instrument.contract_size)
    actual_risk = ounces * distance * _d(usd_to_account)
    return PositionSize(
        lots=lots,
        ounces=ounces,
        risk_amount=risk_amount,
        actual_risk=actual_risk,
        stop_distance=distance,
    )


def validate_levels(direction: Direction, entry: float, stop: float, target: float) -> None:
    if direction == "BUY" and not stop < entry < target:
        raise InvalidTradeError(f"BUY needs stop < entry < target, got {stop}/{entry}/{target}")
    if direction == "SELL" and not target < entry < stop:
        raise InvalidTradeError(f"SELL needs target < entry < stop, got {target}/{entry}/{stop}")


def reward_risk(direction: Direction, entry: float, stop: float, target: float) -> float:
    """Reward-to-risk multiple of `target` (2.0 means 1:2)."""
    validate_levels(direction, entry, stop, target)
    return abs(target - entry) / abs(entry - stop)


def target_at_r(direction: Direction, entry: float, stop: float, r: float) -> float:
    """Price at `r` multiples of risk from entry."""
    risk = abs(entry - stop)
    return entry + r * risk if direction == "BUY" else entry - r * risk


def sl_buffer(spread: float, atr: float, spread_mult: float, atr_mult: float) -> float:
    """Distance beyond the sweep extreme: max(spread * k, ATR * m)."""
    return max(spread * spread_mult, atr * atr_mult)


def entry_price(direction: Direction, zone_low: float, zone_high: float, mode: str) -> float:
    """Price assumed for the fill. 'proximal' = the edge price reaches first on a retrace."""
    if zone_low > zone_high:
        raise InvalidTradeError("zone_low must be <= zone_high")
    if mode == "mid":
        return (zone_low + zone_high) / 2
    return zone_high if direction == "BUY" else zone_low
