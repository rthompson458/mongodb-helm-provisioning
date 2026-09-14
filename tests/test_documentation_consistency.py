"""Regression tests that keep README command/test references synchronized."""

from __future__ import annotations

import unittest
from pathlib import Path

from privateWorkerReplacement import admin_cli, cli
from harness import (
    scenario_admin,
    scenario_locking,
    scenario_preflight,
    scenario_replicaset,
    scenario_sharded,
)


REPO_ROOT = Path(__file__).resolve().parent.parent


def _subcommand_names(parser) -> set[str]:
    """Return every supported subcommand name from one argparse parser."""

    return set(
        next(
            action.choices
            for action in parser._actions
            if getattr(action, "choices", None)
        )
    )


class DocumentationConsistencyTests(unittest.TestCase):
    """Keep authoritative README references aligned with executable interfaces."""

    def test_customer_readme_mentions_every_public_command(self) -> None:
        """Every supported customer command must appear in the customer guide."""

        text = (REPO_ROOT / "README-privateWorkerReplacement.md").read_text(
            encoding="utf-8"
        )
        missing = sorted(
            command
            for command in _subcommand_names(cli.build_parser(cli.DEFAULT_CONFIG))
            if command not in text
        )
        self.assertEqual(missing, [])

    def test_admin_readme_mentions_every_admin_command(self) -> None:
        """Every supported administrator command must appear in the admin guide."""

        text = (REPO_ROOT / "README-privateWorkerReplacementAdmin.md").read_text(
            encoding="utf-8"
        )
        missing = sorted(
            command
            for command in _subcommand_names(admin_cli.build_parser())
            if command not in text
        )
        self.assertEqual(missing, [])

    def test_primary_guides_link_to_maintainer_guide(self) -> None:
        """Keep the reviewer-oriented architecture map discoverable."""

        for relative in (
            "README.md",
            "README-privateWorkerReplacement.md",
            "README-privateWorkerReplacementAdmin.md",
            "tests/README.md",
            "terraform-dbaas/README.md",
        ):
            with self.subTest(document=relative):
                text = (REPO_ROOT / relative).read_text(encoding="utf-8")
                self.assertIn("docs/MAINTAINER-GUIDE.md", text)

    def test_maintainer_guide_mentions_every_production_module(self) -> None:
        """Every production Python module must have an ownership breadcrumb."""

        guide = (REPO_ROOT / "docs" / "MAINTAINER-GUIDE.md").read_text(
            encoding="utf-8"
        )
        production_modules = sorted(
            path.name
            for path in (REPO_ROOT / "privateWorkerReplacement").glob("*.py")
            if path.name != "__init__.py"
        )
        missing = [name for name in production_modules if f"`{name}`" not in guide]
        self.assertEqual(
            missing,
            [],
            "Production modules missing from maintainer guide: "
            + ", ".join(missing),
        )

    def test_testing_readme_matches_live_profile_counts(self) -> None:
        """Profile totals in tests/README.md must match executable scenario counts."""

        text = (REPO_ROOT / "tests" / "README.md").read_text(encoding="utf-8")
        preflight = scenario_preflight.TEST_COUNT
        totals = {
            "Preflight": preflight,
            "ReplicaSet": preflight + scenario_replicaset.TEST_COUNT,
            "ShardedCluster": preflight + scenario_sharded.TEST_COUNT,
            "Locking": preflight + scenario_locking.TEST_COUNT,
            "Administrator recovery suite": preflight + scenario_admin.TEST_COUNT,
            "Complete acceptance run": (
                preflight
                + scenario_replicaset.TEST_COUNT
                + scenario_sharded.TEST_COUNT
                + scenario_locking.TEST_COUNT
                + scenario_admin.TEST_COUNT
            ),
        }

        for label, total in totals.items():
            with self.subTest(profile=label):
                self.assertIn(
                    f"{label} — {total} total checks",
                    text,
                )


if __name__ == "__main__":
    unittest.main()
