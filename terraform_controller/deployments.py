"""ReplicaSet, ShardedCluster, and shard lifecycle orchestration.

This module owns deployment-level policy:
- validate deployment names/types,
- require healthy resources before changes,
- create/delete ReplicaSets and ShardedClusters through Terraform,
- add/delete shard counts through Terraform,
- enforce the one-shard minimum,
- block unsafe shard deletion,
- format deployment/shard status.

Python does not edit MongoDB CRs or persistent volumes directly.  Every managed
change is expressed as desired state and passed to apply_inventory().
"""

from __future__ import annotations

from typing import Any

from . import kube
from .common import ControllerError, iso_utc, normalize_deployment, print_table, utc_now
from .logging_component import log_event
from .terraform_runner import apply_inventory
from .deployment_lock import (
    acquire_deployment_lock,
    describe_deployment_lock,
    read_deployment_lock,
    release_deployment_lock,
    require_no_active_change,
    validate_topology_resume,
)
from .vault import VaultClient


def deployment_type_label(deployment: dict[str, Any]) -> str:
    """Return the normalized deployment type, defaulting legacy state to ReplicaSet."""
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
            f"MongoDB deployment '{name}' does not exist in terraformController. "
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

    This implements the convenience rule used by database commands: omission is
    allowed only when exactly one deployment exists.
    """

    if name:
        return require_deployment(inventory, name)
    if not inventory:
        raise ControllerError(
            "No terraformController-managed MongoDB deployments exist. "
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

    ReplicaSet readiness is the MongoDB phase.  ShardedCluster readiness is
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
            config, deployment_key, int(deployment["shard_count"])
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


def _new_common(
    config: dict[str, Any], display: str, deployment_type: str
) -> dict[str, Any]:
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
    if key in inventory:
        existing = inventory[key]
        raise ControllerError(
            f"Deployment name '{display}' is already used by "
            f"{deployment_type_label(existing)} '{existing['display_name']}'."
        )
    live = kube.get_json(config, "mongodb", key)
    if live is not None:
        labels = live.get("metadata", {}).get("labels", {})
        if labels.get("app.kubernetes.io/managed-by") != "terraformController":
            raise ControllerError(
                f"Kubernetes MongoDB resource '{key}' already exists outside "
                "terraformController; automatic adoption is blocked."
            )
        spec_type = str(live.get("spec", {}).get("type", ""))
        if spec_type and spec_type != deployment_type:
            raise ControllerError(
                f"An incomplete terraformController Kubernetes resource named '{key}' "
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
        config, "mongodb", key, "Running", config["rs_ready_timeout"]
    )
    kube.wait_phase(
        config,
        "mongodbuser",
        kube.controller_user(key),
        "Updated",
        config["rs_ready_timeout"],
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
    config: dict[str, Any], vault: VaultClient, name: str, shards: int | None
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
        config, key, shard_count, config["sc_ready_timeout"]
    )
    kube.wait_phase(
        config,
        "mongodbuser",
        kube.controller_user(key),
        "Updated",
        config["sc_ready_timeout"],
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
    if not confirmed:
        raise ControllerError(
            f"{command} is destructive and requires '--confirm'. "
            f"Example: terraformController.py {command} {name} --confirm"
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
    log_event(
        "deployment.delete.succeeded",
        deployment=item["display_name"],
        deployment_type=expected_type,
    )
    print(f"\n{expected_type} '{item['display_name']}' was successfully deleted.")


def delete_replica_set(
    config: dict[str, Any], vault: VaultClient, name: str, confirmed: bool
) -> None:
    """Delete an empty ReplicaSet after managed and live DB checks pass."""

    _delete_deployment(
        config, vault, name, confirmed, "ReplicaSet", "DeleteReplicaSet"
    )


def delete_sharded_cluster(
    config: dict[str, Any], vault: VaultClient, name: str, confirmed: bool
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


def _require_positive_shard_count(count: int) -> None:
    if count < 1:
        raise ControllerError("Shard COUNT must be at least 1.")


def _resume_or_acquire_lock(
    config: dict[str, Any],
    inventory: dict[str, dict[str, Any]],
    key: str,
    item: dict[str, Any],
    action: str,
    count: int,
    target: int,
) -> dict[str, Any]:
    existing = read_deployment_lock(config, key)
    if existing:
        validate_topology_resume(item, existing, action, count)
        print(
            f"Resuming {existing['action']} on ShardedCluster "
            f"'{item['display_name']}' ({existing['start_shards']} -> "
            f"{existing['target_shards']} shards) ..."
        )
        log_event(
            "shard.operation.resumed",
            deployment=item["display_name"],
            lock_action=existing["action"],
            start_shards=existing["start_shards"],
            target_shards=existing["target_shards"],
            operation_id=existing["operation_id"],
        )
        return existing

    require_running(config, key, item)
    return acquire_deployment_lock(
        config,
        inventory,
        key,
        item,
        category="topology",
        action=action,
        start_shards=int(item["shard_count"]),
        target_shards=target,
    )


def _raise_topology_failure(
    item: dict[str, Any],
    action: str,
    count: int,
    exc: ControllerError,
) -> None:
    raise ControllerError(
        f"{action} did not complete for ShardedCluster '{item['display_name']}'. "
        "The deployment lock remains in place to prevent conflicting changes. "
        f"Correct the reported problem, then rerun '{action} "
        f"{item['display_name']} {count}'"
        + (" --confirm" if action == "DeleteShard" else "")
        + f" to resume safely. Underlying error: {exc}"
    ) from exc


def add_shard(
    config: dict[str, Any], vault: VaultClient, name: str, count: int = 1
) -> None:
    """Add COUNT shards with a resume-safe, two-stage Terraform workflow.

    Stage 1 prepares all required persistent storage.  Stage 2 raises shardCount.
    A deployment lock prevents concurrent mutations until the target is Online.
    """

    _require_positive_shard_count(count)
    inventory = vault.load_inventory()
    key, item = require_deployment(inventory, name, "ShardedCluster")

    existing = read_deployment_lock(config, key)
    if existing:
        validate_topology_resume(item, existing, "AddShard", count)
        target = int(existing["target_shards"])
        start = int(existing["start_shards"])
        lock = existing
        print(
            f"Resuming AddShard on ShardedCluster '{item['display_name']}' "
            f"({start} -> {target} shards) ..."
        )
    else:
        start = int(item["shard_count"])
        target = start + count
        lock = _resume_or_acquire_lock(
            config, inventory, key, item, "AddShard", count, target
        )

    log_event(
        "shard.add.requested",
        deployment=item["display_name"],
        add_count=count,
        previous_shards=start,
        target_shards=target,
        operation_id=lock["operation_id"],
    )

    try:
        if int(item["storage_shard_count"]) < target:
            item["storage_shard_count"] = target
            apply_inventory(config, inventory)

        if int(item["shard_count"]) < target:
            item["shard_count"] = target
            apply_inventory(config, inventory)

        print(
            f"Waiting for ShardedCluster '{item['display_name']}' to reach "
            f"{target} fully online shards ..."
        )
        kube.wait_sharded_cluster_ready(
            config, key, target, config["sc_ready_timeout"]
        )

        release_deployment_lock(config, inventory, key, item, lock)
    except ControllerError as exc:
        _raise_topology_failure(item, "AddShard", count, exc)

    log_event(
        "shard.add.succeeded",
        deployment=item["display_name"],
        added=count,
        previous_shards=start,
        shard_count=target,
    )
    print(
        f"\nSuccessfully added {count} shard(s) to ShardedCluster "
        f"'{item['display_name']}'."
    )
    print(f"Previous shards: {start}")
    print(f"Total shards:    {target}")
    print("Status:          Running")
    if item["databases"]:
        print(f"Databases:       {len(item['databases'])} preserved")


def delete_shard(
    config: dict[str, Any],
    vault: VaultClient,
    name: str,
    count: int = 1,
    confirmed: bool = False,
) -> None:
    """Delete COUNT highest-numbered shards while always retaining at least one.

    Shard removal is allowed while application databases exist. Python owns
    validation, locking, waiting, and reporting only. The topology mutation is
    still Terraform-driven: Terraform lowers the MongoDB ShardedCluster
    spec.shardCount and the MongoDB Kubernetes Operator/Ops Manager reconciles
    the supported scale-down before Terraform removes old storage.
    """

    _require_positive_shard_count(count)
    if not confirmed:
        raise ControllerError(
            "DeleteShard is destructive and requires '--confirm'. "
            f"Example: terraformController.py DeleteShard {name} {count} --confirm"
        )

    inventory = vault.load_inventory()
    key, item = require_deployment(inventory, name, "ShardedCluster")
    existing = read_deployment_lock(config, key)

    if existing:
        validate_topology_resume(item, existing, "DeleteShard", count)
        start = int(existing["start_shards"])
        target = int(existing["target_shards"])
        lock = existing
        print(
            f"Resuming DeleteShard on ShardedCluster '{item['display_name']}' "
            f"({start} -> {target} shards) ..."
        )
    else:
        start = int(item["shard_count"])
        target = start - count
        if target < 1:
            maximum = max(0, start - 1)
            raise ControllerError(
                f"Cannot delete {count} shard(s) from ShardedCluster "
                f"'{item['display_name']}'. It currently has {start} shard(s), "
                "and a ShardedCluster must retain at least 1 shard. "
                f"Maximum deletable now: {maximum}."
            )

        require_running(config, key, item)


        lock = acquire_deployment_lock(
            config,
            inventory,
            key,
            item,
            category="topology",
            action="DeleteShard",
            start_shards=start,
            target_shards=target,
        )

    log_event(
        "shard.delete.requested",
        deployment=item["display_name"],
        delete_count=count,
        previous_shards=start,
        target_shards=target,
        operation_id=lock["operation_id"],
    )

    try:
        if int(item["shard_count"]) > target:
            print(
                f"Scaling ShardedCluster '{item['display_name']}' from "
                f"{start} to {target} shard(s) through Terraform ..."
            )
            if item["databases"]:
                print(
                    "Existing databases will be preserved while the MongoDB "
                    "Kubernetes Operator/Ops Manager reconciles the shard scale-down."
                )
            item["shard_count"] = target
            apply_inventory(config, inventory)

        kube.wait_sharded_cluster_ready(
            config, key, target, config["sc_ready_timeout"]
        )
        for index in range(target, start):
            kube.wait_absent(
                config,
                "statefulset",
                f"{key}-{index}",
                config["sc_ready_timeout"],
            )

        if int(item["storage_shard_count"]) > target:
            item["storage_shard_count"] = target
            apply_inventory(config, inventory)

        release_deployment_lock(config, inventory, key, item, lock)
    except ControllerError as exc:
        _raise_topology_failure(item, "DeleteShard", count, exc)

    log_event(
        "shard.delete.succeeded",
        deployment=item["display_name"],
        deleted=count,
        previous_shards=start,
        shard_count=target,
    )
    print(
        f"\nSuccessfully deleted {count} shard(s) from ShardedCluster "
        f"'{item['display_name']}'."
    )
    print(f"Previous shards: {start}")
    print(f"Total shards:    {target}")
    print("Status:          Running")


def _deployment_row(
    config: dict[str, Any], key: str, item: dict[str, Any]
) -> tuple[str, ...]:
    dtype = deployment_type_label(item)
    if dtype == "ShardedCluster":
        topology = (
            f"{item['shard_count']} shards / "
            f"{item['members_per_shard']} members"
        )
    else:
        topology = f"{item['members']} members"
    return (
        item["display_name"],
        dtype,
        kube.phase(config, key),
        topology,
        item["version"],
        str(len(item["databases"])),
    )


def list_deployments(config: dict[str, Any], vault: VaultClient) -> None:
    """List all managed ReplicaSets and ShardedClusters."""
    inventory = vault.load_inventory()
    if not inventory:
        print("No terraformController-managed MongoDB deployments exist.")
        return
    rows = [
        _deployment_row(config, key, inventory[key])
        for key in sorted(inventory)
    ]
    print_table(
        ("DEPLOYMENT", "TYPE", "PHASE", "TOPOLOGY", "MONGODB", "DATABASES"),
        rows,
    )


def list_replica_sets(config: dict[str, Any], vault: VaultClient) -> None:
    inventory = vault.load_inventory()
    rows = [
        _deployment_row(config, key, inventory[key])
        for key in sorted(inventory)
        if deployment_type_label(inventory[key]) == "ReplicaSet"
    ]
    if not rows:
        print("No terraformController-managed ReplicaSets exist.")
        return
    print_table(
        ("REPLICA SET", "TYPE", "PHASE", "TOPOLOGY", "MONGODB", "DATABASES"),
        rows,
    )


def list_sharded_clusters(config: dict[str, Any], vault: VaultClient) -> None:
    inventory = vault.load_inventory()
    rows = [
        _deployment_row(config, key, inventory[key])
        for key in sorted(inventory)
        if deployment_type_label(inventory[key]) == "ShardedCluster"
    ]
    if not rows:
        print("No terraformController-managed ShardedClusters exist.")
        return
    print_table(
        (
            "SHARDED CLUSTER",
            "TYPE",
            "PHASE",
            "TOPOLOGY",
            "MONGODB",
            "DATABASES",
        ),
        rows,
    )


def _status_count(item: dict[str, Any], lock: dict[str, Any] | None) -> int:
    count = int(item["shard_count"])
    if not lock:
        return count
    return max(
        count,
        int(lock["start_shards"]),
        int(lock["target_shards"]),
    )


def _shard_status(
    config: dict[str, Any],
    key: str,
    item: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    lock = read_deployment_lock(config, key)
    status = kube.sharded_cluster_status(
        config, key, _status_count(item, lock)
    )

    if lock:
        start = int(lock["start_shards"])
        target = int(lock["target_shards"])
        for index, shard in enumerate(status["shards"]):
            if (
                lock["action"] == "AddShard"
                and start <= index < target
                and shard["desired"] == 0
            ):
                shard["status"] = "Creating"
            elif lock["action"] == "DeleteShard" and target <= index < start:
                shard["status"] = (
                    "Removed" if shard["desired"] == 0 else "Removing"
                )
    return status, lock


def _print_shards(
    config: dict[str, Any], key: str, item: dict[str, Any]
) -> None:
    status, lock = _shard_status(config, key, item)
    rows = [
        (
            x["shard"],
            x["status"],
            str(x["ready"]),
            str(x["desired"]),
            str(x["updated"]),
        )
        for x in status["shards"]
    ]
    print_table(("SHARD", "STATUS", "READY", "DESIRED", "UPDATED"), rows)
    if lock:
        print(f"Active change:   {describe_deployment_lock(lock)}")
        if lock["started_at"]:
            print(f"Started:         {lock['started_at']}")
    else:
        print("Active change:   None")
    print(
        f"Config servers: {status['config_servers']['status']} "
        f"({status['config_servers']['ready']}/"
        f"{status['config_servers']['desired']})"
    )
    print(
        f"mongos:         {status['mongos']['status']} "
        f"({status['mongos']['ready']}/{status['mongos']['desired']})"
    )


def list_deployment(
    config: dict[str, Any], vault: VaultClient, name: str
) -> None:
    inventory = vault.load_inventory()
    key, item = require_deployment(inventory, name)
    dtype = deployment_type_label(item)
    print(f"Deployment:      {item['display_name']}")
    print(f"Type:            {dtype}")
    print(f"K8s Resource:    {key}")
    print(f"Phase:           {kube.phase(config, key)}")
    print(f"MongoDB:         {item['version']}")
    print(f"Databases:       {len(item['databases'])}")
    if dtype == "ReplicaSet":
        print(f"Members:         {item['members']}")
    else:
        print(f"Shards:          {item['shard_count']}")
        print(f"Members/Shard:   {item['members_per_shard']}")
        print(f"mongos:          {item['mongos_count']}")
        print(f"Config Servers:  {item['config_server_count']}")
        print()
        _print_shards(config, key, item)


def list_replica_set(
    config: dict[str, Any], vault: VaultClient, name: str
) -> None:
    inventory = vault.load_inventory()
    _, item = require_deployment(inventory, name, "ReplicaSet")
    list_deployment(config, vault, item["display_name"])


def list_sharded_cluster(
    config: dict[str, Any], vault: VaultClient, name: str
) -> None:
    inventory = vault.load_inventory()
    _, item = require_deployment(inventory, name, "ShardedCluster")
    list_deployment(config, vault, item["display_name"])


def list_shards(
    config: dict[str, Any],
    vault: VaultClient,
    name: str | None = None,
) -> None:
    """List shard status globally or for one targeted ShardedCluster."""

    inventory = vault.load_inventory()

    if name:
        key, item = require_deployment(inventory, name, "ShardedCluster")
        print(f"ShardedCluster: {item['display_name']}")
        print(f"Phase:          {kube.phase(config, key)}")
        print()
        _print_shards(config, key, item)
        return

    clusters = [
        (key, inventory[key])
        for key in sorted(inventory)
        if deployment_type_label(inventory[key]) == "ShardedCluster"
    ]
    if not clusters:
        print("No terraformController-managed ShardedClusters exist.")
        return

    rows: list[tuple[str, ...]] = []
    for key, item in clusters:
        status, lock = _shard_status(config, key, item)
        change = describe_deployment_lock(lock) if lock else "-"
        for shard in status["shards"]:
            rows.append(
                (
                    item["display_name"],
                    shard["shard"],
                    shard["status"],
                    str(shard["ready"]),
                    str(shard["desired"]),
                    str(shard["updated"]),
                    change,
                )
            )

    print_table(
        (
            "CLUSTER",
            "SHARD",
            "STATUS",
            "READY",
            "DESIRED",
            "UPDATED",
            "ACTIVE CHANGE",
        ),
        rows,
    )
