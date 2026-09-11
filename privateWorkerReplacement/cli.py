"""End-user command-line interface for privateWorkerReplacement.

This module owns public command parsing, help text, configuration loading, and
dispatch. Business rules stay in lifecycle/status modules and all managed
mutations remain Terraform-driven.

Public-interface rules:
  1. No command prints help instead of an argparse error.
  2. Long-running deployment, topology, and database create/delete work is async.
  3. Database status and database-account details are separate commands.
  4. Terraform/Git/Kubernetes internals do not belong on the customer terminal.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .async_operations import (
    ASYNC_COMMANDS,
    launch_operation,
    mark_failed,
    mark_running,
    mark_succeeded,
    public_submission_instructions,
)
from .common import ControllerError
from .config import load_config
from .controller import (
    add_database,
    add_replica_set,
    add_shard,
    add_sharded_cluster,
    delete_database,
    delete_replica_set,
    delete_shard,
    delete_sharded_cluster,
    disable_owner,
    enable_owner,
    list_database,
    list_database_accounts,
    list_databases,
    list_deployment,
    list_deployments,
    list_replica_set,
    list_replica_sets,
    list_sharded_cluster,
    list_sharded_clusters,
    list_shards,
    rotate_passwords,
)
from .logging_component import configure_logging, log_event, log_exception
from .vault import VaultClient

# Keep these values separate on purpose. REPO_ROOT locates the controller code
# for detached workers. DEFAULT_CONFIG_DISPLAY is the friendly path a person
# sees and types, while DEFAULT_CONFIG is the Path object used by Python.
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_DISPLAY = "./dev.config"
DEFAULT_CONFIG = Path(DEFAULT_CONFIG_DISPLAY)


def _config_path_from_argv(argv: list[str]) -> Path:
    """Return the config path selected before argparse renders help."""

    for index, value in enumerate(argv):
        if value == "--config" and index + 1 < len(argv):
            return Path(argv[index + 1]).expanduser()
        if value.startswith("--config="):
            return Path(value.split("=", 1)[1]).expanduser()
    return DEFAULT_CONFIG


def _configured_default_shards(config_path: Path) -> int | None:
    """Read the configured AddShardedCluster default for help text."""

    try:
        return int(load_config(config_path)["default_shards"])
    except (ControllerError, OSError, KeyError, TypeError, ValueError):
        # Help should remain usable even when configuration is broken.
        return None


def _confirm(parser: argparse.ArgumentParser) -> None:
    """Add the shared explicit-confirmation flag used by destructive commands."""

    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Required confirmation for this destructive command.",
    )


def _deployment(parser: argparse.ArgumentParser, label: str = "DEPLOYMENT") -> None:
    """Add one required managed-deployment positional argument."""

    parser.add_argument(
        "deployment",
        metavar=label,
        help="Managed deployment name. Example: RS1 or SC9",
    )


def _database_target(parser: argparse.ArgumentParser) -> None:
    """Add DEPLOYMENT DATABASE syntax with the one-deployment shorthand."""

    parser.add_argument(
        "deployment_or_database",
        metavar="DEPLOYMENT_OR_DATABASE",
        help=(
            "With two names this is DEPLOYMENT. With one name it is DATABASE, "
            "which is allowed only when exactly one managed deployment exists."
        ),
    )
    parser.add_argument(
        "database",
        metavar="DATABASE",
        nargs="?",
        help="Database name when an explicit deployment was supplied.",
    )


def _sub(
    subparsers,
    name: str,
    help_text: str,
    description: str,
    examples: str,
) -> argparse.ArgumentParser:
    """Create one subcommand with consistent detailed help and examples."""

    return subparsers.add_parser(
        name,
        help=help_text,
        description=description,
        epilog=f"Examples:\n{examples}",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )


def build_parser(config_path: Path | None = None) -> argparse.ArgumentParser:
    """Build the complete public CLI contract."""

    help_config_path = (config_path or DEFAULT_CONFIG).expanduser()
    configured_default_shards = _configured_default_shards(help_config_path)
    configured_shards_text = (
        str(configured_default_shards)
        if configured_default_shards is not None
        else "configured value unavailable"
    )

    parser = argparse.ArgumentParser(
        prog="privateWorkerReplacement.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=f"""Terraform-driven MongoDB DBaaS controller.

