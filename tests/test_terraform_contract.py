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


if __name__ == "__main__":
    unittest.main()
