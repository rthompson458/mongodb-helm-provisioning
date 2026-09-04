from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

REPLICA_SET_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,28}[A-Za-z0-9])?$")
DATABASE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$")
ACCOUNT_TYPES = {
    "owner": {"display": "Owner", "suffix": "owner"},
    "read": {"display": "Read", "suffix": "read"},
    "readwrite": {"display": "ReadWrite", "suffix": "readWrite"},
}
SYSTEM_DATABASES = {"admin", "config", "local"}


class ControllerError(RuntimeError):
    pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ControllerError(f"Invalid UTC timestamp in Vault metadata: {value}") from exc
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def normalize_replica_set(name: str) -> tuple[str, str]:
    display = name.strip()
    if not display or not REPLICA_SET_RE.fullmatch(display):
        raise ControllerError(
            "ReplicaSet name must be 1-30 characters, use only letters, numbers, or hyphens, "
            "and start/end with a letter or number."
        )
    return display.lower(), display


def normalize_database(name: str) -> tuple[str, str]:
    display = name.strip()
    if not display or not DATABASE_RE.fullmatch(display):
        raise ControllerError(
            "Database name must be 1-48 characters and use only letters, numbers, underscores, or hyphens."
        )
    return display.lower(), display


def normalize_account_type(value: str) -> str:
    key = value.strip().lower()
    if key not in ACCOUNT_TYPES:
        raise ControllerError("ACCOUNT_TYPE must be Owner, Read, or ReadWrite. Matching is case-insensitive.")
    return key


def run_process(
    command: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None,
    input_text: str | None = None, capture: bool = False, check: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            command, cwd=cwd, env=env, input=input_text, text=True,
            capture_output=capture, check=False,
        )
    except FileNotFoundError as exc:
        raise ControllerError(f"Required executable was not found: {command[0]}") from exc

    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip() if capture else ""
        message = f"Command failed ({result.returncode}): {' '.join(command)}"
        raise ControllerError(message + (f"\n{detail}" if detail else ""))
    return result


def rotation_remaining(rotated_at: str, rotation_days: int) -> str:
    remaining = parse_utc(rotated_at) + timedelta(days=rotation_days) - utc_now()
    seconds = int(remaining.total_seconds())
    if seconds <= 0:
        days, rem = divmod(abs(seconds), 86400)
        hours = rem // 3600
        return f"DUE ({days}d {hours}h overdue)" if days else f"DUE ({hours}h overdue)"
    days, rem = divmod(seconds, 86400)
    hours = rem // 3600
    return f"{days}d {hours}h" if days else f"{hours}h {(rem % 3600) // 60}m"


def print_table(headers: tuple[str, ...], rows: list[tuple[str, ...]]) -> None:
    if not rows:
        return
    widths = [max(len(headers[i]), max(len(row[i]) for row in rows)) for i in range(len(headers))]
    print("  ".join(headers[i].ljust(widths[i]) for i in range(len(headers))))
    print("  ".join("-" * widths[i] for i in range(len(headers))))
    for row in rows:
        print("  ".join(row[i].ljust(widths[i]) for i in range(len(row))))


def account_resource_name(rs_key: str, db_key: str, account_key: str) -> str:
    import hashlib
    digest = hashlib.md5(f"{rs_key}/{db_key}/{account_key}".encode()).hexdigest()[:6]
    return f"tc-{rs_key[:8]}-{db_key[:10]}-{account_key}-{digest}"


def database_rows(rs: dict[str, Any], db: dict[str, Any], rotation_days: int) -> list[tuple[str, ...]]:
    rotates = rotation_remaining(db["rotated_at"], rotation_days)
    rows = []
    for key in ("owner", "readwrite", "read"):
        account = ACCOUNT_TYPES[key]
        status = "Disabled" if key == "owner" and db["owner_disabled"] else "Enabled"
        rows.append((
            rs["display_name"], db["display_name"],
            f"{db['display_name']}_{account['suffix']}", account["display"], status, rotates,
        ))
    return rows