A fresh installation starts with zero user-facing MongoDB deployments.
Create and name a ReplicaSet or ShardedCluster before creating databases.

Managed hierarchy:
  Deployment
    ReplicaSet
      Database
    ShardedCluster
      Shards
      Database

Each managed database gets exactly three accounts:
  <Database>_owner      -> dbOwner
  <Database>_readWrite  -> readWrite
  <Database>_read       -> read

Database inventory/status commands intentionally do not mix in account details.
Use ListDatabaseAccounts when you need roles, account status, rotation timing,
or browser-ready Vault credential URLs.

Database commands can omit the deployment only when exactly one managed
deployment exists. If multiple deployments exist, specify the deployment.

Long-running deployment, shard-topology, and database create/delete requests
run in the background. The submitting shell returns promptly with a status
command to use while the request finishes.

Configured defaults:
  Configuration file                 = ./dev.config
  AddShardedCluster initial shards   = {configured_shards_text}
    (read from controller configuration)
  AddShard count                     = 1
  DeleteShard count                  = 1

Run this program with no command, or use -h/--help, to show this help.
Use '<command> --help' for detailed command-specific help.
""",
        epilog="""Typical flows:

ReplicaSet:
  python3 privateWorkerReplacement.py AddReplicaSet RS1
  python3 privateWorkerReplacement.py AddDatabase RS1 HouseInfo
  python3 privateWorkerReplacement.py ListDatabase RS1 HouseInfo
  python3 privateWorkerReplacement.py ListDatabaseAccounts RS1 HouseInfo

ShardedCluster:
  python3 privateWorkerReplacement.py AddShardedCluster SC9
  python3 privateWorkerReplacement.py ListShards SC9
  python3 privateWorkerReplacement.py AddShard SC9 2
  python3 privateWorkerReplacement.py AddDatabase SC9 HouseInfo

Inventory:
  python3 privateWorkerReplacement.py ListDeployments
  python3 privateWorkerReplacement.py ListDatabases
