"""Unit tests for the end-user command-line interface."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

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


    def test_public_help_is_customer_facing(self) -> None:
        help_text = cli.build_parser().format_help()
        self.assertIn("Terraform-driven MongoDB DBaaS controller", help_text)
        self.assertNotIn("ListOperation", help_text)
        self.assertNotIn("RecoverDeploymentLock", help_text)
        self.assertNotIn("RecoverOrphanedResources", help_text)
        self.assertNotIn("Reconcile", help_text)


    def test_public_help_shows_live_configured_shard_default(self) -> None:
        configured = cli._configured_default_shards(cli.DEFAULT_CONFIG)
        self.assertIsNotNone(configured)
        help_text = cli.build_parser(cli.DEFAULT_CONFIG).format_help()
        self.assertIn(
            f"AddShardedCluster initial shards = {configured}",
            help_text,
        )
        self.assertIn("(read from controller configuration)", help_text)
        self.assertIn("AddShard count                  = 1", help_text)
        self.assertIn("DeleteShard count               = 1", help_text)

    def test_add_sharded_cluster_help_labels_configured_default(self) -> None:
        with mock.patch.object(
            cli, "_configured_default_shards", return_value=7
        ):
            parser = cli.build_parser(cli.DEFAULT_CONFIG)

        subparsers = next(
            action
            for action in parser._actions
            if getattr(action, "choices", None)
            and "AddShardedCluster" in action.choices
        )
        help_text = subparsers.choices["AddShardedCluster"].format_help()
        normalized_help = " ".join(help_text.split())
        self.assertIn(
            "Default: 7 (read from controller configuration)",
            normalized_help,
        )
        self.assertIn("uses configured default: 7 shard(s)", normalized_help)

    def test_config_path_prescan_honors_custom_config(self) -> None:
        selected = cli._config_path_from_argv(
            ["--config", "/tmp/customer-controller.conf", "--help"]
        )
        self.assertEqual(selected, Path("/tmp/customer-controller.conf"))


    def test_add_and_delete_shard_help_state_default_one(self) -> None:
        parser = cli.build_parser(cli.DEFAULT_CONFIG)
        subparsers = next(
            action
            for action in parser._actions
            if getattr(action, "choices", None)
            and "AddShard" in action.choices
        )
        add_help = subparsers.choices["AddShard"].format_help()
        delete_help = subparsers.choices["DeleteShard"].format_help()
        self.assertIn("Optional. Default: 1.", add_help)
        self.assertIn("Optional. Default: 1.", delete_help)


if __name__ == "__main__":
    unittest.main()
