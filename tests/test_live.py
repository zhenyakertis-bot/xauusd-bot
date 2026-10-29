"""Replay the live loop over a synthetic day with a known answer."""

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from app.config import Settings
from app.data.candles import drop_incomplete, empty_frame, normalize, resample, tf_delta
from app.live import LiveRunner
from app.storage.db import Store
from tests.synthetic import bars
from tests.test_dedup_backtest import to_m1
from tests.test_smc_engine import CFG, scenario


class FakeProvider:
    """Serves a fixed M1 history as if the current time were `self.now`."""

    name = "fake"

    def __init__(self, m1: pd.DataFrame) -> None:
        self.m1 = m1
        self.now = datetime(2026, 10, 29, tzinfo=UTC)
        self.calls = 0
        self.frozen_at: datetime | None = None  # simulate a feed that stops updating

    def _visible(self) -> pd.DataFrame:
        clock = self.frozen_at or self.now
        return drop_incomplete(self.m1, "M1", clock)

    async def get_latest(self, tf: str, count: int) -> pd.DataFrame:
        self.calls += 1
        clock = self.frozen_at or self.now
        return drop_incomplete(resample(self._visible(), tf), tf, clock).tail(count)

    async def get_candles(self, tf: str, start: datetime, end: datetime) -> pd.DataFrame:
        self.calls += 1
        df = resample(self._visible(), tf)
        return df[
            (df.index >= pd.Timestamp(start)) & (df.index + tf_delta(tf) <= pd.Timestamp(end))
        ]

    async def get_price(self) -> float:
        self.calls += 1
        v = self._visible()
        return float(v["close"].iloc[-1]) if len(v) else 0.0

    async def aclose(self) -> None:
        return None


class FakeMessenger:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str, int | None]] = []

    async def send(self, text: str, reply_to: int | None = None) -> int | None:
        msg_id = len(self.sent) + 100
        self.sent.append((msg_id, text, reply_to))
        return msg_id


def day_m1() -> pd.DataFrame:
    m5, _ = scenario()
    after = bars(
        "2026-10-29 07:50",
        [
            (2641.5, 2641.6, 2634.0, 2635.0),  # retrace fills the 2634.50 limit
            *[(2635 + 4 * i, 2639 + 4 * i, 2634.5 + 4 * i, 2639 + 4 * i) for i in range(12)],
        ],
        minutes=5,
    )
    return to_m1(normalize(pd.concat([m5, after])))


SETTINGS = Settings(_env_file=None, paper_balance=10_000, risk_percent=1.0)  # type: ignore[call-arg]


def make(store: Store, provider: FakeProvider, msgr: FakeMessenger) -> LiveRunner:
    return LiveRunner(SETTINGS, CFG, provider, store, msgr)


async def run_until(runner: LiveRunner, provider: FakeProvider, start: str, end: str) -> None:
    t = pd.Timestamp(start, tz="UTC")
    while t <= pd.Timestamp(end, tz="UTC"):
        provider.now = t.to_pydatetime()
        await runner.cycle(provider.now)
        t += timedelta(minutes=5)


async def test_signal_then_updates_threaded() -> None:
    store, provider, msgr = Store("sqlite:///:memory:"), FakeProvider(day_m1()), FakeMessenger()
    runner = make(store, provider, msgr)
    await run_until(runner, provider, "2026-10-29 07:00", "2026-10-29 09:00")

    signal_msgs = [m for m in msgr.sent if m[1].startswith("XAUUSD — BUY")]
    assert len(signal_msgs) == 1
    sig_id = signal_msgs[0][0]
    assert "Entry zone: 2633.00 – 2634.50" in signal_msgs[0][1]
    assert signal_msgs[0][1].endswith("Paper signal. Not financial advice.")

    updates = [m for m in msgr.sent if m[2] == sig_id]
    assert [u[1].splitlines()[0].split(" — ")[1] for u in updates] == ["TRIGGERED", "TP1"]
    assert all(u[1].endswith("Paper signal. Not financial advice.") for u in updates)

    (row,) = store.last(5)
    assert row.status == "TP1" and row.closed_at is not None
    assert row.result_r == pytest.approx(row.rr1)
    assert row.message_id == sig_id and row.lots == pytest.approx(0.14)
    assert store.open_signals() == [] and runner.tracked == {}


async def test_restart_mid_trade_resumes_tracking() -> None:
    store, provider, msgr = Store("sqlite:///:memory:"), FakeProvider(day_m1()), FakeMessenger()
    r1 = make(store, provider, msgr)
    await run_until(r1, provider, "2026-10-29 07:00", "2026-10-29 07:55")
    assert store.open_signals()[0].status == "TRIGGERED"

    r2 = make(store, provider, msgr)  # "process restart": state comes from the DB
    assert list(r2.tracked) == [store.open_signals()[0].id]
    await run_until(r2, provider, "2026-10-29 08:00", "2026-10-29 09:00")
    (row,) = store.last(5)
    assert row.status == "TP1" and row.closed_at is not None
    # No duplicate signal after the restart (gate restored from DB).
    assert sum(m[1].startswith("XAUUSD — BUY") for m in msgr.sent) == 1


async def test_pause_blocks_new_signals() -> None:
    store, provider, msgr = Store("sqlite:///:memory:"), FakeProvider(day_m1()), FakeMessenger()
    store.set_flag("paused", True)
    await run_until(make(store, provider, msgr), provider, "2026-10-29 07:00", "2026-10-29 09:00")
    assert msgr.sent == [] and store.last(5) == []


async def test_weekend_is_quiet() -> None:
    store, provider, msgr = (
        Store("sqlite:///:memory:"),
        FakeProvider(empty_frame()),
        FakeMessenger(),
    )
    runner = make(store, provider, msgr)
    await run_until(runner, provider, "2026-10-31 07:00", "2026-10-31 12:00")  # Saturday
    assert provider.calls == 0 and msgr.sent == []


async def test_heartbeat_alert_once_and_recovery() -> None:
    store, provider, msgr = Store("sqlite:///:memory:"), FakeProvider(day_m1()), FakeMessenger()
    store.set_flag("paused", True)
    runner = make(store, provider, msgr)
    provider.frozen_at = datetime(2026, 10, 29, 7, 0, tzinfo=UTC)  # feed stops at 07:00
    await run_until(runner, provider, "2026-10-29 07:00", "2026-10-29 07:45")
    alerts = [m for m in msgr.sent if "no new market data" in m[1]]
    assert len(alerts) == 1
    provider.frozen_at = None
    await run_until(runner, provider, "2026-10-29 07:50", "2026-10-29 07:50")
    assert "flowing again" in msgr.sent[-1][1]


async def test_daily_report() -> None:
    store, provider, msgr = Store("sqlite:///:memory:"), FakeProvider(day_m1()), FakeMessenger()
    runner = make(store, provider, msgr)
    await run_until(runner, provider, "2026-10-29 07:00", "2026-10-29 09:00")
    await runner.daily_report(datetime(2026, 10, 29, 15, 30, tzinfo=UTC))
    report = msgr.sent[-1][1]
    assert report.startswith("XAUUSD daily report — 2026-10-29")
    assert "→ TP1" in report


async def test_snapshot_fields() -> None:
    store, provider, msgr = Store("sqlite:///:memory:"), FakeProvider(day_m1()), FakeMessenger()
    runner = make(store, provider, msgr)
    await run_until(runner, provider, "2026-10-29 07:00", "2026-10-29 07:10")
    snap = await runner.snapshot()
    assert snap["Last price"] != "-" and snap["M15 bias"].startswith(("up", "down", "neutral"))
