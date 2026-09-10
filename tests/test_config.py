"""Unit tests for privateWorkerReplacement configuration validation."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from privateWorkerReplacement.common import ControllerError
from privateWorkerReplacement.config import load_config


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
cache_directory = ~/.cache/privateWorkerReplacement
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
"""


class ConfigTests(unittest.TestCase):
    """Verify required fields and important safety ranges."""

    def _load(self, text: str):
        with tempfile.TemporaryDirectory() as temp:
            config_dir = Path(temp) / "config-home"
            config_dir.mkdir()
            path = config_dir / "privateWorkerReplacement.config"
            path.write_text(text, encoding="utf-8")
            return load_config(path), config_dir.resolve(), path.resolve()

    def test_valid_config_loads_typed_values(self) -> None:
        config, config_dir, config_path = self._load(VALID_CONFIG)

        self.assertEqual(config["default_shards"], 3)
        self.assertEqual(config["default_members_per_shard"], 3)
        self.assertTrue(config["persistent"])
        self.assertEqual(Path(config["config_path"]), config_path)
        self.assertEqual(config_path.parent, config_dir)

        # Logging is convention-based now. No logging policy belongs in the
        # environment config dictionary.
        self.assertNotIn("logging_directory", config)
        self.assertNotIn("logging_mode", config)
        self.assertNotIn("logging_filename_format", config)
        self.assertNotIn("logging_enabled", config)

    def test_config_does_not_require_logging_section(self) -> None:
        config, _, _ = self._load(VALID_CONFIG)
        self.assertEqual(config["rotation_days"], 30)

    def test_shard_count_cannot_be_zero(self) -> None:
        text = VALID_CONFIG.replace(
            "default_shards = 3",
            "default_shards = 0",
        )
        with self.assertRaises(ControllerError):
            self._load(text)

    def test_static_local_storage_requires_node_name(self) -> None:
        text = VALID_CONFIG.replace("node_name = node-0", "node_name =")
        with self.assertRaises(ControllerError):
            self._load(text)

    def test_dynamic_storage_does_not_require_local_path_or_node(self) -> None:
        text = VALID_CONFIG.replace(
            "mode = static-local\nbase_path = /tmp/mongodb\nnode_name = node-0",
            "mode = dynamic\nbase_path =\nnode_name =",
        )
        config, _, _ = self._load(text)
        self.assertEqual(config["storage_mode"], "dynamic")


if __name__ == "__main__":
    unittest.main()
