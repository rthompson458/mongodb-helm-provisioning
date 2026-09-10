"""Controller event logging and consolidated operation diagnostics.

The product uses two predictable daily log families beside the selected config:

    logs/controller/controller-YYYYMMDD.log
    logs/operations/operations-YYYYMMDD.log

The controller log is structured JSON Lines.  It answers questions such as
"what command ran?" and "did the controller report success or failure?"

The operations log contains the detailed stdout/stderr from Git, Terraform, and
other implementation work that would otherwise clutter the customer terminal.
It is intentionally human-readable troubleshooting evidence.

Neither log should ever receive Vault tokens or managed plaintext passwords.
Callers must not pass secrets as log fields or command-line arguments.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import shlex
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from .runtime_paths import controller_log_path, operations_log_path

LOGGER_NAME = "privateWorkerReplacement"

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


def _config_path(config: dict[str, Any]) -> Path:
    """Return the normalized config path stored by config.load_config()."""

    value = config.get("config_path")
    if not value:
        raise ValueError("Controller configuration does not include config_path.")
    return Path(str(value)).expanduser().resolve()


def _secure_directory(path: Path) -> None:
    """Create a runtime directory and prefer owner-only permissions on Linux/WSL."""

    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        # Logging must remain usable on filesystems that do not support POSIX
        # permissions.  Production host permissions remain the real boundary.
        pass


def _secure_file(path: Path) -> None:
    """Prefer owner read/write permissions without making logging platform-fragile."""

    try:
        path.chmod(0o600)
    except OSError:
        pass


def configure_logging(config: dict[str, Any]) -> Path:
    """Configure the process-wide structured controller logger once.

    Logging is deliberately convention-based rather than configurable.  Every
    process appends to the UTC-dated controller log selected from the config
    file's directory.  Repeated calls return the original path so imported
    modules cannot accidentally install duplicate handlers.
    """

    global _CONFIGURED, _LOG_PATH

    if _CONFIGURED and _LOG_PATH is not None:
        return _LOG_PATH

    path = controller_log_path(_config_path(config))
    _secure_directory(path.parent)

    logger = logging.getLogger(LOGGER_NAME)
    logger.propagate = False
    logger.handlers.clear()
    logger.setLevel(logging.INFO)

    handler = logging.FileHandler(path, mode="a", encoding="utf-8")
    handler.setFormatter(JsonLineFormatter())
    logger.addHandler(handler)

    _secure_file(path)
    _CONFIGURED = True
    _LOG_PATH = path

    log_event("logging.configured", path=str(path), mode="append", date_basis="UTC")
    return path


def log_event(
    event: str,
    *,
    level: int = logging.INFO,
    **fields: Any,
) -> None:
    """Write one structured controller event."""

    logger = logging.getLogger(LOGGER_NAME)
    logger.log(level, event, extra={"tc_fields": fields})


def log_exception(event: str, **fields: Any) -> None:
    """Write an ERROR event plus the active Python exception traceback."""

    logger = logging.getLogger(LOGGER_NAME)
    logger.exception(event, extra={"tc_fields": fields})


def log_path() -> Path | None:
    """Return the controller log selected for this process."""

    return _LOG_PATH


def current_operations_log_path(config: dict[str, Any]) -> Path:
    """Return the daily detailed-operation log for the selected config."""

    return operations_log_path(_config_path(config))


def _append_operation_block(path: Path, lines: Sequence[str]) -> None:
    """Append one complete diagnostic block without interleaving other writers.

    Multiple foreground commands or detached workers can run at the same time.
    A short advisory file lock keeps each diagnostic block together while still
    allowing the actual controller operations to execute concurrently.
    """

    _secure_directory(path.parent)
    with path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            for line in lines:
                handle.write(line)
                if not line.endswith("\n"):
                    handle.write("\n")
            handle.flush()
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    _secure_file(path)


def append_process_diagnostic(
    config: dict[str, Any],
    command: Sequence[str],
    *,
    returncode: int,
    stdout: str = "",
    stderr: str = "",
    label: str = "external command",
) -> Path:
    """Append captured implementation output to the daily operations log.

    Terraform and Git output is valuable to an administrator but is not useful
    customer-facing output.  The Terraform runner captures it and calls this
    function before deciding whether the external command succeeded.
    """

    path = current_operations_log_path(config)
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )
    operation_id = os.environ.get("TC_OPERATION_ID", "-")
    operation_command = os.environ.get("TC_OPERATION_COMMAND", "-")

    lines = [
        (
            f"===== {timestamp} | operation={operation_id} | "
            f"controller_command={operation_command} | {label} | exit={returncode} ====="
        ),
        f"$ {shlex.join(list(command))}",
    ]
    if stdout:
        lines.extend(["--- stdout ---", stdout.rstrip("\n")])
    if stderr:
        lines.extend(["--- stderr ---", stderr.rstrip("\n")])
    if not stdout and not stderr:
        lines.append("(no command output)")
    lines.extend(["===== end command =====", ""])
    _append_operation_block(path, lines)
    return path


def append_worker_transcript(
    config_path: Path,
    *,
    operation_id: str,
    command: str,
    deployment: str,
    result: str,
    message: str,
    transcript: str,
    log_file: str | None = None,
) -> Path:
    """Append one detached worker's buffered transcript to its daily log.

    Detached workers write ordinary Python stdout/stderr to a private temporary
    file while running.  Consolidating that file only after the worker reaches a
    terminal result keeps unrelated operation transcripts from becoming mixed.
    Detailed Terraform/Git blocks may already have been written to the same
    daily log by append_process_diagnostic().
    """

    path = Path(log_file).expanduser() if log_file else operations_log_path(config_path)
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )
    lines = [
        (
            f"===== {timestamp} | operation={operation_id} | command={command} | "
            f"deployment={deployment or '-'} | result={result} ====="
        )
    ]
    if transcript.strip():
        lines.extend(["--- controller transcript ---", transcript.rstrip("\n")])
    else:
        lines.append("(no controller transcript)")
    lines.extend([f"Message: {message}", "===== end operation =====", ""])
    _append_operation_block(path, lines)
    return path
