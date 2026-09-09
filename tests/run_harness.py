#!/usr/bin/env python3
"""Run live end-to-end terraformController scenarios.

This file is intentionally a thin entry point.  Scenario logic lives in the
tests/harness package so the harness remains readable and easy to extend.

SAFE DEFAULT:
    With no arguments, only read-only preflight checks run.

FULL LIVE TEST:
    python3 tests/run_harness.py --profile all \
        --allow-mutations --allow-destructive

The two opt-in flags are deliberately verbose.  They make it difficult to run
resource-creating or resource-deleting tests by accident.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

# When Python executes a file inside tests/, sys.path starts at tests/ rather
# than the repository root.  Add the root explicitly before importing harness
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


def build_parser() -> argparse.ArgumentParser:
    """Build the live-harness command-line parser."""

    parser = argparse.ArgumentParser(
        description=(
            "Live end-to-end harness for terraformController. "
            "Preflight is read-only; lifecycle profiles create/delete test resources."
        )
    )
    parser.add_argument(
        "--profile",
        choices=("preflight", "replicaset", "sharded", "locking", "all"),
        default="preflight",
        help="Scenario group to run. Default: preflight.",
    )
    parser.add_argument(
        "--config",
        default=str(REPO_ROOT / "terraformController.config"),
        help="terraformController configuration file.",
    )
    parser.add_argument(
        "--python",
        default=sys.executable,
        help="Python executable used to launch terraformController.py.",
    )
    parser.add_argument(
        "--allow-mutations",
        action="store_true",
        help="Required before the harness may create or modify MongoDB resources.",
    )
    parser.add_argument(
        "--allow-destructive",
        action="store_true",
        help="Required before lifecycle profiles may delete temporary resources.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print stdout/stderr for passing steps as well as failures.",
    )
    parser.add_argument(
        "--suffix",
        default=datetime.now().strftime("%m%d%H%M%S"),
        help=(
            "Unique suffix used in temporary names. "
            "Default is current MMDDHHMMSS."
        ),
    )
    return parser


def _require_live_opt_in(args: argparse.Namespace) -> None:
    """Reject mutating profiles unless both safety flags were supplied."""

    if args.profile == "preflight":
        return

    missing = []
    if not args.allow_mutations:
        missing.append("--allow-mutations")
    if not args.allow_destructive:
        missing.append("--allow-destructive")
    if missing:
        raise SystemExit(
            "ERROR: Live lifecycle profiles create and delete test resources. "
            "Re-run with " + " ".join(missing) + "."
        )



def _total_tests(profile: str) -> int:
    """Return the exact number of PASS/FAIL checks for the selected profile."""

    total = scenario_preflight.TEST_COUNT
    if profile == "replicaset":
        return total + scenario_replicaset.TEST_COUNT
    if profile == "sharded":
        return total + scenario_sharded.TEST_COUNT
    if profile == "locking":
        return total + scenario_locking.TEST_COUNT
    if profile == "all":
        return (
            total
            + scenario_replicaset.TEST_COUNT
            + scenario_sharded.TEST_COUNT
            + scenario_locking.TEST_COUNT
        )
    return total


def main() -> int:
    """Run the requested scenarios and return zero only when all checks pass."""

    args = build_parser().parse_args()
    _require_live_opt_in(args)

    config_path = Path(args.config).expanduser().resolve()
    if not config_path.exists():
        raise SystemExit(f"ERROR: Configuration file does not exist: {config_path}")

    context = HarnessContext(
        repo_root=REPO_ROOT,
        config_path=config_path,
        python=args.python,
        suffix=args.suffix,
        allow_mutations=args.allow_mutations,
        allow_destructive=args.allow_destructive,
        verbose=args.verbose,
        total_tests=_total_tests(args.profile),
    )
    runner = HarnessRunner(context)

    print("terraformController Live Test Harness")
    print("=" * 68)
    print(f"Profile: {args.profile}")
    print(f"Config:  {config_path}")
    print(f"Suffix:  {args.suffix}")
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
