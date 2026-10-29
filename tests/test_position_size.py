"""Hand-worked sizing examples. XAUUSD: 1 lot = 100 oz, $1 move = $1 per oz."""

from decimal import Decimal

import pytest

from app.config import InstrumentCfg
from app.risk.position_size import (
    InvalidTradeError,
    entry_price,
    position_size,
    reward_risk,
    sl_buffer,
    target_at_r,
)

XAU = InstrumentCfg()  # contract 100, step 0.01, min 0.01


@pytest.mark.parametrize(
    ("balance", "risk_pct", "entry", "stop", "lots", "actual_risk"),
    [
        # $100 risk / $4 stop = 25 oz = 0.25 lot, exact.
        (10_000, 1.0, 4025.00, 4021.00, "0.25", "100.00"),
        # $100 / $3 = 33.33 oz -> 0.33 lot (33 oz) -> $99 actual.
        (10_000, 1.0, 4025.00, 4022.00, "0.33", "99.00"),
        # SELL works the same: $100 / $7 = 14.28 oz -> 0.14 lot -> $98.
        (10_000, 1.0, 4025.00, 4032.00, "0.14", "98.00"),
        # $50k account, 0.5%: $250 / $2.50 = 100 oz = 1.00 lot.
        (50_000, 0.5, 2651.20, 2648.70, "1.00", "250.00"),
        # Float trap: $30 / $1 = 30 oz = 0.30 lot (float math would give 0.29).
        (3_000, 1.0, 2000.00, 1999.00, "0.30", "30.00"),
    ],
)
def test_position_size_examples(
    balance: float, risk_pct: float, entry: float, stop: float, lots: str, actual_risk: str
) -> None:
    ps = position_size(
        balance=balance, risk_percent=risk_pct, entry=entry, stop=stop, instrument=XAU
    )
    assert ps.lots == Decimal(lots)
    assert ps.ounces == Decimal(lots) * 100
    assert ps.actual_risk == Decimal(actual_risk)
    assert ps.actual_risk <= ps.risk_amount


def test_non_usd_account() -> None:
    # EUR account, 1 USD = 0.90 EUR. Risk 100 EUR = 111.11 USD; / $4 = 27.8 oz -> 0.27 lot.
    ps = position_size(
        balance=10_000, risk_percent=1.0, entry=4025, stop=4021, instrument=XAU, usd_to_account=0.9
    )
    assert ps.lots == Decimal("0.27")
    assert ps.actual_risk == Decimal("97.200")  # 27 oz * $4 * 0.9


def test_below_min_lot_rejected() -> None:
    # $100 / $150 stop = 0.67 oz = 0.0067 lot < 0.01.
    with pytest.raises(InvalidTradeError, match="min_lot"):
        position_size(balance=10_000, risk_percent=1.0, entry=4025, stop=3875, instrument=XAU)


def test_zero_stop_rejected() -> None:
    with pytest.raises(InvalidTradeError):
        position_size(balance=10_000, risk_percent=1.0, entry=4025, stop=4025, instrument=XAU)


def test_reward_risk() -> None:
    assert reward_risk("BUY", 4025, 4021, 4033) == pytest.approx(2.0)
    assert reward_risk("SELL", 4025, 4028, 4016) == pytest.approx(3.0)
    with pytest.raises(InvalidTradeError):
        reward_risk("BUY", 4025, 4027, 4033)  # stop above entry
    with pytest.raises(InvalidTradeError):
        reward_risk("SELL", 4025, 4028, 4030)  # target above entry


def test_target_at_r() -> None:
    assert target_at_r("BUY", 4025, 4021, 3.0) == pytest.approx(4037)
    assert target_at_r("SELL", 4025, 4028, 2.0) == pytest.approx(4019)


def test_sl_buffer() -> None:
    assert sl_buffer(spread=0.30, atr=4.0, spread_mult=2.0, atr_mult=0.25) == pytest.approx(1.0)
    assert sl_buffer(spread=0.80, atr=2.0, spread_mult=2.0, atr_mult=0.25) == pytest.approx(1.6)


def test_entry_price_modes() -> None:
    assert entry_price("BUY", 2651.20, 2652.40, "proximal") == 2652.40
    assert entry_price("SELL", 2651.20, 2652.40, "proximal") == 2651.20
    assert entry_price("BUY", 2651.20, 2652.40, "mid") == pytest.approx(2651.80)
