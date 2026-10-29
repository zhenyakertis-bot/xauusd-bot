"""Live checks against real APIs. Run with: pytest -m live"""

from datetime import timedelta

import pandas as pd
import pytest

from app.config import Settings
from app.data.factory import make_provider

pytestmark = pytest.mark.live


@pytest.mark.parametrize("provider_name", ["twelvedata", "dukascopy"])
async def test_last_100_m15(provider_name: str) -> None:
    provider = make_provider(Settings(), provider_name)
    try:
        df = await provider.get_latest("M15", 100)
    finally:
        await provider.aclose()
    assert len(df) == 100
    assert df.index.is_monotonic_increasing
    assert (df["high"] >= df[["open", "close"]].max(axis=1)).all()
    assert (df["low"] <= df[["open", "close"]].min(axis=1)).all()
    # Every returned bar has closed.
    assert df.index[-1] + timedelta(minutes=15) <= pd.Timestamp.now(tz="UTC")
