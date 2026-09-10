"""Regression tests for administrator managed-resource status presentation."""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from terraform_controller import admin_status
from helpers import FakeVault


class AdminStatusTests(unittest.TestCase):
    """Resource presence is inventory information, not a health alarm."""

    def _capture(self, resources: dict[str, list[str]]) -> str:
        output = io.StringIO()
        with (
            patch.object(admin_status, "managed_resource_inventory", return_value=resources),
            redirect_stdout(output),
        ):
            admin_status.list_managed_resources({}, FakeVault({}))
        return output.getvalue()

    def test_empty_inventory_reports_clean(self) -> None:
        resources = {
            "managed_deployments": [],
            "mongodb_resources": [],
            "mongodb_users": [],
            "pvcs": [],
            "pvs": [],
            "deployment_locks": [],
        }

        text = self._capture(resources)

        self.assertIn("Status: CLEAN", text)
        self.assertNotIn("ATTENTION REQUIRED", text)

    def test_nonempty_inventory_reports_neutral_status_and_names(self) -> None:
        resources = {
            "managed_deployments": ["RS7"],
            "mongodb_resources": ["rs7"],
            "mongodb_users": ["tc-rs7-admin"],
            "pvcs": ["data-rs7-0"],
            "pvs": ["rs7-0"],
            "deployment_locks": [],
        }

        text = self._capture(resources)

        self.assertIn("Status: MANAGED RESOURCES PRESENT", text)
        self.assertNotIn("ATTENTION REQUIRED", text)
        self.assertIn("RS7", text)
        self.assertIn("tc-rs7-admin", text)
        self.assertIn("data-rs7-0", text)
        self.assertIn("rs7-0", text)


if __name__ == "__main__":
    unittest.main()
