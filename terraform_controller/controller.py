from __future__ import annotations

from typing import Any

from .common import (
    ACCOUNT_TYPES, ControllerError, account_resource_name, database_rows, iso_utc,
    normalize_account_type, normalize_database, normalize_replica_set, print_table, utc_now,
)
from . import kube
from .terraform_runner import apply_inventory
from .vault import VaultClient


def require_rs(inventory: dict[str, dict[str, Any]], name: str) -> tuple[str, dict[str, Any]]:
    key, _ = normalize_replica_set(name)
    if key not in inventory: raise ControllerError(f"ReplicaSet '{name}' does not exist in terraformController.")
    return key, inventory[key]


def require_db(inventory: dict[str, dict[str, Any]], rs_name: str, db_name: str):
    rs_key, rs = require_rs(inventory, rs_name)
    db_key, _ = normalize_database(db_name)
    if db_key not in rs["databases"]:
        raise ControllerError(f"Database '{db_name}' does not exist on ReplicaSet '{rs['display_name']}'.")
    return rs_key, rs, db_key, rs["databases"][db_key]


def add_replica_set(config: dict[str, Any], vault: VaultClient, name: str) -> None:
    key, display = normalize_replica_set(name)
    inventory = vault.load_inventory()
    if key in inventory: raise ControllerError(f"ReplicaSet '{inventory[key]['display_name']}' already exists.")
    if kube.get_json(config, "mongodb", key) is not None:
        raise ControllerError(f"Kubernetes MongoDB resource '{key}' already exists outside terraformController; automatic adoption is blocked.")
    inventory[key] = {
        "display_name": display, "created_at": iso_utc(utc_now()),
        "members": config["default_members"], "version": config["default_version"],
        "persistent": config["persistent"], "storage_class": config["storage_class"],
        "storage_size": config["storage_size"], "controller_password_version": 1,
        "databases": {},
    }
    if config["persistent"]:
        print(f"Creating {config['default_members']} static local PVs for ReplicaSet '{display}' ...")
        kube.ensure_replica_set_storage(
            config, key, config["default_members"], config["storage_size"], config["storage_class"]
        )
    apply_inventory(config, inventory)
    print(f"Waiting for ReplicaSet '{display}' to become Running ...")
    kube.wait_phase(config, "mongodb", key, "Running", config["rs_ready_timeout"])
    kube.wait_phase(config, "mongodbuser", kube.controller_user(key), "Updated", config["rs_ready_timeout"])
    print(f"\nReplicaSet '{display}' was created as Kubernetes resource '{key}'.")
    print("Databases: 0")


def delete_replica_set(config: dict[str, Any], vault: VaultClient, name: str) -> None:
    inventory = vault.load_inventory()
    key, rs = require_rs(inventory, name)
    if rs["databases"]:
        names = "\n".join(f"  {rs['databases'][x]['display_name']}" for x in sorted(rs["databases"]))
        raise ControllerError(f"ReplicaSet '{rs['display_name']}' contains managed databases:\n{names}\nDelete the databases first.")
    runtime = kube.list_databases(config, key)
    if runtime:
        raise ControllerError(
            f"ReplicaSet '{rs['display_name']}' still contains MongoDB databases:\n" +
            "\n".join(f"  {x}" for x in runtime) + "\nDelete them first. No ReplicaSet was deleted."
        )
    del inventory[key]
    apply_inventory(config, inventory)
    kube.wait_absent(config, "mongodb", key, config["rs_ready_timeout"])
    if rs["persistent"]:
        kube.cleanup_replica_set_storage(config, key, int(rs["members"]))
    print(f"\nReplicaSet '{rs['display_name']}' was deleted.")
    if rs["persistent"]:
        print("Its retained PVCs, static PVs, and local storage directories were also removed.")


