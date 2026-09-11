"""Administrator command-line interface for privateWorkerReplacement.

This module is intentionally separate from privateWorkerReplacement.cli.
Customer-facing database/deployment commands belong in privateWorkerReplacement.py;
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
from .mutation_lock import controller_state_mutation_lock
from .vault import VaultClient

# Keep these values separate on purpose. REPO_ROOT locates the administrator
# entry point for detached recovery workers. DEFAULT_CONFIG_DISPLAY is the
# friendly path an operator sees and types; DEFAULT_CONFIG is the Path object.
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_DISPLAY = "./dev.config"
DEFAULT_CONFIG = Path(DEFAULT_CONFIG_DISPLAY)

# Administrator mutations share the same complete Terraform/Vault desired state
# as customer mutations, so they must participate in the same controller-wide
# serialization boundary.
ADMIN_MUTATING_COMMANDS = {
    "RecoverDeploymentLock",
    "RecoverOrphanedResources",
    "Reconcile",
}


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
    """Build the complete platform-administrator CLI contract."""

    parser = argparse.ArgumentParser(
        prog="privateWorkerReplacementAdmin.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="""privateWorkerReplacement platform administration interface.

This program is for authorized platform administrators and service operators.
It is NOT the DBaaS end-user interface.

Use privateWorkerReplacement.py for normal:
  - ReplicaSet and ShardedCluster lifecycle
  - shard lifecycle
  - database lifecycle
  - database credential lifecycle
  - service status

Use this administrator program for:
  - authoritative managed-resource inventory and zero-state verification
  - asynchronous operation diagnostics
  - controlled recovery after interrupted lifecycle work
  - controller-wide Terraform reconciliation

Default configuration file:
  ./dev.config

Detailed Terraform/external-command diagnostics are written to the daily
operations log instead of being dumped onto the administrator terminal.

Run this program with no command, or use -h/--help, to show this help.
Use '<command> --help' for detailed command-specific help.
""",
        epilog="""Common administrator workflow:

Verify controller-managed resource state:
  python3 privateWorkerReplacementAdmin.py ListManagedResources

Inspect recent background work:
  python3 privateWorkerReplacementAdmin.py ListOperations

Inspect one operation:
  python3 privateWorkerReplacementAdmin.py ListOperation OPERATION_ID

Reapply managed desired state:
  python3 privateWorkerReplacementAdmin.py Reconcile

Exceptional recovery:
  python3 privateWorkerReplacementAdmin.py RecoverDeploymentLock SC9 --confirm
  python3 privateWorkerReplacementAdmin.py RecoverOrphanedResources --confirm

Normal DBaaS users should use:
  python3 privateWorkerReplacement.py --help
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
        "ListManagedResources",
        "List authoritative DBaaS resources and zero-state status.",
        "Read-only authoritative inventory across Vault, Kubernetes, Terraform "
        "backend state, and Ops Manager. The default view shows compact grouped "
        "counts, health/consistency status, and one row per managed deployment. "
        "Permanent controller infrastructure remains visible without preventing "
        "CLEAN; orphan or missing cross-plane state reports ATTENTION REQUIRED. "
        "Use --verbose to append the full object-name inventory for troubleshooting.",
        "  python3 privateWorkerReplacementAdmin.py ListManagedResources\n"
        "  python3 privateWorkerReplacementAdmin.py ListManagedResources --verbose",
    )
    x.add_argument(
        "--verbose",
        action="store_true",
        help="Append full object-name inventory details after the compact summary.",
    )

    x = _sub(
        sp,
        "ListOperation",
        "Show detailed status for one background controller operation.",
        "Displays the internal operation ID, command, scope, result, timestamps, "
        "elapsed time, worker PID, daily operations-log path, and recorded message. "
        "This diagnostic detail is intentionally available only on the administrator "
        "interface.",
        "  python3 privateWorkerReplacementAdmin.py ListOperation 7c1349abc123",
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
        "operation IDs, commands, scopes, results, and elapsed times. This command "
        "reads local operation state only and does not require MongoDB/Vault health.",
        "  python3 privateWorkerReplacementAdmin.py ListOperations",
    )

    x = _sub(
        sp,
        "RecoverDeploymentLock",
        "Release a completed ShardedCluster topology lock after validation.",
        "Exceptional recovery for an interrupted AddShard/DeleteShard that already "
        "reached the lock's target topology. Requires --confirm. Before releasing "
        "the lock, the controller verifies the recorded target, live MongoDB "
        "shardCount, surviving shard readiness, config servers, mongos, and removed "
        "StatefulSets. Only the exact lifecycle lock release is applied through "
        "Terraform; unrelated deployment state is not broadly reconciled.",
        "  python3 privateWorkerReplacementAdmin.py RecoverDeploymentLock SC9 --confirm",
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
        "Exceptional asynchronous controller-state recovery. Requires --confirm. "
        "The request is allowed only when Vault-backed managed deployment inventory "
        "is empty AND Kubernetes contains no privateWorkerReplacement-managed MongoDB "
        "custom resources. If both checks pass, Terraform converges the controller "
        "backend to empty desired state and finishes destroying tracked leftovers. "
        "Use ListOperation with the returned operation ID to monitor completion.",
        "  python3 privateWorkerReplacementAdmin.py RecoverOrphanedResources --confirm",
    )
    _confirm(x)

    _sub(
        sp,
        "Reconcile",
        "Reapply all Vault-backed managed desired state through Terraform.",
        "Reloads managed desired state from Vault, refreshes the local Terraform "
        "execution cache, reapplies the complete controller-managed environment, "
        "waits for deployments/accounts to converge, and reports each resulting "
        "deployment. Reconcile refuses to run while a protected ShardedCluster "
        "change is active.",
        "  python3 privateWorkerReplacementAdmin.py Reconcile",
    )

    return parser


def _recover_orphans_worker_arguments(args: argparse.Namespace) -> list[str]:
    """Rebuild the guarded orphan-recovery command for its detached worker."""

    values = ["RecoverOrphanedResources"]
    if args.confirm:
        values.append("--confirm")
    return values


def _run_admin_action(
    config: dict[str, object],
    command: str,
    action,
) -> None:
    """Run one administrator mutation under the shared desired-state lock."""

    if command in ADMIN_MUTATING_COMMANDS:
        with controller_state_mutation_lock(config, command):
            action()
        return
    action()


def main(argv: list[str] | None = None) -> int:
    """Parse and execute one administrator command; no command prints help."""

    raw_argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    if not raw_argv:
        parser.print_help()
        return 0

    args = parser.parse_args(raw_argv)
    logging_ready = False
    # Keep the display form separate from the absolute path used for file I/O.
    # This lets normal instructions stay readable while workers remain reliable.
    config_display = str(args.config)
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
        # read-only Kubernetes/Ops Manager queries. It performs no mutation.
        if args.command == "ListManagedResources":
            vault = VaultClient(config)
            list_managed_resources(config, vault, args.verbose)
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
                REPO_ROOT,
                command=args.command,
                deployment="controller-state",
                worker_arguments=_recover_orphans_worker_arguments(args),
                entrypoint_name="privateWorkerReplacementAdmin.py",
            )
            print(
                admin_submission_instructions(
                    config_path,
                    state,
                    config_display=config_display,
                )
            )
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
                config,
                vault,
                args.deployment,
                args.confirm,
            ),
            "RecoverOrphanedResources": lambda: recover_orphaned_resources(
                config,
                vault,
                args.confirm,
            ),
            "Reconcile": lambda: reconcile(config, vault),
        }
        _run_admin_action(config, args.command, actions[args.command])

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
