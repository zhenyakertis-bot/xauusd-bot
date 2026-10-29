"""End-to-end engine scenario with a known answer (London BUY, 29 Oct 2026, GMT).

M15 (built from the same M5 path):
  Oct 27 21:00 2620 -> Oct 28 03:00 2655 (swing high) -> 08:00 2600 (swing low, PDL)
  -> 14:00 2678 (closes above 2655: bullish BOS; 2678 = PDH) -> down into Asia
  Asia 00:00-06:00 UTC: 2638 -> 02:00 2631 (Asian low 2630.9) -> 04:00 2640 -> 06:00 2634
M5 at the London open:
  07:15 wick to 2628, close 2633   -> Asian low swept
  07:25 swing high 2636            -> 07:40 displacement closes 2640.5: M5 BOS up
  07:35 bullish, 07:30 bearish     -> order block 2632.0-2634.5
  07:45 low 2638 > 07:35 high 2633 -> FVG 2633.0-2638.0
Expected: BUY, entry zone 2633.0-2634.5, entry 2634.5 (proximal), TP1 = PDH 2678.1.
"""

from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pytest

from app.backtest.runner import apply_overrides
from app.config import load_strategy
from app.data.candles import normalize, resample
from app.signals.models import Direction
from app.strategy.smc_engine import SMCEngine
from tests.synthetic import bars, path_bars

# Tests pin the spec rules so retuning config/strategy.yaml doesn't change known answers.
SPEC_RULES = [
    "structure.choch_sets_bias=false",
    "structure.require_premium_discount=true",
    "liquidity.eq_tolerance=0.30",
    "liquidity.sweep_max_age_minutes=120",
]
CFG = apply_overrides(
    load_strategy(Path(__file__).resolve().parents[1] / "config" / "strategy.yaml"), SPEC_RULES
)
NOW = datetime(2026, 10, 29, 7, 50, tzinfo=UTC)


def scenario() -> tuple[pd.DataFrame, pd.DataFrame]:
    path = path_bars(
        [
            ("2026-10-27 21:00", 2620),
            ("2026-10-28 03:00", 2655),
            ("2026-10-28 08:00", 2600),
            ("2026-10-28 14:00", 2678),
            ("2026-10-28 20:00", 2650),
            ("2026-10-28 23:00", 2645),
            ("2026-10-29 00:00", 2638),
            ("2026-10-29 02:00", 2631),
            ("2026-10-29 04:00", 2640),
            ("2026-10-29 06:00", 2634),
            ("2026-10-29 07:15", 2633),
        ]
    )
    tail = bars(
        "2026-10-29 07:15",
        [
            (2633.0, 2633.5, 2628.0, 2633.0),  # 07:15 sweep
            (2633.0, 2635.0, 2632.5, 2634.5),  # 07:20
            (2634.5, 2636.0, 2633.5, 2634.0),  # 07:25 swing high
            (2634.0, 2634.5, 2632.0, 2632.5),  # 07:30 bearish (OB)
            (2632.5, 2633.0, 2631.5, 2632.8),  # 07:35
            (2632.8, 2641.0, 2632.6, 2640.5),  # 07:40 displacement + break
            (2640.5, 2642.0, 2638.0, 2641.5),  # 07:45 completes the FVG
        ],
        minutes=5,
    )
    m5 = normalize(pd.concat([path, tail]))
    return m5, resample(m5, "M15")


def test_buy_signal_known_answer() -> None:
    m5, m15 = scenario()
    ev = SMCEngine(CFG).evaluate(m5, m15, NOW)
    assert ev.ok, f"{ev.stage}: {ev.reason}"
    s = ev.signal
    assert s is not None
    assert s.direction is Direction.BUY
    assert s.session == "london"
    assert (s.entry_zone.low, s.entry_zone.high) == (2633.0, 2634.5)
    assert s.entry == 2634.5
    assert (s.fvg.zone.low, s.fvg.zone.high) == (2633.0, 2638.0)
    assert s.order_block is not None and (s.order_block.low, s.order_block.high) == (2632.0, 2634.5)
    assert s.sweep.level.name == "Asian low"
    assert s.sweep.level.price == pytest.approx(2630.9)
    assert s.sweep.extreme == 2628.0
    assert s.confirm_event.time == pd.Timestamp("2026-10-29 07:40", tz="UTC")
    assert s.bias_event.direction == "up"
    assert s.stop < 2628.0
    assert s.tp1 == pytest.approx(2678.1)
    assert s.tp1_source.startswith(("PDH", "M15 swing high"))
    assert s.rr1 >= 2.0
    assert s.range_position < 0.5
    assert s.expires_at == pd.Timestamp("2026-10-29 09:20", tz="UTC")
    text = "\n".join(s.explanation())
    assert "Asian low 2630.90 swept at 07:15 UTC" in text
    assert "M5 BOS confirmed at 07:40 UTC" in text


def test_stop_uses_buffer_beyond_sweep_extreme() -> None:
    m5, m15 = scenario()
    s = SMCEngine(CFG).evaluate(m5, m15, NOW).signal
    assert s is not None
    # buffer = max(spread 0.3 * 2, ATR * 0.25); ATR here is ~1.9, so buffer = 0.6.
    assert 2628.0 - s.stop == pytest.approx(0.6, abs=0.05)


