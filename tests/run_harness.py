#!/usr/bin/env python3
"""Run live end-to-end privateWorkerReplacement scenarios.

This file is intentionally a thin entry point. Scenario logic lives in the
tests/harness package so the harness remains readable and easy to extend.

Run this program with no arguments, or use -h/--help, to show the full help
screen. No live tests run when no arguments are supplied.
"""

from __future__ import annotations

import argparse
import re
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
    scenario_admin,
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
        "admin": preflight + scenario_admin.TEST_COUNT,
        "all": (
            preflight
            + scenario_replicaset.TEST_COUNT
            + scenario_sharded.TEST_COUNT
            + scenario_locking.TEST_COUNT
            + scenario_admin.TEST_COUNT
        ),
    }


def _scenario_ranges() -> dict[str, tuple[int, int]]:
    """Return canonical test-number ranges for the complete full-suite order."""

    start = 1
    ranges: dict[str, tuple[int, int]] = {}
    for name, count in (
        ("preflight", scenario_preflight.TEST_COUNT),
        ("replicaset", scenario_replicaset.TEST_COUNT),
        ("sharded", scenario_sharded.TEST_COUNT),
        ("locking", scenario_locking.TEST_COUNT),
        ("admin", scenario_admin.TEST_COUNT),
    ):
        end = start + count - 1
        ranges[name] = (start, end)
        start = end + 1
    return ranges


def _parse_test_list(value: str, maximum: int) -> tuple[int, ...]:
    """Parse comma-separated test IDs/ranges using strict no-whitespace syntax."""

    if not value or any(character.isspace() for character in value):
        raise argparse.ArgumentTypeError(
            "--testList must contain no spaces. Example: 56,58-67"
        )

    if not re.fullmatch(r"\d+(?:-\d+)?(?:,\d+(?:-\d+)?)*", value):
        raise argparse.ArgumentTypeError(
            "--testList must use comma-separated numbers/ranges. "
            "Example: 56,58-67"
        )

    selected: set[int] = set()
    for token in value.split(","):
        if "-" in token:
            first_text, last_text = token.split("-", 1)
            first = int(first_text)
            last = int(last_text)
            if last < first:
                raise argparse.ArgumentTypeError(
                    f"Invalid test range '{token}': the second test number must "
                    "be greater than or equal to the first."
                )
        else:
            first = last = int(token)

        if first < 1 or last > maximum:
            raise argparse.ArgumentTypeError(
                f"Test numbers must be between 1 and {maximum}."
            )
        selected.update(range(first, last + 1))

    return tuple(sorted(selected))


def _format_test_list(numbers: tuple[int, ...]) -> str:
    """Return a compact normalized representation of selected test numbers."""

    if not numbers:
        return ""

    ranges: list[str] = []
    start = previous = numbers[0]
    for number in numbers[1:]:
        if number == previous + 1:
            previous = number
            continue
        ranges.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = number
    ranges.append(str(start) if start == previous else f"{start}-{previous}")
    return ",".join(ranges)


