"""Daily SQLite backups (online, consistent copy via the sqlite3 backup API).

python -m app.storage.backup        # make a backup now
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from app.config import get_settings
from app.logging_setup import setup_logging

log = logging.getLogger(__name__)


def sqlite_path(database_url: str) -> Path | None:
    prefix = "sqlite:///"
    if not database_url.startswith(prefix) or database_url.endswith(":memory:"):
        return None
    return Path(database_url.removeprefix(prefix))


def backup(
    database_url: str, backup_dir: Path, keep: int, now: datetime | None = None
) -> Path | None:
    """Copy the database to backup_dir/bot-YYYYmmdd-HHMM.db and keep the newest `keep`."""
    src = sqlite_path(database_url)
    if src is None or not src.exists():
        log.warning("backup skipped: not a sqlite file database", extra={"url": database_url})
        return None
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%d-%H%M")
    dst = backup_dir / f"bot-{stamp}.db"
    with sqlite3.connect(src) as source, sqlite3.connect(dst) as target:
        source.backup(target)
    old = sorted(backup_dir.glob("bot-*.db"))[:-keep] if keep > 0 else []
    for p in old:
        p.unlink()
    log.info("database backup written", extra={"file": str(dst), "removed": len(old)})
    return dst


def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level, None)
    path = backup(settings.database_url, settings.backup_dir, settings.backup_keep)
    print(path or "no backup made")


if __name__ == "__main__":
    main()
