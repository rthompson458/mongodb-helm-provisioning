"""Detached long-running operation support for privateWorkerReplacement.

Customer requests that can take meaningful time return control to the shell
while a detached worker runs the normal Terraform-driven lifecycle. This
module owns the small operation journal used by administrators and the test
harness to determine whether that worker is queued, running, succeeded, failed,
or was interrupted.

Runtime files are intentionally easy to find:

    logs/operations/operations-YYYYMMDD.log   human diagnostic history
    logs/operations/state/<operation>.json   machine-readable operation state
    logs/operations/work/<operation>.tmp     temporary worker transcript

The temporary transcript is merged into the daily operations log when the
worker reaches a terminal result. Keeping one private transcript while a worker
runs prevents two background jobs from mixing their ordinary stdout/stderr.

This module coordinates execution only. It never performs MongoDB, Kubernetes,
Vault, storage, or topology mutations itself.
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
from .logging_component import append_worker_transcript
from .runtime_paths import (
    operation_state_directory,
    operation_work_directory,
    operations_log_path,
)

ASYNC_COMMANDS = {
    "AddReplicaSet",
    "DeleteReplicaSet",
    "AddShardedCluster",
    "DeleteShardedCluster",
    "AddShard",
    "DeleteShard",
    "AddDatabase",
    "DeleteDatabase",
    "RecoverOrphanedResources",
}

TERMINAL_RESULTS = {"Succeeded", "Failed", "Interrupted"}


def _now() -> str:
    """Return one UTC timestamp suitable for durable operation metadata."""

    return datetime.now(timezone.utc).isoformat()


def operation_directory(config_path: Path) -> Path:
    """Return the machine-readable async state directory.

    The function name is retained because the harness and tests already use it.
    Unlike the earlier implementation, the directory now lives under the
    product's predictable ``logs/operations`` tree instead of ``~/.local``.
    """

    return operation_state_directory(config_path)


def _state_path(config_path: Path, operation_id: str) -> Path:
    return operation_directory(config_path) / f"{operation_id}.json"


def _work_path(config_path: Path, operation_id: str) -> Path:
    return operation_work_directory(config_path) / f"{operation_id}.tmp"


def _write_state(config_path: Path, state: dict[str, Any]) -> None:
    """Atomically persist operation metadata without writing credentials."""

    directory = operation_directory(config_path)
    directory.mkdir(parents=True, exist_ok=True)
    try:
        directory.chmod(0o700)
    except OSError:
        pass

    path = _state_path(config_path, state["operation_id"])
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    temp.replace(path)
    try:
        path.chmod(0o600)
    except OSError:
        pass


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
            # A damaged status file should not make every other operation
            # invisible. ListOperation on that exact ID will still fail loudly.
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
    """Return the visible result, detecting a worker that disappeared early."""

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
    """Create the durable journal entry before launching the worker."""

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
        "log_file": str(operations_log_path(config_path)),
        "work_file": str(_work_path(config_path, operation_id)),
        "transcript_archived": False,
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
    entrypoint_name: str = "privateWorkerReplacement.py",
) -> dict[str, Any]:
    """Launch a detached worker that executes the normal lifecycle function."""

    # This is a local UX guard against obvious double-submits. ShardedCluster
    # cross-process safety still comes from the Terraform-created deployment
    # lock, which remains the authoritative mutation lock.
    for existing in list_operation_records(config_path):
        if (
            deployment
            and str(existing.get("deployment", "")).lower() == deployment.lower()
            and effective_result(existing) == "In Progress"
        ):
            raise ControllerError(
                f"Deployment '{deployment}' already has a controller operation "
                f"in progress: {existing.get('command')}. "
                "Wait for the current change to finish before submitting another "
                "mutation. Use the normal resource status commands to monitor progress."
            )

    state = create_operation(
        config_path,
        command=command,
        deployment=deployment,
        worker_arguments=worker_arguments,
    )
    operation_id = state["operation_id"]
    work_path = Path(state["work_file"])
    work_path.parent.mkdir(parents=True, exist_ok=True)
    entrypoint = repo_root / entrypoint_name

    # The detached worker receives an absolute config path on purpose. A
    # background process must not depend on the user's shell directory after
    # the foreground command has returned.
    worker_command = [
        sys.executable,
        str(entrypoint),
        "--config",
        str(config_path.expanduser().resolve()),
        "--_operation-worker",
        operation_id,
        *worker_arguments,
    ]

    # Environment context lets low-level Terraform/Git diagnostic blocks identify
    # which async request produced them without exposing the ID to DBaaS users.
    worker_env = os.environ.copy()
    worker_env["TC_OPERATION_ID"] = operation_id
    worker_env["TC_OPERATION_COMMAND"] = command

    try:
        with work_path.open("a", encoding="utf-8") as work_handle:
            process = subprocess.Popen(
                worker_command,
                cwd=repo_root,
                env=worker_env,
                stdin=subprocess.DEVNULL,
                stdout=work_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
            )
    except OSError as exc:
        state["result"] = "Failed"
        state["message"] = f"Could not start detached worker: {exc}"
        state["completed_at"] = _now()
        _write_state(config_path, state)
        _finalize_transcript(config_path, state)
        raise ControllerError(state["message"]) from exc

    # A very fast worker can finish before the parent records its PID. Reload so
    # the parent never overwrites a Succeeded/Failed result written by the child.
    latest = load_operation(config_path, operation_id)
    latest["pid"] = process.pid
    if latest.get("result") == "Queued":
        latest["result"] = "In Progress"
        latest["message"] = "Detached local worker started."
    _write_state(config_path, latest)
    return latest


def mark_running(config_path: Path, operation_id: str) -> None:
    state = load_operation(config_path, operation_id)
    state["result"] = "In Progress"
    state["started_at"] = state.get("started_at") or _now()
    state["message"] = f"{state['command']} is running."
    state["pid"] = os.getpid()
    _write_state(config_path, state)


def _flush_standard_streams() -> None:
    """Flush worker transcript buffers before the temporary file is archived."""

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except (AttributeError, OSError):
            pass


def _finalize_transcript(
    config_path: Path,
    state: dict[str, Any],
    *,
    visible_result: str | None = None,
    visible_message: str | None = None,
) -> None:
    """Move one worker's temporary transcript into the daily operations log."""

    if state.get("transcript_archived"):
        return

    _flush_standard_streams()
    work_path = Path(str(state.get("work_file", ""))) if state.get("work_file") else None
    transcript = ""
    if work_path and work_path.exists():
        try:
            transcript = work_path.read_text(encoding="utf-8")
        except OSError as exc:
            transcript = f"Could not read temporary worker transcript: {exc}"

    append_worker_transcript(
        config_path,
        operation_id=str(state.get("operation_id", "")),
        command=str(state.get("command", "")),
        deployment=str(state.get("deployment", "")),
        result=visible_result or str(state.get("result", "")),
        message=visible_message or str(state.get("message", "")),
        transcript=transcript,
        log_file=str(state.get("log_file", "")) or None,
    )

    if work_path and work_path.exists():
        try:
            work_path.unlink()
        except OSError:
            # The permanent daily log already contains the transcript. A stale
            # temp file is untidy but must not turn a successful operation into
            # a false failure.
            pass

    state["transcript_archived"] = True
    _write_state(config_path, state)


