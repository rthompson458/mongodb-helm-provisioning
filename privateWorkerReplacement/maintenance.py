"""Reconcile all Vault-backed desired state through Terraform.

Reconcile is the repair/convergence command.  It does not invent new desired
state; it reloads the state already recorded in Vault, asks Terraform to apply
it, then verifies Kubernetes resources converge.

A live ShardedCluster mutation lock blocks Reconcile because a broad Terraform
apply must not race with an AddShard/DeleteShard/database operation.
"""

from __future__ import annotations

from typing import Any

from . import kube
from .common import ControllerError, account_resource_name
from .deployment_lock import (
    describe_deployment_lock,
    read_deployment_lock,
    release_deployment_lock,
)
from .deployments import deployment_type_label, require_deployment
from .logging_component import log_event
from .ops_manager import list_projects as list_ops_manager_projects
from .terraform_runner import apply_inventory
from .vault import VaultClient


MANAGED_BY_SELECTOR = "app.kubernetes.io/managed-by=privateWorkerReplacement"
DEPLOYMENT_LOCK_PREFIX = "tc-deployment-lock-"


def _kubernetes_names(items: list[dict[str, Any]]) -> list[str]:
    """Return stable Kubernetes resource names for administrator reporting."""

    names = [
        str(item.get("metadata", {}).get("name", "<unknown>"))
        for item in items
    ]
    return sorted(names)


