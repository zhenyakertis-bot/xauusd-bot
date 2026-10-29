import lzma
from datetime import UTC, date, datetime

import httpx
import numpy as np
import pandas as pd
import pytest
import respx

from app.data.base import DataUnavailableError
from app.data.candles import ParquetCache, drop_incomplete, normalize, resample
from app.data.dukascopy import (
    BASE_URL as DUKA_URL,
)
from app.data.dukascopy import (
    CANDLE_DTYPE,
    TICK_DTYPE,
    DukascopyProvider,
    combine_bid_ask,
    decode_candles,
    decode_ticks_to_m1,
)
from app.data.twelvedata import BASE_URL as TD_URL
from app.data.twelvedata import TwelveDataProvider, parse_time_series


def m1_frame(start: str, closes: list[float]) -> pd.DataFrame:
    idx = pd.date_range(start, periods=len(closes), freq="1min", tz="UTC")
    c = np.array(closes)
    return normalize(
        pd.DataFrame(
            {"open": c, "high": c + 1, "low": c - 1, "close": c, "volume": 1.0, "spread": 0.2},
            index=idx,
        )
    )


def bi5_candles(rows: list[tuple[int, int, int, int, int, float]]) -> bytes:
    return lzma.compress(np.array(rows, dtype=CANDLE_DTYPE).tobytes())


# --- candles ---------------------------------------------------------------


def test_resample_m1_to_m5() -> None:
    m1 = m1_frame("2026-10-01 10:00", [float(x) for x in range(100, 110)])
    m5 = resample(m1, "M5")
    assert list(m5.index.strftime("%H:%M")) == ["10:00", "10:05"]
    first = m5.iloc[0]
    assert (first.open, first.high, first.low, first.close) == (100, 105, 99, 104)
    assert first.volume == 5


def test_resample_skips_empty_bins() -> None:
    a = m1_frame("2026-10-01 10:00", [1.0, 2.0])
    b = m1_frame("2026-10-01 10:30", [3.0])
    m15 = resample(pd.concat([a, b]), "M15")
    assert list(m15.index.strftime("%H:%M")) == ["10:00", "10:30"]


def test_drop_incomplete_is_strict_about_close_time() -> None:
    m15 = resample(m1_frame("2026-10-01 10:00", [1.0] * 20), "M15")  # bars 10:00, 10:15
    now = datetime(2026, 10, 1, 10, 29, tzinfo=UTC)
    assert list(drop_incomplete(m15, "M15", now).index.strftime("%H:%M")) == ["10:00"]
    now = datetime(2026, 10, 1, 10, 30, tzinfo=UTC)
    assert len(drop_incomplete(m15, "M15", now)) == 2


def test_parquet_cache_roundtrip(tmp_path) -> None:  # type: ignore[no-untyped-def]
    cache = ParquetCache(tmp_path)
    df = m1_frame("2026-10-01 10:00", [1.0, 2.0])
    assert cache.get("x", "k") is None
    cache.put("x", "k", df)
    pd.testing.assert_frame_equal(cache.get("x", "k"), df, check_freq=False)


# --- dukascopy decoding ------------------------------------------------------


def test_decode_candles_scales_prices() -> None:
    raw = bi5_candles([(0, 4368392, 4368632, 4366585, 4369210, 0.5)])
    df = decode_candles(raw, date(2026, 9, 22), 1000.0)
    row = df.iloc[0]
    assert df.index[0] == pd.Timestamp("2026-09-22 00:00", tz="UTC")
    assert (row.open, row.high, row.low, row.close) == (4368.392, 4369.21, 4366.585, 4368.632)


def test_combine_bid_ask_mid_spread_and_filler() -> None:
    d = date(2026, 9, 22)
    bid = decode_candles(
        bi5_candles(
            [
                (0, 1000000, 1001000, 999000, 1002000, 1.0),
                (60, 1001000, 1001000, 1001000, 1001000, 0.0),
            ]
        ),
        d,
        1000.0,
    )
    ask = decode_candles(
        bi5_candles(
            [
                (0, 1000400, 1001400, 999400, 1002400, 1.0),
                (60, 1001400, 1001400, 1001400, 1001400, 0.0),
            ]
        ),
        d,
        1000.0,
    )
    out = combine_bid_ask(bid, ask)
    assert len(out) == 1  # flat zero-volume minute dropped
    assert out.iloc[0].close == pytest.approx(1001.2)
    assert out.iloc[0].spread == pytest.approx(0.4)