def mark_succeeded(config_path: Path, operation_id: str) -> None:
    state = load_operation(config_path, operation_id)
    state["result"] = "Succeeded"
    state["message"] = f"{state['command']} completed successfully."
    state["completed_at"] = _now()
    _write_state(config_path, state)
    _finalize_transcript(config_path, state)


def mark_failed(config_path: Path, operation_id: str, message: str) -> None:
    state = load_operation(config_path, operation_id)
    state["result"] = "Failed"
    state["message"] = message
    state["completed_at"] = _now()
    _write_state(config_path, state)
    _finalize_transcript(config_path, state)


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


def _interrupted_message(state: dict[str, Any]) -> str:
    """Return actionable administrator guidance for a dead async worker."""

    if state.get("command") in {"AddShard", "DeleteShard"}:
        recovery = (
            "The Terraform deployment lock/resume safeguards remain in effect. "
            "Rerun the same shard command with the same count to resume safely."
        )
    else:
        recovery = (
            "Inspect the normal deployment/database status before another mutation. "
            "Use the Terraform-driven Reconcile or guarded recovery path only after "
            "the recorded service state is understood."
        )
    return (
        "The detached worker is no longer running before a terminal result was "
        f"recorded. {recovery}"
    )


def print_operation(config_path: Path, operation_id: str) -> None:
    """Print one administrator operation result without mutating DBaaS state."""

    state = load_operation(config_path, operation_id)
    result = effective_result(state)
    message = str(state.get("message", ""))
    if result == "Interrupted":
        message = _interrupted_message(state)
        _finalize_transcript(
            config_path,
            state,
            visible_result="Interrupted",
            visible_message=message,
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
    """Print up to 50 recent async operations in a compact administrator table."""

    records = list_operation_records(config_path)
    if not records:
        print("No privateWorkerReplacement asynchronous operations have been recorded.")
        return

    print("OPERATION ID  COMMAND                 DEPLOYMENT          RESULT       ELAPSED")
    print("------------  ----------------------  ------------------  -----------  --------")
    for state in records[:50]:
        print(
            f"{state.get('operation_id',''):<12}  "
            f"{state.get('command',''):<22}  "
            f"{state.get('deployment','-') or '-':<18}  "
            f"{effective_result(state):<11}  "
            f"{format_duration(_elapsed_seconds(state))}"
        )


def public_submission_instructions(
    config_path: Path,
    state: dict[str, Any],
    *,
    details: Sequence[tuple[str, str]],
    status_text: str,
    status_arguments: Sequence[str] | None = None,
    config_display: str | None = None,
) -> str:
    """Return a customer acknowledgement without operation internals.

    ``config_path`` is the resolved path used by the worker. ``config_display``
    is the cleaner path originally supplied by the user, normally
    ``./privateWorkerReplacement.config``. Keeping them separate prevents internal
    absolute paths from leaking into routine customer instructions.
    """

    lines = [f"{state['command']} request accepted.", ""]
    for label, value in details:
        if value:
            lines.append(f"{label + ':':<15} {value}")
    lines.extend(
        [
            f"{'Status:':<15} {status_text}",
            "",
            "The request is being processed in the background.",
        ]
    )

    if status_arguments:
        check_command = shlex.join(
            [
                "python3",
                "privateWorkerReplacement.py",
                "--config",
                config_display or str(config_path),
                *status_arguments,
            ]
        )
        lines.extend(["", "Check service status with:", f"  {check_command}"])

    return "\n".join(lines)


def admin_submission_instructions(
    config_path: Path,
    state: dict[str, Any],
    *,
    config_display: str | None = None,
) -> str:
    """Return administrator-facing async details and exact journal command.

    The worker still uses the resolved absolute path internally. The displayed
    command uses the path the administrator supplied, which is normally the
    friendlier ``./privateWorkerReplacement.config`` form.
    """

    check_command = shlex.join(
        [
            "python3",
            "privateWorkerReplacementAdmin.py",
            "--config",
            config_display or str(config_path),
            "ListOperation",
            state["operation_id"],
        ]
    )
    return "\n".join(
        [
            f"{state['command']} request accepted.",
            "",
            f"Operation ID:   {state['operation_id']}",
            f"Scope:          {state.get('deployment') or '-'}",
            "Status:         In Progress",
            "",
            "Check administrator operation status with:",
            f"  {check_command}",
        ]
    )
