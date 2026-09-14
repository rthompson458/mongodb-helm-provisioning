"""Destructive live acceptance tests for the administrator interface.

The administrator scenario deliberately creates configuration drift and stranded
controller state, then proves the supported recovery commands repair it. This is
engineering-only test code for a disposable development environment.

A successful run starts and ends with a clean DBaaS inventory. Failed runs stop
at the first unsafe or unexpected condition so the broken state remains
available for inspection.
"""

from __future__ import annotations

import base64
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from privateWorkerReplacement import kube
from privateWorkerReplacement.async_operations import list_operation_records
from privateWorkerReplacement.common import (
    account_resource_name,
    normalize_database,
    normalize_deployment,
)
from privateWorkerReplacement.config import load_config
from privateWorkerReplacement.ops_manager import delete_project
from privateWorkerReplacement.vault import VaultClient

from .runner import HarnessRunner


TEST_COUNT = 57


def _latest_operation_id(
    runner: HarnessRunner,
    command: str,
    deployment: str,
) -> str:
    """Return the newest journal ID for one exact harness-created operation."""

    for record in list_operation_records(runner.context.config_path):
        if (
            str(record.get("command", "")) == command
            and str(record.get("deployment", "")).lower() == deployment.lower()
        ):
            return str(record.get("operation_id", ""))
    return ""


def _account_passwords(
    config: dict[str, Any],
    vault: VaultClient,
    deployment: str,
    database: str,
    password_secret: str,
) -> tuple[str, str]:
    """Read one account password from Vault and its Kubernetes password Secret.

    Password values are returned only for in-memory equality checks. They are
    never included in test names, notes, command lines, or harness output.
    """

    username = f"{database}_readWrite"
    vault_record = vault.account_secret(deployment, database, username)
    if not vault_record or not vault_record.get("password"):
        raise RuntimeError("Vault readWrite password is missing.")

    secret = kube.get_json(config, "secret", password_secret)
    if not secret:
        raise RuntimeError(f"Kubernetes Secret '{password_secret}' is missing.")

    encoded = str(secret.get("data", {}).get("password", ""))
    if not encoded:
        raise RuntimeError(
            f"Kubernetes Secret '{password_secret}' has no password field."
        )

    try:
        kubernetes_password = base64.b64decode(encoded).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise RuntimeError(
            f"Kubernetes Secret '{password_secret}' contains invalid password data."
        ) from exc

    return str(vault_record["password"]), kubernetes_password


def _delete_vault_deployment_metadata(
    config: dict[str, Any],
    deployment: str,
) -> tuple[bool, str]:
    """Delete only deployment metadata to manufacture orphaned Terraform state.

    Removing the metadata record makes Vault inventory forget the deployment
    while leaving Terraform-tracked credentials/resources in place. That is the
    exact partial-destroy condition RecoverOrphanedResources is intended to fix.
    """

    token = os.getenv(str(config["vault_token_env"]), "")
    if not token:
        return False, "Vault token environment variable is not set."

    logical_path = (
        f"{config['vault_base_path']}/{deployment}/_metadata"
    )
    api_path = (
        f"{config['vault_mount']}/metadata/"
        f"{urllib.parse.quote(logical_path.strip('/'), safe='/')}"
    )
    url = f"{str(config['vault_address']).rstrip('/')}/v1/{api_path}"
    request = urllib.request.Request(
        url,
        method="DELETE",
        headers={"X-Vault-Token": token},
    )

    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            if response.status not in {200, 204}:
                return False, f"Vault metadata delete returned HTTP {response.status}."
    except urllib.error.HTTPError as exc:
        return False, f"Vault metadata delete returned HTTP {exc.code}."
    except urllib.error.URLError as exc:
        return False, f"Vault metadata delete failed: {exc.reason}."

    return True, "Deployment metadata was intentionally removed from Vault."


