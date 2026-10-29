from pathlib import Path

import pytest

from app.config import Settings, load_strategy
from app.logging_setup import redact

ROOT = Path(__file__).resolve().parents[1]


def test_strategy_yaml_loads() -> None:
    cfg = load_strategy(ROOT / "config" / "strategy.yaml")
    assert {"london", "new_york"} <= set(cfg.sessions)
    assert cfg.structure.swing_lookback == 3
    assert cfg.trade.min_rr_tp1 == 2.0
    assert cfg.trade.entry_expiry_minutes == 90
    assert cfg.signals.max_signals_per_session == 2
    assert cfg.news_filter.enabled is False


def test_empty_secrets_are_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TWELVEDATA_API_KEY", "")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.twelvedata_api_key is None
    assert s.telegram_bot_token is None


def test_allowed_chat_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_ADMIN_CHAT_ID", "42")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "7, 8")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.allowed_chat_ids == {7, 8, 42}


def test_secret_not_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert "AAAAAAAA" not in repr(s)


def test_redact_telegram_token() -> None:
    msg = "GET https://api.telegram.org/bot123456789:FAKEfakeFAKEfakeFAKEfakeFAKEfake_x/getMe"
    out = redact(msg)
    assert "FAKEfake" not in out
    assert "REDACTED" in out


def test_redact_api_key() -> None:
    out = redact(
        "GET /price?apikey=0123456789abcdef0123 failed; Authorization: apikey abcdef0123456789xyz"
    )
    assert "0123456789abcdef" not in out
    assert "abcdef0123456789" not in out


def test_token_placeholder_brackets_stripped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", " <123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi> ")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.telegram_bot_token is not None
    assert s.telegram_bot_token.get_secret_value() == "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"


def test_traceback_is_redacted(capsys: pytest.CaptureFixture[str]) -> None:
    import logging

    from app.logging_setup import setup_logging

    setup_logging("INFO", None)
    try:
        raise ValueError("The token `123456789:FAKEfakeFAKEfakeFAKEfakeFAKEfake_x` was rejected")
    except ValueError:
        logging.getLogger("t").exception("boom")
    out = capsys.readouterr().out
    assert "FAKEfake" not in out and "REDACTED" in out
