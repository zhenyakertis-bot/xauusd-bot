"""Docker HEALTHCHECK: healthy if the live loop touched the health file recently.

The loop runs every 5 minutes (also on weekends, when it only touches the file),
so a file older than `max_age` minutes means the process is stuck.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta

from app.config import get_settings


def check(max_age: timedelta = timedelta(minutes=12)) -> tuple[bool, str]:
    path = get_settings().health_file
    try:
        last = datetime.fromisoformat(path.read_text().strip())
    except (OSError, ValueError) as exc:
        return False, f"no health file ({exc})"
    age = datetime.now(UTC) - last
    if age > max_age:
        return False, f"last cycle {int(age.total_seconds() // 60)} min ago"
    return True, f"last cycle {int(age.total_seconds())} s ago"


def main() -> None:
    ok, msg = check()
    print(msg)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
