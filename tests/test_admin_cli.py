"""Unit tests for the administrator-only privateWorkerReplacement CLI."""

from __future__ import annotations

import unittest
from pathlib import Path

from privateWorkerReplacement import admin_cli


class AdminCliTests(unittest.TestCase):
    """Verify the operator interface contains recovery tools and clear boundaries."""

    def test_admin_parser_exposes_only_admin_commands(self) -> None:
        parser = admin_cli.build_parser()
        expected = {
            "ListManagedResources",
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

    def test_every_admin_command_has_detailed_help_and_examples(self) -> None:
        """No administrator command may degrade to a name-only help stub."""

        parser = admin_cli.build_parser()
        subparsers = next(
            action
            for action in parser._actions
            if getattr(action, "choices", None)
            and "ListManagedResources" in action.choices
        )

        for command, command_parser in subparsers.choices.items():
            with self.subTest(command=command):
                help_text = command_parser.format_help()
                self.assertTrue(command_parser.description)
                self.assertGreater(len(command_parser.description.split()), 8)
                self.assertIn("Examples:", help_text)
                self.assertIn("privateWorkerReplacementAdmin.py", help_text)

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

    def test_list_resources_alias_is_not_supported(self) -> None:
        parser = admin_cli.build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["ListResources"])

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
        self.assertIn("ListManagedResources", help_text)
        self.assertIn("ListOperation", help_text)
        self.assertIn("RecoverDeploymentLock", help_text)
        self.assertIn("RecoverOrphanedResources", help_text)
        self.assertIn("Reconcile", help_text)
        self.assertIn("NOT the DBaaS end-user interface", help_text)
        self.assertIn("./dev.config", help_text)
        self.assertNotIn("Git/Terraform", help_text)

    def test_managed_resource_help_explains_attention_semantics(self) -> None:
        parser = admin_cli.build_parser()
        subparsers = next(
            action
            for action in parser._actions
            if getattr(action, "choices", None)
            and "ListManagedResources" in action.choices
        )
        text = " ".join(
            subparsers.choices["ListManagedResources"].format_help().split()
        )
        self.assertIn("Vault, Kubernetes, Terraform backend state, and Ops Manager", text)
        self.assertIn("ATTENTION REQUIRED", text)
        self.assertIn("Permanent controller infrastructure", text)

    def test_admin_default_config_is_current_directory_file(self) -> None:
        args = admin_cli.build_parser().parse_args(["ListOperations"])
        self.assertEqual(args.config, "./dev.config")
        self.assertEqual(
            admin_cli.DEFAULT_CONFIG_DISPLAY,
            "./dev.config",
        )
        self.assertEqual(
            admin_cli.DEFAULT_CONFIG,
            Path("dev.config"),
        )


if __name__ == "__main__":
    unittest.main()