""",
    )
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG_DISPLAY,
        metavar="FILE",
        help="Optional configuration file. Default: ./dev.config",
    )
    parser.add_argument(
        "--_operation-worker",
        dest="_operation_worker",
        metavar="ID",
        help=argparse.SUPPRESS,
    )
    sp = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)

    x = _sub(
        sp,
        "AddReplicaSet",
        "Create an empty managed ReplicaSet.",
        "Requests creation of a non-sharded MongoDB ReplicaSet and returns promptly while provisioning continues in the background.",
        "  python3 privateWorkerReplacement.py AddReplicaSet RS1",
    )
    _deployment(x, "REPLICASET")

    x = _sub(
        sp,
        "AddShardedCluster",
        "Create an empty managed ShardedCluster.",
        "Requests creation of a MongoDB ShardedCluster and returns promptly while provisioning continues in the background.",
        f"  python3 privateWorkerReplacement.py AddShardedCluster SC9\n  python3 privateWorkerReplacement.py AddShardedCluster SC9 --shards 5    # configured default: {configured_shards_text}",
    )
    _deployment(x, "SHARDED_CLUSTER")
    x.add_argument(
        "--shards",
        type=int,
        metavar="N",
        help=(
            f"Initial shard count. Optional. Default: {configured_shards_text} "
            "(read from controller configuration). Use --shards N to override."
        ),
    )

    x = _sub(
        sp,
        "DeleteReplicaSet",
        "Delete an empty managed ReplicaSet.",
        "Requires --confirm. The request runs in the background and is refused while managed databases remain.",
        "  python3 privateWorkerReplacement.py DeleteReplicaSet RS1 --confirm",
    )
    _deployment(x, "REPLICASET")
    _confirm(x)

    x = _sub(
        sp,
        "DeleteShardedCluster",
        "Delete an empty managed ShardedCluster.",
        "Requires --confirm. The request runs in the background and is refused while managed databases remain.",
        "  python3 privateWorkerReplacement.py DeleteShardedCluster SC9 --confirm",
    )
    _deployment(x, "SHARDED_CLUSTER")
    _confirm(x)

    _sub(
        sp,
        "ListDeployments",
        "List all managed ReplicaSets and ShardedClusters.",
        "Shows deployment name, type, live phase, topology, MongoDB version, and managed database count.",
        "  python3 privateWorkerReplacement.py ListDeployments",
    )

    x = _sub(
        sp,
        "ListDeployment",
        "Show one managed deployment.",
        "Shows detailed service status for either a ReplicaSet or ShardedCluster.",
        "  python3 privateWorkerReplacement.py ListDeployment SC9",
    )
    _deployment(x)

    _sub(
        sp,
        "ListReplicaSets",
        "List managed ReplicaSets.",
        "Lists all privateWorkerReplacement-managed ReplicaSet deployments with live phase, topology, MongoDB version, and managed database count.",
        "  python3 privateWorkerReplacement.py ListReplicaSets",
    )

    x = _sub(
        sp,
        "ListReplicaSet",
        "Show one managed ReplicaSet.",
        "Shows one ReplicaSet, live phase, members, MongoDB version, and database count.",
        "  python3 privateWorkerReplacement.py ListReplicaSet RS1",
    )
    _deployment(x, "REPLICASET")

    _sub(
        sp,
        "ListShardedClusters",
        "List managed ShardedClusters.",
        "Lists all privateWorkerReplacement-managed ShardedCluster deployments with live phase, topology, MongoDB version, and managed database count.",
        "  python3 privateWorkerReplacement.py ListShardedClusters",
    )

    x = _sub(
        sp,
        "ListShardedCluster",
        "Show one managed ShardedCluster.",
        "Shows cluster phase, topology, database count, and individual shard status.",
        "  python3 privateWorkerReplacement.py ListShardedCluster SC9",
    )
    _deployment(x, "SHARDED_CLUSTER")

    x = _sub(
        sp,
        "ListShards",
        "List shard creation/readiness status.",
        "With no cluster name, shows shards across all managed ShardedClusters. With a cluster name, shows detailed shard, config-server, mongos, and active-change status.",
        "  python3 privateWorkerReplacement.py ListShards\n  python3 privateWorkerReplacement.py ListShards SC9",
    )
    x.add_argument(
        "deployment",
        metavar="SHARDED_CLUSTER",
        nargs="?",
        help="Optional ShardedCluster name.",
    )

    x = _sub(
        sp,
        "AddShard",
        "Add one or more shards to a Running ShardedCluster.",
        "COUNT defaults to 1. The request runs in the background; use ListShards to monitor readiness.",
        "  python3 privateWorkerReplacement.py AddShard SC9\n  python3 privateWorkerReplacement.py AddShard SC9 2",
    )
    _deployment(x, "SHARDED_CLUSTER")
    x.add_argument(
        "count",
        metavar="COUNT",
        nargs="?",
        type=int,
        default=1,
        help="Number of shards to add. Optional. Default: 1.",
    )

    x = _sub(
        sp,
        "DeleteShard",
        "Remove one or more shards from a ShardedCluster.",
        "COUNT defaults to 1 and --confirm is required. The request runs in the background. At least one shard must remain.",
        "  python3 privateWorkerReplacement.py DeleteShard SC9 --confirm\n  python3 privateWorkerReplacement.py DeleteShard SC9 2 --confirm",
    )
    _deployment(x, "SHARDED_CLUSTER")
    x.add_argument(
        "count",
        metavar="COUNT",
        nargs="?",
        type=int,
        default=1,
        help="Number of shards to delete. Optional. Default: 1.",
    )
    _confirm(x)

    x = _sub(
        sp,
        "AddDatabase",
        "Create a database on a ready ReplicaSet or ShardedCluster.",
        "The request runs in the background. Terraform creates the database, its three managed accounts, and Vault credentials. Use ListDatabase to monitor database lifecycle status.",
        "  python3 privateWorkerReplacement.py AddDatabase RS1 HouseInfo\n  python3 privateWorkerReplacement.py AddDatabase SC9 HouseInfo\n  python3 privateWorkerReplacement.py AddDatabase HouseInfo    # only one deployment exists",
    )
    _database_target(x)

    x = _sub(
        sp,
        "DeleteDatabase",
        "Delete a database and its managed accounts.",
        "Requires --confirm and runs in the background. Confirmation authorizes deletion of the database and contents, managed MongoDB users, Vault credentials, and lifecycle metadata.",
        "  python3 privateWorkerReplacement.py DeleteDatabase SC9 HouseInfo --confirm\n  python3 privateWorkerReplacement.py DeleteDatabase HouseInfo --confirm",
    )
    _database_target(x)
    _confirm(x)

    x = _sub(
        sp,
        "ListDatabases",
        "List databases and their lifecycle status.",
        "Shows database inventory only: deployment, database name, and status. Account details are intentionally excluded; use ListDatabaseAccounts for those.",
        "  python3 privateWorkerReplacement.py ListDatabases\n  python3 privateWorkerReplacement.py ListDatabases SC9",
    )
    x.add_argument(
        "deployment",
        metavar="DEPLOYMENT",
        nargs="?",
        help="Optional ReplicaSet or ShardedCluster name.",
    )

    x = _sub(
        sp,
        "ListDatabase",
        "Show status for one database.",
        "Shows database-level information only: deployment, deployment type, database name, lifecycle status, and creation time.",
        "  python3 privateWorkerReplacement.py ListDatabase SC9 HouseInfo\n  python3 privateWorkerReplacement.py ListDatabase HouseInfo    # only one deployment exists",
    )
    _database_target(x)

    x = _sub(
        sp,
        "ListDatabaseAccounts",
        "Show the three managed accounts for one database.",
        "Shows Owner, ReadWrite, and Read accounts, enabled/disabled state, rotation due timing, last rotation, Vault paths, and complete browser-ready Vault URLs.",
        "  python3 privateWorkerReplacement.py ListDatabaseAccounts SC9 HouseInfo\n  python3 privateWorkerReplacement.py ListDatabaseAccounts HouseInfo    # only one deployment exists",
    )
    _database_target(x)

    x = _sub(
        sp,
        "RotatePasswords",
        "Rotate all three managed database passwords.",
        "Performs deployment health checks first, rotates Owner/ReadWrite/Read credentials through Terraform, updates Vault, and verifies MongoDB authentication.",
        "  python3 privateWorkerReplacement.py RotatePasswords SC9 HouseInfo\n  python3 privateWorkerReplacement.py RotatePasswords HouseInfo",
    )
    _database_target(x)

    x = _sub(
        sp,
        "DisableOwner",
        "Disable the database Owner account.",
        "Requires --confirm and a ready deployment. The Owner Vault credential remains managed and continues to rotate.",
        "  python3 privateWorkerReplacement.py DisableOwner SC9 HouseInfo --confirm",
    )
    _database_target(x)
    _confirm(x)

    x = _sub(
        sp,
        "EnableOwner",
        "Re-enable the database Owner account.",
        "Requires a ready deployment. Recreates the Owner MongoDB account using the existing managed credential without rotating its password.",
        "  python3 privateWorkerReplacement.py EnableOwner SC9 HouseInfo",
    )
    _database_target(x)

    return parser


def _database_values(args: argparse.Namespace) -> tuple[str, str]:
    """Return (explicit deployment or blank, database) from parsed DB arguments."""

    if args.database is None:
        return "", str(args.deployment_or_database)
    return str(args.deployment_or_database), str(args.database)


def _async_worker_arguments(args: argparse.Namespace) -> list[str]:
    """Rebuild one async command for the detached worker."""

    command = args.command
    if command == "AddReplicaSet":
        return [command, args.deployment]
    if command == "AddShardedCluster":
        values = [command, args.deployment]
        if args.shards is not None:
            values.extend(["--shards", str(args.shards)])
        return values
    if command == "DeleteReplicaSet":
        values = [command, args.deployment]
        if args.confirm:
            values.append("--confirm")
        return values
    if command == "DeleteShardedCluster":
        values = [command, args.deployment]
        if args.confirm:
            values.append("--confirm")
        return values
    if command == "AddShard":
        return [command, args.deployment, str(args.count)]
    if command == "DeleteShard":
        values = [command, args.deployment, str(args.count)]
        if args.confirm:
            values.append("--confirm")
        return values
    if command in {"AddDatabase", "DeleteDatabase"}:
        deployment, database = _database_values(args)
        values = [command]
        if deployment:
            values.append(deployment)
        values.append(database)
        if command == "DeleteDatabase" and args.confirm:
            values.append("--confirm")
        return values
    raise ControllerError(
        f"Command '{command}' is not configured for asynchronous execution."
    )


def _validate_async_submission(args: argparse.Namespace) -> None:
    """Reject obvious invalid async requests before assigning an operation ID."""

    if args.command in {
        "DeleteReplicaSet",
        "DeleteShardedCluster",
        "DeleteShard",
        "DeleteDatabase",
    } and not getattr(args, "confirm", False):
        raise ControllerError(f"{args.command} is destructive and requires '--confirm'.")

    if args.command in {"AddShard", "DeleteShard"} and int(args.count) < 1:
        raise ControllerError("Shard COUNT must be at least 1.")
    if args.command == "AddShardedCluster" and args.shards is not None and int(args.shards) < 1:
        raise ControllerError("--shards must be at least 1.")


def _async_deployment(args: argparse.Namespace) -> str:
    """Return the explicit deployment associated with an async command."""

    if args.command in {"AddDatabase", "DeleteDatabase"}:
        deployment, _ = _database_values(args)
        return deployment
    return str(getattr(args, "deployment", ""))


def _public_async_feedback(
    args: argparse.Namespace,
) -> tuple[list[tuple[str, str]], str, list[str]]:
    """Translate internal async execution into customer-facing service status."""

    command = args.command
    deployment = str(getattr(args, "deployment", ""))

    if command == "AddReplicaSet":
        return [("ReplicaSet", deployment)], "Creation requested", ["ListReplicaSet", deployment]
    if command == "DeleteReplicaSet":
        return [("ReplicaSet", deployment)], "Deletion requested", ["ListReplicaSets"]
    if command == "AddShardedCluster":
        return [("ShardedCluster", deployment)], "Creation requested", ["ListShardedCluster", deployment]
    if command == "DeleteShardedCluster":
        return [("ShardedCluster", deployment)], "Deletion requested", ["ListShardedClusters"]
    if command in {"AddShard", "DeleteShard"}:
        return [("ShardedCluster", deployment)], "Topology change requested", ["ListShards", deployment]
    if command in {"AddDatabase", "DeleteDatabase"}:
        db_deployment, database = _database_values(args)
        details: list[tuple[str, str]] = []
        if db_deployment:
            details.append(("Deployment", db_deployment))
        details.append(("Database", database))
        if command == "AddDatabase":
            status_args = (
                ["ListDatabase", db_deployment, database]
                if db_deployment
                else ["ListDatabase", database]
            )
            return details, "Creation requested", status_args
        status_args = ["ListDatabases", db_deployment] if db_deployment else ["ListDatabases"]
        return details, "Deletion requested", status_args

    raise ControllerError(f"Command '{command}' is not configured for public async feedback.")


def main(argv: list[str] | None = None) -> int:
    """Parse and execute one customer command; no command prints full help."""

    raw_argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser(_config_path_from_argv(raw_argv))
    if not raw_argv:
        parser.print_help()
        return 0

    args = parser.parse_args(raw_argv)
    logging_ready = False
    # Preserve the friendly path exactly as argparse received it for follow-up
    # instructions, then resolve a separate absolute path for actual file I/O.
    config_display = str(args.config)
    config_path = Path(args.config).expanduser().resolve()
    operation_id = getattr(args, "_operation_worker", None)

    try:
        config = load_config(config_path)
        configure_logging(config)
        logging_ready = True
        log_event("command.started", command=args.command)

        if args.command in ASYNC_COMMANDS and not operation_id:
            _validate_async_submission(args)
            state = launch_operation(
                config_path,
                REPO_ROOT,
                command=args.command,
                deployment=_async_deployment(args),
                worker_arguments=_async_worker_arguments(args),
            )
            details, status_text, status_arguments = _public_async_feedback(args)
            print(
                public_submission_instructions(
                    config_path,
                    state,
                    details=details,
                    status_text=status_text,
                    status_arguments=status_arguments,
                    config_display=config_display,
                )
            )
            log_event(
                "command.accepted",
                command=args.command,
                operation_id=state["operation_id"],
                deployment=state.get("deployment", ""),
            )
            return 0

        if operation_id:
            mark_running(config_path, operation_id)

        vault = VaultClient(config)
        actions = {
            "AddReplicaSet": lambda: add_replica_set(config, vault, args.deployment),
            "AddShardedCluster": lambda: add_sharded_cluster(config, vault, args.deployment, args.shards),
            "DeleteReplicaSet": lambda: delete_replica_set(config, vault, args.deployment, args.confirm),
            "DeleteShardedCluster": lambda: delete_sharded_cluster(config, vault, args.deployment, args.confirm),
            "ListDeployments": lambda: list_deployments(config, vault),
            "ListDeployment": lambda: list_deployment(config, vault, args.deployment),
            "ListReplicaSets": lambda: list_replica_sets(config, vault),
            "ListReplicaSet": lambda: list_replica_set(config, vault, args.deployment),
            "ListShardedClusters": lambda: list_sharded_clusters(config, vault),
            "ListShardedCluster": lambda: list_sharded_cluster(config, vault, args.deployment),
            "ListShards": lambda: list_shards(config, vault, args.deployment),
            "AddShard": lambda: add_shard(config, vault, args.deployment, args.count),
            "DeleteShard": lambda: delete_shard(config, vault, args.deployment, args.count, args.confirm),
            "AddDatabase": lambda: add_database(config, vault, args.deployment_or_database, args.database),
            "DeleteDatabase": lambda: delete_database(config, vault, args.deployment_or_database, args.database, args.confirm),
            "ListDatabases": lambda: list_databases(config, vault, args.deployment),
            "ListDatabase": lambda: list_database(config, vault, args.deployment_or_database, args.database),
            "ListDatabaseAccounts": lambda: list_database_accounts(config, vault, args.deployment_or_database, args.database),
            "RotatePasswords": lambda: rotate_passwords(config, vault, args.deployment_or_database, args.database),
            "DisableOwner": lambda: disable_owner(config, vault, args.deployment_or_database, args.database, args.confirm),
            "EnableOwner": lambda: enable_owner(config, vault, args.deployment_or_database, args.database),
        }
        actions[args.command]()

        if operation_id:
            mark_succeeded(config_path, operation_id)
        log_event("command.succeeded", command=args.command)
        return 0

    except (ControllerError, json.JSONDecodeError) as exc:
        if operation_id:
            try:
                mark_failed(config_path, operation_id, str(exc))
            except Exception:
                pass
        if logging_ready:
            log_event(
                "command.failed",
                level=40,
                command=getattr(args, "command", ""),
                error=str(exc),
            )
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    except Exception as exc:
        if operation_id:
            try:
                mark_failed(config_path, operation_id, f"Unexpected failure: {exc}")
            except Exception:
                pass
        if logging_ready:
            log_exception(
                "command.unhandled_exception",
                command=getattr(args, "command", ""),
                error=str(exc),
            )
        print(f"ERROR: Unexpected failure: {exc}", file=sys.stderr)
        return 1
