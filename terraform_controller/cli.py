"""End-user command-line interface for terraformController.

This module owns command parsing, help text, configuration loading, logging
startup, and dispatch to lifecycle functions.  It should remain thin: business
rules belong in deployments.py/databases.py and real mutations belong to
Terraform.

When adding a new command:
  1. Add clear argparse help and at least one example.
  2. Dispatch to a focused lifecycle function.
  3. Keep success/error formatting understandable without Kubernetes knowledge.
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
    print_operation,
    print_operations,
    submission_instructions,
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
    list_database,
    list_databases,
    list_deployment,
    list_deployments,
    list_replica_set,
    list_replica_sets,
    list_sharded_cluster,
    list_sharded_clusters,
    list_shards,
    reconcile,
    rotate_passwords,
)
from .logging_component import configure_logging, log_event, log_exception
from .vault import VaultClient

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "terraformController.config"


def _confirm(parser: argparse.ArgumentParser) -> None:
    """Add the standard --confirm guard used by destructive commands."""
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Required confirmation for this destructive command.",
    )


def _deployment(parser: argparse.ArgumentParser, label: str = "DEPLOYMENT") -> None:
    """Add a required deployment-name positional argument."""
    parser.add_argument(
        "deployment",
        metavar=label,
        help="Managed deployment name. Example: RS1 or SC9",
    )


def _database_target(parser: argparse.ArgumentParser) -> None:
    """Add the one-or-two argument database target syntax.

    With one argument, it is treated as DATABASE and is legal only when exactly
    one managed deployment exists.  With two arguments, they are
    DEPLOYMENT DATABASE.
    """
    parser.add_argument(
        "deployment_or_database",
        metavar="DEPLOYMENT_OR_DATABASE",
        help=(
            "If two names are supplied, this is the deployment. If only one name is "
            "supplied, it is the database and the only managed deployment is selected."
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
    """Create one subcommand parser with consistent examples/help formatting."""

    return subparsers.add_parser(
        name,
        help=help_text,
        description=description,
        epilog=f"Examples:\n{examples}",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the complete public CLI contract.

    Keeping command definitions together makes it easy to audit exactly what an
    end user can do and which destructive commands require --confirm.
    """
    parser = argparse.ArgumentParser(
        prog="terraformController.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="""Terraform-driven MongoDB DBaaS controller.

A fresh installation starts with zero user-facing MongoDB deployments.
Create and name a ReplicaSet or ShardedCluster before creating databases.

Managed hierarchy:
  Deployment
    ReplicaSet
      Database
    ShardedCluster
      Shards
      Database

Each managed database gets exactly:
  <Database>_owner      -> dbOwner
  <Database>_readWrite  -> readWrite
  <Database>_read       -> read

Database commands can omit the deployment only when exactly one managed
deployment exists. If multiple deployments exist, the deployment is required.

ShardedCluster database work is blocked until the MongoDB resource is Running,
all expected shards are Online, config servers are Online, mongos is Online,
and no other managed change holds the ShardedCluster deployment lock.

Use '<command> --help' for detailed help.
""",
        epilog="""Typical flows:

ReplicaSet:
  terraformController.py AddReplicaSet RS1
  terraformController.py AddDatabase RS1 HouseInfo

ShardedCluster:
  terraformController.py AddShardedCluster SC9 --shards 3
  terraformController.py ListShards SC9
  terraformController.py AddShard SC9 2
  terraformController.py AddDatabase SC9 HouseInfo

Only one deployment exists:
  terraformController.py AddDatabase HouseInfo
  terraformController.py RotatePasswords HouseInfo

Inventory:
  terraformController.py ListDeployments
  terraformController.py ListDatabases
""",
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG),
        metavar="FILE",
        help="Configuration file",
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
        "Submits a detached local operation that creates a non-sharded MongoDB ReplicaSet through Terraform. The command returns promptly with an Operation ID and an exact ListOperation command for checking the eventual positive/negative result.",
        "  terraformController.py AddReplicaSet RS1",
    )
    _deployment(x, "REPLICASET")

    x = _sub(
        sp,
        "AddShardedCluster",
        "Create an empty managed ShardedCluster.",
        "Submits a detached local operation that creates a MongoDB ShardedCluster through Terraform. The command returns promptly with an Operation ID; use ListOperation to check the eventual positive/negative result.",
        "  terraformController.py AddShardedCluster SC9\n  terraformController.py AddShardedCluster SC9 --shards 3",
    )
    _deployment(x, "SHARDED_CLUSTER")
    x.add_argument(
        "--shards",
        type=int,
        metavar="N",
        help="Initial shard count. Omit to use [Sharding] default_shards.",
    )

    x = _sub(
        sp,
        "DeleteReplicaSet",
        "Delete an empty managed ReplicaSet.",
        "Requires --confirm. Submits a detached local deletion operation. Terraform performs the normal Running/database safety checks and live emptiness validation; use ListOperation to check the eventual result.",
        "  terraformController.py DeleteReplicaSet RS1 --confirm",
    )
    _deployment(x, "REPLICASET")
    _confirm(x)

    x = _sub(
        sp,
        "DeleteShardedCluster",
        "Delete an empty managed ShardedCluster.",
        "Requires --confirm. Submits a detached local deletion operation. The normal readiness, managed-database, lock, and live emptiness checks still apply; use ListOperation to check the eventual result.",
        "  terraformController.py DeleteShardedCluster SC9 --confirm",
    )
    _deployment(x, "SHARDED_CLUSTER")
    _confirm(x)

    _sub(
        sp,
        "ListDeployments",
        "List all managed ReplicaSets and ShardedClusters.",
        "Shows deployment name, type, live phase, topology, MongoDB version, and managed database count.",
        "  terraformController.py ListDeployments",
    )

    x = _sub(
        sp,
        "ListDeployment",
        "Show one managed deployment.",
        "Shows detailed status for either a ReplicaSet or ShardedCluster. ShardedCluster output includes individual shard status.",
        "  terraformController.py ListDeployment SC9",
    )
    _deployment(x)

    _sub(
        sp,
        "ListReplicaSets",
        "List managed ReplicaSets.",
        "Lists only terraformController-managed standalone ReplicaSet deployments.",
        "  terraformController.py ListReplicaSets",
    )

    x = _sub(
        sp,
        "ListReplicaSet",
        "Show one managed ReplicaSet.",
        "Shows one ReplicaSet, live phase, members, version, and database count.",
        "  terraformController.py ListReplicaSet RS1",
    )
    _deployment(x, "REPLICASET")

    _sub(
        sp,
        "ListShardedClusters",
        "List managed ShardedClusters.",
        "Lists only terraformController-managed ShardedCluster deployments.",
        "  terraformController.py ListShardedClusters",
    )

    x = _sub(
        sp,
        "ListShardedCluster",
        "Show one managed ShardedCluster.",
        "Shows cluster phase, shard count, members per shard, mongos, config servers, database count, and individual shard status.",
        "  terraformController.py ListShardedCluster SC9",
    )
    _deployment(x, "SHARDED_CLUSTER")

    x = _sub(
        sp,
        "ListShards",
        "List shard creation/readiness status.",
        "With no cluster name, shows shards across all managed ShardedClusters. With a cluster name, shows detailed shard, config-server, mongos, and active-change status for that ShardedCluster.",
        "  terraformController.py ListShards\n  terraformController.py ListShards SC9",
    )
    x.add_argument(
        "deployment",
        metavar="SHARDED_CLUSTER",
        nargs="?",
        help="Optional ShardedCluster name. Omit to list shards across all clusters.",
    )

    x = _sub(
        sp,
        "AddShard",
        "Add one or more shards to a Running ShardedCluster.",
        "COUNT defaults to 1. Submits a detached local operation. The worker acquires the Terraform-managed ShardedCluster lock, prepares storage, changes shardCount, waits for the requested total to become online, and releases the lock. The CLI immediately returns an Operation ID plus ListOperation/ListShards check commands.",
        "  terraformController.py AddShard SC9\n  terraformController.py AddShard SC9 2",
    )
    _deployment(x, "SHARDED_CLUSTER")
    x.add_argument(
        "count",
        metavar="COUNT",
        nargs="?",
        type=int,
        default=1,
        help="Number of shards to add. Default: 1.",
    )

    x = _sub(
        sp,
        "DeleteShard",
        "Remove one or more shards from a ShardedCluster.",
        "COUNT defaults to 1 and --confirm is required. Submits a detached local operation and immediately returns an Operation ID plus ListOperation/ListShards check commands. The one-shard minimum, database preservation, Terraform-driven scale-down, readiness wait, storage cleanup, highest-numbered-first behavior, and resume safety remain unchanged.",
        "  terraformController.py DeleteShard SC9 --confirm\n  terraformController.py DeleteShard SC9 2 --confirm",
    )
    _deployment(x, "SHARDED_CLUSTER")
    x.add_argument(
        "count",
        metavar="COUNT",
        nargs="?",
        type=int,
        default=1,
        help="Number of shards to delete. Default: 1.",
    )
    _confirm(x)

    x = _sub(
        sp,
        "AddDatabase",
        "Create a database on a ready ReplicaSet or ShardedCluster.",
        "Validates the target deployment before any change. ReplicaSets must be Running. ShardedClusters must be Running with every shard, config server, and mongos component online. Terraform then creates the database and its three managed accounts and Vault credentials.",
        "  terraformController.py AddDatabase RS1 HouseInfo\n  terraformController.py AddDatabase SC9 HouseInfo\n  terraformController.py AddDatabase HouseInfo    # only one deployment exists",
    )
    _database_target(x)

    x = _sub(
        sp,
        "DeleteDatabase",
        "Delete a database and its three managed accounts.",
        "Requires --confirm. The target deployment must be fully ready. Confirmation authorizes deletion of the database and its contents, MongoDB users, Kubernetes password resources, Vault credentials, and lifecycle metadata.",
        "  terraformController.py DeleteDatabase SC9 HouseInfo --confirm\n  terraformController.py DeleteDatabase HouseInfo --confirm    # only one deployment exists",
    )
    _database_target(x)
    _confirm(x)

    x = _sub(
        sp,
        "ListDatabases",
        "List databases on one deployment or all deployments.",
        "With DEPLOYMENT, lists databases on that deployment. With no DEPLOYMENT, lists all managed databases across ReplicaSets and ShardedClusters.",
        "  terraformController.py ListDatabases\n  terraformController.py ListDatabases SC9",
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
        "Show one database and its managed accounts.",
        "Shows deployment, deployment type, database accounts, enabled/disabled state, last rotation, rotation countdown, and Vault URL.",
        "  terraformController.py ListDatabase SC9 HouseInfo\n  terraformController.py ListDatabase HouseInfo    # only one deployment exists",
    )
    _database_target(x)

    x = _sub(
        sp,
        "RotatePasswords",
        "Rotate all three managed database passwords.",
        "Performs deployment health checks first. Terraform rotates Owner, ReadWrite, and Read credentials, writes current credentials to Vault, verifies MongoDB authentication, and applies the configured Owner-disable lifecycle rule.",
        "  terraformController.py RotatePasswords SC9 HouseInfo\n  terraformController.py RotatePasswords HouseInfo    # only one deployment exists",
    )
    _database_target(x)

    x = _sub(
        sp,
        "DisableOwner",
        "Disable the database Owner account.",
        "Requires --confirm and a fully ready deployment. Terraform removes the Owner MongoDBUser while retaining and continuing to rotate its Vault credential.",
        "  terraformController.py DisableOwner SC9 HouseInfo --confirm",
    )
    _database_target(x)
    _confirm(x)

    x = _sub(
        sp,
        "ListOperation",
        "Show one asynchronous operation result.",
        "Read-only status for one Operation ID. Reports In Progress, Succeeded, Failed, or Interrupted plus timestamps, elapsed time, worker PID, log path, and failure message.",
        "  terraformController.py ListOperation 7c1349abc123",
    )
    x.add_argument(
        "operation_id",
        metavar="OPERATION_ID",
        help="Operation ID returned by an asynchronous lifecycle command.",
    )

    _sub(
        sp,
        "ListOperations",
        "List recent asynchronous operations.",
        "Read-only summary of up to 50 recent asynchronous deployment/topology operations, including their positive/negative result and elapsed time.",
        "  terraformController.py ListOperations",
    )

    _sub(
        sp,
        "Reconcile",
        "Reapply complete Vault-backed desired state through Terraform.",
        "Refreshes Terraform from GitHub, reapplies all managed ReplicaSets, ShardedClusters, databases, accounts, and lifecycle state, then waits for convergence.",
        "  terraformController.py Reconcile",
    )

    return parser


def _async_worker_arguments(args: argparse.Namespace) -> list[str]:
    """Rebuild one async lifecycle command for the detached worker.

    Reconstructing from parsed values avoids depending on the caller's current
    directory or on the original placement of global argparse options.
    """

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
    raise ControllerError(f"Command '{command}' is not configured for asynchronous execution.")



def _validate_async_submission(args: argparse.Namespace) -> None:
    """Reject obvious invalid async requests before assigning an Operation ID."""

    if args.command in {"DeleteReplicaSet", "DeleteShardedCluster", "DeleteShard"}:
        if not getattr(args, "confirm", False):
            raise ControllerError(
                f"{args.command} is destructive and requires '--confirm'."
            )

    if args.command in {"AddShard", "DeleteShard"} and int(args.count) < 1:
        raise ControllerError("Shard COUNT must be at least 1.")

    if (
        args.command == "AddShardedCluster"
        and args.shards is not None
        and int(args.shards) < 1
    ):
        raise ControllerError("--shards must be at least 1.")


def _async_deployment(args: argparse.Namespace) -> str:
    """Return the deployment name associated with an asynchronous command."""

    return str(getattr(args, "deployment", ""))



def main(argv: list[str] | None = None) -> int:
    """Parse one command, initialize shared services, and execute it.

    Long-running deployment/topology commands are asynchronous from the user's
    perspective. A detached worker executes the same Terraform-driven lifecycle
    implementation while ListOperation/ListOperations remain read-only.
    """

    args = build_parser().parse_args(argv)
    logging_ready = False
    config_path = Path(args.config).expanduser().resolve()
    operation_id = getattr(args, "_operation_worker", None)

    try:
        config = load_config(config_path)
        configure_logging(config)
        logging_ready = True
        log_event("command.started", command=args.command)

        # Operation status must remain available even when Vault or MongoDB is
        # unhealthy, so these read-only commands intentionally do not construct
        # a Vault client.
        if args.command == "ListOperation":
            print_operation(config_path, args.operation_id)
            log_event("command.succeeded", command=args.command)
            return 0
        if args.command == "ListOperations":
            print_operations(config_path)
            log_event("command.succeeded", command=args.command)
            return 0

        # Public long-running commands return as soon as a detached worker has
        # been safely started. The worker re-enters this same CLI with the
        # hidden operation ID and therefore executes the normal synchronous
        # Terraform lifecycle rather than spawning another worker.
        if args.command in ASYNC_COMMANDS and not operation_id:
            _validate_async_submission(args)
            state = launch_operation(
                config_path,
                DEFAULT_CONFIG.parent,
                command=args.command,
                deployment=_async_deployment(args),
                worker_arguments=_async_worker_arguments(args),
            )
            print(
                submission_instructions(
                    config_path,
                    state,
                    shard_status=args.command in {"AddShard", "DeleteShard"},
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
        # The dispatch table keeps main() simple. Each command maps to one
        # focused lifecycle function; the CLI itself does not mutate resources.
        actions = {
            "AddReplicaSet": lambda: add_replica_set(config, vault, args.deployment),
            "AddShardedCluster": lambda: add_sharded_cluster(
                config, vault, args.deployment, args.shards
            ),
            "DeleteReplicaSet": lambda: delete_replica_set(
                config, vault, args.deployment, args.confirm
            ),
            "DeleteShardedCluster": lambda: delete_sharded_cluster(
                config, vault, args.deployment, args.confirm
            ),
            "ListDeployments": lambda: list_deployments(config, vault),
            "ListDeployment": lambda: list_deployment(config, vault, args.deployment),
            "ListReplicaSets": lambda: list_replica_sets(config, vault),
            "ListReplicaSet": lambda: list_replica_set(config, vault, args.deployment),
            "ListShardedClusters": lambda: list_sharded_clusters(config, vault),
            "ListShardedCluster": lambda: list_sharded_cluster(
                config, vault, args.deployment
            ),
            "ListShards": lambda: list_shards(config, vault, args.deployment),
            "AddShard": lambda: add_shard(
                config, vault, args.deployment, args.count
            ),
            "DeleteShard": lambda: delete_shard(
                config, vault, args.deployment, args.count, args.confirm
            ),
            "AddDatabase": lambda: add_database(
                config, vault, args.deployment_or_database, args.database
            ),
            "DeleteDatabase": lambda: delete_database(
                config,
                vault,
                args.deployment_or_database,
                args.database,
                args.confirm,
            ),
            "ListDatabases": lambda: list_databases(config, vault, args.deployment),
            "ListDatabase": lambda: list_database(
                config, vault, args.deployment_or_database, args.database
            ),
            "RotatePasswords": lambda: rotate_passwords(
                config, vault, args.deployment_or_database, args.database
            ),
            "DisableOwner": lambda: disable_owner(
                config,
                vault,
                args.deployment_or_database,
                args.database,
                args.confirm,
            ),
            "Reconcile": lambda: reconcile(config, vault),
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
