from __future__ import annotations

import json
import os
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any

from .common import ControllerError, run_process


def _require(*names: str) -> None:
    missing = [name for name in names if not shutil.which(name)]
    if missing:
        raise ControllerError("Required executable(s) not found in PATH: " + ", ".join(missing))


def _check_version() -> None:
    result = run_process(["terraform", "version", "-json"], capture=True)
    try:
        version = json.loads(result.stdout)["terraform_version"]
        major, minor = [int(x) for x in version.split(".")[:2]]
    except Exception as exc:
        raise ControllerError("Could not determine Terraform version.") from exc
    if (major, minor) < (1, 11):
        raise ControllerError(f"Terraform {version} is installed; terraformController requires 1.11 or newer.")


def _sync(config: dict[str, Any]) -> Path:
    cache: Path = config["terraform_cache"]
    branch = config["terraform_branch"]
    repo = config["terraform_repo"]
    if not (cache / ".git").exists():
        cache.parent.mkdir(parents=True, exist_ok=True)
        if cache.exists() and any(cache.iterdir()):
            raise ControllerError(f"Terraform cache exists but is not a Git repository: {cache}")
        print(f"Cloning Terraform from {repo} ...")
        run_process(["git", "clone", "--depth", "1", "--branch", branch, repo, str(cache)])
    else:
        print(f"Refreshing Terraform from GitHub branch '{branch}' ...")
        run_process(["git", "-C", str(cache), "fetch", "--depth", "1", "origin", branch])
        run_process(["git", "-C", str(cache), "reset", "--hard", "FETCH_HEAD"])
        run_process(["git", "-C", str(cache), "clean", "-fd", "-e", ".terraform"])
    tfdir = cache / config["terraform_subdir"]
    if not tfdir.is_dir():
        raise ControllerError(f"Terraform subdirectory not found: {tfdir}")
    return tfdir


def _operation_payload(operation: dict[str, Any] | None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "action": "none",
        "replica_set": "",
        "database": "",
        "members": 0,
        "nonce": "",
    }
    if operation:
        payload.update(operation)
        if payload["action"] != "none" and not payload["nonce"]:
            payload["nonce"] = uuid.uuid4().hex
    return payload


def apply_inventory(
    config: dict[str, Any],
    inventory: dict[str, dict[str, Any]],
    operation: dict[str, Any] | None = None,
) -> None:
    """Apply desired state and an optional lifecycle operation through Terraform.

    Python supplies intent and reads status. Terraform performs all managed
    MongoDB, Vault, Kubernetes, and local-storage lifecycle changes.
    """
    _require("terraform", "git", "kubectl")
    _check_version()
    tfdir = _sync(config)
    token_name = config["vault_token_env"]
    token = os.getenv(token_name, "")
    if not token:
        raise ControllerError(f"Vault token environment variable '{token_name}' is not set.")

    env = os.environ.copy()
    env.update({
        "VAULT_ADDR": config["vault_address"],
        "VAULT_TOKEN": token,
        "TF_VAR_vault_address": config["vault_address"],
        "TF_VAR_vault_mount": config["vault_mount"],
        "TF_VAR_vault_base_path": config["vault_base_path"],
        "TF_VAR_mongodb_namespace": config["mongodb_namespace"],
        "TF_VAR_ops_manager_config_map": config["ops_manager_config_map"],
        "TF_VAR_ops_manager_credentials_secret": config["ops_manager_credentials_secret"],
        "TF_VAR_mongodb_auth_database": config["mongodb_auth_database"],
        "TF_VAR_kubeconfig_path": config["kubeconfig"],
        "TF_VAR_kube_context": config["kube_context"],
        "TF_VAR_mongo_image": config["mongo_image"],
        "TF_VAR_placeholder_collection": config["placeholder_collection"],
        "TF_VAR_default_members": str(config["default_members"]),
        "TF_VAR_default_storage_class": config["storage_class"],
        "TF_VAR_default_storage_size": config["storage_size"],
        "TF_VAR_storage_base_path": config["storage_base_path"],
        "TF_VAR_storage_node_name": config["storage_node_name"],
    })

    init = [
        "terraform",
        "init",
        "-input=false",
        "-reconfigure",
        f"-backend-config=secret_suffix={config['backend_secret_suffix']}",
        f"-backend-config=namespace={config['backend_namespace']}",
        f"-backend-config=config_path={config['kubeconfig']}",
    ]
    if config["kube_context"]:
        init.append(f"-backend-config=config_context={config['kube_context']}")

    print("Initializing Terraform ...")
    run_process(init, cwd=tfdir, env=env)

    temp: Path | None = None
    try:
        payload = {
            "replica_sets": inventory,
            "operation": _operation_payload(operation),
        }
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=tfdir,
            prefix=".tc.",
            suffix=".tfvars.json",
            delete=False,
        ) as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            temp = Path(handle.name)

        print("Applying Terraform ...")
        run_process(
            ["terraform", "apply", "-input=false", "-auto-approve", f"-var-file={temp.name}"],
            cwd=tfdir,
            env=env,
        )
    finally:
        if temp and temp.exists():
            temp.unlink()