def managed_resource_inventory(
    config: dict[str, Any],
    vault: VaultClient,
) -> dict[str, list[str]]:
    """Return a read-only inventory of controller-owned resources."""

    inventory = vault.load_inventory()

    deployments = sorted(
        str(deployment.get("display_name", key))
        for key, deployment in inventory.items()
    )

    replica_sets = sorted(
        str(deployment.get("display_name", key))
        for key, deployment in inventory.items()
        if deployment.get("deployment_type") == "ReplicaSet"
    )

    sharded_clusters = sorted(
        str(deployment.get("display_name", key))
        for key, deployment in inventory.items()
        if deployment.get("deployment_type") == "ShardedCluster"
    )

    databases = []
    accounts = []

    for key, deployment in inventory.items():
        deployment_name = str(deployment.get("display_name", key))

        for db_key, db in deployment.get("databases", {}).items():
            db_name = str(db.get("display_name", db_key))
            databases.append(f"{deployment_name}/{db_name}")

            for suffix in ("owner", "readWrite", "read"):
                accounts.append(
                    f"{deployment_name}/{db_name}/{db_name}_{suffix}"
                )

    mongodb_resources = kube.list_json(
        config,
        "mongodb",
        label_selector=MANAGED_BY_SELECTOR,
    )
    mongodb_users = kube.list_json(
        config,
        "mongodbuser",
        label_selector=MANAGED_BY_SELECTOR,
    )
    pvcs = kube.list_json(
        config,
        "pvc",
        label_selector=MANAGED_BY_SELECTOR,
    )
    pvs = kube.list_json(
        config,
        "pv",
        label_selector=MANAGED_BY_SELECTOR,
        namespaced=False,
    )

    secrets = kube.list_json(config, "secret")
    configmaps = kube.list_json(config, "configmap")

    secret_names = _kubernetes_names(secrets)
    configmap_names = _kubernetes_names(configmaps)

    # Controller-owned secrets such as admin/database credentials are active
    # DBaaS resources. Ops Manager group secrets are classified separately
    # because their project IDs let us detect orphaned Operator artifacts.
    controller_secrets = sorted(
        name
        for name in secret_names
        if name.startswith("tc-")
    )

    group_secrets = sorted(
        name
        for name in secret_names
        if name.endswith("-group-secret")
    )

    # Terraform backend state and the shared Ops Manager ConfigMap are permanent
    # controller infrastructure. They remain present when there are zero
    # customer deployments and therefore must not make zero-state "dirty".
    terraform_states = sorted(
        name
        for name in secret_names
        if name.startswith("tfstate-")
        and name.endswith(config["backend_secret_suffix"])
    )

    controller_infrastructure_configmaps = sorted(
        name
        for name in configmap_names
        if name == "tc-ops-manager-projects"
    )

    controller_configmaps = sorted(
        name
        for name in configmap_names
        if name.startswith("tc-")
        and name != "tc-ops-manager-projects"
        and not name.startswith(DEPLOYMENT_LOCK_PREFIX)
    )

    locks = sorted(
        name
        for name in configmap_names
        if name.startswith(DEPLOYMENT_LOCK_PREFIX)
    )

    permanent_project, projects = list_ops_manager_projects(config)

    expected_project_names = {
        name.lower()
        for name in deployments
    }

    platform_project = next(
        (
            project
            for project in projects
            if project["name"].lower() == permanent_project.lower()
        ),
        None,
    )

    nonplatform_projects = [
        project
        for project in projects
        if project["name"].lower() != permanent_project.lower()
    ]

    active_projects = [
        project
        for project in nonplatform_projects
        if project["name"].lower() in expected_project_names
    ]

    orphan_projects = [
        project
        for project in nonplatform_projects
        if project["name"].lower() not in expected_project_names
    ]

    actual_project_names = {
        project["name"].lower()
        for project in nonplatform_projects
    }

    missing_ops_manager_projects = sorted(
        name
        for name in deployments
        if name.lower() not in actual_project_names
    )

    project_ids = {
        project["id"]
        for project in projects
        if project["id"]
    }

    platform_project_id = (
        platform_project["id"]
        if platform_project is not None
        else ""
    )

    active_project_ids = {
        project["id"]
        for project in active_projects
        if project["id"]
    }

    platform_group_secrets = []
    active_group_secrets = []
    orphan_group_secrets = []

    for secret_name in group_secrets:
        project_id = secret_name.removesuffix("-group-secret")
        rendered = f"{secret_name} (Project ID: {project_id})"

        if project_id == platform_project_id:
            platform_group_secrets.append(rendered)
        elif project_id in active_project_ids:
            active_group_secrets.append(rendered)
        elif project_id not in project_ids:
            orphan_group_secrets.append(rendered)
        else:
            # A live non-platform Ops Manager project that is not represented in
            # Vault is itself orphaned; keep its group secret in the orphan view.
            orphan_group_secrets.append(rendered)

    ops_manager_platform_project = (
        [
            f"{platform_project['name']} (Project ID: {platform_project['id']})"
        ]
        if platform_project is not None
        else [f"{permanent_project} (Project ID: NOT FOUND)"]
    )

    ops_manager_projects = sorted(
        f"{project['name']} (Project ID: {project['id']})"
        for project in active_projects
    )

    ops_manager_orphans = sorted(
        f"{project['name']} (Project ID: {project['id']})"
        for project in orphan_projects
    )

    return {
        "managed_deployments": deployments,
        "replica_sets": replica_sets,
        "sharded_clusters": sharded_clusters,
        "databases": sorted(databases),
        "managed_accounts": sorted(accounts),
        "mongodb_resources": _kubernetes_names(mongodb_resources),
        "mongodb_users": _kubernetes_names(mongodb_users),
        "pvcs": _kubernetes_names(pvcs),
        "pvs": _kubernetes_names(pvs),
        "controller_secrets": controller_secrets,
        "controller_configmaps": controller_configmaps,
        "controller_infrastructure_configmaps": controller_infrastructure_configmaps,
        "terraform_states": terraform_states,
        "deployment_locks": locks,
        "ops_manager_platform_project": ops_manager_platform_project,
        "ops_manager_projects": ops_manager_projects,
        "ops_manager_orphans": ops_manager_orphans,
        "ops_manager_platform_group_secrets": sorted(platform_group_secrets),
        "ops_manager_group_secrets": sorted(active_group_secrets),
        "orphan_group_secrets": sorted(orphan_group_secrets),
        "missing_ops_manager_projects": missing_ops_manager_projects,
    }


