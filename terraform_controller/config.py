"""Read and validate terraformController.config.

The configuration file contains environment-specific values needed to operate
the MongoDB DBaaS service: Vault, Terraform, Kubernetes, MongoDB defaults,
sharding defaults, storage, rotation, and runtime timeouts.

Logging is intentionally *not* configurable here.  The controller always uses
predictable daily append-only files under ``logs/`` beside this config file.
That convention is implemented in runtime_paths.py and logging_component.py.

This module converts INI text into a typed Python dictionary and fails early on
invalid values so lifecycle code can work with a clean internal contract.
"""

from __future__ import annotations

import configparser
import os
from pathlib import Path
from typing import Any

from .common import ControllerError


def _bool(value: str, label: str) -> bool:
    """Parse a human-friendly true/false value from the INI file."""

    v = value.strip().lower()
    if v in {"true", "yes", "1", "on"}:
        return True
    if v in {"false", "no", "0", "off"}:
        return False
    raise ControllerError(f"{label} must be true or false.")


def _integer(
    parser: configparser.ConfigParser,
    section: str,
    key: str,
    minimum: int,
) -> int:
    """Read an integer and enforce the minimum allowed value."""

    try:
        value = parser.getint(section, key)
    except ValueError as exc:
        raise ControllerError(f"'{key}' in [{section}] must be an integer.") from exc
    if value < minimum:
        raise ControllerError(f"'{key}' in [{section}] must be at least {minimum}.")
    return value


def load_config(path: Path) -> dict[str, Any]:
    """Load, validate, normalize, and return controller configuration.

    Paths and environment variables are expanded once here.  The normalized
    config file path is also returned because runtime logs are always stored in
    a ``logs`` directory beside the selected config file.
    """

    path = path.expanduser().resolve()
    if not path.exists():
        raise ControllerError(f"Configuration file does not exist: {path}")

    p = configparser.ConfigParser(interpolation=None)
    p.read(path, encoding="utf-8")

    # Keep the required-field list in one place. Missing configuration is much
    # easier to diagnose here than after Terraform has started changing state.
    required = {
        "Vault": ["address", "token_environment_variable", "mount", "base_path"],
        "Terraform": [
            "repository_url",
            "branch",
            "subdirectory",
            "cache_directory",
            "backend_namespace",
            "backend_secret_suffix",
        ],
        "Kubernetes": ["kubeconfig", "context", "namespace"],
        "MongoDB": [
            "ops_manager_config_map",
            "ops_manager_credentials_secret",
            "auth_database",
            "default_version",
            "default_members",
            "persistent",
            "storage_class",
            "storage_size",
        ],
        "Sharding": [
            "default_shards",
            "default_members_per_shard",
            "default_mongos",
            "default_config_servers",
        ],
        "Storage": ["mode"],
        "Rotation": ["days"],
        "Runtime": [
            "mongo_image",
            "placeholder_collection",
            "job_timeout_seconds",
            "replica_set_ready_timeout_seconds",
            "sharded_cluster_ready_timeout_seconds",
        ],
    }
    for section, keys in required.items():
        if not p.has_section(section):
            raise ControllerError(f"Missing [{section}] section in {path}.")
        for key in keys:
            if not p.get(section, key, fallback="").strip():
                raise ControllerError(f"Missing '{key}' in [{section}].")

    members = _integer(p, "MongoDB", "default_members", 1)
    default_shards = _integer(p, "Sharding", "default_shards", 1)
    members_per_shard = _integer(p, "Sharding", "default_members_per_shard", 1)
    default_mongos = _integer(p, "Sharding", "default_mongos", 1)
    default_config_servers = _integer(p, "Sharding", "default_config_servers", 1)
    rotation = _integer(p, "Rotation", "days", 1)
    job_timeout = _integer(p, "Runtime", "job_timeout_seconds", 30)
    rs_ready_timeout = _integer(p, "Runtime", "replica_set_ready_timeout_seconds", 30)
    sc_ready_timeout = _integer(
        p, "Runtime", "sharded_cluster_ready_timeout_seconds", 30
    )

    # Storage has two supported models. static-local needs an explicit host path
    # and node name; dynamic delegates volume provisioning to a StorageClass.
    storage_mode = p.get("Storage", "mode").strip().lower()
    if storage_mode not in {"static-local", "dynamic"}:
        raise ControllerError("Storage mode must be 'static-local' or 'dynamic'.")
    if storage_mode == "static-local":
        for key in ("base_path", "node_name"):
            if not p.get("Storage", key, fallback="").strip():
                raise ControllerError(
                    f"Storage '{key}' is required when mode is static-local."
                )

    expand = lambda v: os.path.expandvars(os.path.expanduser(v.strip()))

    return {
        "config_path": str(path),
        "vault_address": p.get("Vault", "address").strip().rstrip("/"),
        "vault_token_env": p.get("Vault", "token_environment_variable").strip(),
        "vault_mount": p.get("Vault", "mount").strip().strip("/"),
        "vault_base_path": p.get("Vault", "base_path").strip().strip("/"),
        "terraform_repo": p.get("Terraform", "repository_url").strip(),
        "terraform_branch": p.get("Terraform", "branch").strip(),
        "terraform_subdir": p.get("Terraform", "subdirectory").strip().strip("/"),
        "terraform_cache": Path(expand(p.get("Terraform", "cache_directory"))),
        "backend_namespace": p.get("Terraform", "backend_namespace").strip(),
        "backend_secret_suffix": p.get("Terraform", "backend_secret_suffix").strip(),
        "kubeconfig": expand(p.get("Kubernetes", "kubeconfig")),
        "kube_context": p.get("Kubernetes", "context").strip(),
        "mongodb_namespace": p.get("Kubernetes", "namespace").strip(),
        "ops_manager_config_map": p.get("MongoDB", "ops_manager_config_map").strip(),
        "ops_manager_credentials_secret": p.get(
            "MongoDB", "ops_manager_credentials_secret"
        ).strip(),
        "mongodb_auth_database": p.get("MongoDB", "auth_database").strip(),
        "default_version": p.get("MongoDB", "default_version").strip(),
        "default_members": members,
        "persistent": _bool(p.get("MongoDB", "persistent"), "persistent"),
        "storage_class": p.get("MongoDB", "storage_class").strip(),
        "storage_size": p.get("MongoDB", "storage_size").strip(),
        "default_shards": default_shards,
        "default_members_per_shard": members_per_shard,
        "default_mongos": default_mongos,
        "default_config_servers": default_config_servers,
        "storage_mode": storage_mode,
        "storage_base_path": expand(p.get("Storage", "base_path", fallback="")),
        "storage_node_name": p.get("Storage", "node_name", fallback="").strip(),
        "rotation_days": rotation,
        "mongo_image": p.get("Runtime", "mongo_image").strip(),
        "placeholder_collection": p.get("Runtime", "placeholder_collection").strip(),
        "job_timeout": job_timeout,
        "rs_ready_timeout": rs_ready_timeout,
        "sc_ready_timeout": sc_ready_timeout,
    }
