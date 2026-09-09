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
    sp = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)

    x = _sub(
        sp,
        "AddReplicaSet",
        "Create an empty managed ReplicaSet.",
        "Creates a non-sharded MongoDB ReplicaSet through Terraform. The command waits until MongoDB is Running and the internal controller account is ready before reporting success.",
        "  terraformController.py AddReplicaSet RS1",
    )
    _deployment(x, "REPLICASET")

    x = _sub(
        sp,
        "AddShardedCluster",
        "Create an empty managed ShardedCluster.",
        "Creates a MongoDB ShardedCluster through Terraform. The command waits for the cluster, every shard, config servers, mongos, and the internal controller account before reporting success.",
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
        "Requires --confirm. The ReplicaSet must be Running and contain no managed or live application databases. Terraform performs a final runtime emptiness validation before deletion.",
        "  terraformController.py DeleteReplicaSet RS1 --confirm",
    )
    _deployment(x, "REPLICASET")
    _confirm(x)

    x = _sub(
        sp,
        "DeleteShardedCluster",
        "Delete an empty managed ShardedCluster.",
        "Requires --confirm. The ShardedCluster must be fully Running and contain no managed or live application databases. Terraform performs a final runtime emptiness validation before deletion.",
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
        "COUNT defaults to 1. Terraform acquires the ShardedCluster deployment lock, prepares storage, changes shardCount, waits for the requested shard total to become fully online, then releases the lock. Rerun the same command to resume an interrupted shard addition.",
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
        "COUNT defaults to 1 and --confirm is required. The operation can never reduce the cluster below one shard. Application databases may remain on the ShardedCluster. Terraform lowers the managed ShardedCluster shardCount, the MongoDB Kubernetes Operator/Ops Manager reconciles the supported scale-down, and Terraform cleans old shard storage only after the removed shard StatefulSets are gone and the remaining cluster is fully ready. The highest-numbered shards are removed first. Rerun the same command to resume an interrupted deletion.",
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

    _sub(
        sp,
        "Reconcile",
        "Reapply complete Vault-backed desired state through Terraform.",
        "Refreshes Terraform from GitHub, reapplies all managed ReplicaSets, ShardedClusters, databases, accounts, and lifecycle state, then waits for convergence.",
        "  terraformController.py Reconcile",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse one command, initialize shared services, and execute it.

    Expected ControllerError failures are printed without a traceback.  Truly
    unexpected exceptions are logged with a traceback so developers have enough
    detail to debug them while the user still gets a concise ERROR line.
    """
    args = build_parser().parse_args(argv)
    logging_ready = False
    try:
        config = load_config(Path(args.config).expanduser())
        configure_logging(config)
        logging_ready = True
        log_event("command.started", command=args.command)

        vault = VaultClient(config)
        # The dispatch table keeps main() simple.  Each command maps to one
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
        log_event("command.succeeded", command=args.command)
        return 0
    except (ControllerError, json.JSONDecodeError) as exc:
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
        if logging_ready:
            log_exception(
                "command.unhandled_exception",
                command=getattr(args, "command", ""),
                error=str(exc),
            )
        print(f"ERROR: Unexpected failure: {exc}", file=sys.stderr)
        return 1
