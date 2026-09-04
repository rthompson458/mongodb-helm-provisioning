from __future__ import annotations

import configparser
import os
from pathlib import Path
from typing import Any

from .common import ControllerError


def _bool(value: str, label: str) -> bool:
    v = value.strip().lower()
    if v in {"true", "yes", "1", "on"}: return True
    if v in {"false", "no", "0", "off"}: return False
    raise ControllerError(f"{label} must be true or false.")


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ControllerError(f"Configuration file does not exist: {path}")
    p = configparser.ConfigParser()
    p.read(path, encoding="utf-8")
    required = {
        "Vault": ["address", "token_environment_variable", "mount", "base_path"],
        "Terraform": ["repository_url", "branch", "subdirectory", "cache_directory", "backend_namespace", "backend_secret_suffix"],
        "Kubernetes": ["kubeconfig", "context", "namespace"],
        "MongoDB": ["ops_manager_config_map", "ops_manager_credentials_secret", "auth_database", "default_version", "default_members", "persistent", "storage_class", "storage_size"],
        "Storage": ["base_path", "node_name"],
        "Rotation": ["days"],
        "Runtime": ["mongo_image", "placeholder_collection", "job_timeout_seconds", "replica_set_ready_timeout_seconds"],
    }
    for section, keys in required.items():
        if not p.has_section(section): raise ControllerError(f"Missing [{section}] section in {path}.")
        for key in keys:
            if not p.get(section, key, fallback="").strip(): raise ControllerError(f"Missing '{key}' in [{section}].")
    try:
        members = p.getint("MongoDB", "default_members")
        rotation = p.getint("Rotation", "days")
        job_timeout = p.getint("Runtime", "job_timeout_seconds")
        ready_timeout = p.getint("Runtime", "replica_set_ready_timeout_seconds")
    except ValueError as exc:
        raise ControllerError("Configured numeric values must be integers.") from exc
    if members < 1 or rotation < 1 or job_timeout < 30 or ready_timeout < 30:
        raise ControllerError("Members/rotation must be >=1 and runtime timeouts must be >=30 seconds.")
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
        "ops_manager_credentials_secret": p.get("MongoDB", "ops_manager_credentials_secret").strip(),
        "mongodb_auth_database": p.get("MongoDB", "auth_database").strip(),
        "default_version": p.get("MongoDB", "default_version").strip(),
        "default_members": members,
        "persistent": _bool(p.get("MongoDB", "persistent"), "persistent"),
        "storage_class": p.get("MongoDB", "storage_class").strip(),
        "storage_size": p.get("MongoDB", "storage_size").strip(),
        "storage_base_path": expand(p.get("Storage", "base_path")),
        "storage_node_name": p.get("Storage", "node_name").strip(),
        "rotation_days": rotation,
        "mongo_image": p.get("Runtime", "mongo_image").strip(),
        "placeholder_collection": p.get("Runtime", "placeholder_collection").strip(),
        "job_timeout": job_timeout,
        "rs_ready_timeout": ready_timeout,
    }
