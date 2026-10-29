"""Settings from .env (secrets, runtime) and strategy.yaml (strategy parameters)."""

from __future__ import annotations

from datetime import time
from functools import lru_cache
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # twelvedata: live (needs key); dukascopy: free history, ~1h delayed (backtests).
    data_provider: Literal["twelvedata", "dukascopy"] = "twelvedata"
    twelvedata_api_key: SecretStr | None = None
    cache_dir: Path = Path("cache")

    telegram_bot_token: SecretStr | None = None
    telegram_admin_chat_id: int | None = None
    telegram_allowed_chat_ids: str = ""

    risk_percent: float = Field(default=1.0, gt=0, le=10)
    paper_balance: float | None = Field(default=None, gt=0)
    account_currency: str = "USD"

    log_level: str = "INFO"
    log_dir: Path = Path("logs")
    database_url: str = "sqlite:///data/bot.db"
    strategy_config: Path = Path("config/strategy.yaml")
    health_file: Path = Path("data/health")  # touched every cycle; Docker healthcheck reads it

    @field_validator(
        "paper_balance",
        "telegram_admin_chat_id",
        "twelvedata_api_key",
        "telegram_bot_token",
        mode="before",
    )
    @classmethod
    def _empty_to_none(cls, v: object) -> object:
        return None if v == "" else v

    @field_validator("telegram_bot_token", "twelvedata_api_key", mode="before")
    @classmethod
    def _strip_token(cls, v: object) -> object:
        # Values pasted from docs often keep quotes or <placeholder> brackets.
        if isinstance(v, str):
            v = v.strip().strip("\"'").strip("<>").strip()
            return v or None
        return v

    @property
    def allowed_chat_ids(self) -> set[int]:
        ids = {int(x) for x in self.telegram_allowed_chat_ids.split(",") if x.strip()}
        if self.telegram_admin_chat_id is not None:
            ids.add(self.telegram_admin_chat_id)
        return ids


class InstrumentCfg(BaseModel):
    """Contract spec. No broker API to read it from, so it lives in config (match your broker)."""

    symbol: str = "XAUUSD"
    price_precision: int = Field(2, ge=0)
    contract_size: float = Field(100, gt=0)  # ounces per 1.00 lot
    lot_step: float = Field(0.01, gt=0)
    min_lot: float = Field(0.01, gt=0)


class SessionWindow(BaseModel):
    """Local-time window [start, end) in `tz`; DST is handled by zoneinfo."""

    tz: str
    start: time
    end: time
    signals: bool = True  # False: window only defines a range (e.g. Asia high/low)
    label: str | None = None

    @model_validator(mode="after")
    def _check(self) -> SessionWindow:
        ZoneInfo(self.tz)  # raises on unknown zone
        if self.end <= self.start:
            raise ValueError("session end must be after start (no midnight crossing)")
        return self


class StructureCfg(BaseModel):
    swing_lookback: int = Field(3, ge=1)
    entry_swing_lookback: int = Field(2, ge=1)
    bias_timeframe: str = "M15"
    entry_timeframe: str = "M5"
    # Spec: bias comes from the last BOS. A CHoCH makes bias neutral unless this is true.
    choch_sets_bias: bool = False
    require_premium_discount: bool = True
    equilibrium: float = Field(0.5, gt=0, lt=1)
    lookback_bars_bias: int = Field(400, ge=50)
    lookback_bars_entry: int = Field(300, ge=50)


class LiquidityCfg(BaseModel):
    eq_tolerance: float = Field(0.30, ge=0)
    use_asian_range: bool = True
    use_previous_day: bool = True
    use_equal_levels: bool = True
    eq_lookback_swings: int = Field(10, ge=2)
    use_swing_targets: bool = True
    sweep_timeframes: list[str] = ["M5", "M15"]
    sweep_max_age_minutes: int = Field(120, gt=0)


class ConfirmationCfg(BaseModel):
    atr_period: int = Field(14, ge=1)
    displacement_atr_mult: float = Field(1.5, gt=0)
    min_fvg_size: float = Field(0.5, ge=0)
    ob_max_lookback: int = Field(10, ge=1)
    impulse_max_gap_bars: int = Field(3, ge=0)
    max_age_bars: int = Field(3, ge=1)


class TradeCfg(BaseModel):
    sl_spread_mult: float = Field(2.0, ge=0)
    sl_atr_mult: float = Field(0.25, ge=0)
    min_rr_tp1: float = Field(2.0, gt=0)
    tp2_r: float = Field(3.0, gt=0)
    entry_expiry_minutes: int = Field(90, gt=0)
    # Price used for R:R, sizing and fills: zone edge price reaches first, or zone middle.
    entry_mode: Literal["proximal", "mid"] = "proximal"
    assumed_spread: float = Field(0.30, ge=0)  # when the data feed has no bid/ask
    tp1_close_fraction: float = Field(0.5, gt=0, le=1)  # booked at TP1; rest runs to TP2 at BE


class SignalsCfg(BaseModel):
    signal_cooldown_minutes: int = Field(30, ge=0)
    max_signals_per_session: int = Field(2, ge=1)
    max_active_per_direction: int = Field(1, ge=1)
    price_rounding: float = Field(0.10, gt=0)


class NewsFilterCfg(BaseModel):
    enabled: bool = False
    window_minutes: int = 30
    currencies: list[str] = ["USD"]
    impact: list[str] = ["High"]


class MarketHoursCfg(BaseModel):
    tz: str = "America/New_York"
    daily_close: time = time(17, 0)
    daily_open: time = time(18, 0)


class MonitoringCfg(BaseModel):
    heartbeat_stale_minutes: int = 15
    daily_report_after: time = time(11, 30)


class BacktestCfg(BaseModel):
    spread: float = 0.30  # used when use_data_spread is false or the data has none
    use_data_spread: bool = True
    slippage: float = 0.05  # applied to stop exits
    warmup_days: int = 5
    in_sample_ratio: float = Field(0.7, gt=0, lt=1)


class StrategyConfig(BaseModel):
    instrument: InstrumentCfg = InstrumentCfg()
    sessions: dict[str, SessionWindow]
    structure: StructureCfg = StructureCfg()
    liquidity: LiquidityCfg = LiquidityCfg()
    confirmation: ConfirmationCfg = ConfirmationCfg()
    trade: TradeCfg = TradeCfg()
    signals: SignalsCfg = SignalsCfg()
    news_filter: NewsFilterCfg = NewsFilterCfg()
    market_hours: MarketHoursCfg = MarketHoursCfg()
    monitoring: MonitoringCfg = MonitoringCfg()
    backtest: BacktestCfg = BacktestCfg()


def load_strategy(path: Path) -> StrategyConfig:
    with path.open(encoding="utf-8") as f:
        return StrategyConfig.model_validate(yaml.safe_load(f))


@lru_cache
def get_settings() -> Settings:
    return Settings()
