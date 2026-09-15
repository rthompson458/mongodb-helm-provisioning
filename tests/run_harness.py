#!/usr/bin/env python3
"""Run live end-to-end privateWorkerReplacement scenarios.

This file is intentionally a thin entry point. Scenario logic lives in the
tests/harness package so the harness remains readable and easy to extend.

Run this program with no arguments, or use -h/--help, to show the full help
screen. No live tests run when no arguments are supplied.
"""

# MAINTAINER READING GUIDE
# User-facing live-harness entry point. It parses the requested profile or exact test list, selects scenarios, and delegates execution to the harness runner.
# Treat these tests as executable design documentation. A failing assertion
# should identify which controller contract changed, not merely that text moved.


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

from privateWorkerReplacement.async_operations import list_operation_records

from harness.models import HarnessContext
from harness.runner import HarnessRunner
from harness import (
    scenario_admin,
    scenario_locking,
    scenario_preflight,
    scenario_replicaset,
    scenario_sharded,
)

# Canonical full-suite order. Keep this single plan authoritative so profile
# counts, --testList ranges, and execution order cannot drift independently.
SCENARIO_PLAN = (
    ("Preflight", "preflight", scenario_preflight),
    ("ReplicaSet", "replicaset", scenario_replicaset),
    ("ShardedCluster", "sharded", scenario_sharded),
    ("Locking", "locking", scenario_locking),
    ("Admin", "admin", scenario_admin),
)


