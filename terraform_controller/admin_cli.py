"""Administrator command-line interface for terraformController.

This module is intentionally separate from terraform_controller.cli.
Customer-facing database/deployment commands belong in terraformController.py;
platform diagnostics, repair, reconciliation, and guarded recovery belong here.

The executable split improves clarity but is not an authorization boundary.
Production must still restrict host, Kubernetes, Vault, and Terraform access to
authorized administrators.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .async_operations import (
    admin_submission_instructions,
    launch_operation,
    mark_failed,
    mark_running,
    mark_succeeded,
    print_operation,
    print_operations,
)
from .common import ControllerError
from .config import load_config
from .controller import (
    list_managed_resources,
    reconcile,
    recover_deployment_lock,
    recover_orphaned_resources,
)
from .logging_component import configure_logging, log_event, log_exception
from .vault import VaultClient

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "terraformController.config"


def _confirm(parser: argparse.ArgumentParser) -> None:
    """Add the standard explicit confirmation flag for recovery actions."""

    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Required explicit confirmation for this administrative action.",
    )


def _sub(
    subparsers,
    name: str,
    help_text: str,
    description: str,
    examples: str,
) -> argparse.ArgumentParser:
    """Create one administrator subcommand with consistent detailed help."""

    return subparsers.add_parser(
        name,
        help=help_text,
        description=description,
        epilog=f"Examples:\n{examples}",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the platform-administrator CLI contract."""

    parser = argparse.ArgumentParser(
        prog="terraformControllerAdmin.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="""terraformController platform administration interface.

This program is for authorized platform administrators and service operators.
It is NOT the DBaaS end-user interface.

Use terraformController.py for normal:
  - ReplicaSet and ShardedCluster lifecycle
  - shard lifecycle
  - database lifecycle
  - database credential lifecycle
  - service status

Use this administrator program for:
  - managed resource inventory and zero-state verification
  - asynchronous operation diagnostics
  - controlled recovery after interrupted lifecycle work
  - controller-wide Terraform reconciliation

Detailed Git/Terraform output is written to the daily operations log instead of
being dumped onto the administrator terminal.

Run this program with no command, or use -h/--help, to show this help.
Use '<command> --help' for detailed command-specific help.
""",
        epilog="""Common administrator workflow:

Verify controller-managed resource state:
  python3 terraformControllerAdmin.py ListManagedResources

Inspect recent background work:
  python3 terraformControllerAdmin.py ListOperations

Inspect one operation:
  python3 terraformControllerAdmin.py ListOperation OPERATION_ID

Reapply managed desired state:
  python3 terraformControllerAdmin.py Reconcile

Exceptional recovery:
  python3 terraformControllerAdmin.py RecoverDeploymentLock SC9 --confirm
  python3 terraformControllerAdmin.py RecoverOrphanedResources --confirm

Normal DBaaS users should use:
  python3 terraformController.py --help
""",
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG),
        metavar="FILE",
        help="Controller configuration file. Uses terraformController.config by default.",
    )
    parser.add_argument(
        "--_operation-worker",
        dest="_operation_worker",
        metavar="ID",
        help=argparse.SUPPRESS,
    )

    sp = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)

    _sub(
        sp,
        "ListManagedResources",
        "List controller-managed deployment resources and zero-state status.",
        "Read-only administrator inventory of Vault-backed managed deployments, "
        "terraformController-managed MongoDB and MongoDBUser custom resources, persistent "
        "volume claims, persistent volumes, and deployment-lock ConfigMaps. Reports "
        "CLEAN only when all six categories are empty.",
        "  python3 terraformControllerAdmin.py ListManagedResources",
    )

    x = _sub(
        sp,
        "ListOperation",
        "Show detailed status for one background controller operation.",
        "Displays the internal operation ID, command, scope, result, timestamps, "
        "elapsed time, worker PID, daily operations-log path, and recorded message. "
        "This diagnostic detail is intentionally available only on the administrator "
        "interface.",
        "  python3 terraformControllerAdmin.py ListOperation 7c1349abc123",
    )
    x.add_argument(
        "operation_id",
        metavar="OPERATION_ID",
        help="Operation ID from the administrator operation journal.",
    )

    _sub(
        sp,
        "ListOperations",
        "List recent background controller operations.",
        "Shows up to 50 recent asynchronous controller operations with their "
        "operation IDs, commands, scopes, results, and elapsed times.",
        "  python3 terraformControllerAdmin.py ListOperations",
    )

    x = _sub(
        sp,
        "RecoverDeploymentLock",
        "Release a completed ShardedCluster topology lock after validation.",
        "Exceptional recovery for an interrupted AddShard/DeleteShard that already "
        "reached the lock's target topology. Before releasing the lock, the controller "
        "verifies the recorded target, live MongoDB shardCount, surviving shard "
        "readiness, config servers, mongos, and removed StatefulSets. The lock release "
        "remains Terraform-driven.",
        "  python3 terraformControllerAdmin.py RecoverDeploymentLock SC9 --confirm",
    )
    x.add_argument(
        "deployment",
        metavar="SHARDED_CLUSTER",
        help="Managed ShardedCluster name whose topology lock is being recovered.",
    )
    _confirm(x)

    x = _sub(
        sp,
        "RecoverOrphanedResources",
        "Finish Terraform cleanup after desired-state inventory is already empty.",
        "Exceptional controller-state recovery. The command is allowed only when "
        "Vault-backed managed deployment inventory is empty AND Kubernetes contains "
        "no terraformController-managed MongoDB custom resources. If both checks pass, "
        "Terraform converges the controller backend to empty desired state and finishes "
        "destroying resources still tracked in state.",
        "  python3 terraformControllerAdmin.py RecoverOrphanedResources --confirm",
    )
    _confirm(x)

    _sub(
        sp,
        "Reconcile",
        "Reapply all Vault-backed managed desired state through Terraform.",
        "Reloads managed desired state from Vault, refreshes Terraform, reapplies the "
        "complete controller-managed environment, and waits for convergence. Reconcile "
        "refuses to run while a protected ShardedCluster change is active.",
        "  python3 terraformControllerAdmin.py Reconcile",
    )

    return parser


def _recover_orphans_worker_arguments(args: argparse.Namespace) -> list[str]:
    """Rebuild the guarded orphan-recovery command for its detached worker."""

    values = ["RecoverOrphanedResources"]
    if args.confirm:
        values.append("--confirm")
    return values


def main(argv: list[str] | None = None) -> int:
    """Parse and execute one administrator command; no command prints help."""

    raw_argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    if not raw_argv:
        parser.print_help()
        return 0

    args = parser.parse_args(raw_argv)
    logging_ready = False
    config_path = Path(args.config).expanduser().resolve()
    operation_id = getattr(args, "_operation_worker", None)

    try:
        config = load_config(config_path)
        configure_logging(config)
        logging_ready = True
        log_event("admin.command.started", command=args.command)

        # Operation diagnostics must remain available even when Vault/MongoDB is
        # unhealthy, so these commands do not construct a Vault client.
        if args.command == "ListOperation":
            print_operation(config_path, args.operation_id)
            log_event("admin.command.succeeded", command=args.command)
            return 0

        if args.command == "ListOperations":
            print_operations(config_path)
            log_event("admin.command.succeeded", command=args.command)
            return 0

        # ListManagedResources combines Vault desired-state inventory with
        # read-only Kubernetes queries. It performs no managed mutation.
        if args.command == "ListManagedResources":
            vault = VaultClient(config)
            list_managed_resources(config, vault)
            log_event("admin.command.succeeded", command=args.command)
            return 0

        # Orphan cleanup can contain bounded storage waits, so it remains async.
        # The worker re-enters this administrator executable, never the public CLI.
        if args.command == "RecoverOrphanedResources" and not operation_id:
            if not args.confirm:
                raise ControllerError(
                    "RecoverOrphanedResources is destructive and requires '--confirm'."
                )
            state = launch_operation(
                config_path,
                DEFAULT_CONFIG.parent,
                command=args.command,
                deployment="controller-state",
                worker_arguments=_recover_orphans_worker_arguments(args),
                entrypoint_name="terraformControllerAdmin.py",
            )
            print(admin_submission_instructions(config_path, state))
            log_event(
                "admin.command.accepted",
                command=args.command,
                operation_id=state["operation_id"],
                scope="controller-state",
            )
            return 0

        if operation_id:
            mark_running(config_path, operation_id)

        vault = VaultClient(config)
        actions = {
            "RecoverDeploymentLock": lambda: recover_deployment_lock(
                config, vault, args.deployment, args.confirm
            ),
            "RecoverOrphanedResources": lambda: recover_orphaned_resources(
                config, vault, args.confirm
            ),
            "Reconcile": lambda: reconcile(config, vault),
        }
        actions[args.command]()

        if operation_id:
            mark_succeeded(config_path, operation_id)

        log_event("admin.command.succeeded", command=args.command)
        return 0

    except (ControllerError, json.JSONDecodeError) as exc:
        if operation_id:
            try:
                mark_failed(config_path, operation_id, str(exc))
            except Exception:
                pass
        if logging_ready:
            log_event(
                "admin.command.failed",
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
                "admin.command.unhandled_exception",
                command=getattr(args, "command", ""),
                error=str(exc),
            )
        print(f"ERROR: Unexpected failure: {exc}", file=sys.stderr)
        return 1
