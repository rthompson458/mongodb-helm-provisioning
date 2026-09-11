"""Read-only database and database-account status for the customer CLI.

The public interface keeps database lifecycle status separate from account
credential details:

- ListDatabases/ListDatabase report database state only.
- ListDatabaseAccounts reports the three managed accounts, rotation state, and
  browser-ready Vault URLs.

Database status combines Vault-backed desired state, live deployment health,
and any active asynchronous AddDatabase/DeleteDatabase operation. This lets a
customer see Creating or Deleting while background work is still in progress.

This module never mutates DBaaS state. Shared Vault path/URL presentation lives
in credential_display.py so status code does not depend on lifecycle internals.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import kube
from .async_operations import effective_result, list_operation_records
from .common import ControllerError, database_rows, normalize_database, print_table
from .credential_display import print_vault_credentials
from .deployments import deployment_type_label, require_deployment, resolve_deployment
from .vault import VaultClient


def _resolve_database_args(
    config: dict[str, Any],
    inventory: dict[str, dict[str, Any]],
    deployment_or_database: str,
    database: str | None,
) -> tuple[str, dict[str, Any], str]:
    """Resolve DEPLOYMENT DATABASE or the one-deployment shorthand."""

    if database is None:
        key, deployment = resolve_deployment(config, inventory, None)
        return key, deployment, deployment_or_database
    key, deployment = require_deployment(inventory, deployment_or_database)
    return key, deployment, database


def _operation_database_target(
    record: dict[str, Any],
    inventory: dict[str, dict[str, Any]],
) -> tuple[str, str, str] | None:
    """Return deployment key, database key, and display name for active DB work."""

    if record.get("command") not in {"AddDatabase", "DeleteDatabase"}:
        return None
    if effective_result(record) != "In Progress":
        return None

    arguments = [
        str(value)
        for value in record.get("worker_arguments", [])
        if str(value) != "--confirm"
    ]
    if not arguments or arguments[0] not in {"AddDatabase", "DeleteDatabase"}:
        return None

    payload = arguments[1:]
    if len(payload) >= 2:
        deployment_key = payload[0].lower()
        database_name = payload[1]
    elif len(payload) == 1 and len(inventory) == 1:
        deployment_key = next(iter(inventory))
        database_name = payload[0]
    else:
        return None

    try:
        database_key, display = normalize_database(database_name)
    except ControllerError:
        return None
    return deployment_key, database_key, display


def _active_database_changes(
    config: dict[str, Any],
    inventory: dict[str, dict[str, Any]],
) -> dict[tuple[str, str], tuple[str, str]]:
    """Map active database targets to (status, original display name)."""

    changes: dict[tuple[str, str], tuple[str, str]] = {}
    config_path = Path(str(config["config_path"]))
    for record in list_operation_records(config_path):
        target = _operation_database_target(record, inventory)
        if target is None:
            continue
        deployment_key, database_key, display = target
        key = (deployment_key, database_key)
        if key in changes:
            continue
        status = "Creating" if record.get("command") == "AddDatabase" else "Deleting"
        changes[key] = (status, display)
    return changes


def _deployment_is_ready(
    config: dict[str, Any], deployment_key: str, deployment: dict[str, Any]
) -> bool:
    """Return True only when the parent deployment is usable for DB work."""

    if kube.phase(config, deployment_key) != "Running":
        return False
    if deployment_type_label(deployment) != "ShardedCluster":
        return True

    status = kube.sharded_cluster_status(
        config, deployment_key, int(deployment["shard_count"])
    )
    return (
        all(shard["status"] == "Online" for shard in status["shards"])
        and status["config_servers"]["status"] == "Online"
        and status["mongos"]["status"] == "Online"
    )


def _database_status(
    config: dict[str, Any],
    deployment_key: str,
    deployment: dict[str, Any],
    database_key: str,
    active_changes: dict[tuple[str, str], tuple[str, str]],
) -> str:
    """Return the customer-facing lifecycle status for one database."""

    active = active_changes.get((deployment_key, database_key))
    if active:
        return active[0]
    return "Ready" if _deployment_is_ready(config, deployment_key, deployment) else "Unavailable"


def list_databases(
    config: dict[str, Any], vault: VaultClient, deployment_name: str | None = None
) -> None:
    """List databases only, including active Creating/Deleting requests."""

    inventory = vault.load_inventory()
    if deployment_name:
        selected_key, selected = require_deployment(inventory, deployment_name)
        deployments = [(selected_key, selected)]
    else:
        deployments = [(key, inventory[key]) for key in sorted(inventory)]

    active_changes = _active_database_changes(config, inventory)
    rows: list[tuple[str, ...]] = []
    seen: set[tuple[str, str]] = set()

    for deployment_key, deployment in deployments:
        for database_key in sorted(deployment["databases"]):
            db = deployment["databases"][database_key]
            rows.append(
                (
                    deployment["display_name"],
                    db["display_name"],
                    _database_status(
                        config,
                        deployment_key,
                        deployment,
                        database_key,
                        active_changes,
                    ),
                )
            )
            seen.add((deployment_key, database_key))

    # AddDatabase can be Creating before its Vault inventory entry exists.
    # DeleteDatabase can also remain In Progress briefly after its inventory
    # entry is removed while account/storage verification finishes. Keep both
    # active states visible so public status never claims the database vanished
    # before the asynchronous operation reaches a terminal result.
    selected_keys = {key for key, _ in deployments}
    for (deployment_key, database_key), (status, display) in sorted(active_changes.items()):
        if (deployment_key, database_key) in seen:
            continue
        if deployment_key not in selected_keys or deployment_key not in inventory:
            continue
        rows.append((inventory[deployment_key]["display_name"], display, status))

    if not rows:
        if deployment_name:
            print(
                f"No managed databases exist on {deployment_type_label(selected)} "
                f"'{selected['display_name']}'."
            )
        else:
            print("No managed databases exist.")
        return

    print_table(("DEPLOYMENT", "DATABASE", "STATUS"), rows)


def list_database(
    config: dict[str, Any],
    vault: VaultClient,
    deployment_or_database: str,
    database: str | None = None,
) -> None:
    """Show database-level state only; account details have their own command."""

    inventory = vault.load_inventory()
    deployment_key, deployment, db_name = _resolve_database_args(
        config, inventory, deployment_or_database, database
    )
    db_key, _ = normalize_database(db_name)
    active_changes = _active_database_changes(config, inventory)

    db = deployment["databases"].get(db_key)
    if db is None:
        active = active_changes.get((deployment_key, db_key))
        if active:
            print(f"Deployment:      {deployment['display_name']}")
            print(f"Deployment Type: {deployment_type_label(deployment)}")
            print(f"Database:        {active[1]}")
            print(f"Status:          {active[0]}")
            return
        raise ControllerError(
            f"Database '{db_name}' does not exist on "
            f"{deployment_type_label(deployment)} '{deployment['display_name']}'."
        )

    print(f"Deployment:      {deployment['display_name']}")
    print(f"Deployment Type: {deployment_type_label(deployment)}")
    print(f"Database:        {db['display_name']}")
    print(
        f"Status:          {_database_status(config, deployment_key, deployment, db_key, active_changes)}"
    )
    print(f"Created:         {db['created_at']}")


def list_database_accounts(
    config: dict[str, Any],
    vault: VaultClient,
    deployment_or_database: str,
    database: str | None = None,
) -> None:
    """Show the three managed accounts, rotation state, and Vault URLs."""

    inventory = vault.load_inventory()
    _, deployment, db_name = _resolve_database_args(
        config, inventory, deployment_or_database, database
    )
    db_key, _ = normalize_database(db_name)
    if db_key not in deployment["databases"]:
        raise ControllerError(
            f"Database '{db_name}' does not exist on "
            f"{deployment_type_label(deployment)} '{deployment['display_name']}'."
        )

    db = deployment["databases"][db_key]
    print_table(
        ("DEPLOYMENT", "DATABASE", "ACCOUNT", "TYPE", "STATUS", "ROTATES IN"),
        database_rows(deployment, db, config["rotation_days"]),
    )
    print(f"Last rotated: {db['rotated_at']}")
    print()
    print_vault_credentials(config, deployment, db)
