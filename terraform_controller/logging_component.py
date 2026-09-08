"""Structured operational logging for terraformController.

All controller modules call log_event()/log_exception() instead of opening log
files themselves. That separation is deliberate: lifecycle code describes
*what happened*, while this component decides *how and where* it is recorded.

The controller currently writes JSON Lines content. Each event is one complete
JSON object on one line. The configured file may use a .log extension because
operators often treat it as a normal application log, while tools such as jq,
Splunk, or Elasticsearch can still parse each JSON record easily.

Secret values and Vault tokens must never be passed as log fields.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LOGGER_NAME = "terraformController"
DEFAULT_LOG_FILENAME = "Controller.log"

_CONFIGURED = False
_LOG_PATH: Path | None = None


class JsonLineFormatter(logging.Formatter):
    """Convert one Python LogRecord into one compact JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
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


def _render_filename(filename_format: str) -> str:
    """Render the configured file name using standard strftime tokens.

    An empty format has one simple, predictable meaning: Controller.log.
    """
    if not filename_format.strip():
        return DEFAULT_LOG_FILENAME
    return datetime.now().strftime(filename_format.strip())


def configure_logging(config: dict[str, Any]) -> Path | None:
    """Configure the process-wide controller logger once.

    Logging behavior comes entirely from terraformController.config:

    - logging_directory: already resolved to an absolute path by config.py.
    - logging_mode: append or overwrite.
    - logging_filename_format: blank means Controller.log; otherwise strftime.

    Repeated calls return the original log path. This prevents imported modules
    from accidentally adding duplicate handlers and writing each event twice.
    """
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

    # config.py has already expanded/normalized this path. Creating it here
    # keeps directory creation in the logging component where it belongs.
    directory = Path(config["logging_directory"])
    directory.mkdir(parents=True, exist_ok=True)

    mode = str(config.get("logging_mode", "append")).lower()
    filename_format = str(config.get("logging_filename_format", ""))
    filename = _render_filename(filename_format)
    path = directory / filename

    # Python FileHandler uses "a" for append and "w" for overwrite.
    file_mode = "a" if mode == "append" else "w"

    handler = logging.FileHandler(
        path,
        mode=file_mode,
        encoding="utf-8",
    )
    handler.setFormatter(JsonLineFormatter())
    logger.addHandler(handler)
    _LOG_PATH = path

    log_event(
        "logging.configured",
        path=str(path),
        mode=mode,
        filename=filename,
        configured_level=level_name,
    )
    return path


def log_event(
    event: str,
    *,
    level: int = logging.INFO,
    **fields: Any,
) -> None:
    """Write one structured operational event."""
    logger = logging.getLogger(LOGGER_NAME)
    logger.log(level, event, extra={"tc_fields": fields})


def log_exception(event: str, **fields: Any) -> None:
    """Write an ERROR event plus the active Python exception traceback."""
    logger = logging.getLogger(LOGGER_NAME)
    logger.exception(event, extra={"tc_fields": fields})


def log_path() -> Path | None:
    """Return the log file selected for this controller process."""
    return _LOG_PATH
