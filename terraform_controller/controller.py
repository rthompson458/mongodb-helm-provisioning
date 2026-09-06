from __future__ import annotations

from typing import Any

from .common import (
    ControllerError,
    account_resource_name,
    database_rows,
    iso_utc,
    normalize_database,
    normalize_replica_set,
    print_table,
    utc_now,
)
from . import kube
from .terraform_runner import apply_inventory
from .vault import VaultClient


def require_rs(inventory: dict[str, dict[str, Any]], name: str) -> tuple[str, dict[str, Any]]:
    key, _ = normalize_replica_set(name)
    if key not in inventory:
        raise ControllerError(f"ReplicaSet '{name}' does not exist in terraformController.")
    return key, inventory[key]


def require_db(inventory: dict[str, dict[str, Any]], rs_name: str, db_name: str):
    rs_key, rs = require_rs(inventory, rs_name)
    db_key, _ = normalize_database(db_name)
    if db_key not in rs["databases"]:
        raise ControllerError(f"Database '{db_name}' does not exist on ReplicaSet '{rs['display_name']}'.")
    return rs_key, rs, db_key, rs["databases"][db_key]


def _require_confirm(confirmed: bool, command: str, example: str) -> None:
    if not confirmed:
        raise ControllerError(f"{command} is destructive and requires '--confirm'. Example: {example}")


def _require_running(config: dict[str, Any], rs_key: str, display_name: str) -> None:
    current = kube.phase(config, rs_key)
    if current != "Running":
        raise ControllerError(
            f"ReplicaSet '{display_name}' is not Running. Current phase: {current}. "
            "No database change was attempted."
        )


def _vault_ui(config: dict[str, Any]) -> str:
    return f"{config['vault_address'].rstrip('/')}/ui/"


def _vault_paths(config: dict[str, Any], rs: dict[str, Any], db: dict[str, Any]) -> list[str]:
    base = config["vault_base_path"].strip("/")
    root = f"{base}/{rs['display_name']}/{db['display_name']}"
    name = db["display_name"]
    return [
        f"{root}/{name}_owner",
        f"{root}/{name}_readWrite",
        f"{root}/{name}_read",
    ]


def add_replica_set(config: dict[str, Any], vault: VaultClient, name: str) -> None:
    key, display = normalize_replica_set(name)
    inventory = vault.load_inventory()
    if key in inventory:
        raise ControllerError(f"ReplicaSet '{inventory[key]['display_name']}' already exists.")
    existing = kube.get_json(config, "mongodb", key)
    if existing is not None:
        labels = existing.get("metadata", {}).get("labels", {})
        if labels.get("app.kubernetes.io/managed-by") != "terraformController":
            raise ControllerError(
                f"Kubernetes MongoDB resource '{key}' already exists outside "
                "terraformController; automatic adoption is blocked."
            )
        print(
            f"Recovering an incomplete terraformController creation for "
            f"ReplicaSet '{display}' ..."
        )

    inventory[key] = {
        "display_name": display,
        "created_at": iso_utc(utc_now()),
        "members": config["default_members"],
        "version": config["default_version"],
        "persistent": config["persistent"],
        "storage_class": config["storage_class"],
        "storage_size": config["storage_size"],
        "storage_mode": config["storage_mode"],
        "storage_base_path": config["storage_base_path"],
        "storage_node_name": config["storage_node_name"],
        "controller_password_version": 1,
        "databases": {},
    }

    apply_inventory(config, inventory)
    print(f"Waiting for ReplicaSet '{display}' to become Running ...")
    kube.wait_phase(config, "mongodb", key, "Running", config["rs_ready_timeout"])
    kube.wait_phase(config, "mongodbuser", kube.controller_user(key), "Updated", config["rs_ready_timeout"])
    print(f"\nReplicaSet '{display}' was created as Kubernetes resource '{key}'.")
    print("Phase:     Running")
    print("Databases: 0")