def _delete_ops_manager_project(
    config: dict[str, Any],
    deployment: str,
) -> tuple[bool, str, float]:
    """Delete the test deployment project and matching group Secret."""

    started = time.monotonic()
    try:
        delete_project(config, deployment)
    except Exception as exc:  # Harness must convert setup damage into a test failure.
        return False, f"Ops Manager cleanup failed: {exc}", time.monotonic() - started
    return (
        True,
        "Ops Manager project and matching group Secret were intentionally removed.",
        time.monotonic() - started,
    )


def _harness_kubernetes_leftovers(
    config: dict[str, Any],
    deployment_keys: tuple[str, ...],
) -> list[str]:
    """Return named Kubernetes artifacts still owned by this admin test run."""

    leftovers: list[str] = []
    namespaced_resources = ("mongodb", "mongodbuser", "pvc", "secret", "configmap")

    for resource in namespaced_resources:
        for item in kube.list_json(config, resource):
            metadata = item.get("metadata", {})
            name = str(metadata.get("name", ""))
            labels = metadata.get("labels", {}) or {}
            owner = str(labels.get("dbaas.deployment", ""))
            if owner in deployment_keys or any(
                name == key
                or name.startswith(f"tc-{key}-")
                or name.startswith(f"data-{key}-")
                or name == f"tc-deployment-lock-{key}"
                for key in deployment_keys
            ):
                leftovers.append(f"{resource}/{name}")

    for item in kube.list_json(config, "pv", namespaced=False):
        metadata = item.get("metadata", {})
        name = str(metadata.get("name", ""))
        labels = metadata.get("labels", {}) or {}
        owner = str(labels.get("dbaas.deployment", ""))
        if owner in deployment_keys or any(
            name.startswith(f"{key}-") for key in deployment_keys
        ):
            leftovers.append(f"pv/{name}")

    return sorted(set(leftovers))


def _authentication_environment(
    config: dict[str, Any],
    deployment_key: str,
    database: str,
) -> dict[str, str]:
    """Build the environment used by the existing lifecycle authentication probe."""

    env = os.environ.copy()
    env.update(
        {
            "TC_ACTION": "verify_database_accounts",
            "TC_NAMESPACE": str(config["mongodb_namespace"]),
            "TC_KUBECONFIG": str(config["kubeconfig"]),
            "TC_KUBE_CONTEXT": str(config["kube_context"]),
            "TC_DEPLOYMENT": deployment_key,
            "TC_DATABASE": database,
            "TC_MONGO_IMAGE": str(config["mongo_image"]),
            "TC_AUTH_DATABASE": str(config["mongodb_auth_database"]),
        }
    )
    return env


