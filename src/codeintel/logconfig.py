from __future__ import annotations

import json
import logging
import os
import sys


class _JsonFormatter(logging.Formatter):
    """Minimal structured formatter for log aggregators (ELK / Splunk / Datadog). One JSON object
    per line; no external dependency."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


_configured = False


def configure_logging() -> None:
    """Configure the ``codeintel`` logger for a long-running server process. Idempotent, and
    scoped to the ``codeintel`` logger only — importing codeintel as a library never hijacks the
    root logger. Honors:

      * ``CODEINTEL_LOG_LEVEL``  (default ``WARNING``) — DEBUG | INFO | WARNING | ERROR
      * ``CODEINTEL_LOG_FORMAT`` (default plain)       — set to ``json`` for structured output
    """
    global _configured
    if _configured:
        return
    level_name = os.environ.get("CODEINTEL_LOG_LEVEL", "WARNING").strip().upper()
    level = getattr(logging, level_name, logging.WARNING)
    if not isinstance(level, int):
        level = logging.WARNING

    handler = logging.StreamHandler(sys.stderr)
    if os.environ.get("CODEINTEL_LOG_FORMAT", "").strip().lower() == "json":
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))

    logger = logging.getLogger("codeintel")
    logger.setLevel(level)
    logger.handlers[:] = [handler]
    logger.propagate = False
    _configured = True
