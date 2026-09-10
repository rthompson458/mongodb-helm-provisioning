"""Database, account, credential, and rotation lifecycle orchestration.

Every managed database gets exactly three fixed accounts:
    <DB>_owner      -> dbOwner
    <DB>_readWrite  -> readWrite
    <DB>_read       -> read

This module validates deployment readiness, builds desired state, asks Terraform
to apply that state, waits for MongoDBUser reconciliation, and verifies real
authentication. It does not directly create users, write Vault secrets, or
change MongoDB.

Customer-facing credential output includes complete browser-ready Vault URLs so
users do not have to assemble a URL from a mount name and secret path.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from . import kube
from .common import (
    ControllerError,
    account_resource_name,
    database_rows,
    iso_utc,
    normalize_database,
    print_table,
    utc_now,
)
from .deployment_lock import protected_database_change, require_no_active_change
from .deployments import (
    deployment_type_label,
    require_deployment,
    require_running,
    resolve_deployment,
)
from .logging_component import log_event
from .terraform_runner import apply_inventory
from .vault import VaultClient


def _vault_paths(
    config: dict[str, Any], deployment: dict[str, Any], db: dict[str, Any]
) -> list[str]:
    """Return the three logical Vault paths for one managed database."""

    base = config["vault_base_path"].strip("/")
    root = f"{base}/{deployment['display_name']}/{db['display_name']}"
    name = db["display_name"]
    return [
        f"{root}/{name}_owner",
        f"{root}/{name}_readWrite",
        f"{root}/{name}_read",
    ]


def _vault_browser_url(config: dict[str, Any], secret_path: str) -> str:
    """Build a Vault UI URL that opens the requested KV secret in a browser.

    Vault's UI route identifies the KV mount separately from the path stored
    inside that mount. Each path segment is URL-encoded so valid database names
    remain safe in a browser address.
    """

    base = config["vault_address"].rstrip("/")
    mount = quote(config["vault_mount"].strip("/"), safe="")
    encoded_path = "/".join(
        quote(part, safe="") for part in secret_path.strip("/").split("/")
    )
    return f"{base}/ui/vault/secrets/{mount}/show/{encoded_path}"


def _print_vault_credentials(
    config: dict[str, Any], deployment: dict[str, Any], db: dict[str, Any]
) -> None:
    """Print Vault paths plus complete browser URLs for all three credentials."""

    print("Vault credentials:")
    for path in _vault_paths(config, deployment, db):
        print(f"  Path: {path}")
        print(f"  URL:  {_vault_browser_url(config, path)}")


def _resolve_database_args(
    config: dict[str, Any],
    inventory: dict[str, dict[str, Any]],
    deployment_or_database: str,
    database: str | None,
) -> tuple[str, dict[str, Any], str]:
    """Resolve explicit DEPLOYMENT DATABASE or the one-deployment shorthand."""

    if database is None:
        key, deployment = resolve_deployment(config, inventory, None)
        return key, deployment, deployment_or_database
    key, deployment = require_deployment(inventory, deployment_or_database)
    return key, deployment, database


def require_db(
    inventory: dict[str, dict[str, Any]],
    deployment_name: str,
    db_name: str,
) -> tuple[str, dict[str, Any], str, dict[str, Any]]:
    """Find one managed database inside one deployment or fail clearly."""

    deployment_key, deployment = require_deployment(inventory, deployment_name)
    db_key, _ = normalize_database(db_name)
    if db_key not in deployment["databases"]:
        raise ControllerError(
            f"Database '{db_name}' does not exist on "
            f"{deployment_type_label(deployment)} '{deployment['display_name']}'."
        )
    return (
        deployment_key,
        deployment,
        db_key,
        deployment["databases"][db_key],
    )


def _operation(
    action: str,
    deployment_key: str,
    deployment: dict[str, Any],
    database: str = "",
) -> dict[str, Any]:
    """Build the small one-shot operation object consumed by Terraform."""

    members = (
        int(deployment["members_per_shard"])
        if deployment_type_label(deployment) == "ShardedCluster"
        else int(deployment["members"])
    )
    return {
        "action": action,
        "deployment": deployment_key,
        "deployment_type": deployment_type_label(deployment),
        "database": database,
        "members": members,
    }


def _verify_database_accounts(
    config: dict[str, Any],
    inventory: dict[str, dict[str, Any]],
    deployment_key: str,
    deployment: dict[str, Any],
    db_key: str,
    db: dict[str, Any],
) -> None:
    """Wait for expected MongoDBUser state, then verify real authentication.

    Kubernetes phase Updated proves reconciliation but not that the credential
    actually authenticates. The final Terraform lifecycle action runs a mongosh
    ping using each current connection secret.
    """

    if db["owner_disabled"]:
        kube.wait_absent(
            config,
            "mongodbuser",
            account_resource_name(deployment_key, db_key, "owner"),
            config["sc_ready_timeout"]
            if deployment_type_label(deployment) == "ShardedCluster"
            else config["rs_ready_timeout"],
        )
        accounts = ("readwrite", "read")
        action = "verify_database_accounts_owner_disabled"
    else:
        accounts = ("owner", "readwrite", "read")
        action = "verify_database_accounts"

    timeout = (
        config["sc_ready_timeout"]
        if deployment_type_label(deployment) == "ShardedCluster"
        else config["rs_ready_timeout"]
    )
    for account in accounts:
        kube.wait_phase(
            config,
            "mongodbuser",
            account_resource_name(deployment_key, db_key, account),
            "Updated",
            timeout,
        )

    apply_inventory(
        config,
        inventory,
        _operation(action, deployment_key, deployment, db["display_name"]),
    )


def add_database(
    config: dict[str, Any],
    vault: VaultClient,
    deployment_or_database: str,
    database: str | None = None,
) -> None:
    """Create one logical database plus its three managed accounts.

    MongoDB does not retain a truly empty database, so the database is
    materialized before desired account state is committed. Public AddDatabase
    now invokes this function from a detached worker; this function itself stays
    synchronous so the worker can report success only after verification.
    """

    inventory = vault.load_inventory()
    deployment_key, deployment, db_name = _resolve_database_args(
        config, inventory, deployment_or_database, database
    )
    require_no_active_change(config, deployment_key, deployment)
    require_running(config, deployment_key, deployment)

    db_key, display = normalize_database(db_name)
    if db_key in deployment["databases"]:
        raise ControllerError(
            f"Database '{display}' already exists on "
            f"{deployment_type_label(deployment)} '{deployment['display_name']}'. "
            "Use the administrator Reconcile command if an earlier AddDatabase "
            "was interrupted."
        )

    log_event(
        "database.create.requested",
        deployment=deployment["display_name"],
        deployment_type=deployment_type_label(deployment),
        database=display,
    )

    with protected_database_change(
        config,
        vault,
        inventory,
        deployment_key,
        deployment,
        "AddDatabase",
        display,
    ):
        # Stage 1: materialize the database before adding account/credential
        # desired state. This prevents credentials for a database that never
        # successfully came into existence.
        apply_inventory(
            config,
            inventory,
            _operation("create_database", deployment_key, deployment, display),
        )

        # Stage 2: record managed database lifecycle state and let Terraform
        # create the three fixed accounts plus Vault credentials.
        now = iso_utc(utc_now())
        deployment["databases"][db_key] = {
            "display_name": display,
            "created_at": now,
            "owner_disabled": False,
            "owner_disabled_at": "",
            "rotation_version": 1,
            "rotated_at": now,
        }
        apply_inventory(config, inventory)

        updated_inventory = vault.load_inventory()
        updated_key, updated_deployment, updated_db_key, updated_db = require_db(
            updated_inventory, deployment["display_name"], display
        )
        require_running(config, updated_key, updated_deployment)
        _verify_database_accounts(
            config,
            updated_inventory,
            updated_key,
            updated_deployment,
            updated_db_key,
            updated_db,
        )

    dtype = deployment_type_label(updated_deployment)
    log_event(
        "database.create.succeeded",
        deployment=updated_deployment["display_name"],
        deployment_type=dtype,
        database=display,
    )

    print(
        f"MongoDB database '{display}' was successfully created on "
        f"{dtype} '{updated_deployment['display_name']}'."
    )
    print("Managed accounts created:")
    print(f"  {display}_owner      (dbOwner)")
    print(f"  {display}_readWrite  (readWrite)")
    print(f"  {display}_read       (read)")
    print()
    _print_vault_credentials(config, updated_deployment, updated_db)
    print()
    print(f"Password rotation interval: {config['rotation_days']} days")
    print("Owner policy: disabled at the first rotation at or after day 30.")


def delete_database(
    config: dict[str, Any],
    vault: VaultClient,
    deployment_or_database: str,
    database: str | None,
    confirmed: bool,
) -> None:
    """Drop one database and remove all three managed accounts/credentials."""

    inventory = vault.load_inventory()
    deployment_key, deployment, db_name = _resolve_database_args(
        config, inventory, deployment_or_database, database
    )
    if not confirmed:
        example = (
            f"python3 privateWorkerReplacement.py DeleteDatabase "
            f"{deployment['display_name']} {db_name} --confirm"
        )
        raise ControllerError(
            f"DeleteDatabase is destructive and requires '--confirm'. Example: {example}"
        )

    db_key, _ = normalize_database(db_name)
    if db_key not in deployment["databases"]:
        raise ControllerError(
            f"Database '{db_name}' does not exist on "
            f"{deployment_type_label(deployment)} '{deployment['display_name']}'."
        )
    db = deployment["databases"][db_key]
    require_no_active_change(config, deployment_key, deployment)
    require_running(config, deployment_key, deployment)

    log_event(
        "database.delete.requested",
        deployment=deployment["display_name"],
        deployment_type=deployment_type_label(deployment),
        database=db["display_name"],
    )

    with protected_database_change(
        config,
        vault,
        inventory,
        deployment_key,
        deployment,
        "DeleteDatabase",
        db["display_name"],
    ):
        # --confirm authorizes deletion of the database and all of its contents.
        apply_inventory(
            config,
            inventory,
            _operation(
                "delete_database", deployment_key, deployment, db["display_name"]
            ),
        )

        del deployment["databases"][db_key]
        apply_inventory(config, inventory)

        timeout = (
            config["sc_ready_timeout"]
            if deployment_type_label(deployment) == "ShardedCluster"
            else config["rs_ready_timeout"]
        )
        for account in ("owner", "readwrite", "read"):
            kube.wait_absent(
                config,
                "mongodbuser",
                account_resource_name(deployment_key, db_key, account),
                timeout,
            )

        apply_inventory(
            config,
            inventory,
            _operation(
                "verify_database_users_absent",
                deployment_key,
                deployment,
                db["display_name"],
            ),
        )

    log_event(
        "database.delete.succeeded",
        deployment=deployment["display_name"],
        deployment_type=deployment_type_label(deployment),
        database=db["display_name"],
    )
    print(
        f"MongoDB database '{db['display_name']}' was successfully deleted from "
        f"{deployment_type_label(deployment)} '{deployment['display_name']}'."
    )
    print("Managed accounts removed: Owner, ReadWrite, Read")
    print("Vault credentials removed.")


def rotate_passwords(
    config: dict[str, Any],
    vault: VaultClient,
    deployment_or_database: str,
    database: str | None = None,
) -> None:
    """Rotate all three credentials through Terraform and verify convergence.

    A partial multi-provider apply gets one controlled retry with fresh Vault
    metadata so password revisions move forward instead of reusing uncertain
    credential state.
    """

    inventory = vault.load_inventory()
    deployment_key, deployment, db_name = _resolve_database_args(
        config, inventory, deployment_or_database, database
    )
    db_key, _ = normalize_database(db_name)
    if db_key not in deployment["databases"]:
        raise ControllerError(
            f"Database '{db_name}' does not exist on "
            f"{deployment_type_label(deployment)} '{deployment['display_name']}'."
        )
    db = deployment["databases"][db_key]
    require_no_active_change(config, deployment_key, deployment)
    require_running(config, deployment_key, deployment)

    log_event(
        "password.rotate.requested",
        deployment=deployment["display_name"],
        database=db["display_name"],
    )

    with protected_database_change(
        config,
        vault,
        inventory,
        deployment_key,
        deployment,
        "RotatePasswords",
        db["display_name"],
    ):
        last_error: ControllerError | None = None
        for attempt in range(2):
            if attempt:
                inventory = vault.load_inventory()
                deployment_key, deployment, _, db = require_db(
                    inventory, deployment["display_name"], db_name
                )
                require_running(config, deployment_key, deployment)
                print("Password rotation did not converge on the first attempt; retrying ...")
                log_event(
                    "password.rotate.recovery_retry",
                    deployment=deployment["display_name"],
                    database=db["display_name"],
                )

            try:
                apply_inventory(
                    config,
                    inventory,
                    _operation(
                        "rotate_passwords",
                        deployment_key,
                        deployment,
                        db["display_name"],
                    ),
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
                "the reported infrastructure error."
            ) from last_error

        updated_inventory = vault.load_inventory()
        updated_key, updated_deployment, updated_db_key, updated_db = require_db(
            updated_inventory, deployment["display_name"], db_name
        )
        require_running(config, updated_key, updated_deployment)
        _verify_database_accounts(
            config,
            updated_inventory,
            updated_key,
            updated_deployment,
            updated_db_key,
            updated_db,
        )

    log_event(
        "password.rotate.succeeded",
        deployment=updated_deployment["display_name"],
        database=updated_db["display_name"],
        rotation_version=updated_db["rotation_version"],
    )
    print(
        f"Rotated all three passwords for "
        f"'{updated_deployment['display_name']}/{updated_db['display_name']}'."
    )
    if updated_db["owner_disabled"]:
        print(
            "Owner status: Disabled in MongoDB. "
            "Its newly rotated password remains available in Vault."
        )
    else:
        print("Owner status: Enabled.")
    print(f"Last rotated: {updated_db['rotated_at']}")
    print()
    _print_vault_credentials(config, updated_deployment, updated_db)


def disable_owner(
    config: dict[str, Any],
    vault: VaultClient,
    deployment_or_database: str,
    database: str | None,
    confirmed: bool,
) -> None:
    """Disable the Owner MongoDBUser while retaining its rotating Vault secret."""

    inventory = vault.load_inventory()
    deployment_key, deployment, db_name = _resolve_database_args(
        config, inventory, deployment_or_database, database
    )
    db_key, _ = normalize_database(db_name)
    if db_key not in deployment["databases"]:
        raise ControllerError(
            f"Database '{db_name}' does not exist on "
            f"{deployment_type_label(deployment)} '{deployment['display_name']}'."
        )
    db = deployment["databases"][db_key]

    if not confirmed:
        raise ControllerError(
            "DisableOwner is destructive and requires '--confirm'. Example: "
            f"python3 privateWorkerReplacement.py DisableOwner "
            f"{deployment['display_name']} {db['display_name']} --confirm"
        )
    require_no_active_change(config, deployment_key, deployment)
    require_running(config, deployment_key, deployment)

    if db["owner_disabled"]:
        print(f"Owner account '{db['display_name']}_owner' is already Disabled.")
        return

    log_event(
        "owner.disable.requested",
        deployment=deployment["display_name"],
        database=db["display_name"],
    )

    with protected_database_change(
        config,
        vault,
        inventory,
        deployment_key,
        deployment,
        "DisableOwner",
        db["display_name"],
    ):
        apply_inventory(
            config,
            inventory,
            _operation(
                "disable_owner", deployment_key, deployment, db["display_name"]
            ),
        )

        updated_inventory = vault.load_inventory()
        updated_key, updated_deployment, updated_db_key, updated_db = require_db(
            updated_inventory, deployment["display_name"], db_name
        )
        require_running(config, updated_key, updated_deployment)
        _verify_database_accounts(
            config,
            updated_inventory,
            updated_key,
            updated_deployment,
            updated_db_key,
            updated_db,
        )

    log_event(
        "owner.disable.succeeded",
        deployment=updated_deployment["display_name"],
        database=updated_db["display_name"],
    )
    print(
        f"Owner account '{updated_db['display_name']}_owner' is now Disabled in MongoDB."
    )
    print("Its Vault credential remains present and will continue to rotate.")
    owner_path = _vault_paths(config, updated_deployment, updated_db)[0]
    print(f"Vault URL: {_vault_browser_url(config, owner_path)}")


def list_databases(
    config: dict[str, Any], vault: VaultClient, deployment_name: str | None = None
) -> None:
    """List managed databases on one deployment or across all deployments."""

    inventory = vault.load_inventory()
    rows: list[tuple[str, ...]] = []

    if deployment_name:
        _, deployment = require_deployment(inventory, deployment_name)
        deployments = [deployment]
    else:
        deployments = [inventory[key] for key in sorted(inventory)]

    for deployment in deployments:
        for db_key in sorted(deployment["databases"]):
            rows += database_rows(
                deployment,
                deployment["databases"][db_key],
                config["rotation_days"],
            )

    if not rows:
        if deployment_name:
            print(
                f"No managed databases exist on "
                f"{deployment_type_label(deployments[0])} "
                f"'{deployments[0]['display_name']}'."
            )
        else:
            print("No managed databases exist.")
        return

    print_table(
        ("DEPLOYMENT", "DATABASE", "ACCOUNT", "TYPE", "STATUS", "ROTATES IN"),
        rows,
    )


def list_database(
    config: dict[str, Any],
    vault: VaultClient,
    deployment_or_database: str,
    database: str | None = None,
) -> None:
    """Show one database, its accounts, lifecycle state, and Vault browser URLs."""

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
    print(f"Deployment type: {deployment_type_label(deployment)}")
    print(f"Created:         {db['created_at']}")
    print(f"Last rotated:    {db['rotated_at']}")
    print()
    _print_vault_credentials(config, deployment, db)
