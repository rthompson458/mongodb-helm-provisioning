"""Unit tests for the end-user command-line interface."""

from __future__ import annotations

import unittest

from terraform_controller import cli


class CliTests(unittest.TestCase):
    """Verify important command shapes and safe defaults."""

    def test_add_shard_accepts_optional_count(self) -> None:
        args = cli.build_parser().parse_args(["AddShard", "SC9", "2"])
        self.assertEqual(args.deployment, "SC9")
        self.assertEqual(args.count, 2)

    def test_add_shard_defaults_to_one(self) -> None:
        args = cli.build_parser().parse_args(["AddShard", "SC9"])
        self.assertEqual(args.count, 1)

    def test_delete_shard_defaults_to_one(self) -> None:
        args = cli.build_parser().parse_args(
            ["DeleteShard", "SC9", "--confirm"]
        )
        self.assertEqual(args.count, 1)
        self.assertTrue(args.confirm)

    def test_list_shards_cluster_is_optional(self) -> None:
        args = cli.build_parser().parse_args(["ListShards"])
        self.assertIsNone(args.deployment)

    def test_database_target_supports_explicit_deployment(self) -> None:
        args = cli.build_parser().parse_args(
            ["AddDatabase", "SC9", "HouseInfo"]
        )
        self.assertEqual(args.deployment_or_database, "SC9")
        self.assertEqual(args.database, "HouseInfo")

    def test_database_target_supports_single_deployment_short_form(self) -> None:
        args = cli.build_parser().parse_args(["AddDatabase", "HouseInfo"])
        self.assertEqual(args.deployment_or_database, "HouseInfo")
        self.assertIsNone(args.database)


if __name__ == "__main__":
    unittest.main()