def list_replica_sets(config: dict[str, Any], vault: VaultClient) -> None:
    inventory = vault.load_inventory()
    if not inventory:
        print("No terraformController-managed ReplicaSets exist.")
        return
    rows = []
    for key in sorted(inventory):
        rs = inventory[key]
        rows.append((rs["display_name"], key, kube.phase(config, key), str(rs["members"]), str(len(rs["databases"]))))
    print_table(("REPLICA SET", "K8S RESOURCE", "PHASE", "MEMBERS", "DATABASES"), rows)


def list_replica_set(config: dict[str, Any], vault: VaultClient, name: str) -> None:
    inventory = vault.load_inventory()
    key, rs = require_rs(inventory, name)
    print(f"ReplicaSet:    {rs['display_name']}")
    print(f"K8s Resource: {key}")
    print(f"Phase:        {kube.phase(config, key)}")
    print(f"Members:      {rs['members']}")
    print(f"MongoDB:      {rs['version']}")
    print(f"Databases:    {len(rs['databases'])}")
    rows = []
    for db_key in sorted(rs["databases"]): rows += database_rows(rs, rs["databases"][db_key], config["rotation_days"])
    if rows:
        print()
        print_table(("REPLICA SET", "DATABASE", "ACCOUNT", "TYPE", "STATUS", "ROTATES IN"), rows)


def add_database(config: dict[str, Any], vault: VaultClient, rs_name: str, db_name: str) -> None:
    inventory = vault.load_inventory()
    rs_key, rs = require_rs(inventory, rs_name)
    db_key, display = normalize_database(db_name)
    if db_key in rs["databases"]: raise ControllerError(f"Database '{display}' already exists on ReplicaSet '{rs['display_name']}'.")
    now = iso_utc(utc_now())
    rs["databases"][db_key] = {
        "display_name": display, "created_at": now, "owner_disabled": False,
        "rotation_version": 1, "rotated_at": now,
    }
    apply_inventory(config, inventory)
    for account in ("owner", "readwrite", "read"):
        kube.wait_phase(config, "mongodbuser", account_resource_name(rs_key, db_key, account), "Updated", config["rs_ready_timeout"])
    kube.ensure_database(config, rs_key, display)
    print(f"\nDatabase '{display}' was created on ReplicaSet '{rs['display_name']}'.")
    print("Created accounts:")
    print(f"  {display}_owner\n  {display}_readWrite\n  {display}_read")


def delete_database(config: dict[str, Any], vault: VaultClient, rs_name: str, db_name: str) -> None:
    inventory = vault.load_inventory()
    rs_key, rs, db_key, db = require_db(inventory, rs_name, db_name)
    kube.delete_database_if_empty(config, rs_key, db["display_name"])
    del rs["databases"][db_key]
    apply_inventory(config, inventory)
    print(f"\nDatabase '{db['display_name']}' was deleted from ReplicaSet '{rs['display_name']}'.")
    print("All three role accounts and their Vault credentials were deleted.")


def rotate_password(config: dict[str, Any], vault: VaultClient, rs_name: str, db_name: str) -> None:
    inventory = vault.load_inventory()
    _, rs, _, db = require_db(inventory, rs_name, db_name)
    db["rotation_version"] += 1
    db["rotated_at"] = iso_utc(utc_now())
    disabled = db["owner_disabled"]
    apply_inventory(config, inventory)
    print(f"\nRotated all three passwords for '{rs['display_name']}/{db['display_name']}'.")
    if disabled: print("The owner credential rotated and the owner account remains Disabled.")


def disable_owner(config: dict[str, Any], vault: VaultClient, rs_name: str, db_name: str) -> None:
    inventory = vault.load_inventory()
    rs_key, _, db_key, db = require_db(inventory, rs_name, db_name)
    if db["owner_disabled"]:
        print(f"Owner account '{db['display_name']}_owner' is already Disabled.")
        return
    db["owner_disabled"] = True
    apply_inventory(config, inventory)
    kube.wait_absent(config, "mongodbuser", account_resource_name(rs_key, db_key, "owner"))
    print(f"\nOwner account '{db['display_name']}_owner' is now Disabled.")
    print("Its Vault credential remains present and can still be rotated.")


