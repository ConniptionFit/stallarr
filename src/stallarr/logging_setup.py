"""Logging: stdout for docker logs, plus an in-memory ring buffer for the UI."""

from __future__ import annotations

import logging
import time
from collections import deque
from typing import Any

_BUFFER: deque[dict[str, Any]] = deque(maxlen=500)


class RingBufferHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            _BUFFER.append(
                {
                    "ts": record.created,
                    "level": record.levelname,
                    "logger": record.name.replace("stallarr.", ""),
                    "message": self.format(record),
                }
            )
        except Exception:  # noqa: BLE001 - logging must never raise
            self.handleError(record)


def recent_logs(limit: int = 200, level: str | None = None) -> list[dict[str, Any]]:
    entries = list(_BUFFER)
    if level:
        wanted = logging.getLevelName(level.upper())
        if isinstance(wanted, int):
            entries = [e for e in entries if logging.getLevelName(e["level"]) >= wanted]
    return entries[-limit:][::-1]


def setup(level: str = "INFO") -> None:
    root = logging.getLogger()
    root.setLevel(logging.WARNING)  # keep third-party libraries quiet

    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")

    stream = logging.StreamHandler()
    stream.setFormatter(fmt)

    ring = RingBufferHandler()
    ring.setFormatter(logging.Formatter("%(message)s"))

    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.addHandler(stream)
    root.addHandler(ring)

    app_logger = logging.getLogger("stallarr")
    app_logger.setLevel(level.upper() if level else "INFO")

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def utc_now() -> float:
    return time.time()
