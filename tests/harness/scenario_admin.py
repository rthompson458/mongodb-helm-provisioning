"""Destructive live acceptance tests for the administrator interface.

The administrator scenario deliberately creates configuration drift and stranded
controller state, then proves the supported recovery commands repair it. This is
engineering-only test code for a disposable development environment.

Canonical full-suite test intent:
39. Verify administrator CLI help renders without starting work.
40. Verify the asynchronous operation journal is readable.
41. Verify an unknown operation ID is rejected cleanly.
42. Require a clean DBaaS inventory before destructive Admin testing starts.
43. Prove asynchronous Reconcile is a no-op against empty desired state.
44. Require explicit confirmation before orphan-resource recovery.
45. Require explicit confirmation before deployment-lock recovery.
46. Create the ReplicaSet fixture used for Reconcile drift testing.
47. Verify ListOperation reports the completed ReplicaSet creation.
48. Verify ListOperations includes that completed lifecycle operation.
49. Create the database fixture used for account/Reconcile testing.
50. Verify managed-resource inventory sees the active fixture deployment.
51. Verify verbose inventory includes the exact test database.
52. Verify Vault and Kubernetes hold the same readWrite password before drift.
53. Delete only the managed readWrite MongoDBUser to manufacture drift.
54. Verify the deliberately deleted MongoDBUser is absent.
55. Verify its Kubernetes password Secret survives the drift.
56. Verify compact inventory reports ATTENTION REQUIRED for the missing user.
57. Verify verbose inventory identifies the exact missing MongoDBUser.
58. Run asynchronous Reconcile and repair the deleted MongoDBUser.
59. Verify the recreated readWrite MongoDBUser reaches Updated.
60. Authenticate all database accounts against MongoDB after Reconcile.
61. Verify Reconcile did not change the Vault password.
62. Verify Reconcile did not change the Kubernetes password Secret.
63. Verify inventory returns to a healthy managed-resource state.
64. Delete the Reconcile test database.
65. Delete the Reconcile test ReplicaSet.
66. Verify Reconcile-test cleanup returns inventory to CLEAN.
67. Verify configuration permits the two-shard lock-recovery fixture.
68. Create the two-shard ShardedCluster used for lock recovery.
69. Create an intentionally mismatched synthetic topology lock.
70. Verify Reconcile refuses to race an active ShardedCluster lock.
71. Verify lock recovery refuses a target that disagrees with desired state.
72. Remove the intentionally invalid synthetic lock.
73. Create a valid stranded AddShard lock.
74. Verify verbose inventory exposes the stranded deployment lock.
75. Verify valid lock recovery still requires explicit confirmation.
76. Recover the validated stranded AddShard lock asynchronously.
77. Verify the recovered AddShard lock is absent.
78. Reduce the test cluster to one shard for DeleteShard recovery.
79. Create a valid stranded DeleteShard lock.
80. Recover and validate the completed DeleteShard lock asynchronously.
81. Verify the recovered DeleteShard lock is absent.
82. Verify the ShardedCluster remains Running after lock recovery.
83. Verify Reconcile succeeds after the lock-recovery sequence.
84. Delete the administrator lock-recovery ShardedCluster.
85. Verify lock-recovery cleanup returns inventory to CLEAN.
86. Create the ReplicaSet fixture used for orphan-resource recovery.
87. Verify orphan recovery refuses nonempty Vault desired-state inventory.
88. Remove only Vault deployment metadata to manufacture orphaned state.
89. Verify Vault desired-state inventory is empty.
90. Verify orphan recovery refuses while a live managed MongoDB CR remains.
91. Delete the orphan-test MongoDB CR outside the controller.
92. Verify the orphan-test MongoDB CR is absent.
93. Delete the orphan-test Ops Manager project and group Secret.
94. Verify inventory exposes the manufactured orphaned controller resources.
95. Recover stranded Terraform-managed resources asynchronously.
96. Verify the administrator suite returns managed-resource inventory to CLEAN.
97. Verify no administrator-test Kubernetes or orphan Operator/Helm artifacts remain.
98. Verify no administrator-test Vault folders remain.
99. Verify the operation journal records administrator orphan recovery.
100. Verify final asynchronous Reconcile has no desired state left to repair.

A successful full Admin run starts and ends with a clean DBaaS inventory. Failed
runs stop at the first unsafe or unexpected condition so the broken state remains
available for inspection. In --testList mode, only requested numbered tests run;
prerequisite tests are not added automatically.
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
from privateWorkerReplacement.operator_artifacts import list_orphan_operator_artifacts
from privateWorkerReplacement.vault import VaultClient

from .runner import HarnessRunner


TEST_COUNT = 62


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
    namespaced_resources = (
        "mongodb",
        "mongodbuser",
        "statefulset",
        "pod",
        "job",
        "pvc",
        "secret",
        "configmap",
    )

    for resource in namespaced_resources:
        for item in kube.list_json(config, resource):
            metadata = item.get("metadata", {})
            name = str(metadata.get("name", ""))
            labels = metadata.get("labels", {}) or {}
            owner = str(labels.get("dbaas.deployment", ""))
            if owner in deployment_keys or any(
                name == key
                or name.startswith(f"{key}-")
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
    # TEST 39 - Render administrator CLI help.
    # WHY: Proves the operator-facing entry point starts safely and exposes the administration interface.
    # PASS: Admin help renders successfully with the expected interface description.
    if not runner.admin(
        "Administrator CLI help renders",
        "--help",
        expected_text="platform administration interface",
    ).passed:
        return

    # TEST 40 - Read the asynchronous operation journal.
    # WHY: Administrators need operation history to diagnose detached lifecycle and recovery work.
    # PASS: ListOperations completes successfully.
    if not runner.admin(
        "Administrator operation journal is readable",
        "ListOperations",
    ).passed:
        return

    # TEST 41 - Reject an unknown operation ID cleanly.
    # WHY: Prevents a bad diagnostic lookup from producing a traceback or misleading result.
    # PASS: ListOperation returns a controlled `does not exist` error.
    if not runner.admin(
        "Unknown administrator operation ID is rejected",
        "ListOperation",
        f"missing-{ctx.run_id}",
        expect_success=False,
        expected_text="does not exist",
    ).passed:
        return

    # TEST 42 - Require a clean starting DBaaS inventory.
    # WHY: The destructive Admin suite must never adopt or damage unrelated managed resources.
    # PASS: ListManagedResources reports `Status: CLEAN` before destructive testing begins.
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

    # TEST 43 - Reconcile an empty desired-state inventory.
    # WHY: Proves Reconcile is safe when there is nothing to repair or create.
    # PASS: The asynchronous Reconcile operation succeeds without creating managed resources.
    if not runner.admin_async(
        "Async Reconcile is a no-op when desired-state inventory is empty",
        "Reconcile",
        timeout=300,
    ).passed:
        return

    # TEST 44 - Require confirmation for orphan-resource recovery.
    # WHY: RecoverOrphanedResources can destroy stranded infrastructure and must not run accidentally.
    # PASS: The command is refused when `--confirm` is omitted.
    if not runner.admin(
        "Orphan recovery requires explicit confirmation",
        "RecoverOrphanedResources",
        expect_success=False,
        expected_text="requires '--confirm'",
    ).passed:
        return

    # TEST 45 - Require confirmation for deployment-lock recovery.
    # WHY: Releasing a deployment lock is an administrator recovery action with lifecycle risk.
    # PASS: RecoverDeploymentLock is refused when `--confirm` is omitted.
    if not runner.admin(
        "Deployment-lock recovery requires explicit confirmation",
        "RecoverDeploymentLock",
        "NoSuchCluster",
        expect_success=False,
        expected_text="requires '--confirm'",
    ).passed:
        return

    # ------------------------------------------------------------------
    # Reconcile: submit the background repair, then prove drift is repaired
    # without changing the credential that already belongs to the account.
    # ------------------------------------------------------------------
    # TEST 46 - Create the ReplicaSet used for Reconcile drift testing.
    # WHY: Reconcile must be proven against a real managed deployment, not only mocked state.
    # PASS: The asynchronous AddReplicaSet operation completes successfully.
    if not runner.controller_async(
        "Create administrator Reconcile test ReplicaSet",
        "AddReplicaSet",
        admin_rs,
        timeout=1800,
    ).passed:
        return

    # TEST 47 - Inspect the completed ReplicaSet creation with ListOperation.
    # WHY: Proves an administrator can diagnose one specific background lifecycle operation.
    # PASS: ListOperation reports the creation operation as Succeeded.
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

    # TEST 48 - Find the ReplicaSet creation in the operation journal.
    # WHY: Proves completed customer lifecycle work is visible in the administrator operation history.
    # PASS: ListOperations includes the generated ReplicaSet name.
    if not runner.admin(
        "ListOperations includes the administrator test ReplicaSet",
        "ListOperations",
        expected_text=admin_rs,
    ).passed:
        return

    # TEST 49 - Create the database used for Reconcile account-drift testing.
    # WHY: The repair test needs real managed MongoDBUser objects and credentials to damage safely.
    # PASS: The asynchronous AddDatabase operation completes successfully.
    if not runner.controller_async(
        "Create administrator Reconcile test database",
        "AddDatabase",
        admin_rs,
        admin_db,
        timeout=1200,
    ).passed:
        return

    # TEST 50 - Verify inventory sees the active managed deployment.
    # WHY: The administrator inventory must distinguish healthy managed resources from a clean empty system.
    # PASS: ListManagedResources reports `MANAGED RESOURCES PRESENT`.
    if not runner.admin(
        "Managed-resource inventory sees the live Reconcile test deployment",
        "ListManagedResources",
        expected_text="Status: MANAGED RESOURCES PRESENT",
    ).passed:
        return

    # TEST 51 - Verify verbose inventory identifies the exact test database.
    # WHY: Operators need object-level detail when diagnosing a specific deployment or database.
    # PASS: Verbose ListManagedResources includes the generated deployment/database pair.
    if not runner.admin(
        "Verbose managed-resource inventory includes the test database",
        "ListManagedResources",
        "--verbose",
        expected_text=f"{admin_rs}/{admin_db}",
    ).passed:
        return

    # TEST 52 - Establish a credential baseline before creating drift.
    # WHY: Reconcile must repair missing runtime objects without silently changing an existing password.
    # PASS: Vault and the Kubernetes password Secret contain the same non-displayed password.
    vault_password_before = ""
    kubernetes_password_before = ""
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

    # TEST 53 - Delete only the managed readWrite MongoDBUser.
    # WHY: Deliberately creates realistic runtime drift while preserving desired state and credentials.
    # PASS: kubectl deletes the MongoDBUser successfully.
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

    # TEST 54 - Confirm the manufactured MongoDBUser drift exists.
    # WHY: The recovery test is valid only if the target user is truly missing before Reconcile.
    # PASS: Kubernetes returns NotFound for the deleted MongoDBUser.
    if not runner.run(
        "Confirm the readWrite MongoDBUser is absent",
        kubectl + ["-n", namespace, "get", "mongodbuser", rw_resource],
        expect_success=False,
        expected_text="NotFound",
        timeout=60,
    ).passed:
        return

    # TEST 55 - Confirm the password Secret survived the user deletion.
    # WHY: Isolates the failure to the MongoDBUser object so Reconcile can reuse the existing credential.
    # PASS: The original Kubernetes password Secret still exists.
    if not runner.run(
        "Confirm the readWrite password Secret survives the drift",
        kubectl + ["-n", namespace, "get", "secret", rw_password_secret],
        expected_text=rw_password_secret,
        timeout=60,
    ).passed:
        return

    # TEST 56 - Detect the missing MongoDBUser in compact inventory.
    # WHY: Runtime drift must prevent a false healthy status.
    # PASS: ListManagedResources reports `ATTENTION REQUIRED`.
    if not runner.admin(
        "Managed-resource inventory flags the missing MongoDBUser",
        "ListManagedResources",
        expected_text="Status: ATTENTION REQUIRED",
    ).passed:
        return

    # TEST 57 - Identify the exact missing MongoDBUser in verbose inventory.
    # WHY: An operator must know which object is broken before choosing a recovery action.
    # PASS: Verbose inventory includes the deterministic MongoDBUser resource name.
    if not runner.admin(
        "Verbose inventory identifies the exact missing MongoDBUser",
        "ListManagedResources",
        "--verbose",
        expected_text=rw_resource,
    ).passed:
        return

    # TEST 58 - Reconcile the deliberately deleted MongoDBUser.
    # WHY: Proves Vault-backed desired state can repair runtime drift through the supported recovery path.
    # PASS: The asynchronous Reconcile operation completes successfully.
    if not runner.admin_async(
        "Async Reconcile repairs the deliberately deleted MongoDBUser",
        "Reconcile",
        timeout=1800,
    ).passed:
        return

    # TEST 59 - Verify the recreated MongoDBUser reaches Updated.
    # WHY: Reconcile success is meaningful only if the MongoDB Operator finishes provisioning the user.
    # PASS: The recreated MongoDBUser reports phase `Updated`.
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

    # TEST 60 - Authenticate all managed database accounts after Reconcile.
    # WHY: Proves recovery restored real MongoDB access, not just Kubernetes object appearance.
    # PASS: The lifecycle authentication probe returns `TC_RESULT=AUTH_OK`.
    if not runner.run(
        "All database accounts authenticate after Reconcile",
        ["bash", str(ctx.repo_root / "terraform-dbaas" / "scripts" / "lifecycle.sh")],
        expected_text="TC_RESULT=AUTH_OK",
        timeout=300,
        env=_authentication_environment(config, admin_rs_key, admin_db),
    ).passed:
        return

    # TEST 61 - Verify Reconcile preserved the Vault password.
    # WHY: Repairing a missing user must not rotate a credential that was not scheduled for rotation.
    # PASS: The post-Reconcile Vault password exactly matches the pre-drift value.
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

    # TEST 62 - Verify Reconcile preserved the Kubernetes password Secret.
    # WHY: The recreated user must continue using the same credential material as Vault.
    # PASS: The post-Reconcile Kubernetes password equals the pre-drift Secret value.
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

    # TEST 63 - Verify inventory is healthy after Reconcile.
    # WHY: A successful repair must clear the drift warning while keeping legitimate resources visible.
    # PASS: ListManagedResources reports `MANAGED RESOURCES PRESENT`, not ATTENTION REQUIRED.
    if not runner.admin(
        "Inventory returns to healthy managed-resource state after Reconcile",
        "ListManagedResources",
        expected_text="Status: MANAGED RESOURCES PRESENT",
    ).passed:
        return

    # TEST 64 - Delete the Reconcile test database.
    # WHY: Begins supported cleanup of the fixture after the repair assertions are complete.
    # PASS: The asynchronous DeleteDatabase operation completes successfully.
    if not runner.controller_async(
        "Delete administrator Reconcile test database",
        "DeleteDatabase",
        admin_rs,
        admin_db,
        "--confirm",
        timeout=1200,
    ).passed:
        return

    # TEST 65 - Delete the Reconcile test ReplicaSet.
    # WHY: Proves the repaired deployment can still complete normal lifecycle teardown.
    # PASS: The asynchronous DeleteReplicaSet operation completes successfully.
    if not runner.controller_async(
        "Delete administrator Reconcile test ReplicaSet",
        "DeleteReplicaSet",
        admin_rs,
        "--confirm",
        timeout=1800,
    ).passed:
        return

    # TEST 66 - Verify Reconcile fixture cleanup returned the environment to CLEAN.
    # WHY: Later destructive Admin scenarios must start without leftovers from the drift test.
    # PASS: ListManagedResources reports `Status: CLEAN`.
    if not runner.admin(
        "Reconcile test cleanup returns inventory to CLEAN",
        "ListManagedResources",
        expected_text="Status: CLEAN",
    ).passed:
        return

    # ------------------------------------------------------------------
    # RecoverDeploymentLock: prove background recovery refuses unsafe release,
    # then succeeds for validated AddShard and DeleteShard stranded locks.
    # ------------------------------------------------------------------
    # TEST 67 - Verify configuration supports the lock-recovery fixture.
    # WHY: The recovery scenario needs two shards to model completed AddShard and DeleteShard states safely.
    # PASS: `max_shards_per_cluster` is at least 2.
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

    # TEST 68 - Create the ShardedCluster used for deployment-lock recovery.
    # WHY: Lock recovery must be proven against a real managed sharded deployment.
    # PASS: A two-shard ShardedCluster is created successfully.
    if not runner.controller_async(
        "Create two-shard administrator lock-recovery cluster",
        "AddShardedCluster",
        admin_sc,
        "--shards",
        "2",
        timeout=2400,
    ).passed:
        return

    # TEST 69 - Create an intentionally mismatched stranded topology lock.
    # WHY: Builds an unsafe recovery case where recorded lock target and desired state disagree.
    # PASS: Kubernetes creates the synthetic lock ConfigMap with the mismatched target.
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

    if not runner.admin_async(
        "Async Reconcile refuses to race an active ShardedCluster lock",
        "Reconcile",
        expect_success=False,
        expected_text="Reconcile is blocked",
        timeout=300,
    ).passed:
        return

    if not runner.admin_async(
        "Async lock recovery refuses a target that does not match desired state",
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

    if not runner.admin_async(
        "Async RecoverDeploymentLock releases the validated stranded lock",
        "RecoverDeploymentLock",
        admin_sc,
        "--confirm",
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

    if not runner.controller_async(
        "Reduce administrator cluster to one shard for DeleteShard recovery",
        "DeleteShard",
        admin_sc,
        "1",
        "--confirm",
        timeout=2400,
    ).passed:
        return

    delete_lock_id = f"harness-delete-{ctx.run_id}"
    if not runner.run(
        "Create valid synthetic stranded DeleteShard lock",
        kubectl
        + [
            "-n",
            namespace,
            "create",
            "configmap",
            lock_name,
            f"--from-literal=operation_id={delete_lock_id}",
            "--from-literal=category=topology",
            "--from-literal=action=DeleteShard",
            "--from-literal=database=",
            "--from-literal=start_shards=2",
            "--from-literal=target_shards=1",
            "--from-literal=started_at=2026-01-01T00:00:00Z",
        ],
        timeout=60,
    ).passed:
        return

    if not runner.admin_async(
        "Async RecoverDeploymentLock validates completed DeleteShard cleanup",
        "RecoverDeploymentLock",
        admin_sc,
        "--confirm",
        timeout=900,
    ).passed:
        return

    if not runner.run(
        "Recovered DeleteShard deployment lock is absent",
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

    if not runner.admin_async(
        "Async Reconcile succeeds after deployment-lock recovery",
        "Reconcile",
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

    # Test 88 is itself the destructive Vault metadata action. In selective
    # mode, do not perform that hidden mutation unless Test 88 was requested.
    if runner.is_test_selected(88):
        metadata_ok, metadata_note = _delete_vault_deployment_metadata(
            config,
            orphan_rs,
        )
    else:
        metadata_ok = True
        metadata_note = "Test 88 was not selected."
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

    if not runner.admin_async(
        "Orphan recovery refuses a live managed MongoDB resource",
        "RecoverOrphanedResources",
        "--confirm",
        timeout=300,
        expect_success=False,
        expected_text="live privateWorkerReplacement-managed MongoDB",
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

    # Test 93 deliberately deletes the fixture's Ops Manager project. Keep
    # selective runs exact: do not perform that side effect when Test 93 is not
    # part of --testList.
    if runner.is_test_selected(93):
        ops_ok, ops_note, ops_elapsed = _delete_ops_manager_project(
            config,
            orphan_rs,
        )
    else:
        ops_ok = True
        ops_note = "Test 93 was not selected."
        ops_elapsed = 0.0
    if not runner.check(
        "Delete orphan-test Ops Manager project and group Secret",
        ops_ok,
        ops_note,
        elapsed_seconds=ops_elapsed,
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

    # Test 97 is also useful by itself after a failed full run. Include any
    # globally orphaned Operator/Helm runtime artifacts, not only resources that
    # happen to contain this new selective run's generated names.
    leftovers.extend(
        list_orphan_operator_artifacts(
            config,
            managed_deployment_keys=set(),
        )
    )
    leftovers = sorted(set(leftovers))
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

    runner.admin_async(
        "Final async Reconcile confirms there is no desired state left to repair",
        "Reconcile",
        timeout=300,
    )
