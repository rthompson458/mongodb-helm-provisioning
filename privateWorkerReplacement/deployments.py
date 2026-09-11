"""ReplicaSet, ShardedCluster, and shard lifecycle orchestration.

This module owns deployment-level lifecycle and shared deployment validation:
- validate deployment names/types;
- require healthy resources before changes; and
- create/delete ReplicaSets and ShardedClusters through Terraform.

Shard-topology mutations live in shards.py. Read-only deployment/shard
presentation lives in deployment_status.py. Python does not edit MongoDB CRs or
persistent volumes directly. Every managed change is expressed as desired state
and passed to apply_inventory().
"""

from __future__ import annotations

from typing import Any

from . import kube
from .common import ControllerError, iso_utc, normalize_deployment, utc_now
from .deployment_lock import require_no_active_change
from .logging_component import log_event
from .ops_manager import delete_project as delete_ops_manager_project
from .terraform_runner import apply_inventory
from .vault import VaultClient


def deployment_type_label(deployment: dict[str, Any]) -> str:
    """Return the normalized type, defaulting legacy records to ReplicaSet."""

    return deployment.get("deployment_type", "ReplicaSet")


def require_deployment(
    inventory: dict[str, dict[str, Any]],
    name: str,
    expected_type: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Find one managed deployment and optionally require a specific type."""

    key, _ = normalize_deployment(name)
    if key not in inventory:
        raise ControllerError(
            f"MongoDB deployment '{name}' does not exist in privateWorkerReplacement. "
            "Use ListDeployments to see available deployments."
        )
    deployment = inventory[key]
    actual = deployment_type_label(deployment)
    if expected_type and actual != expected_type:
        raise ControllerError(
            f"Deployment '{deployment['display_name']}' is a {actual}, not a {expected_type}."
        )
    return key, deployment


def resolve_deployment(
    config: dict[str, Any],
    inventory: dict[str, dict[str, Any]],
    name: str | None,
) -> tuple[str, dict[str, Any]]:
    """Resolve an explicit target or the sole managed deployment.

    Database commands may omit the deployment only when exactly one managed
    deployment exists. If multiple deployments exist, the error includes their
    live phase so the user can choose deliberately.
    """

    if name:
        return require_deployment(inventory, name)
    if not inventory:
        raise ControllerError(
            "No privateWorkerReplacement-managed MongoDB deployments exist. "
            "Create one first with AddReplicaSet or AddShardedCluster."
        )
    if len(inventory) == 1:
        key = next(iter(inventory))
        return key, inventory[key]

    choices = []
    for key in sorted(inventory):
        item = inventory[key]
        choices.append(
            f"  {item['display_name']}  {deployment_type_label(item)}  "
            f"{kube.phase(config, key)}"
        )
    raise ControllerError(
        "Multiple MongoDB deployments exist. Specify the target deployment.\n"
        "Available deployments:\n" + "\n".join(choices)
    )


def require_running(
    config: dict[str, Any],
    deployment_key: str,
    deployment: dict[str, Any],
) -> None:
    """Require a deployment to be ready before database/topology work.

    ReplicaSet readiness is the MongoDB phase. ShardedCluster readiness is
    stricter: overall Running plus every shard, config server, and mongos Online.
    """

    label = deployment_type_label(deployment)
    current = kube.phase(config, deployment_key)
    if current != "Running":
        message = kube.phase_message(config, deployment_key)
        raise ControllerError(
            f"{label} '{deployment['display_name']}' is not Running. "
            f"Current phase: {current}. No change was attempted."
            + (f" Operator message: {message}" if message else "")
        )

    if label == "ShardedCluster":
        status = kube.sharded_cluster_status(
            config,
            deployment_key,
            int(deployment["shard_count"]),
        )
        unavailable = [x for x in status["shards"] if x["status"] != "Online"]
        if unavailable:
            detail = ", ".join(
                f"{x['shard']}={x['status']}({x['ready']}/{x['desired']})"
                for x in unavailable
            )
            raise ControllerError(
                f"ShardedCluster '{deployment['display_name']}' is not ready for "
                f"database work. Shard status: {detail}. No change was attempted."
            )

        for component_name, component in (
            ("config servers", status["config_servers"]),
            ("mongos", status["mongos"]),
        ):
            if component["status"] != "Online":
                raise ControllerError(
                    f"ShardedCluster '{deployment['display_name']}' is not ready for "
                    f"database work. {component_name} status: {component['status']} "
                    f"({component['ready']}/{component['desired']}). "
                    "No change was attempted."
                )


def _deployment_operation(
    action: str,
    deployment_key: str,
    deployment: dict[str, Any],
) -> dict[str, Any]:
    """Build one Terraform operation used for deployment-level verification."""

    members = (
        int(deployment["members_per_shard"])
        if deployment_type_label(deployment) == "ShardedCluster"
        else int(deployment["members"])
    )
    return {
        "action": action,
        "deployment": deployment_key,
        "deployment_type": deployment_type_label(deployment),
        "database": "",
        "members": members,
    }


def _new_common(
    config: dict[str, Any],
    display: str,
    deployment_type: str,
) -> dict[str, Any]:
    """Build the fields shared by new ReplicaSet and ShardedCluster inventory."""

    return {
        "display_name": display,
        "deployment_type": deployment_type,
        "created_at": iso_utc(utc_now()),
        "members": config["default_members"],
        "version": config["default_version"],
        "persistent": config["persistent"],
        "storage_class": config["storage_class"],
        "storage_size": config["storage_size"],
        "storage_mode": config["storage_mode"],
        "storage_base_path": config["storage_base_path"],
        "storage_node_name": config["storage_node_name"],
        "controller_password_version": 1,
        "shard_count": 0,
        "storage_shard_count": 0,
        "members_per_shard": 0,
        "mongos_count": 0,
        "config_server_count": 0,
        "databases": {},
    }


def _check_new_name(
    config: dict[str, Any],
    inventory: dict[str, dict[str, Any]],
    key: str,
    display: str,
    deployment_type: str,
) -> None:
    """Refuse duplicate names and unsafe adoption of unrelated live resources."""

    if key in inventory:
        existing = inventory[key]
        raise ControllerError(
            f"Deployment name '{display}' is already used by "
            f"{deployment_type_label(existing)} '{existing['display_name']}'."
        )

    live = kube.get_json(config, "mongodb", key)
    if live is None:
        return

    labels = live.get("metadata", {}).get("labels", {})
    if labels.get("app.kubernetes.io/managed-by") != "privateWorkerReplacement":
        raise ControllerError(
            f"Kubernetes MongoDB resource '{key}' already exists outside "
            "privateWorkerReplacement; automatic adoption is blocked."
        )

    spec_type = str(live.get("spec", {}).get("type", ""))
    if spec_type and spec_type != deployment_type:
        raise ControllerError(
            f"An incomplete privateWorkerReplacement Kubernetes resource named '{key}' "
            f"exists as type '{spec_type}', not '{deployment_type}'."
        )


def add_replica_set(config: dict[str, Any], vault: VaultClient, name: str) -> None:
    """Create one empty managed ReplicaSet and wait until it is usable."""

    key, display = normalize_deployment(name)
    inventory = vault.load_inventory()
    _check_new_name(config, inventory, key, display, "ReplicaSet")
    log_event(
        "deployment.create.requested",
        deployment=display,
        deployment_type="ReplicaSet",
    )

    item = _new_common(config, display, "ReplicaSet")
    item["members"] = config["default_members"]
    inventory[key] = item

    apply_inventory(config, inventory)
    print(f"Waiting for ReplicaSet '{display}' to become Running ...")
    kube.wait_phase(
        config,
        "mongodb",
        key,
        "Running",
        config["rs_ready_timeout"],
    )
    kube.wait_phase(
        config,
        "mongodbuser",
        kube.controller_user(key),
        "Updated",
        config["rs_ready_timeout"],
    )

    # Operator status alone is not enough. Verify the hidden controller account
    # can really authenticate before declaring the deployment usable.
    print(
        f"Verifying controller administrator authentication for ReplicaSet "
        f"'{display}' ..."
    )
    apply_inventory(
        config,
        inventory,
        _deployment_operation("verify_controller_admin", key, item),
    )

    log_event(
        "deployment.create.succeeded",
        deployment=display,
        deployment_type="ReplicaSet",
    )
    print(f"\nReplicaSet '{display}' was successfully created.")
    print(f"Members:   {item['members']}")
    print("Status:    Running")
    print("Databases: 0")


def add_sharded_cluster(
    config: dict[str, Any],
    vault: VaultClient,
    name: str,
    shards: int | None,
) -> None:
    """Create one empty managed ShardedCluster and wait for every component."""

    key, display = normalize_deployment(name)
    inventory = vault.load_inventory()
    _check_new_name(config, inventory, key, display, "ShardedCluster")

    shard_count = int(shards if shards is not None else config["default_shards"])
    if shard_count < 1:
        raise ControllerError("--shards must be at least 1.")

    item = _new_common(config, display, "ShardedCluster")
    item.update(
        {
            "members": config["default_members_per_shard"],
            "shard_count": shard_count,
            "storage_shard_count": shard_count,
            "members_per_shard": config["default_members_per_shard"],
            "mongos_count": config["default_mongos"],
            "config_server_count": config["default_config_servers"],
        }
    )
    inventory[key] = item
    log_event(
        "deployment.create.requested",
        deployment=display,
        deployment_type="ShardedCluster",
        shards=shard_count,
    )

    apply_inventory(config, inventory)
    print(
        f"Waiting for ShardedCluster '{display}' and all {shard_count} shard(s) "
        "to become fully online ..."
    )
    kube.wait_sharded_cluster_ready(
        config,
        key,
        shard_count,
        config["sc_ready_timeout"],
    )
    kube.wait_phase(
        config,
        "mongodbuser",
        kube.controller_user(key),
        "Updated",
        config["sc_ready_timeout"],
    )
    print(
        f"Verifying controller administrator authentication for ShardedCluster "
        f"'{display}' ..."
    )
    apply_inventory(
        config,
        inventory,
        _deployment_operation("verify_controller_admin", key, item),
    )

    log_event(
        "deployment.create.succeeded",
        deployment=display,
        deployment_type="ShardedCluster",
        shards=shard_count,
    )
    print(f"\nShardedCluster '{display}' was successfully created.")
    print(f"Shards:            {shard_count}")
    print(f"Members per shard: {item['members_per_shard']}")
    print(f"mongos routers:    {item['mongos_count']}")
    print(f"Config servers:    {item['config_server_count']}")
    print("Status:            Running")
    print("Databases:         0")


def _delete_deployment(
    config: dict[str, Any],
    vault: VaultClient,
    name: str,
    confirmed: bool,
    expected_type: str,
    command: str,
) -> None:
    """Common validated delete workflow for ReplicaSets and ShardedClusters."""

    if not confirmed:
        raise ControllerError(
            f"{command} is destructive and requires '--confirm'. "
            f"Example: privateWorkerReplacement.py {command} {name} --confirm"
        )

    inventory = vault.load_inventory()
    key, item = require_deployment(inventory, name, expected_type)
    require_no_active_change(config, key, item)
    require_running(config, key, item)

    if item["databases"]:
        names = "\n".join(
            f"  {item['databases'][x]['display_name']}"
            for x in sorted(item["databases"])
        )
        raise ControllerError(
            f"{expected_type} '{item['display_name']}' contains managed databases:\n"
            f"{names}\nDelete the databases first."
        )

    # Validate against live MongoDB state before desired-state removal. This
    # prevents hidden user databases from being destroyed merely because Vault
    # metadata says the deployment is empty.
    apply_inventory(
        config,
        inventory,
        {
            "action": "validate_deployment_empty",
            "deployment": key,
            "deployment_type": expected_type,
            "database": "",
            "members": int(item["members"]),
        },
    )

    log_event(
        "deployment.delete.requested",
        deployment=item["display_name"],
        deployment_type=expected_type,
    )
    del inventory[key]
    apply_inventory(config, inventory)

    timeout = (
        config["sc_ready_timeout"]
        if expected_type == "ShardedCluster"
        else config["rs_ready_timeout"]
    )
    kube.wait_absent(config, "mongodb", key, timeout)

    # Each deployment gets its own Ops Manager project. Teardown is not complete
    # until the project and its Operator-created <PROJECT_ID>-group-secret are
    # both gone; delete_project performs and verifies that cross-plane cleanup.
    delete_ops_manager_project(
        config,
        item["display_name"],
        timeout=timeout,
    )

    log_event(
        "deployment.delete.succeeded",
        deployment=item["display_name"],
        deployment_type=expected_type,
    )
    print(f"\n{expected_type} '{item['display_name']}' was successfully deleted.")


def delete_replica_set(
    config: dict[str, Any],
    vault: VaultClient,
    name: str,
    confirmed: bool,
) -> None:
    """Delete an empty ReplicaSet after managed and live DB checks pass."""

    _delete_deployment(
        config,
        vault,
        name,
        confirmed,
        "ReplicaSet",
        "DeleteReplicaSet",
    )


def delete_sharded_cluster(
    config: dict[str, Any],
    vault: VaultClient,
    name: str,
    confirmed: bool,
) -> None:
    """Delete an empty ShardedCluster after safety and lock checks pass."""

    _delete_deployment(
        config,
        vault,
        name,
        confirmed,
        "ShardedCluster",
        "DeleteShardedCluster",
    )
