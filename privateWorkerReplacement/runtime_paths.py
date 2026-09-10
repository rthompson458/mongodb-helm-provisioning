"""Predictable runtime paths for controller logs and operation state.

The controller keeps runtime evidence under a single ``logs`` directory beside
``privateWorkerReplacement.config``.  Centralizing the path rules here prevents
the customer CLI, administrator CLI, logging code, and async worker code from
each inventing slightly different locations.

Human-readable logs are daily append-only files:

    logs/controller/controller-YYYYMMDD.log
    logs/operations/operations-YYYYMMDD.log

The small JSON files used to track asynchronous operation status are controller
state, not human logs.  They live under ``logs/operations/state`` so an operator
can still find everything related to controller execution in one place.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path


def _config_directory(config_path: Path) -> Path:
    """Return the directory that owns the selected controller config file."""

    return config_path.expanduser().resolve().parent


def log_root(config_path: Path) -> Path:
    """Return the top-level runtime log directory for one controller config."""

    return _config_directory(config_path) / "logs"


def controller_log_directory(config_path: Path) -> Path:
    """Return the directory containing structured controller event logs."""

    return log_root(config_path) / "controller"


def operations_log_directory(config_path: Path) -> Path:
    """Return the directory containing operation diagnostics and state."""

    return log_root(config_path) / "operations"


def operation_state_directory(config_path: Path) -> Path:
    """Return the directory containing machine-readable async status records."""

    return operations_log_directory(config_path) / "state"


def operation_work_directory(config_path: Path) -> Path:
    """Return the temporary transcript directory used by detached workers.

    A worker writes its normal stdout/stderr to a private temporary file while
    it runs.  When the worker reaches a terminal result, that transcript is
    appended as one block to the daily operations log and the temporary file is
    removed.  This avoids unreadable interleaving when multiple workers run at
    the same time.
    """

    return operations_log_directory(config_path) / "work"


def _date_stamp(when: datetime | None = None) -> str:
    """Return the UTC YYYYMMDD date used by both daily log families."""

    value = when or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y%m%d")


def controller_log_path(config_path: Path, when: datetime | None = None) -> Path:
    """Return today's structured controller log path."""

    return controller_log_directory(config_path) / f"controller-{_date_stamp(when)}.log"


def operations_log_path(config_path: Path, when: datetime | None = None) -> Path:
    """Return today's consolidated operation diagnostic log path."""

    return operations_log_directory(config_path) / f"operations-{_date_stamp(when)}.log"
