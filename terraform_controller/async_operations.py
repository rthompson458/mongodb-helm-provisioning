"""Detached long-running operation support for terraformController.

The public CLI should not hold a user's terminal for lengthy deployment/topology
changes.  This module records a small local operation journal, launches the
existing synchronous lifecycle function in a detached worker process, and lets
read-only status commands report the eventual positive/negative result.

Important architecture boundary:
- The detached worker still calls the normal controller lifecycle functions.
- Those lifecycle functions still drive all managed mutations through Terraform.
- This module never edits MongoDB, Kubernetes topology, Vault, or storage state.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from .common import ControllerError

ASYNC_COMMANDS = {
    "AddReplicaSet",
    "DeleteReplicaSet",
    "AddShardedCluster",
    "DeleteShardedCluster",
    "AddShard",
    "DeleteShard",
}

TERMINAL_RESULTS = {"Succeeded", "Failed", "Interrupted"}


def _now() -> str:
    """Return one UTC timestamp suitable for durable operation metadata."""

    return datetime.now(timezone.utc).isoformat()


def operation_directory(config_path: Path) -> Path:
    """Return the config-relative directory that stores operation journals."""

    return config_path.expanduser().resolve().parent / ".terraformController" / "operations"


def _state_path(config_path: Path, operation_id: str) -> Path:
    return operation_directory(config_path) / f"{operation_id}.json"


def _log_path(config_path: Path, operation_id: str) -> Path:
    return operation_directory(config_path) / f"{operation_id}.log"


def _write_state(config_path: Path, state: dict[str, Any]) -> None:
    """Atomically persist operation metadata without ever writing credentials."""

    directory = operation_directory(config_path)
    directory.mkdir(parents=True, exist_ok=True)
    path = _state_path(config_path, state["operation_id"])
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    temp.replace(path)


def load_operation(config_path: Path, operation_id: str) -> dict[str, Any]:
    """Load one exact operation ID from the local operation journal."""

    path = _state_path(config_path, operation_id)
    if not path.exists():
        raise ControllerError(f"Operation '{operation_id}' does not exist.")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ControllerError(
            f"Operation record '{operation_id}' could not be read: {exc}"
        ) from exc


def list_operation_records(config_path: Path) -> list[dict[str, Any]]:
    """Return operation records newest-first."""

    directory = operation_directory(config_path)
    if not directory.exists():
        return []

    records: list[dict[str, Any]] = []
    for path in directory.glob("*.json"):
        try:
            records.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return sorted(records, key=lambda item: item.get("submitted_at", ""), reverse=True)


def _worker_alive(pid: int | None) -> bool:
    """Best-effort local worker liveness check without an external dependency."""

    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError):
        return False
    return True


def effective_result(state: dict[str, Any]) -> str:
    """Return the user-visible result, detecting an interrupted local worker."""

    result = str(state.get("result", "In Progress"))
    if result in {"Queued", "In Progress"}:
        if state.get("pid") and not _worker_alive(state.get("pid")):
            return "Interrupted"
        return "In Progress"
    return result


def create_operation(
    config_path: Path,
    *,
    command: str,
    deployment: str,
    worker_arguments: Sequence[str],
) -> dict[str, Any]:
    """Create the durable local journal entry before launching the worker."""

    operation_id = uuid.uuid4().hex[:12]
    state = {
        "operation_id": operation_id,
        "command": command,
        "deployment": deployment,
        "result": "Queued",
        "message": "Operation is queued for the detached local worker.",
        "submitted_at": _now(),
        "started_at": "",
        "completed_at": "",
        "pid": None,
        "worker_arguments": list(worker_arguments),
        "log_file": str(_log_path(config_path, operation_id)),
    }
    _write_state(config_path, state)
    return state


def launch_operation(
    config_path: Path,
    repo_root: Path,
    *,
    command: str,
    deployment: str,
    worker_arguments: Sequence[str],
) -> dict[str, Any]:
    """Launch a detached worker that executes the normal synchronous lifecycle."""

    state = create_operation(
        config_path,
        command=command,
        deployment=deployment,
        worker_arguments=worker_arguments,
    )
    operation_id = state["operation_id"]
    log_path = _log_path(config_path, operation_id)
    entrypoint = repo_root / "terraformController.py"

    worker_command = [
        sys.executable,
        str(entrypoint),
        "--config",
        str(config_path.expanduser().resolve()),
        "--_operation-worker",
        operation_id,
        *worker_arguments,
    ]

    try:
        with log_path.open("a", encoding="utf-8") as log_handle:
            process = subprocess.Popen(
                worker_command,
                cwd=repo_root,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
            )
    except OSError as exc:
        state["result"] = "Failed"
        state["message"] = f"Could not start detached worker: {exc}"
        state["completed_at"] = _now()
        _write_state(config_path, state)
        raise ControllerError(state["message"]) from exc

    state["pid"] = process.pid
    state["result"] = "In Progress"
    state["message"] = "Detached local worker started."
    _write_state(config_path, state)
    return state


def mark_running(config_path: Path, operation_id: str) -> None:
    state = load_operation(config_path, operation_id)
    state["result"] = "In Progress"
    state["started_at"] = state.get("started_at") or _now()
    state["message"] = f"{state['command']} is running."
    state["pid"] = os.getpid()
    _write_state(config_path, state)


def mark_succeeded(config_path: Path, operation_id: str) -> None:
    state = load_operation(config_path, operation_id)
    state["result"] = "Succeeded"
    state["message"] = f"{state['command']} completed successfully."
    state["completed_at"] = _now()
    _write_state(config_path, state)


def mark_failed(config_path: Path, operation_id: str, message: str) -> None:
    state = load_operation(config_path, operation_id)
    state["result"] = "Failed"
    state["message"] = message
    state["completed_at"] = _now()
    _write_state(config_path, state)


def _elapsed_seconds(state: dict[str, Any]) -> int | None:
    start_text = state.get("started_at") or state.get("submitted_at")
    end_text = state.get("completed_at") or _now()
    try:
        start = datetime.fromisoformat(start_text)
        end = datetime.fromisoformat(end_text)
    except (TypeError, ValueError):
        return None
    return max(0, int((end - start).total_seconds()))


def format_duration(seconds: int | float | None) -> str:
    """Format a duration as HH:MM:SS for users and the test harness."""

    if seconds is None:
        return "-"
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def print_operation(config_path: Path, operation_id: str) -> None:
    """Print one operation result without mutating managed infrastructure."""

    state = load_operation(config_path, operation_id)
    result = effective_result(state)
    message = state.get("message", "")
    if result == "Interrupted":
        message = (
            "The detached worker is no longer running before a terminal result was "
            "recorded. The existing Terraform/deployment-lock resume safeguards remain "
            "in effect; rerun the same lifecycle command to resume safely."
        )

    print(f"Operation ID: {state['operation_id']}")
    print(f"Command:      {state['command']}")
    print(f"Deployment:   {state.get('deployment') or '-'}")
    print(f"Result:       {result}")
    print(f"Submitted:    {state.get('submitted_at') or '-'}")
    print(f"Started:      {state.get('started_at') or '-'}")
    print(f"Completed:    {state.get('completed_at') or '-'}")
    print(f"Elapsed:      {format_duration(_elapsed_seconds(state))}")
    print(f"Worker PID:   {state.get('pid') or '-'}")
    print(f"Log:          {state.get('log_file') or '-'}")
    print(f"Message:      {message or '-'}")


def print_operations(config_path: Path) -> None:
    """Print recent operation results in a compact table-like format."""

    records = list_operation_records(config_path)
    if not records:
        print("No terraformController asynchronous operations have been recorded.")
        return

    print("OPERATION ID  COMMAND                 DEPLOYMENT          RESULT       ELAPSED")
    print("------------  ----------------------  ------------------  -----------  --------")
    for state in records[:50]:
        print(
            f"{state.get('operation_id',''):<12}  "
            f"{state.get('command',''):<22}  "
            f"{state.get('deployment','-'):<18}  "
            f"{effective_result(state):<11}  "
            f"{format_duration(_elapsed_seconds(state))}"
        )


def submission_instructions(
    config_path: Path,
    state: dict[str, Any],
    *,
    shard_status: bool = False,
) -> str:
    """Return exact copy/paste commands for checking an accepted operation."""

    check_command = shlex.join(
        [
            sys.executable,
            "terraformController.py",
            "--config",
            str(config_path.expanduser().resolve()),
            "ListOperation",
            state["operation_id"],
        ]
    )
    lines = [
        f"{state['command']} request accepted.",
        "",
        f"Operation ID:   {state['operation_id']}",
        f"Deployment:     {state.get('deployment') or '-'}",
        "Status:         In Progress",
        "",
        "Check positive/negative result with:",
        f"  {check_command}",
    ]
    if shard_status and state.get("deployment"):
        shard_command = shlex.join(
            [
                sys.executable,
                "terraformController.py",
                "--config",
                str(config_path.expanduser().resolve()),
                "ListShards",
                state["deployment"],
            ]
        )
        lines.extend(
            [
                "",
                "Check live shard progress with:",
                f"  {shard_command}",
            ]
        )
    return "\n".join(lines)