def list_databases(config: dict[str, Any], vault: VaultClient) -> None:
    inventory = vault.load_inventory()
    rows = []
    for key in sorted(inventory):
        rs = inventory[key]
        for db_key in sorted(rs["databases"]): rows += database_rows(rs, rs["databases"][db_key], config["rotation_days"])
    if not rows:
        print("No managed databases exist.")
        return
    print_table(("REPLICA SET", "DATABASE", "ACCOUNT", "TYPE", "STATUS", "ROTATES IN"), rows)


def list_database(config: dict[str, Any], vault: VaultClient, rs_name: str, db_name: str) -> None:
    inventory = vault.load_inventory()
    _, rs, _, db = require_db(inventory, rs_name, db_name)
    print_table(("REPLICA SET", "DATABASE", "ACCOUNT", "TYPE", "STATUS", "ROTATES IN"), database_rows(rs, db, config["rotation_days"]))


def recover_password(config: dict[str, Any], vault: VaultClient, rs_name: str, db_name: str, account_type: str) -> None:
    inventory = vault.load_inventory()
    rs_key, rs, db_key, db = require_db(inventory, rs_name, db_name)
    account = normalize_account_type(account_type)
    secret = vault.account_secret(rs_key, db_key, account)
    if not secret: raise ControllerError("Vault credential was not found.")
    password = secret.get("password")
    if password is None: raise ControllerError("Vault credential contains no password.")
    username = secret.get("username", f"{db['display_name']}_{ACCOUNT_TYPES[account]['suffix']}")
    status = "Disabled" if account == "owner" and db["owner_disabled"] else "Enabled"
    print(f"ReplicaSet:   {rs['display_name']}")
    print(f"Database:     {db['display_name']}")
    print(f"Account Type: {ACCOUNT_TYPES[account]['display']}")
    print(f"Account:      {username}")
    print(f"Status:       {status}")
    print(f"Password:     {password}")
    print("\nDEMO ONLY: RecoverPassword exposes the password on the terminal.")


def reset_vault(config: dict[str, Any], vault: VaultClient, confirmed: bool) -> None:
    if not confirmed:
        raise ControllerError("ResetVault is destructive and requires '--confirm'. Example: terraformController.py ResetVault --confirm")
    inventory = vault.load_inventory()
    if not inventory:
        print("No terraformController-managed ReplicaSets exist. Nothing to reset.")
        return
    count = 0
    for key in sorted(inventory):
        print(f"Deleting non-system databases from ReplicaSet '{inventory[key]['display_name']}' ...")
        count += len(kube.drop_all_databases(config, key))

    for replica_set in inventory.values():
        replica_set["databases"] = {}

    apply_inventory(config, inventory)

    print("\nResetVault completed.")
    print("All managed databases, their three role accounts, and database Vault records were removed.")
    print("Managed ReplicaSets remain running and are now empty.")
    if count:
        print(f"MongoDB databases dropped: {count}")


def reconcile(config: dict[str, Any], vault: VaultClient) -> None:
    """Reapply the complete Vault-backed desired inventory and wait for convergence."""
    inventory = vault.load_inventory()
    if not inventory:
        print("No terraformController-managed ReplicaSets exist. Nothing to reconcile.")
        return

    apply_inventory(config, inventory)

    print("\nWaiting for managed ReplicaSets to converge ...")
    for key in sorted(inventory):
        rs = inventory[key]
        kube.wait_phase(config, "mongodb", key, "Running", config["rs_ready_timeout"])
        kube.wait_phase(
            config,
            "mongodbuser",
            kube.controller_user(key),
            "Updated",
            config["rs_ready_timeout"],
        )
        print(f"  {rs['display_name']}: Running")

    print("\nReconcile complete.")