def reconcile(config: dict[str, Any], vault: VaultClient) -> None:
    """Reapply complete Vault-backed desired state and verify convergence."""
    inventory = vault.load_inventory()
    if not inventory:
        print("No privateWorkerReplacement-managed MongoDB deployments exist. Nothing to reconcile.")
        return

    # Reconcile touches the complete inventory.  Refuse to start while any
    # ShardedCluster has a protected mutation in flight.
    active = []
    for key in sorted(inventory):
        deployment = inventory[key]
        if deployment_type_label(deployment) != "ShardedCluster":
            continue
        lock = read_deployment_lock(config, key)
        if lock:
            active.append(
                f"  {deployment['display_name']}: {describe_deployment_lock(lock)}"
            )

    if active:
        raise ControllerError(
            "Reconcile is blocked while a ShardedCluster managed change is in progress:\n"
            + "\n".join(active)
            + "\nUse ListShards [SHARDED_CLUSTER] to view progress."
        )

    log_event("reconcile.requested", deployments=len(inventory))
    # From this point forward Terraform owns the mutation.  Python only waits
    # for the resulting Kubernetes state and reports it.
    apply_inventory(config, inventory)
    print("\nWaiting for managed deployments and accounts to converge ...")

    for key in sorted(inventory):
        deployment = inventory[key]
        dtype = deployment_type_label(deployment)
        timeout = (
            config["sc_ready_timeout"]
            if dtype == "ShardedCluster"
            else config["rs_ready_timeout"]
        )

        if dtype == "ShardedCluster":
            kube.wait_sharded_cluster_ready(
                config, key, int(deployment["shard_count"]), timeout
            )
        else:
            kube.wait_phase(config, "mongodb", key, "Running", timeout)

        kube.wait_phase(
            config,
            "mongodbuser",
            kube.controller_user(key),
            "Updated",
            timeout,
        )

        for db_key in sorted(deployment["databases"]):
            db = deployment["databases"][db_key]
            if db["owner_disabled"]:
                kube.wait_absent(
                    config,
                    "mongodbuser",
                    account_resource_name(key, db_key, "owner"),
                    timeout,
                )
                accounts = ("readwrite", "read")
            else:
                accounts = ("owner", "readwrite", "read")

            for account in accounts:
                kube.wait_phase(
                    config,
                    "mongodbuser",
                    account_resource_name(key, db_key, account),
                    "Updated",
                    timeout,
                )

        print(f"  {deployment['display_name']} ({dtype}): Running")

    log_event("reconcile.succeeded", deployments=len(inventory))
    print("\nReconcile complete.")



def recover_deployment_lock(
    config: dict[str, Any],
    vault: VaultClient,
    name: str,
    confirmed: bool,
) -> None:
    """Release a completed topology lock without reconciling unrelated storage.

    This recovery path is intentionally narrow. It is for an interrupted
    AddShard/DeleteShard where MongoDB has already reached the lock's target
    topology but a later bookkeeping/storage step failed.

    The lock release itself is still Terraform-driven. A targeted Terraform
    apply executes only terraform_data.lifecycle_operation so unrelated legacy
    storage resources are not reconciled during recovery.
    """

    if not confirmed:
        raise ControllerError(
            "RecoverDeploymentLock is a recovery action and requires '--confirm'. "
            f"Example: privateWorkerReplacementAdmin.py RecoverDeploymentLock {name} --confirm"
        )

    inventory = vault.load_inventory()
    key, deployment = require_deployment(inventory, name, "ShardedCluster")
    lock = read_deployment_lock(config, key)
    if not lock:
        raise ControllerError(
            f"ShardedCluster '{deployment['display_name']}' has no active deployment lock."
        )
    if lock["category"] != "topology" or lock["action"] not in {"AddShard", "DeleteShard"}:
        raise ControllerError(
            f"RecoverDeploymentLock only supports topology locks. Active change: "
            f"{describe_deployment_lock(lock)}."
        )

    target = int(lock["target_shards"])
    recorded = int(deployment["shard_count"])
    if recorded != target:
        raise ControllerError(
            f"Cannot recover lock for ShardedCluster '{deployment['display_name']}'. "
            f"Vault desired shard count is {recorded}, but the lock target is {target}."
        )

    mongodb = kube.get_json(config, "mongodb", key)
    if not mongodb:
        raise ControllerError(
            f"Cannot recover lock for ShardedCluster '{deployment['display_name']}' "
            "because the MongoDB resource is absent."
        )
    live_target = int(mongodb.get("spec", {}).get("shardCount", 0) or 0)
    if live_target != target:
        raise ControllerError(
            f"Cannot recover lock for ShardedCluster '{deployment['display_name']}'. "
            f"Live MongoDB shardCount is {live_target}, but the lock target is {target}."
        )

    status = kube.sharded_cluster_status(config, key, target)
    shards_ready = all(x["status"] == "Online" for x in status["shards"])
    config_ready = status["config_servers"]["status"] == "Online"
    mongos_ready = status["mongos"]["status"] == "Online"
    if not (
        status["phase"] == "Running"
        and shards_ready
        and config_ready
        and mongos_ready
    ):
        raise ControllerError(
            f"Cannot recover lock for ShardedCluster '{deployment['display_name']}'. "
            "The target topology is not fully healthy."
        )

    if lock["action"] == "DeleteShard":
        for index in range(target, int(lock["start_shards"])):
            if kube.get_json(config, "statefulset", f"{key}-{index}") is not None:
                raise ControllerError(
                    f"Cannot recover lock for ShardedCluster '{deployment['display_name']}'. "
                    f"Removed shard StatefulSet '{key}-{index}' still exists."
                )

    log_event(
        "deployment_lock.recovery.requested",
        deployment=deployment["display_name"],
        lock_action=lock["action"],
        start_shards=int(lock["start_shards"]),
        target_shards=target,
        operation_id=lock["operation_id"],
    )

    release_deployment_lock(
        config,
        inventory,
        key,
        deployment,
        lock,
        targets=["terraform_data.lifecycle_operation"],
    )

    log_event(
        "deployment_lock.recovery.succeeded",
        deployment=deployment["display_name"],
        lock_action=lock["action"],
        operation_id=lock["operation_id"],
    )
    print(
        f"Recovered completed {lock['action']} lock for ShardedCluster "
        f"'{deployment['display_name']}'."
    )
    print(f"Current shards: {target}")
    print("Status:         Running")
    print("Deployment lock: Released")



