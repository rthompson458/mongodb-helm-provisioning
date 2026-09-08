from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LOGGER_NAME = "terraformController"
_CONFIGURED = False
_LOG_PATH: Path | None = None


class JsonLineFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        fields = getattr(record, "tc_fields", None)
        if isinstance(fields, dict):
            payload.update(fields)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _render_filename(pattern: str) -> str:
    rendered = datetime.now().strftime(pattern)
    return rendered.replace("{pid}", str(os.getpid()))


def configure_logging(config: dict[str, Any]) -> Path | None:
    global _CONFIGURED, _LOG_PATH
    if _CONFIGURED:
        return _LOG_PATH
    _CONFIGURED = True

    logger = logging.getLogger(LOGGER_NAME)
    logger.propagate = False
    logger.handlers.clear()

    if not config.get("logging_enabled", True):
        logger.addHandler(logging.NullHandler())
        return None

    level_name = str(config.get("logging_level", "INFO")).upper()
    level = getattr(logging, level_name, logging.INFO)
    logger.setLevel(level)

    directory = Path(config["logging_directory"]).expanduser()
    directory.mkdir(parents=True, exist_ok=True)
    mode = config.get("logging_mode", "per-run")
    pattern = config.get("logging_filename_pattern", "terraformController-%Y%m%d-%H%M%S-{pid}.jsonl")
    filename = _render_filename(pattern)
    path = directory / filename

    if mode == "replace" and path.exists():
        path.unlink()

    if mode == "per-run":
        retention_days = int(config.get("logging_retention_days", 30))
        if retention_days > 0:
            cutoff = datetime.now().timestamp() - (retention_days * 86400)
            for old in directory.glob("*.jsonl"):
                try:
                    if old.stat().st_mtime < cutoff:
                        old.unlink()
                except OSError:
                    pass

    handler = logging.FileHandler(path, mode="a", encoding="utf-8")
    handler.setFormatter(JsonLineFormatter())
    logger.addHandler(handler)
    _LOG_PATH = path
    log_event("logging.configured", path=str(path), mode=mode, level=level_name)
    return path


def log_event(event: str, *, level: int = logging.INFO, **fields: Any) -> None:
    logger = logging.getLogger(LOGGER_NAME)
    logger.log(level, event, extra={"tc_fields": fields})


def log_exception(event: str, **fields: Any) -> None:
    logger = logging.getLogger(LOGGER_NAME)
    logger.exception(event, extra={"tc_fields": fields})


def log_path() -> Path | None:
    return _LOG_PATH
