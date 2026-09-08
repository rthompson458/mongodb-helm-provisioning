"""Read and validate terraformController.config.

The configuration file is intentionally the single place where environment
specific values live: Vault address, Kubernetes context, Terraform repository,
MongoDB defaults, sharding defaults, storage, timeouts, and logging.

This module converts text from the INI file into a typed Python dictionary.
It fails early on invalid values so later lifecycle code does not need to keep
re-checking basic configuration rules.
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


def _expand_path(value: str) -> Path:
    """Expand ~ and environment variables without deciding relative location."""
    return Path(os.path.expandvars(os.path.expanduser(value.strip())))


def _resolve_config_relative_path(value: str, config_directory: Path) -> Path:
    """Resolve a config path exactly once.

    Absolute paths stay absolute.
    Relative paths are resolved from the directory containing the config file,
    not from whichever directory the user happened to run the controller from.
    """
    candidate = _expand_path(value)
    if candidate.is_absolute():
        return candidate
    return (config_directory / candidate).resolve()


def load_config(path: Path) -> dict[str, Any]:
    """Load, validate, normalize, and return all controller configuration.

    The returned dictionary is the internal configuration contract used by the
    rest of the package. Paths and environment variables are expanded here so
    downstream modules receive ready-to-use values.
    """
    path = path.expanduser().resolve()
    if not path.exists():
        raise ControllerError(f"Configuration file does not exist: {path}")

    config_directory = path.parent

    # Disable ConfigParser's old-style % interpolation. Logging filename
    # formats legitimately contain strftime tokens such as %Y%m%d.
    p = configparser.ConfigParser(interpolation=None)
    p.read(path, encoding="utf-8")

    # Keep the required-field list in one place. Missing configuration is
    # easier to diagnose here than after Terraform has already started.
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
        "Logging": ["enabled", "level", "directory", "mode"],
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

    # Storage has two supported models. static-local needs a host path and node
    # name; dynamic delegates volume provisioning to a StorageClass.
    storage_mode = p.get("Storage", "mode").strip().lower()
    if storage_mode not in {"static-local", "dynamic"}:
        raise ControllerError("Storage mode must be 'static-local' or 'dynamic'.")
    if storage_mode == "static-local":
        for key in ("base_path", "node_name"):
            if not p.get("Storage", key, fallback="").strip():
                raise ControllerError(
                    f"Storage '{key}' is required when mode is static-local."
                )

    # Logging intentionally has only two write modes. This makes the behavior
    # easy for an operator to understand from the config file.
    logging_mode = p.get("Logging", "mode").strip().lower()
    if logging_mode not in {"append", "overwrite"}:
        raise ControllerError("Logging mode must be 'append' or 'overwrite'.")

    logging_level = p.get("Logging", "level").strip().upper()
    if logging_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ControllerError(
            "Logging level must be DEBUG, INFO, WARNING, ERROR, or CRITICAL."
        )

    filename_format = p.get("Logging", "filename_format", fallback="").strip()

    # filename_format is deliberately a file name only. Keeping path selection
    # in the separate directory setting prevents surprising path traversal.
    if "/" in filename_format or "\\" in filename_format:
        raise ControllerError(
            "Logging filename_format must be a file name only. "
            "Put directory information in Logging.directory."
        )

    expand = lambda v: os.path.expandvars(os.path.expanduser(v.strip()))

    return {
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
        "logging_enabled": _bool(
            p.get("Logging", "enabled"), "Logging.enabled"
        ),
        "logging_level": logging_level,
        "logging_directory": str(
            _resolve_config_relative_path(
                p.get("Logging", "directory"), config_directory
            )
        ),
        "logging_mode": logging_mode,
        "logging_filename_format": filename_format,
    }
