#!/usr/bin/env python3
"""Run live end-to-end privateWorkerReplacement scenarios.

This file is intentionally a thin entry point. Scenario logic lives in the
tests/harness package so the harness remains readable and easy to extend.

Run this program with no arguments, or use -h/--help, to show the full help
screen. No live tests run when no arguments are supplied.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

# When Python executes a file inside tests/, sys.path starts at tests/ rather
# than the repository root. Add the root explicitly before importing harness
# scenarios because some scenarios reuse production config/Kubernetes helpers.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from harness.models import HarnessContext
from harness.runner import HarnessRunner
from harness import (
    scenario_locking,
    scenario_preflight,
    scenario_replicaset,
    scenario_sharded,
)


def _profile_totals() -> dict[str, int]:
    """Return the displayed check count for every live-test profile.

    Every lifecycle profile includes the read-only preflight checks. Deriving
    these values from each scenario's TEST_COUNT keeps --help synchronized with
    the checks the harness actually executes when scenarios are added or removed.
    """

    preflight = scenario_preflight.TEST_COUNT
    return {
        "preflight": preflight,
        "replicaset": preflight + scenario_replicaset.TEST_COUNT,
        "sharded": preflight + scenario_sharded.TEST_COUNT,
        "locking": preflight + scenario_locking.TEST_COUNT,
        "all": (
            preflight
            + scenario_replicaset.TEST_COUNT
            + scenario_sharded.TEST_COUNT
            + scenario_locking.TEST_COUNT
        ),
    }


def build_parser() -> argparse.ArgumentParser:
    """Build the live-harness command-line parser."""

    totals = _profile_totals()
    parser = argparse.ArgumentParser(
        description=(
            "Live end-to-end test harness for privateWorkerReplacement. "
            "Choose a profile explicitly before running tests."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
Profiles:
  preflight   {totals['preflight']:2d} read-only checks. Creates, changes, and deletes nothing.
  replicaset  {totals['replicaset']:2d} checks total. Tests ReplicaSet + database lifecycle.
  sharded     {totals['sharded']:2d} checks total. Tests ShardedCluster + shard + database lifecycle.
  locking     {totals['locking']:2d} checks total. Tests ShardedCluster mutation locking.
  all         {totals['all']:2d} checks total. Runs the complete live acceptance gauntlet.

Safety:
  Lifecycle profiles (replicaset, sharded, locking, all) require --allow-changes.
  This explicitly allows the harness to create, modify, and delete temporary
  test resources in the configured environment. Preflight is read-only and does
  not require --allow-changes.

Common commands:
  Read-only preflight:
    python3 tests/run_harness.py --profile preflight

  ReplicaSet lifecycle only:
    python3 tests/run_harness.py --profile replicaset --allow-changes

  ShardedCluster lifecycle only:
    python3 tests/run_harness.py --profile sharded --allow-changes

  Locking/concurrency only:
    python3 tests/run_harness.py --profile locking --allow-changes

  FULL GAUNTLET - all {totals['all']} live acceptance checks:
    python3 tests/run_harness.py --profile all --allow-changes

Configuration:
  ./dev.config is used by default. Use --config FILE only when
  the configuration file is somewhere else.
""",
    )
    parser.add_argument(
        "--profile",
        choices=("preflight", "replicaset", "sharded", "locking", "all"),
        required=True,
        help=(
            "Test group to run. Every profile starts with the read-only preflight "
            f"checks. Use 'all' only for the complete {totals['all']}-check live "
            "acceptance run."
        ),
    )
    parser.add_argument(
        "--config",
        default="./dev.config",
        metavar="FILE",
        help=(
            "Optional controller configuration file. "
            "Default: ./dev.config"
        ),
    )
    parser.add_argument(
        "--allow-changes",
        action="store_true",
        help=(
            "Required for lifecycle profiles. Allows the harness to create, modify, "
            "and delete temporary test resources in the configured environment."
        ),
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help=(
            "Show command stdout/stderr for passing checks. Failures always show "
            "their diagnostic output."
        ),
    )
    return parser


def _require_live_opt_in(args: argparse.Namespace) -> None:
    """Reject lifecycle profiles unless the change acknowledgement is supplied."""

    if args.profile == "preflight":
        return

    if not args.allow_changes:
        raise SystemExit(
            "ERROR: Live lifecycle profiles create, modify, and delete temporary "
            "test resources. Re-run with --allow-changes."
        )


def _total_tests(profile: str) -> int:
    """Return the exact number of PASS/FAIL checks for the selected profile."""

    return _profile_totals()[profile]


def main(argv: list[str] | None = None) -> int:
    """Run the requested scenarios and return zero only when all checks pass."""

    raw_argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    if not raw_argv:
        parser.print_help()
        return 0

    args = parser.parse_args(raw_argv)
    _require_live_opt_in(args)

    config_display = args.config
    config_path = Path(args.config).expanduser().resolve()
    if not config_path.exists():
        raise SystemExit(f"ERROR: Configuration file does not exist: {config_display}")

    # Test names need to be unique, but testers should not have to invent or
    # understand suffixes. Generate a compact run ID internally.
    run_id = datetime.now().strftime("%m%d%H%M%S")

    context = HarnessContext(
        repo_root=REPO_ROOT,
        config_path=config_path,
        python=sys.executable,
        run_id=run_id,
        verbose=args.verbose,
        total_tests=_total_tests(args.profile),
    )
    runner = HarnessRunner(context)

    print("privateWorkerReplacement Live Test Harness")
    print("=" * 68)
    print(f"Profile: {args.profile}")
    print(f"Config:  {config_display}")
    print(f"Run ID:  {run_id}")
    print(f"Tests:   {context.total_tests}")
    print()

    # Preflight always runs first. Stop immediately if a prerequisite is broken
    # so later scenarios do not create misleading secondary failures.
    runner.start_profile("Preflight")
    scenario_preflight.run(runner)
    runner.finish_profile("Preflight")
    if any(not result.passed for result in runner.results):
        return runner.summary()

    if args.profile in {"replicaset", "all"}:
        runner.start_profile("ReplicaSet")
        scenario_replicaset.run(runner)
        runner.finish_profile("ReplicaSet")
        if any(not result.passed for result in runner.results):
            return runner.summary()

    if args.profile in {"sharded", "all"}:
        runner.start_profile("ShardedCluster")
        scenario_sharded.run(runner)
        runner.finish_profile("ShardedCluster")
        if any(not result.passed for result in runner.results):
            return runner.summary()

    if args.profile in {"locking", "all"}:
        runner.start_profile("Locking")
        scenario_locking.run(runner)
        runner.finish_profile("Locking")

    return runner.summary()


if __name__ == "__main__":
    raise SystemExit(main())