def test_decode_ticks_to_m1() -> None:
    ticks = np.array(
        [
            (1000, 2000500, 2000000, 1.0, 1.0),
            (30000, 2001500, 2001000, 1.0, 2.0),
            (61000, 2000500, 2000000, 1.0, 1.0),
        ],
        dtype=TICK_DTYPE,
    )
    hour = datetime(2026, 10, 29, 9, tzinfo=UTC)
    m1 = decode_ticks_to_m1(lzma.compress(ticks.tobytes()), hour, 1000.0)
    assert len(m1) == 2
    assert m1.iloc[0].open == pytest.approx(2000.25)
    assert m1.iloc[0].high == pytest.approx(2001.25)
    assert m1.iloc[0].volume == 3.0
    assert m1.iloc[0].spread == pytest.approx(0.5)


@respx.mock
async def test_dukascopy_uses_zero_based_month_and_caches(tmp_path) -> None:  # type: ignore[no-untyped-def]
    raw = bi5_candles([(600, 1000000, 1001000, 999000, 1002000, 1.0)])
    route = respx.get(
        url__regex=rf"{DUKA_URL}/XAUUSD/2026/08/22/(BID|ASK)_candles_min_1\.bi5"
    ).mock(return_value=httpx.Response(200, content=raw))
    provider = DukascopyProvider(ParquetCache(tmp_path), min_interval=0)
    now = datetime(2026, 10, 1, tzinfo=UTC)
    start, end = datetime(2026, 9, 22, tzinfo=UTC), datetime(2026, 9, 23, tzinfo=UTC)
    df = await provider.get_m1(start, end, now=now)
    assert route.call_count == 2  # September -> "08"
    assert df.index[0] == pd.Timestamp("2026-09-22 00:10", tz="UTC")
    await provider.get_m1(start, end, now=now)
    assert route.call_count == 2  # second call served from parquet cache
    await provider.aclose()


# --- twelvedata ----------------------------------------------------------------

TD_PAYLOAD = {
    "meta": {"symbol": "XAU/USD", "interval": "15min"},
    "values": [
        {
            "datetime": "2026-10-29 12:30:00",
            "open": "4020",
            "high": "4030",
            "low": "4010",
            "close": "4025",
        },
        {
            "datetime": "2026-10-29 12:45:00",
            "open": "4025",
            "high": "4027",
            "low": "4017",
            "close": "4020",
        },
        {"datetime": "2099-01-01 00:00:00", "open": "1", "high": "1", "low": "1", "close": "1"},
    ],
    "status": "ok",
}


def test_parse_time_series() -> None:
    df = parse_time_series(TD_PAYLOAD)
    assert len(df) == 3
    assert df.index[0] == pd.Timestamp("2026-10-29 12:30", tz="UTC")
    assert df.iloc[0].close == 4025.0
    assert df.iloc[0].spread == 0.0


def test_parse_time_series_error() -> None:
    with pytest.raises(DataUnavailableError):
        parse_time_series({"status": "error", "code": 400, "message": "bad symbol"})


@respx.mock
async def test_twelvedata_drops_forming_bar_and_sends_key_in_header() -> None:
    route = respx.get(f"{TD_URL}/time_series").mock(
        return_value=httpx.Response(200, json=TD_PAYLOAD)
    )
    provider = TwelveDataProvider("secret-key")
    df = await provider.get_latest("M15", 10)
    assert len(df) == 2  # the 2099 bar has not closed yet
    request = route.calls.last.request
    assert request.headers["Authorization"] == "apikey secret-key"
    assert "secret-key" not in str(request.url)
    assert request.url.params["interval"] == "15min"
    await provider.aclose()


@respx.mock
async def test_twelvedata_bad_key() -> None:
    respx.get(f"{TD_URL}/price").mock(return_value=httpx.Response(401, json={"status": "error"}))
    provider = TwelveDataProvider("bad")
    with pytest.raises(DataUnavailableError):
        await provider.get_price()
    await provider.aclose()


@respx.mock
async def test_retry_on_429_then_success(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("app.data.http.asyncio.sleep", no_sleep)
    respx.get(f"{TD_URL}/price").mock(
        side_effect=[httpx.Response(429), httpx.Response(200, json={"price": "4024.5"})]
    )
    provider = TwelveDataProvider("k")
    assert await provider.get_price() == 4024.5
    await provider.aclose()
