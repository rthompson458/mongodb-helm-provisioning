#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from terraform_controller.common import ControllerError
from terraform_controller.config import load_config
from terraform_controller.controller import (
    add_database, add_replica_set, delete_database, delete_replica_set,
    disable_owner, list_database, list_databases, list_replica_set,
    list_replica_sets, recover_password, reset_vault, rotate_password,
)
from terraform_controller.vault import VaultClient

DEFAULT_CONFIG = Path(__file__).resolve().parent / "terraformController.config"


def rs_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument("replica_set", metavar="REPLICASET", help="ReplicaSet name. Example: RS1")


def db_args(p: argparse.ArgumentParser) -> None:
    rs_arg(p)
    p.add_argument("database", metavar="DATABASE", help="Database name. Example: Tank")


def sub(sp, name: str, help_text: str, description: str, example: str):
    return sp.add_parser(
        name, help=help_text, description=description,
        epilog=f"Example:\n  {example}", formatter_class=argparse.RawDescriptionHelpFormatter,
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="terraformController.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="""Manage MongoDB ReplicaSets, databases, and Vault-backed role accounts.

Hierarchy:
  ReplicaSet
    Database
      <Database>_owner
      <Database>_readWrite
      <Database>_read

ReplicaSet/database lookup and RecoverPassword account types are case-insensitive.
Command names use the exact CamelCase spelling shown below.
""",
        epilog="""Common flow:
  terraformController.py AddReplicaSet RS1
  terraformController.py AddDatabase RS1 Tank
  terraformController.py ListDatabase RS1 Tank
  terraformController.py RotatePassword RS1 Tank
  terraformController.py DisableOwner RS1 Tank
  terraformController.py DeleteDatabase RS1 Tank
  terraformController.py DeleteReplicaSet RS1

Use '<command> --help' for command-specific help.
""",
    )
    p.add_argument("--config", default=str(DEFAULT_CONFIG), metavar="FILE", help="Configuration file")
    sp = p.add_subparsers(dest="command", metavar="COMMAND", required=True)

    x = sub(sp, "AddReplicaSet", "Create an empty managed ReplicaSet.",
            "Create a MongoDB ReplicaSet plus one hidden controller admin. No application database is created.",
            "terraformController.py AddReplicaSet RS1")
    rs_arg(x)

    x = sub(sp, "DeleteReplicaSet", "Delete a ReplicaSet only when empty.",
            "Delete is blocked if Vault inventory or MongoDB runtime reports any non-system database. There is no force option.",
            "terraformController.py DeleteReplicaSet RS1")
    rs_arg(x)

    sub(sp, "ListReplicaSets", "List managed ReplicaSets.",
        "List managed ReplicaSets, Kubernetes phase, members, and database count.",
        "terraformController.py ListReplicaSets")

    x = sub(sp, "ListReplicaSet", "Show one ReplicaSet and its databases.",
            "Show one ReplicaSet plus database account status and rotation countdown.",
            "terraformController.py ListReplicaSet RS1")
    rs_arg(x)

    x = sub(sp, "AddDatabase", "Create a database and three role accounts.",
            "Creates the logical database plus Owner(dbOwner), ReadWrite(readWrite), and Read(read) accounts. An empty internal collection materializes the database.",
            "terraformController.py AddDatabase RS1 Tank")
    db_args(x)

    x = sub(sp, "DeleteDatabase", "Delete an empty database and its accounts.",
            "Blocked if application collections remain. When allowed, removes MongoDB data, the three role accounts, Kubernetes password Secrets, and Vault credentials.",
            "terraformController.py DeleteDatabase RS1 Tank")
    db_args(x)

    x = sub(sp, "RotatePassword", "Rotate all three database passwords.",
            "Rotates Owner, ReadWrite, and Read together and restarts the configured rotation countdown. A disabled Owner remains disabled.",
            "terraformController.py RotatePassword RS1 Tank")
    db_args(x)

    x = sub(sp, "DisableOwner", "Disable the Owner account.",
            "Removes the Owner MongoDBUser while retaining its Vault credential and password Secret. Later rotations still rotate its stored password.",
            "terraformController.py DisableOwner RS1 Tank")
    db_args(x)

    sub(sp, "ListDatabases", "List all databases and role accounts.",
        "Lists every managed database across ReplicaSets, account status, and time until rotation is due.",
        "terraformController.py ListDatabases")

    x = sub(sp, "ListDatabase", "List one database and its role accounts.",
            "Shows the selected database's three accounts, status, and rotation countdown.",
            "terraformController.py ListDatabase RS1 Tank")
    db_args(x)

    x = sub(sp, "RecoverPassword", "Display one Vault password for demo use.",
            "Displays one password from Vault. ACCOUNT_TYPE is Owner, Read, or ReadWrite and is case-insensitive. Demo only.",
            "terraformController.py RecoverPassword RS1 Tank ReadWrite")
    db_args(x)
    x.add_argument("account_type", metavar="ACCOUNT_TYPE", help="Owner, Read, or ReadWrite")

    x = sub(sp, "ResetVault", "Delete all controller-managed ReplicaSets/databases.",
            "Destructive demo reset. Drops all non-system databases on controller-managed ReplicaSets and removes their accounts, Vault records, and ReplicaSet resources. Unmanaged ReplicaSets are untouched.",
            "terraformController.py ResetVault --confirm")
    x.add_argument("--confirm", action="store_true", help="Required destructive confirmation")
    return p


def main() -> int:
    args = build_parser().parse_args()
    try:
        config = load_config(Path(args.config).expanduser())
        vault = VaultClient(config)
        actions = {
            "AddReplicaSet": lambda: add_replica_set(config, vault, args.replica_set),
            "DeleteReplicaSet": lambda: delete_replica_set(config, vault, args.replica_set),
            "ListReplicaSets": lambda: list_replica_sets(config, vault),
            "ListReplicaSet": lambda: list_replica_set(config, vault, args.replica_set),
            "AddDatabase": lambda: add_database(config, vault, args.replica_set, args.database),
            "DeleteDatabase": lambda: delete_database(config, vault, args.replica_set, args.database),
            "RotatePassword": lambda: rotate_password(config, vault, args.replica_set, args.database),
            "DisableOwner": lambda: disable_owner(config, vault, args.replica_set, args.database),
            "ListDatabases": lambda: list_databases(config, vault),
            "ListDatabase": lambda: list_database(config, vault, args.replica_set, args.database),
            "RecoverPassword": lambda: recover_password(config, vault, args.replica_set, args.database, args.account_type),
            "ResetVault": lambda: reset_vault(config, vault, args.confirm),
        }
        actions[args.command]()
        return 0
    except (ControllerError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
