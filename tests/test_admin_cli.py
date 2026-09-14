"""Unit tests for the administrator-only privateWorkerReplacement CLI."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

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

    def test_managed_resources_accepts_verbose_flag(self) -> None:
        """The forensic object-name dump is opt-in through --verbose."""

        args = admin_cli.build_parser().parse_args(
            ["ListManagedResources", "--verbose"]
        )
        self.assertTrue(args.verbose)

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

    def test_admin_worker_arguments_preserve_each_command_contract(self) -> None:
        reconcile = admin_cli.build_parser().parse_args(["Reconcile"])
        recover_lock = admin_cli.build_parser().parse_args(
            ["RecoverDeploymentLock", "SC9", "--confirm"]
        )
        recover_orphans = admin_cli.build_parser().parse_args(
            ["RecoverOrphanedResources", "--confirm"]
        )

        self.assertEqual(
            admin_cli._admin_worker_arguments(reconcile),
            ["Reconcile"],
        )
        self.assertEqual(
            admin_cli._admin_worker_arguments(recover_lock),
            ["RecoverDeploymentLock", "SC9", "--confirm"],
        )
        self.assertEqual(
            admin_cli._admin_worker_arguments(recover_orphans),
            ["RecoverOrphanedResources", "--confirm"],
        )
        self.assertEqual(
            admin_cli._admin_operation_scope(reconcile),
            "controller-state",
        )
        self.assertEqual(
            admin_cli._admin_operation_scope(recover_lock),
            "SC9",
        )

    def test_all_admin_mutations_are_background_operations(self) -> None:
        self.assertEqual(
            admin_cli.ADMIN_ASYNC_COMMANDS,
            {
                "RecoverDeploymentLock",
                "RecoverOrphanedResources",
                "Reconcile",
            },
        )

    def test_reconcile_foreground_process_launches_admin_worker(self) -> None:
        state = {
            "operation_id": "abc123",
            "command": "Reconcile",
            "deployment": "controller-state",
        }

        with (
            mock.patch.object(admin_cli, "load_config", return_value={}),
            mock.patch.object(admin_cli, "configure_logging"),
            mock.patch.object(admin_cli, "log_event"),
            mock.patch.object(admin_cli, "launch_operation", return_value=state) as launch,
            mock.patch.object(
                admin_cli,
                "admin_submission_instructions",
                return_value="accepted",
            ),
            mock.patch.object(admin_cli, "VaultClient") as vault_client,
            mock.patch("builtins.print"),
        ):
            result = admin_cli.main(["Reconcile"])

        self.assertEqual(result, 0)
        vault_client.assert_not_called()
        launch.assert_called_once()
        self.assertEqual(launch.call_args.kwargs["command"], "Reconcile")
        self.assertEqual(
            launch.call_args.kwargs["deployment"],
            "controller-state",
        )
        self.assertEqual(
            launch.call_args.kwargs["worker_arguments"],
            ["Reconcile"],
        )

    def test_lock_recovery_without_confirmation_never_launches_worker(self) -> None:
        with (
            mock.patch.object(admin_cli, "load_config", return_value={}),
            mock.patch.object(admin_cli, "configure_logging"),
            mock.patch.object(admin_cli, "log_event"),
            mock.patch.object(admin_cli, "launch_operation") as launch,
            mock.patch("builtins.print"),
        ):
            result = admin_cli.main(["RecoverDeploymentLock", "SC9"])

        self.assertEqual(result, 1)
        launch.assert_not_called()

    def test_admin_mutations_use_controller_state_lock(self) -> None:
        action = mock.Mock()
        with mock.patch.object(
            admin_cli,
            "controller_state_mutation_lock",
        ) as lock:
            admin_cli._run_admin_action({}, "Reconcile", action)

        lock.assert_called_once_with({}, "Reconcile")
        action.assert_called_once_with()

    def test_admin_help_identifies_operator_interface(self) -> None:
        help_text = admin_cli.build_parser().format_help()
        self.assertIn("platform administration interface", help_text)
        self.assertIn("ListManagedResources", help_text)
        self.assertIn("ListOperation", help_text)
        self.assertIn("RecoverDeploymentLock", help_text)
        self.assertIn("RecoverOrphanedResources", help_text)
        self.assertIn("Reconcile", help_text)
        self.assertIn("detached background operations", help_text)
        self.assertIn("Operation ID", help_text)
        self.assertIn("NOT the DBaaS end-user interface", help_text)
        self.assertIn("./dev.config", help_text)
        self.assertNotIn("Git/Terraform", help_text)


    def test_long_running_admin_help_explains_async_monitoring(self) -> None:
        parser = admin_cli.build_parser()
        subparsers = next(
            action
            for action in parser._actions
            if getattr(action, "choices", None)
            and "ListManagedResources" in action.choices
        )

        for command in (
            "Reconcile",
            "RecoverDeploymentLock",
            "RecoverOrphanedResources",
        ):
            with self.subTest(command=command):
                text = " ".join(
                    subparsers.choices[command].format_help().split()
                )
                self.assertIn("Operation ID", text)
                self.assertIn("ListOperation", text)

    def test_orphan_recovery_help_explains_non_terraform_cleanup(self) -> None:
        parser = admin_cli.build_parser()
        subparsers = next(
            action
            for action in parser._actions
            if getattr(action, "choices", None)
            and "RecoverOrphanedResources" in action.choices
        )
        text = " ".join(
            subparsers.choices[
                "RecoverOrphanedResources"
            ].format_help().split()
        )
        self.assertIn("Operator/Helm", text)
        self.assertIn("outside Terraform state", text)

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
        self.assertIn("Operator/Helm", text)
        self.assertIn("Permanent controller infrastructure", text)
        self.assertIn("--verbose", text)

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