def test_no_lookahead_unclosed_bars_ignored() -> None:
    m5, m15 = scenario()
    # Evaluating at 07:45 the FVG's third candle hasn't closed yet -> no signal.
    ev = SMCEngine(CFG).evaluate(m5, m15, datetime(2026, 10, 29, 7, 45, tzinfo=UTC))
    assert not ev.ok
    # Passing future bars must not change a past evaluation.
    extra = bars("2026-10-29 07:50", [(2641.5, 2690, 2641, 2689)], minutes=5)
    ev2 = SMCEngine(CFG).evaluate(
        normalize(pd.concat([m5, extra])), m15, datetime(2026, 10, 29, 7, 45, tzinfo=UTC)
    )
    assert (ev2.stage, ev2.reason) == (ev.stage, ev.reason)


def test_rejected_outside_session() -> None:
    m5, m15 = scenario()
    ev = SMCEngine(CFG).evaluate(m5, m15, datetime(2026, 10, 29, 11, 0, tzinfo=UTC))
    assert (ev.stage, ev.ok) == ("session", False)


def test_rejected_when_rr_too_low() -> None:
    m5, m15 = scenario()
    cfg = CFG.model_copy(update={"trade": CFG.trade.model_copy(update={"min_rr_tp1": 10.0})})
    ev = SMCEngine(cfg).evaluate(m5, m15, NOW)
    assert ev.stage == "rr" and "R:R to TP1" in ev.reason


def test_rejected_when_not_in_discount() -> None:
    m5, m15 = scenario()
    cfg = CFG.model_copy(
        update={"structure": CFG.structure.model_copy(update={"equilibrium": 0.2})}
    )
    ev = SMCEngine(cfg).evaluate(m5, m15, NOW)
    assert ev.stage == "premium_discount"


def test_stale_setup_rejected() -> None:
    m5, _ = scenario()
    quiet = bars("2026-10-29 07:50", [(2641.5, 2642.5, 2640.5, 2641.8)] * 4, minutes=5)
    m5b = normalize(pd.concat([m5, quiet]))
    ev = SMCEngine(CFG).evaluate(
        m5b, resample(m5b, "M15"), datetime(2026, 10, 29, 8, 10, tzinfo=UTC)
    )
    assert ev.stage == "confirmation" and "too long ago" in ev.reason


def test_weak_first_break_then_displacement_break() -> None:
    """A first M5 break without displacement must not hide a later impulse that has one."""
    m5, _ = scenario()
    base = m5[m5.index < pd.Timestamp("2026-10-29 07:20", tz="UTC")]
    tail = bars(
        "2026-10-29 07:20",
        [
            (2633.0, 2634.0, 2632.5, 2633.8),  # 07:20
            (2633.8, 2635.0, 2633.0, 2634.5),  # 07:25 swing high 2635
            (2634.5, 2634.8, 2633.5, 2634.0),  # 07:30
            (2634.0, 2634.5, 2633.2, 2633.6),  # 07:35
            (2633.6, 2636.0, 2633.5, 2635.5),  # 07:40 weak break of 2635, no gap
            (2635.5, 2636.5, 2634.5, 2635.0),  # 07:45 swing high 2636.5
            (2635.0, 2635.5, 2634.0, 2634.3),  # 07:50
            (2634.3, 2634.8, 2633.8, 2634.2),  # 07:55 bearish
            (2634.2, 2642.0, 2634.0, 2641.5),  # 08:00 displacement breaks 2636.5
            (2641.5, 2643.0, 2639.0, 2642.5),  # 08:05 FVG 2634.8-2639.0
        ],
        minutes=5,
    )
    m5b = normalize(pd.concat([base, tail]))
    ev = SMCEngine(CFG).evaluate(
        m5b, resample(m5b, "M15"), datetime(2026, 10, 29, 8, 10, tzinfo=UTC)
    )
    assert ev.ok, f"{ev.stage}: {ev.reason}"
    assert ev.signal is not None
    assert ev.signal.confirm_event.time == pd.Timestamp("2026-10-29 08:00", tz="UTC")
    assert (ev.signal.fvg.zone.low, ev.signal.fvg.zone.high) == (2634.8, 2639.0)


def test_displacement_right_after_break() -> None:
    """Break closes barely above the swing; the displacement candle follows it."""
    m5, _ = scenario()
    base = m5[m5.index < pd.Timestamp("2026-10-29 07:40", tz="UTC")]
    tail = bars(
        "2026-10-29 07:40",
        [
            (2632.8, 2636.3, 2632.6, 2636.1),  # 07:40 closes just above 2636: M5 BOS
            (2636.1, 2643.0, 2636.0, 2642.5),  # 07:45 displacement
            (2642.5, 2644.0, 2639.5, 2643.5),  # 07:50 FVG 2636.3-2639.5
        ],
        minutes=5,
    )
    m5b = normalize(pd.concat([base, tail]))
    # Only the impulse pairing is under test here, not premium/discount.
    cfg = CFG.model_copy(
        update={"structure": CFG.structure.model_copy(update={"require_premium_discount": False})}
    )
    ev = SMCEngine(cfg).evaluate(
        m5b, resample(m5b, "M15"), datetime(2026, 10, 29, 7, 55, tzinfo=UTC)
    )
    assert ev.ok, f"{ev.stage}: {ev.reason}"
    assert ev.signal is not None
    assert ev.signal.confirm_event.time == pd.Timestamp("2026-10-29 07:40", tz="UTC")
    assert ev.signal.fvg.displacement_time == pd.Timestamp("2026-10-29 07:45", tz="UTC")
