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



    def test_async_worker_arguments_preserve_delete_confirmation(self) -> None:
        args = cli.build_parser().parse_args(
            ["DeleteShard", "SC9", "3", "--confirm"]
        )
        self.assertEqual(
            cli._async_worker_arguments(args),
            ["DeleteShard", "SC9", "3", "--confirm"],
        )

    def test_internal_operation_worker_flag_is_hidden_but_parseable(self) -> None:
        args = cli.build_parser().parse_args(
            ["--_operation-worker", "abc123", "AddShard", "SC9", "2"]
        )
        self.assertEqual(args._operation_worker, "abc123")
        self.assertEqual(args.command, "AddShard")


    def test_async_submission_rejects_missing_confirmation(self) -> None:
        args = cli.build_parser().parse_args(["DeleteShard", "SC9", "1"])
        with self.assertRaises(cli.ControllerError):
            cli._validate_async_submission(args)

    def test_async_submission_rejects_zero_shards(self) -> None:
        args = cli.build_parser().parse_args(["AddShard", "SC9", "0"])
        with self.assertRaises(cli.ControllerError):
            cli._validate_async_submission(args)







    def test_public_parser_does_not_expose_administrator_commands(self) -> None:
        parser = cli.build_parser()
        admin_commands = {
            "ListOperation",
            "ListOperations",
            "RecoverDeploymentLock",
            "RecoverOrphanedResources",
            "Reconcile",
        }
        for command in admin_commands:
            with self.subTest(command=command):
                with self.assertRaises(SystemExit):
                    parser.parse_args([command])

    def test_public_async_feedback_uses_service_status_commands(self) -> None:
        args = cli.build_parser().parse_args(["AddShard", "SC9", "2"])
        label, status, status_args = cli._public_async_feedback(args)
        self.assertEqual(label, "ShardedCluster")
        self.assertEqual(status, "Topology change requested")
        self.assertEqual(status_args, ["ListShards", "SC9"])


if __name__ == "__main__":
    unittest.main()
