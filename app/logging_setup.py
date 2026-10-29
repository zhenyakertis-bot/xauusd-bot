"""Structured JSON logging with rotation and secret redaction."""

from __future__ import annotations

import logging
import logging.handlers
import re
import sys
from pathlib import Path

from pythonjsonlogger.json import JsonFormatter

# Telegram bot tokens (123456:ABC...) and long bearer-like tokens.
_SECRET_PATTERNS = [
    re.compile(r"\d{6,}:[A-Za-z0-9_-]{30,}"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._-]{20,}"),
    re.compile(r"(?i)(apikey[\s=:]+)[A-Za-z0-9]{16,}"),
]


def redact(text: str) -> str:
    for pat in _SECRET_PATTERNS:
        text = pat.sub(lambda m: (m.group(1) if m.groups() else "") + "***REDACTED***", text)
    return text


class RedactingJsonFormatter(JsonFormatter):
    """Redacts the final line, so secrets inside exception tracebacks are masked too."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = None
        return True


def setup_logging(level: str = "INFO", log_dir: Path | None = None) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level.upper())

    fmt = RedactingJsonFormatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        rename_fields={"asctime": "ts", "levelname": "level", "name": "logger"},
    )
    redactor = RedactingFilter()

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    console.addFilter(redactor)
    root.addHandler(console)

    if log_dir is not None:
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                log_dir / "bot.log", maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
            )
        except OSError as exc:
            root.warning(
                "file logging disabled", extra={"log_dir": str(log_dir), "error": str(exc)}
            )
        else:
            file_handler.setFormatter(fmt)
            file_handler.addFilter(redactor)
            root.addHandler(file_handler)

    # httpx logs full URLs at INFO; keep it quiet.
    logging.getLogger("httpx").setLevel(logging.WARNING)