def build_parser() -> argparse.ArgumentParser:
    """Build the live-harness command-line parser."""

    totals = _profile_totals()
    parser = argparse.ArgumentParser(
        description=(
            "Live end-to-end test harness for privateWorkerReplacement. "
            "Choose a profile, administrator suite, or exact test list explicitly."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
Profiles and administrator suite:
  preflight   {totals['preflight']:2d} read-only checks. Creates, changes, and deletes nothing.
  replicaset  {totals['replicaset']:2d} checks total. Tests ReplicaSet + database lifecycle.
  sharded     {totals['sharded']:2d} checks total. Tests ShardedCluster + shard + database lifecycle.
  locking     {totals['locking']:2d} checks total. Tests ShardedCluster mutation locking.
  --admin     {totals['admin']:2d} checks total when run alone. Tests administrator diagnostics/recovery.
  all         {totals['all']:2d} checks total. Runs every lifecycle and administrator test.

Safety:
  Lifecycle profiles, --admin, and every --testList selection require --allow-changes.
  The administrator suite deliberately creates drift, stranded locks, and
  orphaned Terraform state, then proves supported recovery returns the
  environment to CLEAN. It requires a clean DBaaS starting inventory.
  Preflight is read-only and does not require --allow-changes.

Common commands:
  Read-only preflight:
    python3 tests/run_harness.py --profile preflight

  ReplicaSet lifecycle only:
    python3 tests/run_harness.py --profile replicaset --allow-changes

  ShardedCluster lifecycle only:
    python3 tests/run_harness.py --profile sharded --allow-changes

  Locking/concurrency only:
    python3 tests/run_harness.py --profile locking --allow-changes

  Full administrator diagnostics/recovery suite:
    python3 tests/run_harness.py --admin --allow-changes

  Run only exact full-suite test numbers:
    python3 tests/run_harness.py --testList 97-100 --allow-changes
    python3 tests/run_harness.py --testList 56,58-67 --allow-changes

  --testList uses the canonical numbering from the full {totals['all']}-test run.
  It runs ONLY the requested checks and does not automatically run prerequisite
  tests. The value must contain no spaces, and range end must be >= range start.
  --profile and --testList are mutually exclusive. --admin cannot be combined
  with --testList.

  FULL GAUNTLET - all {totals['all']} live acceptance checks:
    python3 tests/run_harness.py --profile all --allow-changes

Configuration:
  ./dev.config is used by default. Use --config FILE only when
  the configuration file is somewhere else.
""",
    )
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument(
        "--profile",
        choices=("preflight", "replicaset", "sharded", "locking", "all"),
        required=False,
        help=(
            "Optional customer/lifecycle test group. Every profile run starts with "
            "the read-only preflight checks. 'all' also runs the complete "
            "administrator suite."
        ),
    )
    selection.add_argument(
        "--testList",
        dest="test_list",
        type=lambda value: _parse_test_list(value, totals["all"]),
        metavar="LIST",
        help=(
            "Run only specific canonical full-suite test numbers, for example "
            "97-100 or 56,58-67. No spaces are allowed inside LIST. Does not "
            "automatically run prerequisite tests."
        ),
    )
    parser.add_argument(
        "--admin",
        action="store_true",
        help=(
            "Run the complete destructive administrator diagnostics/recovery suite. "
            "May be used alone or added to a non-'all' profile. The 'all' profile "
            "already includes every administrator test."
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
            "Required for every mutating selection, --admin, and --testList. "
            "Allows the harness to create, modify, deliberately damage, recover, "
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


def _require_selection(args: argparse.Namespace) -> None:
    """Require one explicit live-test selection and reject ambiguous combinations."""

    if args.test_list is not None and args.admin:
        raise SystemExit(
            "ERROR: --testList cannot be combined with --admin. "
            "Use canonical full-suite test numbers instead."
        )

    if args.profile or args.admin or args.test_list is not None:
        return
    raise SystemExit(
        "ERROR: Choose --profile {preflight,replicaset,sharded,locking,all}, "
        "--admin, or --testList LIST."
    )


def _require_live_opt_in(args: argparse.Namespace) -> None:
    """Reject every mutating selection unless the change acknowledgement is supplied."""

    # Selective execution intentionally skips normal scenario prerequisites.
    # Require the explicit change acknowledgement for every --testList request,
    # even when a particular chosen test happens to be read-only.
    mutating = args.test_list is not None or args.admin or args.profile in {
        "replicaset",
        "sharded",
        "locking",
        "all",
    }
    if not mutating:
        return

    if not args.allow_changes:
        raise SystemExit(
            "ERROR: Selected live tests create, modify, deliberately damage, and "
            "delete temporary test resources. Re-run with --allow-changes."
        )


def _total_tests(profile: str | None, admin: bool) -> int:
    """Return the exact number of PASS/FAIL checks for this combined selection."""

    total = scenario_preflight.TEST_COUNT
    if profile in {"replicaset", "all"}:
        total += scenario_replicaset.TEST_COUNT
    if profile in {"sharded", "all"}:
        total += scenario_sharded.TEST_COUNT
    if profile in {"locking", "all"}:
        total += scenario_locking.TEST_COUNT
    if admin or profile == "all":
        total += scenario_admin.TEST_COUNT
    return total


def _selection_label(
    profile: str | None,
    admin: bool,
    test_list: tuple[int, ...] | None = None,
) -> str:
    """Return a concise label for the requested harness work."""

    if test_list is not None:
        return f"testList {_format_test_list(test_list)}"

    parts: list[str] = []
    if profile:
        parts.append(profile)
    if admin and profile != "all":
        parts.append("admin")
    return " + ".join(parts)


def main(argv: list[str] | None = None) -> int:
    """Run the requested scenarios and return zero only when all checks pass."""

    raw_argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    if not raw_argv:
        parser.print_help()
        return 0

    args = parser.parse_args(raw_argv)
    _require_selection(args)
    _require_live_opt_in(args)

    config_display = args.config
    config_path = Path(args.config).expanduser().resolve()
    if not config_path.exists():
        raise SystemExit(f"ERROR: Configuration file does not exist: {config_display}")

    # Test names need to be unique, but testers should not have to invent or
    # understand suffixes. Generate a compact run ID internally.
    run_id = datetime.now().strftime("%m%d%H%M%S")

    selected_tests = (
        frozenset(args.test_list)
        if args.test_list is not None
        else None
    )
    canonical_total = _profile_totals()["all"]
    context = HarnessContext(
        repo_root=REPO_ROOT,
        config_path=config_path,
        python=sys.executable,
        run_id=run_id,
        verbose=args.verbose,
        total_tests=(
            canonical_total
            if selected_tests is not None
            else _total_tests(args.profile, args.admin)
        ),
        selected_tests=selected_tests,
        canonical_total_tests=canonical_total,
    )
    runner = HarnessRunner(context)

    print("privateWorkerReplacement Live Test Harness")
    print("=" * 68)
    print(
        f"Selection: "
        f"{_selection_label(args.profile, args.admin, args.test_list)}"
    )
    print(f"Config:  {config_display}")
    print(f"Run ID:  {run_id}")
    if selected_tests is not None:
        print(f"Tests:   {len(selected_tests)} selected of {canonical_total}")
        print(
            "NOTE: --testList runs only the requested checks; prerequisite tests "
            "are not added automatically."
        )
    else:
        print(f"Tests:   {context.total_tests}")
    print()

    if selected_tests is not None:
        # Selective mode uses the canonical numbering from --profile all and
        # does not inject preflight or scenario prerequisites. Entire scenarios
        # with no selected numbers are skipped so a late test such as 97-100
        # starts immediately instead of replaying the preceding hour of work.
        scenario_ranges = _scenario_ranges()
        scenario_plan = (
            ("Preflight", "preflight", scenario_preflight),
            ("ReplicaSet", "replicaset", scenario_replicaset),
            ("ShardedCluster", "sharded", scenario_sharded),
            ("Locking", "locking", scenario_locking),
            ("Admin", "admin", scenario_admin),
        )
        for label, key, scenario in scenario_plan:
            start, end = scenario_ranges[key]
            if not runner.any_test_selected(start, end):
                continue

            runner.set_canonical_position(start - 1)
            runner.start_profile(label)
            scenario.run(runner)
            runner.finish_profile(label)
            if any(not result.passed for result in runner.results):
                return runner.summary()

        return runner.summary()

    # Normal profile/admin runs keep the established behavior: preflight always
    # runs first and numbering is compact for the selected profile(s).
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
        if any(not result.passed for result in runner.results):
            return runner.summary()

    if args.admin or args.profile == "all":
        runner.start_profile("Admin")
        scenario_admin.run(runner)
        runner.finish_profile("Admin")

    return runner.summary()


if __name__ == "__main__":
    raise SystemExit(main())