def delete_replica_set(
    config: dict[str, Any], vault: VaultClient, name: str, confirmed: bool
) -> None:
    _require_confirm(confirmed, "DeleteReplicaSet", f"terraformController.py DeleteReplicaSet {name} --confirm")
    inventory = vault.load_inventory()
    key, rs = require_rs(inventory, name)

    if rs["databases"]:
        names = "\n".join(f"  {rs['databases'][x]['display_name']}" for x in sorted(rs["databases"]))
        raise ControllerError(
            f"ReplicaSet '{rs['display_name']}' contains managed databases:\n{names}\nDelete the databases first."
        )

    apply_inventory(
        config,
        inventory,
        {
            "action": "validate_replica_set_empty",
            "replica_set": key,
            "database": "",
            "members": int(rs["members"]),
        },
    )

    del inventory[key]
    apply_inventory(config, inventory)
    kube.wait_absent(config, "mongodb", key, config["rs_ready_timeout"])

    print(f"\nReplicaSet '{rs['display_name']}' was deleted.")


def list_replica_sets(config: dict[str, Any], vault: VaultClient) -> None:
    inventory = vault.load_inventory()
    if not inventory:
        print("No terraformController-managed application ReplicaSets exist.")
        return
    rows = []
    for key in sorted(inventory):
        rs = inventory[key]
        rows.append(
            (
                rs["display_name"],
                key,
                kube.phase(config, key),
                str(rs["members"]),
                rs["version"],
                str(len(rs["databases"])),
            )
        )
    print_table(("REPLICA SET", "K8S RESOURCE", "PHASE", "MEMBERS", "MONGODB", "DATABASES"), rows)


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
    for db_key in sorted(rs["databases"]):
        rows += database_rows(rs, rs["databases"][db_key], config["rotation_days"])
    if rows:
        print()
        print_table(("REPLICA SET", "DATABASE", "ACCOUNT", "TYPE", "STATUS", "ROTATES IN"), rows)


def _verify_database_accounts(
    config: dict[str, Any],
    inventory: dict[str, dict[str, Any]],
    rs_key: str,
    rs: dict[str, Any],
    db_key: str,
    db: dict[str, Any],
) -> None:
    if db["owner_disabled"]:
        kube.wait_absent(
            config,
            "mongodbuser",
            account_resource_name(rs_key, db_key, "owner"),
            config["rs_ready_timeout"],
        )
        accounts = ("readwrite", "read")
        action = "verify_database_accounts_owner_disabled"
    else:
        accounts = ("owner", "readwrite", "read")
        action = "verify_database_accounts"

    for account in accounts:
        kube.wait_phase(
            config,
            "mongodbuser",
            account_resource_name(rs_key, db_key, account),
            "Updated",
            config["rs_ready_timeout"],
        )

    apply_inventory(
        config,
        inventory,
        {
            "action": action,
            "replica_set": rs_key,
            "database": db["display_name"],
            "members": int(rs["members"]),
        },
    )


def add_database(config: dict[str, Any], vault: VaultClient, rs_name: str, db_name: str) -> None:
    inventory = vault.load_inventory()
    rs_key, rs = require_rs(inventory, rs_name)
    _require_running(config, rs_key, rs["display_name"])

    db_key, display = normalize_database(db_name)
    if db_key in rs["databases"]:
        raise ControllerError(
            f"Database '{display}' already exists on ReplicaSet '{rs['display_name']}'. "
            "Use Reconcile if a previous AddDatabase was interrupted."
        )

    # Stage 1: Terraform materializes the MongoDB database while the desired
    # inventory is still unchanged. If this fails, no DB users or Vault
    # credentials have been added.
    apply_inventory(
        config,
        inventory,
        {
            "action": "create_database",
            "replica_set": rs_key,
            "database": display,
            "members": int(rs["members"]),
        },
    )

    # Stage 2: after database materialization succeeds, Terraform records the
    # lifecycle metadata and creates the three fixed accounts and credentials.
    now = iso_utc(utc_now())
    rs["databases"][db_key] = {
        "display_name": display,
        "created_at": now,
        "owner_disabled": False,
        "owner_disabled_at": "",
        "rotation_version": 1,
        "rotated_at": now,
    }
    apply_inventory(config, inventory)

    updated_inventory = vault.load_inventory()
    rs_key, updated_rs, db_key, updated_db = require_db(updated_inventory, rs_name, db_name)
    _verify_database_accounts(
        config, updated_inventory, rs_key, updated_rs, db_key, updated_db
    )

    print(f"\nDatabase '{display}' was created on ReplicaSet '{updated_rs['display_name']}'.")
    print("Created accounts:")
    print(f"  {display}_owner      (dbOwner)")
    print(f"  {display}_readWrite  (readWrite)")
    print(f"  {display}_read       (read)")
    print(f"\nVault UI: {_vault_ui(config)}")
    print("Vault credential paths:")
    for path in _vault_paths(config, updated_rs, updated_db):
        print(f"  {path}")
    print(f"\nPassword rotation interval: {config['rotation_days']} days")
    print("The owner account will be disabled at the first rotation at or after day 30.")