def recover_orphaned_resources(
    config: dict[str, Any],
    vault: VaultClient,
    confirmed: bool,
) -> None:
    """Finish Terraform cleanup after desired-state inventory is already empty.

    This is an exceptional recovery path for a failed deployment destroy that
    removed its Vault inventory before Terraform finished destroying all
    controller-managed Kubernetes/storage resources.

    Safety rules are intentionally strict:
    - explicit --confirm is required,
    - Vault inventory must already be completely empty,
    - Kubernetes must contain no privateWorkerReplacement-managed MongoDB CRs.

    Only after both independent checks prove there is no live managed deployment
    does Terraform receive an empty desired-state inventory so it can finish
    destroying any resources still recorded in the controller backend state.
    """

    if not confirmed:
        raise ControllerError(
            "RecoverOrphanedResources is destructive and requires '--confirm'. "
            "Example: privateWorkerReplacementAdmin.py RecoverOrphanedResources --confirm"
        )

    inventory = vault.load_inventory()
    if inventory:
        names = ", ".join(
            inventory[key]["display_name"] for key in sorted(inventory)
        )
        raise ControllerError(
            "RecoverOrphanedResources is allowed only when the Vault-backed "
            f"controller inventory is empty. Managed deployment(s) still exist: {names}."
        )

    live = kube.list_json(
        config,
        "mongodb",
        label_selector="app.kubernetes.io/managed-by=privateWorkerReplacement",
    )
    if live:
        names = ", ".join(
            str(item.get("metadata", {}).get("name", "<unknown>"))
            for item in live
        )
        raise ControllerError(
            "RecoverOrphanedResources refused because live "
            "privateWorkerReplacement-managed MongoDB resource(s) still exist: "
            f"{names}."
        )

    log_event("orphaned_resources.recovery.requested")
    print(
        "Vault inventory is empty and no live privateWorkerReplacement-managed "
        "MongoDB deployments exist."
    )
    print("Applying empty desired state through Terraform to finish orphan cleanup ...")

    apply_inventory(config, {})

    remaining = kube.list_json(
        config,
        "mongodb",
        label_selector="app.kubernetes.io/managed-by=privateWorkerReplacement",
    )
    if remaining:
        raise ControllerError(
            "Terraform cleanup completed, but a managed MongoDB resource is still present."
        )

    log_event("orphaned_resources.recovery.succeeded")
    print("\nOrphaned privateWorkerReplacement resources were successfully reconciled.")
    print("Managed deployments: 0")
    print("Status:              Clean")
