#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from terraform_controller.common import ControllerError
from terraform_controller.config import load_config
from terraform_controller.controller import (
    add_database,
    add_replica_set,
    delete_database,
    delete_replica_set,
    disable_owner,
    list_database,
    list_databases,
    list_replica_set,
    list_replica_sets,
    reconcile,
    rotate_passwords,
)
from terraform_controller.vault import VaultClient

DEFAULT_CONFIG = Path(__file__).resolve().parent / "terraformController.config"


def rs_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument("replica_set", metavar="REPLICASET", help="ReplicaSet name. Example: RS1")


def db_args(p: argparse.ArgumentParser) -> None:
    rs_arg(p)
    p.add_argument("database", metavar="DATABASE", help="Database name. Example: HouseInfo")


def add_confirm(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--confirm",
        action="store_true",
        help="Required confirmation for this destructive command.",
    )


def sub(sp, name: str, help_text: str, description: str, example: str):
    return sp.add_parser(
        name,
        help=help_text,
        description=description,
        epilog=f"Example:\n  {example}",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="terraformController.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="""Terraform-driven MongoDB DBaaS controller.

terraformController.py is the temporary local orchestration layer that replaces
what a Spacelift private worker will do later. All MongoDB, Vault, and managed
Kubernetes changes are initiated by Terraform in the GitHub repository.

Managed hierarchy:
  ReplicaSet
    Database
      <Database>_owner      -> dbOwner
      <Database>_readWrite  -> readWrite
      <Database>_read       -> read

Rules:
  * A database must be created on an existing ReplicaSet that is Running.
  * A ReplicaSet can contain multiple databases.
  * Only controller-managed application ReplicaSets are listed.
  * admin, config, and local are MongoDB system databases and are never treated
    as application databases.
  * Database passwords are rotated together every configured rotation period.
  * The owner password is also rotated. At or after the first 30-day rotation,
    the owner MongoDB account is disabled. Its current password remains in Vault.
  * Re-enabling the owner is controlled by Vault lifecycle metadata and becomes
    effective when Terraform is reconciled.

Use '<command> --help' for detailed command help.
""",
        epilog="""Typical flow:
  terraformController.py AddReplicaSet RS1
  terraformController.py ListReplicaSets
  terraformController.py AddDatabase RS1 HouseInfo
  terraformController.py ListDatabases RS1
  terraformController.py RotatePasswords RS1 HouseInfo
  terraformController.py DeleteDatabase RS1 HouseInfo --confirm
  terraformController.py DeleteReplicaSet RS1 --confirm

Maintenance/demo:
  terraformController.py DisableOwner RS1 HouseInfo --confirm
  terraformController.py Reconcile
""",
    )
    p.add_argument("--config", default=str(DEFAULT_CONFIG), metavar="FILE", help="Configuration file")
    sp = p.add_subparsers(dest="command", metavar="COMMAND", required=True)

    x = sub(
        sp,
        "AddReplicaSet",
        "Create an empty managed application ReplicaSet.",
        "Creates a MongoDB ReplicaSet through Terraform. No application database is created. The command waits until the ReplicaSet is Running before reporting success.",
        "terraformController.py AddReplicaSet RS1",
    )
    rs_arg(x)

    x = sub(
        sp,
        "DeleteReplicaSet",
        "Delete an empty application ReplicaSet.",
        "Requires --confirm. Terraform deletion is blocked when the ReplicaSet contains any application database. MongoDB system databases admin, config, and local do not block deletion.",
        "terraformController.py DeleteReplicaSet RS1 --confirm",
    )
    rs_arg(x)
    add_confirm(x)

    sub(
        sp,
        "ListReplicaSets",
        "List managed application ReplicaSets and status.",
        "Lists only ReplicaSets managed by terraformController. Shows the Kubernetes/MongoDB phase, member count, MongoDB version, and managed database count.",
        "terraformController.py ListReplicaSets",
    )

    x = sub(
        sp,
        "ListReplicaSet",
        "Show one managed ReplicaSet.",
        "Shows one managed ReplicaSet, current phase, member count, MongoDB version, database count, and database account lifecycle state.",
        "terraformController.py ListReplicaSet RS1",
    )
    rs_arg(x)

    x = sub(
        sp,
        "AddDatabase",
        "Create a database on a Running ReplicaSet.",
        "Validates that the ReplicaSet exists and is Running. Terraform creates the database and exactly three managed role accounts: <DB>_owner, <DB>_readWrite, and <DB>_read. The result includes the Vault UI URL and Vault credential paths.",
        "terraformController.py AddDatabase RS1 HouseInfo",
    )
    db_args(x)

    x = sub(
        sp,
        "DeleteDatabase",
        "Delete an application database and its three managed accounts.",
        "Requires --confirm. Confirmation authorizes deletion of the database and its contents. Terraform drops the database, removes the three MongoDB users, removes their Kubernetes password resources, and removes their Vault credentials.",
        "terraformController.py DeleteDatabase RS1 HouseInfo --confirm",
    )
    db_args(x)
    add_confirm(x)

    x = sub(
        sp,
        "ListDatabases",
        "List databases on one ReplicaSet or on all managed ReplicaSets.",
        "With REPLICASET, lists databases on that ReplicaSet. With no REPLICASET, lists all managed databases on all managed application ReplicaSets.",
        "terraformController.py ListDatabases RS1",
    )
    x.add_argument(
        "replica_set",
        metavar="REPLICASET",
        nargs="?",
        help="Optional ReplicaSet name. Omit it to list databases on all managed ReplicaSets.",
    )

    x = sub(
        sp,
        "ListDatabase",
        "Show one database and its managed accounts.",
        "Shows one database, its three fixed role accounts, enabled/disabled state, last rotation time, and rotation countdown.",
        "terraformController.py ListDatabase RS1 HouseInfo",
    )
    db_args(x)

    x = sub(
        sp,
        "RotatePasswords",
        "Rotate all three managed database passwords.",
        "Terraform generates and installs new passwords for Owner, ReadWrite, and Read, writes the current passwords to Vault, and resets the rotation countdown. If the database is at least 30 days old, the owner MongoDB account is disabled after the rotation and remains disabled until an administrator re-enables it through the Vault lifecycle state and Terraform is reconciled.",
        "terraformController.py RotatePasswords RS1 HouseInfo",
    )
    db_args(x)

    x = sub(
        sp,
        "DisableOwner",
        "Disable the database owner account for lifecycle testing or administration.",
        "Requires --confirm. Terraform removes the Owner MongoDBUser so it cannot authenticate. The current owner password remains in Vault and continues to rotate with RotatePasswords.",
        "terraformController.py DisableOwner RS1 HouseInfo --confirm",
    )
    db_args(x)
    add_confirm(x)

    sub(
        sp,
        "Reconcile",
        "Reapply Vault-backed desired state through Terraform.",
        "Refreshes Terraform from GitHub and reapplies the complete desired inventory reconstructed from Vault. Use this after an administrator changes supported lifecycle metadata in Vault, including re-enabling an owner account.",
        "terraformController.py Reconcile",
    )
    return p


def main() -> int:
    args = build_parser().parse_args()
    try:
        config = load_config(Path(args.config).expanduser())
        vault = VaultClient(config)
        actions = {
            "AddReplicaSet": lambda: add_replica_set(config, vault, args.replica_set),
            "DeleteReplicaSet": lambda: delete_replica_set(config, vault, args.replica_set, args.confirm),
            "ListReplicaSets": lambda: list_replica_sets(config, vault),
            "ListReplicaSet": lambda: list_replica_set(config, vault, args.replica_set),
            "AddDatabase": lambda: add_database(config, vault, args.replica_set, args.database),
            "DeleteDatabase": lambda: delete_database(config, vault, args.replica_set, args.database, args.confirm),
            "ListDatabases": lambda: list_databases(config, vault, args.replica_set),
            "ListDatabase": lambda: list_database(config, vault, args.replica_set, args.database),
            "RotatePasswords": lambda: rotate_passwords(config, vault, args.replica_set, args.database),
            "DisableOwner": lambda: disable_owner(config, vault, args.replica_set, args.database, args.confirm),
            "Reconcile": lambda: reconcile(config, vault),
        }
        actions[args.command]()
        return 0
    except (ControllerError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