def run(runner: HarnessRunner) -> None:
    """Hammer the live administrator interface and prove cleanup/recovery behavior."""

    ctx = runner.context

    admin_rs = f"AdminRSTest-{ctx.run_id}"
    admin_db = f"AdminDB_{ctx.run_id}"
    admin_sc = f"AdminSCTest-{ctx.run_id}"
    orphan_rs = f"OrphanRSTest-{ctx.run_id}"

    admin_rs_key, _ = normalize_deployment(admin_rs)
    admin_db_key, _ = normalize_database(admin_db)
    admin_sc_key, _ = normalize_deployment(admin_sc)
    orphan_rs_key, _ = normalize_deployment(orphan_rs)

    rw_resource = account_resource_name(admin_rs_key, admin_db_key, "readwrite")
    rw_password_secret = f"{rw_resource}-password"

    # ------------------------------------------------------------------
    # Administrator interface and zero-state safety
    # ------------------------------------------------------------------
    if not runner.admin(
        "Administrator CLI help renders",
        "--help",
        expected_text="platform administration interface",
    ).passed:
        return

    if not runner.admin(
        "Administrator operation journal is readable",
        "ListOperations",
    ).passed:
        return

    if not runner.admin(
        "Unknown administrator operation ID is rejected",
        "ListOperation",
        f"missing-{ctx.run_id}",
        expect_success=False,
        expected_text="does not exist",
    ).passed:
        return

    clean_start = runner.admin(
        "Administrator tests start from a clean DBaaS inventory",
        "ListManagedResources",
        expected_text="Status: CLEAN",
    )
    if not clean_start.passed:
        clean_start.note = (
            clean_start.note
            + " The --admin suite is intentionally destructive and requires a "
            "clean starting environment so it never adopts or deletes unrelated "
            "managed deployments."
        ).strip()
        return

    # ListManagedResources just proved the selected configuration, Vault
    # credentials, Kubernetes access, and Ops Manager inventory path are usable.
    # Load those values in-process only after that guarded live check succeeds so
    # a bad environment becomes a normal harness failure instead of a traceback.
    config = load_config(ctx.config_path)
    vault = VaultClient(config)
    kubectl = kube.base(config)
    namespace = str(config["mongodb_namespace"])

    if not runner.admin(
        "Reconcile is a no-op when desired-state inventory is empty",
        "Reconcile",
        expected_text="Nothing to reconcile.",
        timeout=300,
    ).passed:
        return

    if not runner.admin(
        "Orphan recovery requires explicit confirmation",
        "RecoverOrphanedResources",
        expect_success=False,
        expected_text="requires '--confirm'",
    ).passed:
        return

    if not runner.admin(
        "Deployment-lock recovery requires explicit confirmation",
        "RecoverDeploymentLock",
        "NoSuchCluster",
        expect_success=False,
        expected_text="requires '--confirm'",
    ).passed:
        return

    # ------------------------------------------------------------------
    # Reconcile: create real drift, repair it, and prove credentials survive
    # ------------------------------------------------------------------
    if not runner.controller_async(
        "Create administrator Reconcile test ReplicaSet",
        "AddReplicaSet",
        admin_rs,
        timeout=1800,
    ).passed:
        return

    create_operation_id = _latest_operation_id(
        runner,
        "AddReplicaSet",
        admin_rs,
    )
    if not runner.admin(
        "ListOperation reports the completed ReplicaSet creation",
        "ListOperation",
        create_operation_id or f"missing-{ctx.run_id}",
        expected_text="Result:       Succeeded",
    ).passed:
        return

    if not runner.admin(
        "ListOperations includes the administrator test ReplicaSet",
        "ListOperations",
        expected_text=admin_rs,
    ).passed:
        return

    if not runner.controller_async(
        "Create administrator Reconcile test database",
        "AddDatabase",
        admin_rs,
        admin_db,
        timeout=1200,
    ).passed:
        return

    if not runner.admin(
        "Managed-resource inventory sees the live Reconcile test deployment",
        "ListManagedResources",
        expected_text="Status: MANAGED RESOURCES PRESENT",
    ).passed:
        return

    if not runner.admin(
        "Verbose managed-resource inventory includes the test database",
        "ListManagedResources",
        "--verbose",
        expected_text=f"{admin_rs}/{admin_db}",
    ).passed:
        return

    try:
        vault_password_before, kubernetes_password_before = _account_passwords(
            config,
            vault,
            admin_rs,
            admin_db,
            rw_password_secret,
        )
        passwords_match = vault_password_before == kubernetes_password_before
        password_note = (
            "Vault and the Kubernetes password Secret match before drift."
            if passwords_match
            else "Vault and the Kubernetes password Secret differ before drift."
        )
    except Exception as exc:
        passwords_match = False
        password_note = f"Could not compare managed passwords safely: {exc}"

    if not runner.check(
        "Vault and Kubernetes passwords match before Reconcile drift",
        passwords_match,
        password_note,
    ).passed:
        return

    if not runner.run(
        "Delete only the managed readWrite MongoDBUser",
        kubectl
        + [
            "-n",
            namespace,
            "delete",
            "mongodbuser",
            rw_resource,
            "--wait=true",
        ],
        timeout=120,
    ).passed:
        return

    if not runner.run(
        "Confirm the readWrite MongoDBUser is absent",
        kubectl + ["-n", namespace, "get", "mongodbuser", rw_resource],
        expect_success=False,
        expected_text="NotFound",
        timeout=60,
    ).passed:
        return

    if not runner.run(
        "Confirm the readWrite password Secret survives the drift",
        kubectl + ["-n", namespace, "get", "secret", rw_password_secret],
        expected_text=rw_password_secret,
        timeout=60,
    ).passed:
        return

    if not runner.admin(
        "Managed-resource inventory flags the missing MongoDBUser",
        "ListManagedResources",
        expected_text="Status: ATTENTION REQUIRED",
    ).passed:
        return

    if not runner.admin(
        "Verbose inventory identifies the exact missing MongoDBUser",
        "ListManagedResources",
        "--verbose",
        expected_text=rw_resource,
    ).passed:
        return

    if not runner.admin(
        "Reconcile repairs the deliberately deleted MongoDBUser",
        "Reconcile",
        expected_text="Reconcile complete.",
        timeout=1800,
    ).passed:
        return

    if not runner.run(
        "Recreated readWrite MongoDBUser reaches Updated",
        kubectl
        + [
            "-n",
            namespace,
            "get",
            "mongodbuser",
            rw_resource,
            "-o",
            "jsonpath={.status.phase}",
        ],
        expected_text="Updated",
        timeout=60,
    ).passed:
        return

    if not runner.run(
        "All database accounts authenticate after Reconcile",
        ["bash", str(ctx.repo_root / "terraform-dbaas" / "scripts" / "lifecycle.sh")],
        expected_text="TC_RESULT=AUTH_OK",
        timeout=300,
        env=_authentication_environment(config, admin_rs_key, admin_db),
    ).passed:
        return

    try:
        vault_password_after, kubernetes_password_after = _account_passwords(
            config,
            vault,
            admin_rs,
            admin_db,
            rw_password_secret,
        )
    except Exception as exc:
        vault_password_after = ""
        kubernetes_password_after = ""
        after_note = f"Could not read post-Reconcile passwords safely: {exc}"
    else:
        after_note = "Post-Reconcile passwords were read without displaying them."

    vault_preserved = bool(vault_password_after) and (
        vault_password_after == vault_password_before
    )
    if not runner.check(
        "Reconcile preserves the Vault password",
        vault_preserved,
        (
            "Vault password is unchanged."
            if vault_preserved
            else after_note or "Vault password changed unexpectedly."
        ),
    ).passed:
        return

    kubernetes_preserved = bool(kubernetes_password_after) and (
        kubernetes_password_after == kubernetes_password_before
    )
    if not runner.check(
        "Reconcile preserves the Kubernetes password Secret",
        kubernetes_preserved,
        (
            "Kubernetes password Secret is unchanged."
            if kubernetes_preserved
            else after_note or "Kubernetes password changed unexpectedly."
        ),
    ).passed:
        return

    if not runner.admin(
        "Inventory returns to healthy managed-resource state after Reconcile",
        "ListManagedResources",
        expected_text="Status: MANAGED RESOURCES PRESENT",
    ).passed:
        return

    if not runner.controller_async(
        "Delete administrator Reconcile test database",
        "DeleteDatabase",
        admin_rs,
        admin_db,
        "--confirm",
        timeout=1200,
    ).passed:
        return

    if not runner.controller_async(
        "Delete administrator Reconcile test ReplicaSet",
        "DeleteReplicaSet",
        admin_rs,
        "--confirm",
        timeout=1800,
    ).passed:
        return

    if not runner.admin(
        "Reconcile test cleanup returns inventory to CLEAN",
        "ListManagedResources",
        expected_text="Status: CLEAN",
    ).passed:
        return

    # ------------------------------------------------------------------
    # RecoverDeploymentLock: refuse unsafe release, then recover valid drift
    # ------------------------------------------------------------------
    max_shards_ok = int(config["max_shards_per_cluster"]) >= 2
    if not runner.check(
        "Admin lock-recovery test configuration permits two shards",
        max_shards_ok,
        (
            "Configured maximum allows the two-shard recovery fixture."
            if max_shards_ok
            else "max_shards_per_cluster must be at least 2 for this admin test."
        ),
    ).passed:
        return

    if not runner.controller_async(
        "Create two-shard administrator lock-recovery cluster",
        "AddShardedCluster",
        admin_sc,
        "--shards",
        "2",
        timeout=2400,
    ).passed:
        return

    lock_name = f"tc-deployment-lock-{admin_sc_key}"
    invalid_lock_id = f"harness-invalid-{ctx.run_id}"
    if not runner.run(
        "Create mismatched synthetic stranded topology lock",
        kubectl
        + [
            "-n",
            namespace,
            "create",
            "configmap",
            lock_name,
            f"--from-literal=operation_id={invalid_lock_id}",
            "--from-literal=category=topology",
            "--from-literal=action=AddShard",
            "--from-literal=database=",
            "--from-literal=start_shards=2",
            "--from-literal=target_shards=3",
            "--from-literal=started_at=2026-01-01T00:00:00Z",
        ],
        timeout=60,
    ).passed:
        return

    if not runner.admin(
        "Reconcile refuses to race an active ShardedCluster lock",
        "Reconcile",
        expect_success=False,
        expected_text="Reconcile is blocked",
        timeout=300,
    ).passed:
        return

    if not runner.admin(
        "Lock recovery refuses a target that does not match desired state",
        "RecoverDeploymentLock",
        admin_sc,
        "--confirm",
        expect_success=False,
        expected_text="lock target is 3",
        timeout=300,
    ).passed:
        return

    if not runner.run(
        "Remove the intentionally invalid synthetic lock",
        kubectl + ["-n", namespace, "delete", "configmap", lock_name],
        timeout=60,
    ).passed:
        return

    valid_lock_id = f"harness-valid-{ctx.run_id}"
    if not runner.run(
        "Create valid synthetic stranded AddShard lock",
        kubectl
        + [
            "-n",
            namespace,
            "create",
            "configmap",
            lock_name,
            f"--from-literal=operation_id={valid_lock_id}",
            "--from-literal=category=topology",
            "--from-literal=action=AddShard",
            "--from-literal=database=",
            "--from-literal=start_shards=1",
            "--from-literal=target_shards=2",
            "--from-literal=started_at=2026-01-01T00:00:00Z",
        ],
        timeout=60,
    ).passed:
        return

    if not runner.admin(
        "Verbose inventory exposes the stranded deployment lock",
        "ListManagedResources",
        "--verbose",
        expected_text=lock_name,
    ).passed:
        return

    if not runner.admin(
        "Valid deployment-lock recovery still requires confirmation",
        "RecoverDeploymentLock",
        admin_sc,
        expect_success=False,
        expected_text="requires '--confirm'",
    ).passed:
        return

    if not runner.admin(
        "RecoverDeploymentLock releases the validated stranded lock",
        "RecoverDeploymentLock",
        admin_sc,
        "--confirm",
        expected_text="Deployment lock: Released",
        timeout=900,
    ).passed:
        return

    if not runner.run(
        "Recovered deployment lock is absent",
        kubectl + ["-n", namespace, "get", "configmap", lock_name],
        expect_success=False,
        expected_text="NotFound",
        timeout=60,
    ).passed:
        return

    if not runner.controller(
        "ShardedCluster remains Running after lock recovery",
        "ListShardedCluster",
        admin_sc,
        expected_text="Running",
        timeout=120,
    ).passed:
        return

    if not runner.admin(
        "Reconcile succeeds after deployment-lock recovery",
        "Reconcile",
        expected_text="Reconcile complete.",
        timeout=2400,
    ).passed:
        return

    if not runner.controller_async(
        "Delete administrator lock-recovery ShardedCluster",
        "DeleteShardedCluster",
        admin_sc,
        "--confirm",
        timeout=2400,
    ).passed:
        return

    if not runner.admin(
        "Lock-recovery cleanup returns inventory to CLEAN",
        "ListManagedResources",
        expected_text="Status: CLEAN",
    ).passed:
        return

    # ------------------------------------------------------------------
    # RecoverOrphanedResources: manufacture a real partial-destroy condition
    # ------------------------------------------------------------------
    if not runner.controller_async(
        "Create administrator orphan-recovery ReplicaSet",
        "AddReplicaSet",
        orphan_rs,
        timeout=1800,
    ).passed:
        return

    if not runner.admin_async(
        "Orphan recovery refuses a nonempty Vault desired-state inventory",
        "RecoverOrphanedResources",
        "--confirm",
        timeout=300,
        expect_success=False,
        expected_text="controller inventory is empty",
    ).passed:
        return

    if not runner.run(
        "Delete the orphan-test MongoDB resource outside the controller",
        kubectl
        + [
            "-n",
            namespace,
            "delete",
            "mongodb",
            orphan_rs_key,
            "--wait=true",
            "--timeout=300s",
        ],
        timeout=360,
    ).passed:
        return

    if not runner.run(
        "Confirm the orphan-test MongoDB resource is absent",
        kubectl + ["-n", namespace, "get", "mongodb", orphan_rs_key],
        expect_success=False,
        expected_text="NotFound",
        timeout=60,
    ).passed:
        return

    ops_ok, ops_note, ops_elapsed = _delete_ops_manager_project(config, orphan_rs)
    if not runner.check(
        "Delete orphan-test Ops Manager project and group Secret",
        ops_ok,
        ops_note,
        elapsed_seconds=ops_elapsed,
    ).passed:
        return

    metadata_ok, metadata_note = _delete_vault_deployment_metadata(
        config,
        orphan_rs,
    )
    if not runner.check(
        "Remove orphan-test Vault deployment metadata",
        metadata_ok,
        metadata_note,
    ).passed:
        return

    try:
        inventory_empty = not vault.load_inventory()
        inventory_note = (
            "Vault-backed managed deployment inventory is empty."
            if inventory_empty
            else "Vault still reports managed desired-state deployments."
        )
    except Exception as exc:
        inventory_empty = False
        inventory_note = f"Could not verify Vault inventory: {exc}"

    if not runner.check(
        "Vault inventory is empty before orphan recovery",
        inventory_empty,
        inventory_note,
    ).passed:
        return

    if not runner.admin(
        "Inventory exposes the manufactured orphaned controller resources",
        "ListManagedResources",
        "--verbose",
        expected_text=orphan_rs_key,
    ).passed:
        return

    if not runner.admin_async(
        "RecoverOrphanedResources destroys stranded Terraform-managed resources",
        "RecoverOrphanedResources",
        "--confirm",
        timeout=1800,
    ).passed:
        return

    if not runner.admin(
        "Administrator suite finishes with CLEAN managed-resource inventory",
        "ListManagedResources",
        expected_text="Status: CLEAN",
    ).passed:
        return

    leftovers = _harness_kubernetes_leftovers(
        config,
        (admin_rs_key, admin_sc_key, orphan_rs_key),
    )
    if not runner.check(
        "No administrator-test Kubernetes artifacts remain",
        not leftovers,
        (
            "No administrator-test Kubernetes artifacts remain."
            if not leftovers
            else "Unexpected leftovers: " + ", ".join(leftovers)
        ),
    ).passed:
        return

    try:
        vault_root = vault.list_keys(str(config["vault_base_path"]))
        expected_folders = {
            f"{admin_rs}/",
            f"{admin_sc}/",
            f"{orphan_rs}/",
        }
        vault_leftovers = sorted(expected_folders.intersection(vault_root))
        vault_clean = not vault_leftovers
        vault_note = (
            "No administrator-test Vault folders remain."
            if vault_clean
            else "Unexpected Vault folders remain: " + ", ".join(vault_leftovers)
        )
    except Exception as exc:
        vault_clean = False
        vault_note = f"Could not verify final Vault folders: {exc}"

    if not runner.check(
        "No administrator-test Vault folders remain",
        vault_clean,
        vault_note,
    ).passed:
        return

    if not runner.admin(
        "Operation journal records administrator orphan recovery",
        "ListOperations",
        expected_text="RecoverOrphanedResources",
    ).passed:
        return

    runner.admin(
        "Final Reconcile confirms there is no desired state left to repair",
        "Reconcile",
        expected_text="Nothing to reconcile.",
        timeout=300,
    )
