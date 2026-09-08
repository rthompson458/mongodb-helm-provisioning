"""Unit tests for terraformController configuration validation."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from terraform_controller.common import ControllerError
from terraform_controller.config import load_config


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
directory = logs
mode = append
filename_format =
"""


class ConfigTests(unittest.TestCase):
    """Verify required fields and important safety ranges."""

    def _load(self, text: str):
        with tempfile.TemporaryDirectory() as temp:
            config_dir = Path(temp) / "config-home"
            config_dir.mkdir()
            path = config_dir / "terraformController.config"
            path.write_text(text, encoding="utf-8")
            return load_config(path), config_dir.resolve()

    def test_valid_config_loads_typed_values(self) -> None:
        config, config_dir = self._load(VALID_CONFIG)

        self.assertEqual(config["default_shards"], 3)
        self.assertEqual(config["default_members_per_shard"], 3)
        self.assertTrue(config["persistent"])
        self.assertEqual(config["logging_mode"], "append")
        self.assertEqual(config["logging_filename_format"], "")
        self.assertEqual(
            Path(config["logging_directory"]),
            config_dir / "logs",
        )

    def test_absolute_logging_directory_remains_absolute(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            absolute_logs = str((Path(temp) / "absolute-logs").resolve())
            text = VALID_CONFIG.replace(
                "directory = logs",
                f"directory = {absolute_logs}",
            )
            config, _ = self._load(text)
            self.assertEqual(
                Path(config["logging_directory"]),
                Path(absolute_logs),
            )

    def test_logging_filename_accepts_standard_strftime_tokens(self) -> None:
        text = VALID_CONFIG.replace(
            "filename_format =",
            "filename_format = Controller-%Y%m%d-%H%M.log",
        )
        config, _ = self._load(text)
        self.assertEqual(
            config["logging_filename_format"],
            "Controller-%Y%m%d-%H%M.log",
        )

    def test_logging_filename_must_not_contain_directory(self) -> None:
        text = VALID_CONFIG.replace(
            "filename_format =",
            "filename_format = nested/Controller-%Y%m%d.log",
        )
        with self.assertRaises(ControllerError):
            self._load(text)

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

    def test_invalid_logging_mode_is_rejected(self) -> None:
        text = VALID_CONFIG.replace("mode = append", "mode = per-run")
        with self.assertRaises(ControllerError):
            self._load(text)


if __name__ == "__main__":
    unittest.main()