def delete_database(
    config: dict[str, Any], vault: VaultClient, rs_name: str, db_name: str, confirmed: bool
) -> None:
    _require_confirm(
        confirmed,
        "DeleteDatabase",
        f"terraformController.py DeleteDatabase {rs_name} {db_name} --confirm",
    )
    inventory = vault.load_inventory()
    rs_key, rs, db_key, db = require_db(inventory, rs_name, db_name)
    _require_running(config, rs_key, rs["display_name"])

    # --confirm authorizes deletion of the database and all of its contents.
    # Terraform performs the MongoDB drop before desired account state is removed.
    apply_inventory(
        config,
        inventory,
        {
            "action": "delete_database",
            "replica_set": rs_key,
            "database": db["display_name"],
            "members": int(rs["members"]),
        },
    )

    del rs["databases"][db_key]
    apply_inventory(config, inventory)

    for account in ("owner", "readwrite", "read"):
        kube.wait_absent(
            config,
            "mongodbuser",
            account_resource_name(rs_key, db_key, account),
            config["rs_ready_timeout"],
        )

    apply_inventory(
        config,
        inventory,
        {
            "action": "verify_database_users_absent",
            "replica_set": rs_key,
            "database": db["display_name"],
            "members": int(rs["members"]),
        },
    )

    print(f"\nDatabase '{db['display_name']}' was deleted from ReplicaSet '{rs['display_name']}'.")
    print("All three MongoDB role accounts and their Vault credentials were deleted.")



def rotate_passwords(config: dict[str, Any], vault: VaultClient, rs_name: str, db_name: str) -> None:
    inventory = vault.load_inventory()
    rs_key, rs, _, db = require_db(inventory, rs_name, db_name)
    _require_running(config, rs_key, rs["display_name"])

    # Rotation spans Vault and Kubernetes write-only fields. Database lifecycle
    # metadata is committed first in Terraform. If an apply is interrupted
    # after only one password sink changes, reload the committed revision and
    # perform one fresh rotation. That new revision forces both sinks to use
    # the same new ephemeral password.
    last_error: ControllerError | None = None
    for attempt in range(2):
        if attempt:
            inventory = vault.load_inventory()
            rs_key, rs, _, db = require_db(inventory, rs_name, db_name)
            _require_running(config, rs_key, rs["display_name"])
            print("Retrying password rotation with a fresh Terraform revision ...")

        try:
            apply_inventory(
                config,
                inventory,
                {
                    "action": "rotate_passwords",
                    "replica_set": rs_key,
                    "database": db["display_name"],
                    "members": int(rs["members"]),
                },
            )
            last_error = None
            break
        except ControllerError as exc:
            last_error = exc
            if attempt == 0:
                continue

    if last_error is not None:
        raise ControllerError(
            "Password rotation did not converge after a recovery retry. "
            "No success was reported. Run RotatePasswords again after correcting "
            "the Terraform/Kubernetes/Vault error."
        ) from last_error

    updated_inventory = vault.load_inventory()
    rs_key, updated_rs, db_key, updated_db = require_db(
        updated_inventory, rs_name, db_name
    )
    _verify_database_accounts(
        config, updated_inventory, rs_key, updated_rs, db_key, updated_db
    )

    print(
        f"\nRotated all three passwords for "
        f"'{updated_rs['display_name']}/{updated_db['display_name']}'."
    )
    if updated_db["owner_disabled"]:
        print(
            "Owner status: Disabled in MongoDB. "
            "Its newly rotated password remains available in Vault."
        )
    else:
        print("Owner status: Enabled.")
    print(f"Last rotated: {updated_db['rotated_at']}")
    print(f"Vault UI: {_vault_ui(config)}")


