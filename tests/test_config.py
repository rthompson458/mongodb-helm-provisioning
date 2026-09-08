"""Unit tests for terraformController configuration validation."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from terraform_controller.config import load_config
from terraform_controller.common import ControllerError


VALID_CONFIG = """
[Vault]
address = http://127.0.0.1:8200
token_environment_variable = VAULT_TOKEN
mount = secret
base_path = mongodb

[Terraform]
repository_url = https://example.invalid/repo.git
branch = main
subdirectory = terraform-dbaas
cache_directory = ~/.cache/terraformController
backend_namespace = mongodb
backend_secret_suffix = test

[Kubernetes]
kubeconfig = ~/.kube/config
context = k3d-nix-dev
namespace = mongodb

[MongoDB]
ops_manager_config_map = my-project
ops_manager_credentials_secret = organization-secret
auth_database = admin
default_version = 8.0.29
default_members = 3
persistent = true
storage_class = mongodb-data-local
storage_size = 16Gi

[Sharding]
default_shards = 3
default_members_per_shard = 3
default_mongos = 2
default_config_servers = 3

[Storage]
mode = static-local
base_path = /tmp/mongodb
node_name = node-0

[Rotation]
days = 30

[Runtime]
mongo_image = mongo:8.0
placeholder_collection = __dbaas_metadata
job_timeout_seconds = 300
replica_set_ready_timeout_seconds = 900
sharded_cluster_ready_timeout_seconds = 1800

[Logging]
enabled = true
level = INFO
directory = /tmp/terraformController-logs
mode = per-run
filename_pattern = terraformController-%Y%m%d-%H%M%S-{pid}.jsonl
retention_days = 30
"""


class ConfigTests(unittest.TestCase):
    """Verify required fields and important safety ranges."""

    def _load(self, text: str):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "controller.config"
            path.write_text(text, encoding="utf-8")
            return load_config(path)

    def test_valid_config_loads_typed_values(self) -> None:
        config = self._load(VALID_CONFIG)
        self.assertEqual(config["default_shards"], 3)
        self.assertEqual(config["default_members_per_shard"], 3)
        self.assertTrue(config["persistent"])
        self.assertEqual(config["logging_mode"], "per-run")

    def test_shard_count_cannot_be_zero(self) -> None:
        text = VALID_CONFIG.replace(
            "default_shards = 3", "default_shards = 0"
        )
        with self.assertRaises(ControllerError):
            self._load(text)

    def test_static_local_storage_requires_node_name(self) -> None:
        text = VALID_CONFIG.replace("node_name = node-0", "node_name =")
        with self.assertRaises(ControllerError):
            self._load(text)

    def test_invalid_logging_mode_is_rejected(self) -> None:
        text = VALID_CONFIG.replace("mode = per-run", "mode = mystery")
        with self.assertRaises(ControllerError):
            self._load(text)


if __name__ == "__main__":
    unittest.main()
