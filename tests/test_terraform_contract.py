"""Static contract tests for Terraform lifecycle operation wiring."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


class TerraformContractTests(unittest.TestCase):
    """Keep Terraform operation validation aligned with lifecycle resources."""

    def test_lifecycle_operation_actions_are_allowed_by_variable_validation(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        main_text = (repo_root / "terraform-dbaas" / "main.tf").read_text(
            encoding="utf-8"
        )
        variables_text = (repo_root / "terraform-dbaas" / "variables.tf").read_text(
            encoding="utf-8"
        )

        resource_match = re.search(
            r'resource\s+"terraform_data"\s+"lifecycle_operation"\s*\{'
            r'.*?count\s*=\s*contains\(\[(.*?)\],\s*var\.operation\.action\)',
            main_text,
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


    def test_static_sharded_storage_records_deterministic_pvc_names(self) -> None:
        """Each Terraform PV must identify the exact MongoDB PVC it may delete."""

        repo_root = Path(__file__).resolve().parent.parent
        main_text = (repo_root / "terraform-dbaas" / "main.tf").read_text(
            encoding="utf-8"
        )
        lifecycle_text = (
            repo_root / "terraform-dbaas" / "scripts" / "lifecycle.sh"
        ).read_text(encoding="utf-8")

        self.assertIn(
            'pvc_name          = "data-${key}-${floor(ordinal / cluster.members_per_shard)}-${ordinal % cluster.members_per_shard}"',
            main_text,
        )
        self.assertIn(
            'pvc_name          = "data-${key}-config-${ordinal}"',
            main_text,
        )
        self.assertIn("TC_PVC_NAME", main_text)
        self.assertIn("claimRef:", lifecycle_text)
        self.assertIn("dbaas.pvc-name:", lifecycle_text)
        self.assertIn("Refusing storage cleanup", lifecycle_text)
        self.assertIn('TC_STORAGE_CLEANUP_TIMEOUT:-180s', lifecycle_text)


if __name__ == "__main__":
    unittest.main()
