from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.storage.backup import backup, sqlite_path
from app.storage.db import Store


def test_backup_rotation(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'bot.db'}"
    store = Store(url)
    store.set_flag("paused", True)
    t0 = datetime(2026, 10, 29, tzinfo=UTC)
    for i in range(5):
        backup(url, tmp_path / "backups", keep=3, now=t0 + timedelta(days=i))
    files = sorted(p.name for p in (tmp_path / "backups").glob("*.db"))
    assert files == ["bot-20261031-0000.db", "bot-20261101-0000.db", "bot-20261102-0000.db"]
    restored = Store(f"sqlite:///{tmp_path / 'backups' / files[-1]}")
    assert restored.get_flag("paused") is True


def test_backup_skips_non_sqlite(tmp_path: Path) -> None:
    assert sqlite_path("postgresql://x") is None
    assert backup("sqlite:///:memory:", tmp_path, keep=3) is None


def test_healthcheck(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app import healthcheck
    from app.config import get_settings

    health = tmp_path / "health"
    monkeypatch.setenv("HEALTH_FILE", str(health))
    get_settings.cache_clear()
    try:
        assert healthcheck.check()[0] is False  # no file yet
        health.write_text(datetime.now(UTC).isoformat())
        assert healthcheck.check()[0] is True
        health.write_text((datetime.now(UTC) - timedelta(minutes=30)).isoformat())
        ok, msg = healthcheck.check()
        assert ok is False and "30 min" in msg
    finally:
        get_settings.cache_clear()
