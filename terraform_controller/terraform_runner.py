"""Prepare and execute the Terraform module used by terraformController.

This file is the bridge between Python orchestration and Terraform.  Python
builds desired-state JSON and a small one-shot operation description, then this
module runs Terraform.  The lifecycle resource/script inside Terraform performs
imperative MongoDB/storage/lock work when needed.

Important rule for maintainers:
    Do not add direct MongoDB, Vault, or Kubernetes mutations here.
    This module should only prepare Terraform inputs and execute Terraform.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .common import ControllerError, run_process
from .logging_component import log_event


def _require(*names: str) -> None:
    """Fail early if an external executable needed by Terraform is missing."""
    missing = [name for name in names if not shutil.which(name)]
    if missing:
        raise ControllerError("Required executable(s) not found in PATH: " + ", ".join(missing))


def _check_version() -> None:
    """Require the Terraform version needed for ephemeral/write-only features."""
    result = run_process(["terraform", "version", "-json"], capture=True)
    try:
        version = json.loads(result.stdout)["terraform_version"]
        major, minor = [int(x) for x in version.split(".")[:2]]
    except Exception as exc:
        raise ControllerError("Could not determine Terraform version.") from exc
    if (major, minor) < (1, 11):
        raise ControllerError(
            f"Terraform {version} is installed; terraformController requires 1.11 or newer."
        )



@contextmanager
def _terraform_execution_lock(config: dict[str, Any]):
    """Serialize all local Terraform cache/workdir activity across processes.

    Every controller command shares one disposable Git checkout and one
    terraform-dbaas/.terraform provider directory. Detached async workers make
    overlapping commands much more likely, so git refresh, terraform init, and
    terraform apply must be one cross-process critical section.

    flock is process-safe on Linux/WSL and is automatically released if a
    worker exits or is killed. The lock file lives beside the cache so git
    reset/clean cannot remove it.
    """

    cache: Path = config["terraform_cache"]
    cache.parent.mkdir(parents=True, exist_ok=True)
    lock_path = cache.parent / f".{cache.name}.terraformController.lock"

    with lock_path.open("a+", encoding="utf-8") as handle:
        print("Waiting for exclusive Terraform execution access ...")
        log_event("terraform.execution_lock.waiting", lock_file=str(lock_path))
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.seek(0)
            handle.truncate()
            handle.write(f"pid={os.getpid()}\n")
            handle.flush()
            log_event(
                "terraform.execution_lock.acquired",
                lock_file=str(lock_path),
                pid=os.getpid(),
            )
            yield
        finally:
            log_event(
                "terraform.execution_lock.released",
                lock_file=str(lock_path),
                pid=os.getpid(),
            )
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _sync(config: dict[str, Any]) -> Path:
    """Clone or hard-refresh the configured Terraform source repository.

    The cache is disposable by design.  A hard reset prevents stale local edits
    from silently becoming part of a controller operation.
    """
    cache: Path = config["terraform_cache"]
    branch = config["terraform_branch"]
    repo = config["terraform_repo"]
    if not (cache / ".git").exists():
        cache.parent.mkdir(parents=True, exist_ok=True)
        if cache.exists() and any(cache.iterdir()):
            raise ControllerError(f"Terraform cache exists but is not a Git repository: {cache}")
        print(f"Cloning Terraform from {repo} ...")
        log_event("terraform.repository.clone", repository=repo, branch=branch, cache=str(cache))
        run_process(["git", "clone", "--depth", "1", "--branch", branch, repo, str(cache)])
    else:
        print(f"Refreshing Terraform from GitHub branch '{branch}' ...")
        log_event("terraform.repository.refresh", repository=repo, branch=branch, cache=str(cache))
        run_process(["git", "-C", str(cache), "fetch", "--depth", "1", "origin", branch])
        run_process(["git", "-C", str(cache), "reset", "--hard", "FETCH_HEAD"])
        run_process(["git", "-C", str(cache), "clean", "-fd", "-e", ".terraform"])

    tfdir = cache / config["terraform_subdir"]
    if not tfdir.is_dir():
        raise ControllerError(f"Terraform subdirectory not found: {tfdir}")
    return tfdir


def _operation_payload(operation: dict[str, Any] | None) -> dict[str, Any]:
    """Return a complete Terraform operation object with safe defaults.

    A random nonce forces terraform_data.lifecycle_operation to execute again
    even when the action name and target happen to match a previous command.
    """
    payload: dict[str, Any] = {
        "action": "none",
        "deployment": "",
        "deployment_type": "",
        "database": "",
        "members": 0,
        "lock_category": "",
        "lock_action": "",
        "operation_id": "",
        "start_shards": 0,
        "target_shards": 0,
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
    """Apply desired deployment state and an optional one-shot operation.

    High-level sequence:
      1. Check local tools and Terraform version.
      2. Acquire the cross-process Terraform execution lock.
      3. Refresh the Terraform module from GitHub.
      4. Pass environment/config values as TF_VAR_* variables.
      5. Write desired state to a temporary .tfvars.json file.
      6. Run terraform init and terraform apply.
      7. Delete the temporary input file and release the execution lock.

    Password values are never written into this temporary JSON by Python.
    """

    _require("terraform", "git", "kubectl", "bash", "python3")
    op = _operation_payload(operation)
    if config["storage_mode"] == "static-local":
        _require("docker")

    _check_version()

    # The shared Git checkout, .terraform provider directory, and Terraform
    # backend workflow are treated as one transaction. This prevents one async
    # worker from resetting the checkout or reinstalling a provider while
    # another worker is actively executing it ("text file busy").
    with _terraform_execution_lock(config):
        _apply_inventory_locked(config, inventory, op)


def _apply_inventory_locked(
    config: dict[str, Any],
    inventory: dict[str, dict[str, Any]],
    op: dict[str, Any],
) -> None:
    """Run one Terraform refresh/init/apply while execution access is held."""

    tfdir = _sync(config)
    token_name = config["vault_token_env"]
    token = os.getenv(token_name, "")
    if not token:
        raise ControllerError(
            f"Vault token environment variable '{token_name}' is not set."
        )

    env = os.environ.copy()
    env.update(
        {
            "VAULT_ADDR": config["vault_address"],
            "VAULT_TOKEN": token,
            "TF_VAR_vault_address": config["vault_address"],
            "TF_VAR_vault_mount": config["vault_mount"],
            "TF_VAR_vault_base_path": config["vault_base_path"],
            "TF_VAR_rotation_days": str(config["rotation_days"]),
            "TF_VAR_mongodb_namespace": config["mongodb_namespace"],
            "TF_VAR_ops_manager_config_map": config["ops_manager_config_map"],
            "TF_VAR_ops_manager_credentials_secret": config[
                "ops_manager_credentials_secret"
            ],
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
        }
    )

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
    log_event("terraform.init.started", directory=str(tfdir))
    run_process(init, cwd=tfdir, env=env)
    log_event("terraform.init.succeeded", directory=str(tfdir))

    # The inventory file is temporary because desired state is reconstructed
    # from Vault for each command. Keeping it around would invite stale state.
    temp: Path | None = None
    try:
        payload = {
            "deployments": inventory,
            "operation": op,
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
        log_event(
            "terraform.apply.started",
            action=op["action"],
            deployment=op["deployment"],
            deployment_type=op["deployment_type"],
            database=op["database"],
            lock_category=op["lock_category"],
            lock_action=op["lock_action"],
            start_shards=op["start_shards"],
            target_shards=op["target_shards"],
            deployment_count=len(inventory),
        )
        try:
            run_process(
                [
                    "terraform",
                    "apply",
                    "-input=false",
                    "-auto-approve",
                    f"-var-file={temp.name}",
                ],
                cwd=tfdir,
                env=env,
            )
        except ControllerError:
            log_event(
                "terraform.apply.failed",
                level=40,
                action=op["action"],
                deployment=op["deployment"],
                deployment_type=op["deployment_type"],
                database=op["database"],
            )
            raise
        log_event(
            "terraform.apply.succeeded",
            action=op["action"],
            deployment=op["deployment"],
            deployment_type=op["deployment_type"],
            database=op["database"],
        )
    finally:
        if temp and temp.exists():
            temp.unlink()
