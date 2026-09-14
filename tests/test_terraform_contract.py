"""Static contract tests for Terraform lifecycle operation wiring."""

# MAINTAINER READING GUIDE
# Protects the static contract between Python inputs and terraform-dbaas variables, resources, and lifecycle actions.
# Treat these tests as executable design documentation. A failing assertion
# should identify which controller contract changed, not merely that text moved.


from __future__ import annotations

import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
TERRAFORM_ROOT = REPO_ROOT / "terraform-dbaas"


def _terraform_root_text() -> str:
    """Return the complete Terraform root module independent of file layout."""

    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(TERRAFORM_ROOT.glob("*.tf"))
    )


class TerraformContractTests(unittest.TestCase):
    """Keep Terraform operation validation aligned with lifecycle resources."""

    def test_lifecycle_operation_actions_are_allowed_by_variable_validation(self) -> None:
        root_text = _terraform_root_text()
        variables_text = (TERRAFORM_ROOT / "variables.tf").read_text(
            encoding="utf-8"
        )

        resource_match = re.search(
            r'resource\s+"terraform_data"\s+"lifecycle_operation"\s*\{'
            r'.*?count\s*=\s*contains\(\[(.*?)\],\s*var\.operation\.action\)',
            root_text,
            re.DOTALL,
        )
        self.assertIsNotNone(resource_match, "Could not locate lifecycle_operation action list.")
        lifecycle_actions = set(re.findall(r'"([^"]+)"', resource_match.group(1)))

        validation_match = re.search(
            r'variable\s+"operation"\s*\{.*?validation\s*\{'
            r'.*?condition\s*=\s*contains\(\[(.*?)\],\s*var\.operation\.action\)',
            variables_text,
            re.DOTALL,
        )
        self.assertIsNotNone(validation_match, "Could not locate operation validation allow-list.")
        allowed_actions = set(re.findall(r'"([^"]+)"', validation_match.group(1)))

        missing = lifecycle_actions - allowed_actions
        self.assertFalse(
            missing,
            "Terraform lifecycle actions missing from variables.tf validation: "
            + ", ".join(sorted(missing)),
        )


    def test_root_module_is_split_by_responsibility(self) -> None:
        """Keep the large root module organized without hiding resources in main.tf."""

        expected_files = {
            "locals.tf",
            "ops-manager.tf",
            "storage.tf",
            "deployments.tf",
            "metadata.tf",
            "controller-admin.tf",
            "database-management.tf",
            "database-accounts.tf",
            "lifecycle.tf",
            "outputs.tf",
            "providers.tf",
            "variables.tf",
            "versions.tf",
        }
        actual_files = {path.name for path in TERRAFORM_ROOT.glob("*.tf")}
        self.assertTrue(
            expected_files.issubset(actual_files),
            "Terraform responsibility files are missing: "
            + ", ".join(sorted(expected_files - actual_files)),
        )

        main_text = (TERRAFORM_ROOT / "main.tf").read_text(encoding="utf-8")
        self.assertIn("Root Terraform module organization", main_text)
        self.assertIsNone(
            re.search(
                r'(?m)^\s*(?:data|resource|ephemeral|output)\s+"',
                main_text,
            ),
            "main.tf is an index only; production blocks belong in responsibility files.",
        )

    def test_core_terraform_addresses_survive_file_reorganization(self) -> None:
        """Moving blocks between .tf files must not rename stateful addresses."""

        expected = {
            "data.kubernetes_config_map_v1.ops_manager_source",
            "ephemeral.random_password.controller_admin",
            "ephemeral.random_password.database_account",
            "helm_release.mongodb_management",
            "kubernetes_config_map_v1.controller_ops_manager_projects",
            "kubernetes_manifest.controller_admin",
            "kubernetes_manifest.database_account",
            "kubernetes_manifest.replica_set",
            "kubernetes_manifest.sharded_cluster",
            "kubernetes_secret_v1.controller_admin_password",
            "kubernetes_secret_v1.database_account_password",
            "terraform_data.lifecycle_operation",
            "terraform_data.replica_set_storage",
            "terraform_data.sharded_cluster_config_storage",
            "terraform_data.sharded_cluster_shard_storage",
            "vault_kv_secret_v2.controller_admin",
            "vault_kv_secret_v2.database_account",
            "vault_kv_secret_v2.database_metadata",
            "vault_kv_secret_v2.replica_set_metadata",
        }

        actual: set[str] = set()
        for block_type, resource_type, name in re.findall(
            r'(?m)^\s*(data|resource|ephemeral)\s+"([^"]+)"\s+"([^"]+)"',
            _terraform_root_text(),
        ):
            prefix = "data." if block_type == "data" else ""
            if block_type == "ephemeral":
                prefix = "ephemeral."
            actual.add(f"{prefix}{resource_type}.{name}")

        missing = expected - actual
        self.assertFalse(
            missing,
            "Core Terraform addresses changed during file organization: "
            + ", ".join(sorted(missing)),
        )

    def test_static_sharded_storage_records_deterministic_pvc_names(self) -> None:
        """Each Terraform PV must identify the exact MongoDB PVC it may delete."""

        root_text = _terraform_root_text()
        lifecycle_text = (
            TERRAFORM_ROOT / "scripts" / "lifecycle.sh"
        ).read_text(encoding="utf-8")

        self.assertIn(
            'pvc_name          = "data-${key}-${floor(ordinal / cluster.members_per_shard)}-${ordinal % cluster.members_per_shard}"',
            root_text,
        )
        self.assertIn(
            'pvc_name          = "data-${key}-config-${ordinal}"',
            root_text,
        )
        self.assertIn("TC_PVC_NAME", root_text)
        self.assertIn("claimRef:", lifecycle_text)
        self.assertIn("dbaas.pvc-name:", lifecycle_text)
        self.assertIn("Refusing storage cleanup", lifecycle_text)
        self.assertIn('TC_STORAGE_CLEANUP_TIMEOUT:-180s', lifecycle_text)


if __name__ == "__main__":
    unittest.main()
