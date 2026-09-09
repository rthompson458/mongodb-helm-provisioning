"""Unit tests for the administrator-only terraformController CLI."""

from __future__ import annotations

import unittest

from terraform_controller import admin_cli


class AdminCliTests(unittest.TestCase):
    """Verify the operator interface contains recovery tools and clear boundaries."""

    def test_admin_parser_exposes_only_admin_commands(self) -> None:
        parser = admin_cli.build_parser()
        expected = {
            "ListOperation",
            "ListOperations",
            "RecoverDeploymentLock",
            "RecoverOrphanedResources",
            "Reconcile",
        }
        for command in expected:
            with self.subTest(command=command):
                args = (
                    parser.parse_args([command, "SC9", "--confirm"])
                    if command == "RecoverDeploymentLock"
                    else parser.parse_args([command, "--confirm"])
                    if command == "RecoverOrphanedResources"
                    else parser.parse_args([command, "abc123"])
                    if command == "ListOperation"
                    else parser.parse_args([command])
                )
                self.assertEqual(args.command, command)

    def test_admin_parser_does_not_expose_customer_lifecycle_commands(self) -> None:
        parser = admin_cli.build_parser()
        for command in (
            "AddReplicaSet",
            "DeleteReplicaSet",
            "AddShardedCluster",
            "DeleteShardedCluster",
            "AddShard",
            "DeleteShard",
            "AddDatabase",
            "DeleteDatabase",
        ):
            with self.subTest(command=command):
                with self.assertRaises(SystemExit):
                    parser.parse_args([command])

    def test_recover_deployment_lock_requires_confirmation_shape(self) -> None:
        args = admin_cli.build_parser().parse_args(
            ["RecoverDeploymentLock", "SC9", "--confirm"]
        )
        self.assertEqual(args.deployment, "SC9")
        self.assertTrue(args.confirm)

    def test_orphan_recovery_requires_confirmation_shape(self) -> None:
        args = admin_cli.build_parser().parse_args(
            ["RecoverOrphanedResources", "--confirm"]
        )
        self.assertTrue(args.confirm)

    def test_orphan_recovery_worker_arguments_preserve_confirmation(self) -> None:
        args = admin_cli.build_parser().parse_args(
            ["RecoverOrphanedResources", "--confirm"]
        )
        self.assertEqual(
            admin_cli._recover_orphans_worker_arguments(args),
            ["RecoverOrphanedResources", "--confirm"],
        )


    def test_admin_help_identifies_operator_interface(self) -> None:
        help_text = admin_cli.build_parser().format_help()
        self.assertIn("platform administration interface", help_text)
        self.assertIn("ListOperation", help_text)
        self.assertIn("RecoverDeploymentLock", help_text)
        self.assertIn("RecoverOrphanedResources", help_text)
        self.assertIn("Reconcile", help_text)
        self.assertIn("NOT the DBaaS end-user interface", help_text)


if __name__ == "__main__":
    unittest.main()
