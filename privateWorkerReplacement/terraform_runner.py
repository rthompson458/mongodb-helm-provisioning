"""Prepare and execute the Terraform module used by privateWorkerReplacement.

This file is the bridge between Python orchestration and Terraform. Python
builds desired-state JSON and a small one-shot operation description, then this
module runs Terraform. Terraform delegates logical database create/delete work
to the integrated Helm chart, while the lifecycle resource/script continues to
perform verification, storage, and lock work where required.

User-interface rule:
    Git and Terraform stdout/stderr are implementation diagnostics. They are
    captured here and appended to the daily operations log instead of being
    dumped onto customer or administrator terminals.

Architecture rule:
    Do not add direct MongoDB, Vault, or Kubernetes mutations here. This module
    prepares Terraform inputs and executes Terraform only.
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
from .logging_component import append_process_diagnostic, log_event


def _require(*names: str) -> None:
    """Fail early if an executable needed by Terraform is missing."""

    missing = [name for name in names if not shutil.which(name)]
    if missing:
        raise ControllerError(
            "Required executable(s) not found in PATH: " + ", ".join(missing)
        )


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
            f"Terraform {version} is installed; "
            "privateWorkerReplacement requires 1.11 or newer."
        )


def _run_diagnostic(
    config: dict[str, Any],
    command: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    label: str,
) -> None:
    """Run one implementation command quietly and preserve its detailed output.

    We deliberately call run_process with ``check=False`` so the completed
    stdout/stderr can be written to the operations log before a failure is
    converted into a concise ControllerError. This gives users a readable
    terminal while administrators still retain the evidence needed to debug.
    """

    result = run_process(
        command,
        cwd=cwd,
        env=env,
        capture=True,
        check=False,
    )

    log_path = append_process_diagnostic(
        config,
        command,
        returncode=result.returncode,
        stdout=result.stdout or "",
        stderr=result.stderr or "",
        label=label,
    )

    if result.returncode != 0:
        raise ControllerError(
            f"{label} failed with exit code {result.returncode}. "
            f"Detailed diagnostics were written to {log_path}."
        )


@contextmanager
def _terraform_execution_lock(config: dict[str, Any]):
    """Serialize shared Terraform cache/workdir activity across processes.

    Every controller command shares one disposable Git checkout and one
    terraform-dbaas/.terraform provider directory. Detached workers make
    overlap likely, so Git refresh, terraform init, and terraform apply must be
    one cross-process critical section.

    ``flock`` is process-safe on Linux/WSL and is automatically released if a
    worker exits or is killed. The lock file lives beside the cache so a Git
    reset or clean cannot remove it.
    """

    cache: Path = config["terraform_cache"]
    cache.parent.mkdir(parents=True, exist_ok=True)

    lock_path = cache.parent / (
        f".{cache.name}.privateWorkerReplacement.lock"
    )

    with lock_path.open("a+", encoding="utf-8") as handle:
        log_event(
            "terraform.execution_lock.waiting",
            lock_file=str(lock_path),
        )

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
    """Refresh the disposable Terraform runtime cache from local source files.

    The configured source directory is authoritative and is never modified by
    controller runtime work. Before each Terraform operation, source files are
    copied into the disposable cache so local edits are picked up immediately.

    The cache's .terraform directory is preserved between operations so already
    downloaded providers can be reused. All other cached source files are
    replaced so deleted or renamed source files cannot remain stale.
    """

    source: Path = config["terraform_source"]
    cache: Path = config["terraform_cache"]

    if not source.is_dir():
        raise ControllerError(
            f"Terraform source directory does not exist: {source}"
        )

    if cache == source or source in cache.parents:
        raise ControllerError(
            "Terraform cache directory must not be inside the Terraform "
            f"source directory: {cache}"
        )

    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)

    log_event(
        "terraform.source.refresh",
        source=str(source),
        cache=str(cache),
    )

    # Keep Terraform's downloaded-provider working directory, but remove every
    # other cached file so the execution copy exactly follows current source.
    for child in cache.iterdir():
        if child.name == ".terraform":
            continue

        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()

    shutil.copytree(
        source,
        cache,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(".terraform"),
    )

    return cache


def _operation_payload(
    operation: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return a complete Terraform operation object with safe defaults.

    A random nonce makes one-shot operations unique. Lifecycle operations use
    it through terraform_data triggers, and database create/delete operations
    pass it into the Helm release so a retry produces a fresh Helm revision.
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
    targets: list[str] | None = None,
) -> None:
    """Apply desired deployment state and an optional one-shot operation.

    High-level sequence:
      1. Check local tools and Terraform version.
      2. Acquire the cross-process Terraform execution lock.
      3. Refresh the disposable Terraform cache from the configured local source directory.
      4. Pass environment/config values as TF_VAR_* variables.
      5. Write desired state to a temporary .tfvars.json file.
      6. Run terraform init and terraform apply.
      7. Delete the temporary input file and release the execution lock.

    Password values are never written into the temporary JSON by Python, and
    the Vault token is passed only through the child-process environment.
    """

    _require(
        "terraform",
        "git",
        "kubectl",
        "bash",
        "python3",
    )

    op = _operation_payload(operation)

    if config["storage_mode"] == "static-local":
        _require("docker")

    _check_version()

    # The shared Terraform cache, provider directory, and backend work are treated
    # as one transaction. This prevents one worker from resetting the checkout
    # while another worker is executing Terraform from it.
    with _terraform_execution_lock(config):
        _apply_inventory_locked(
            config,
            inventory,
            op,
            targets,
        )


def _apply_inventory_locked(
    config: dict[str, Any],
    inventory: dict[str, dict[str, Any]],
    op: dict[str, Any],
    targets: list[str] | None = None,
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
            "TF_VAR_ops_manager_config_map": config[
                "ops_manager_config_map"
            ],
            "TF_VAR_ops_manager_credentials_secret": config[
                "ops_manager_credentials_secret"
            ],
            "TF_VAR_mongodb_auth_database": config[
                "mongodb_auth_database"
            ],
            "TF_VAR_kubeconfig_path": config["kubeconfig"],
            "TF_VAR_kube_context": config["kube_context"],
            "TF_VAR_mongo_image": config["mongo_image"],
            "TF_VAR_placeholder_collection": config[
                "placeholder_collection"
            ],
            "TF_VAR_mongodb_management_timeout_seconds": str(
                config["job_timeout"]
            ),
            "TF_VAR_default_members": str(
                config["default_members"]
            ),
            "TF_VAR_default_storage_class": config[
                "storage_class"
            ],
            "TF_VAR_default_storage_size": config[
                "storage_size"
            ],
            "TF_VAR_storage_base_path": config[
                "storage_base_path"
            ],
            "TF_VAR_storage_node_name": config[
                "storage_node_name"
            ],
        }
    )

    init = [
        "terraform",
        "init",
        "-input=false",
        "-reconfigure",
        (
            "-backend-config=secret_suffix="
            f"{config['backend_secret_suffix']}"
        ),
        (
            "-backend-config=namespace="
            f"{config['backend_namespace']}"
        ),
        (
            "-backend-config=config_path="
            f"{config['kubeconfig']}"
        ),
    ]

    if config["kube_context"]:
        init.append(
            "-backend-config=config_context="
            f"{config['kube_context']}"
        )

    log_event(
        "terraform.init.started",
        directory=str(tfdir),
    )

    _run_diagnostic(
        config,
        init,
        cwd=tfdir,
        env=env,
        label="Terraform initialization",
    )

    log_event(
        "terraform.init.succeeded",
        directory=str(tfdir),
    )

    # Desired state is reconstructed from Vault for each command. The temporary
    # file is removed after the apply so stale controller intent cannot linger.
    temp: Path | None = None

    try:
        payload = {
            "deployments": inventory,
            "operation": op,

            # DeleteDatabase --confirm is validated by the user-facing
            # controller before this point. Translate that approved operation
            # into Terraform's explicit destructive-operation safety gate.
            "allow_destructive_mongodb_operations": (
                op["action"] == "delete_database"
            ),
        }

        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=tfdir,
            prefix=".tc.",
            suffix=".tfvars.json",
            delete=False,
        ) as handle:
            json.dump(
                payload,
                handle,
                indent=2,
                sort_keys=True,
            )
            handle.write("\n")
            temp = Path(handle.name)

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
            command = [
                "terraform",
                "apply",
                "-input=false",
                "-auto-approve",
                f"-var-file={temp.name}",
            ]

            for target in targets or []:
                command.append(
                    f"-target={target}"
                )

            _run_diagnostic(
                config,
                command,
                cwd=tfdir,
                env=env,
                label="Terraform apply",
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