def _profile_totals() -> dict[str, int]:
    """Return the displayed check count for every live-test profile.

    Every profile includes the read-only preflight checks. Deriving
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
    for _, key, scenario in SCENARIO_PLAN:
        end = start + scenario.TEST_COUNT - 1
        ranges[key] = (start, end)
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


HARNESS_RUN_ID_PATTERN = re.compile(
    r"^(?:RSTest|SCTest|LockTest|AdminRSTest|AdminSCTest|OrphanRSTest)-(\d{10})$",
    re.IGNORECASE,
)

# These tests create the top-level deployment fixture for their scenario. When
# one is explicitly selected, a fresh run ID is safer than reusing an interrupted
# prior run. Later state-dependent tests instead reuse the newest harness run ID
# when one can be recovered from the operation journal.
TOP_LEVEL_FIXTURE_TESTS = frozenset({6, 18, 33, 46, 68, 86})


def _latest_harness_run_id(config_path: Path) -> str | None:
    """Return the newest prior harness run ID recorded in async operations."""

    for record in list_operation_records(config_path):
        deployment = str(record.get("deployment", ""))
        match = HARNESS_RUN_ID_PATTERN.fullmatch(deployment)
        if match:
            return match.group(1)
    return None


def _select_run_id(
    config_path: Path,
    selected_tests: frozenset[int] | None,
) -> tuple[str, bool]:
    """Choose a fresh or reusable harness run ID for this invocation.

    Normal profile runs always receive a fresh ID. A selective rerun that does
    not include a top-level fixture-creation test reuses the newest prior harness
    ID when possible. That lets a failed run stop at Test 55 and a later
    --testList 56-60 address the same surviving fixture instead of inventing
    names that cannot exist.
    """

    fresh = datetime.now().strftime("%m%d%H%M%S")
    if selected_tests is None:
        return fresh, False

    if selected_tests.intersection(TOP_LEVEL_FIXTURE_TESTS):
        return fresh, False

    previous = _latest_harness_run_id(config_path)
    if previous:
        return previous, True
    return fresh, False


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
            "Choose a profile or exact test list explicitly."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
Profiles:
  preflight   {totals['preflight']:2d} read-only checks. Creates, changes, and deletes nothing.
  replicaset  {totals['replicaset']:2d} checks total. Tests ReplicaSet + database lifecycle.
  sharded     {totals['sharded']:2d} checks total. Tests ShardedCluster + shard + database lifecycle.
  locking     {totals['locking']:2d} checks total. Tests ShardedCluster mutation locking.
  admin       {totals['admin']:2d} checks total. Tests administrator diagnostics/recovery.
  all         {totals['all']:2d} checks total. Runs every lifecycle and administrator test.

Safety:
  The harness is a live test tool. Selecting a mutating profile or --testList
  may create, modify, deliberately damage, recover, and delete temporary test
  resources in the configured environment. The administrator profile requires
  a clean DBaaS starting inventory. Preflight is read-only.

Common commands:
  Read-only preflight:
    python3 tests/run_harness.py --profile preflight

  ReplicaSet lifecycle only:
    python3 tests/run_harness.py --profile replicaset

  ShardedCluster lifecycle only:
    python3 tests/run_harness.py --profile sharded

  Locking/concurrency only:
    python3 tests/run_harness.py --profile locking

  Administrator diagnostics/recovery profile:
    python3 tests/run_harness.py --profile admin

  Run only exact full-suite test numbers:
    python3 tests/run_harness.py --testList 97-100
    python3 tests/run_harness.py --testList 56,58-67

  --testList uses the canonical numbering from the full {totals['all']}-test run.
  It runs ONLY the requested checks and does not automatically run prerequisite
  tests. When possible, it reuses the newest prior harness Run ID so surviving
  fixtures from a failed run keep the same generated names. The value must contain
  no spaces, and range end must be >= range start. --profile and --testList are
  mutually exclusive.

  FULL GAUNTLET - all {totals['all']} live acceptance checks:
    python3 tests/run_harness.py --profile all

Configuration:
  ./dev.config is used by default. Use --config FILE only when
  the configuration file is somewhere else.
""",
    )
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument(
        "--profile",
        choices=("preflight", "replicaset", "sharded", "locking", "admin", "all"),
        required=False,
        help=(
            "Optional live-test group. Every profile run starts with the read-only "
            "preflight checks. 'all' also runs the complete administrator suite."
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
        "--config",
        default="./dev.config",
        metavar="FILE",
        help=(
            "Optional controller configuration file. "
            "Default: ./dev.config"
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
    """Require one explicit live-test selection."""

    if args.profile or args.test_list is not None:
        return
    raise SystemExit(
        "ERROR: Choose --profile {preflight,replicaset,sharded,locking,admin,all} "
        "or --testList LIST."
    )


def _total_tests(profile: str | None) -> int:
    """Return the exact number of PASS/FAIL checks for one profile."""

    total = scenario_preflight.TEST_COUNT
    if profile in {"replicaset", "all"}:
        total += scenario_replicaset.TEST_COUNT
    if profile in {"sharded", "all"}:
        total += scenario_sharded.TEST_COUNT
    if profile in {"locking", "all"}:
        total += scenario_locking.TEST_COUNT
    if profile in {"admin", "all"}:
        total += scenario_admin.TEST_COUNT
    return total


def _selection_label(
    profile: str | None,
    test_list: tuple[int, ...] | None = None,
) -> str:
    """Return a concise label for the requested harness work."""

    if test_list is not None:
        return f"testList {_format_test_list(test_list)}"
    return profile or ""


def _run_scenario(runner: HarnessRunner, label: str, scenario) -> bool:
    """Run one timed scenario and return True only when all results still pass."""

    runner.start_profile(label)
    scenario.run(runner)
    runner.finish_profile(label)
    return not any(not result.passed for result in runner.results)


def _normal_scenario_plan(
    profile: str | None,
) -> list[tuple[str, object]]:
    """Return scenarios selected for a normal non---testList invocation."""

    selected: list[tuple[str, object]] = [("Preflight", scenario_preflight)]
    if profile in {"replicaset", "all"}:
        selected.append(("ReplicaSet", scenario_replicaset))
    if profile in {"sharded", "all"}:
        selected.append(("ShardedCluster", scenario_sharded))
    if profile in {"locking", "all"}:
        selected.append(("Locking", scenario_locking))
    if profile in {"admin", "all"}:
        selected.append(("Admin", scenario_admin))
    return selected


# ENTRY FLOW: parse profile -> validate safety flags -> build the runner ->
# execute selected scenarios -> print a final pass/fail/skip summary.
# Scenario modules contain domain steps; keep orchestration here.
def main(argv: list[str] | None = None) -> int:
    """Run the requested scenarios and return zero only when all checks pass."""

    raw_argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    if not raw_argv:
        parser.print_help()
        return 0

    args = parser.parse_args(raw_argv)
    _require_selection(args)

    config_display = args.config
    config_path = Path(args.config).expanduser().resolve()
    if not config_path.exists():
        raise SystemExit(f"ERROR: Configuration file does not exist: {config_display}")

    selected_tests = (
        frozenset(args.test_list)
        if args.test_list is not None
        else None
    )

    # Full/profile runs always use a new fixture suffix. Focused reruns normally
    # reuse the newest interrupted harness suffix so state-dependent tests can
    # inspect or finish the exact resources left by that earlier run.
    run_id, reused_run_id = _select_run_id(config_path, selected_tests)
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
            else _total_tests(args.profile)
        ),
        selected_tests=selected_tests,
        canonical_total_tests=canonical_total,
    )
    runner = HarnessRunner(context)

    print("privateWorkerReplacement Live Test Harness")
    print("=" * 68)
    print(
        f"Selection: "
        f"{_selection_label(args.profile, args.test_list)}"
    )
    print(f"Config:  {config_display}")
    print(
        f"Run ID:  {run_id}"
        + (" (reused from latest harness operation)" if reused_run_id else "")
    )
    if selected_tests is not None:
        print(f"Tests:   {len(selected_tests)} selected of {canonical_total}")
        print(
            "NOTE: --testList runs only the requested checks; prerequisite tests "
            "are not added automatically."
        )
        if not reused_run_id and not selected_tests.intersection(TOP_LEVEL_FIXTURE_TESTS):
            print(
                "NOTE: No prior harness run ID was found. State-dependent tests "
                "must already have matching resources or they will fail cleanly."
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
        for label, key, scenario in SCENARIO_PLAN:
            start, end = scenario_ranges[key]
            if not runner.any_test_selected(start, end):
                continue

            runner.set_canonical_position(start - 1)
            if not _run_scenario(runner, label, scenario):
                return runner.summary()

        return runner.summary()

    # Normal profile runs keep compact numbering and always begin with preflight.
    # One shared execution helper keeps timing/fail-fast behavior identical across
    # every scenario.
    for label, scenario in _normal_scenario_plan(args.profile):
        if not _run_scenario(runner, label, scenario):
            return runner.summary()

    return runner.summary()


if __name__ == "__main__":
    raise SystemExit(main())