def disable_owner(
    config: dict[str, Any], vault: VaultClient, rs_name: str, db_name: str, confirmed: bool
) -> None:
    _require_confirm(
        confirmed,
        "DisableOwner",
        f"terraformController.py DisableOwner {rs_name} {db_name} --confirm",
    )
    inventory = vault.load_inventory()
    rs_key, rs, _, db = require_db(inventory, rs_name, db_name)
    _require_running(config, rs_key, rs["display_name"])

    if db["owner_disabled"]:
        print(f"Owner account '{db['display_name']}_owner' is already Disabled.")
        return

    # Terraform writes the lifecycle state and removes the MongoDBUser.
    apply_inventory(
        config,
        inventory,
        {
            "action": "disable_owner",
            "replica_set": rs_key,
            "database": db["display_name"],
            "members": int(rs["members"]),
        },
    )

    updated_inventory = vault.load_inventory()
    rs_key, updated_rs, db_key, updated_db = require_db(
        updated_inventory, rs_name, db_name
    )
    _verify_database_accounts(
        config, updated_inventory, rs_key, updated_rs, db_key, updated_db
    )

    print(f"\nOwner account '{updated_db['display_name']}_owner' is now Disabled in MongoDB.")
    print("Its current Vault credential remains present and will continue to rotate.")



def list_databases(
    config: dict[str, Any], vault: VaultClient, rs_name: str | None = None
) -> None:
    inventory = vault.load_inventory()
    rows = []

    if rs_name:
        _, rs = require_rs(inventory, rs_name)
        replica_sets = [rs]
    else:
        replica_sets = [inventory[key] for key in sorted(inventory)]

    for rs in replica_sets:
        for db_key in sorted(rs["databases"]):
            rows += database_rows(rs, rs["databases"][db_key], config["rotation_days"])

    if not rows:
        if rs_name:
            print(f"No managed databases exist on ReplicaSet '{replica_sets[0]['display_name']}'.")
        else:
            print("No managed databases exist.")
        return
    print_table(("REPLICA SET", "DATABASE", "ACCOUNT", "TYPE", "STATUS", "ROTATES IN"), rows)


def list_database(config: dict[str, Any], vault: VaultClient, rs_name: str, db_name: str) -> None:
    inventory = vault.load_inventory()
    _, rs, _, db = require_db(inventory, rs_name, db_name)
    print_table(
        ("REPLICA SET", "DATABASE", "ACCOUNT", "TYPE", "STATUS", "ROTATES IN"),
        database_rows(rs, db, config["rotation_days"]),
    )
    print(f"Created:      {db['created_at']}")
    print(f"Last rotated: {db['rotated_at']}")
    print(f"Vault UI:     {_vault_ui(config)}")


def reconcile(config: dict[str, Any], vault: VaultClient) -> None:
    """Reapply complete Vault-backed desired state through Terraform."""
    inventory = vault.load_inventory()
    if not inventory:
        print("No terraformController-managed ReplicaSets exist. Nothing to reconcile.")
        return

    apply_inventory(config, inventory)
    print("\nWaiting for managed ReplicaSets and accounts to converge ...")
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
        for db_key in sorted(rs["databases"]):
            db = rs["databases"][db_key]
            if db["owner_disabled"]:
                kube.wait_absent(
                    config,
                    "mongodbuser",
                    account_resource_name(key, db_key, "owner"),
                    config["rs_ready_timeout"],
                )
                accounts = ("readwrite", "read")
            else:
                accounts = ("owner", "readwrite", "read")
            for account in accounts:
                kube.wait_phase(
                    config,
                    "mongodbuser",
                    account_resource_name(key, db_key, account),
                    "Updated",
                    config["rs_ready_timeout"],
                )
        print(f"  {rs['display_name']}: Running")

    print("\nReconcile complete.")
