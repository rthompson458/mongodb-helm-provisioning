from __future__ import annotations

from typing import Any

from . import kube
from .common import ControllerError, iso_utc, normalize_deployment, print_table, utc_now
from .logging_component import log_event
from .terraform_runner import apply_inventory
from .vault import VaultClient


def deployment_type_label(deployment: dict[str, Any]) -> str:
    return deployment.get("deployment_type", "ReplicaSet")


def require_deployment(
    inventory: dict[str, dict[str, Any]],
    name: str,
    expected_type: str | None = None,
) -> tuple[str, dict[str, Any]]:
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
            f"  {item['display_name']}  {deployment_type_label(item)}  {kube.phase(config, key)}"
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
    label = deployment_type_label(deployment)
    current = kube.phase(config, deployment_key)
    if current != "Running":
        message = kube.phase_message(config, deployment_key)
        raise ControllerError(
            f"{label} '{deployment['display_name']}' is not Running. Current phase: {current}. "
            "No change was attempted."
            + (f" Operator message: {message}" if message else "")
        )

    if label == "ShardedCluster":
        status = kube.sharded_cluster_status(
            config, deployment_key, int(deployment["shard_count"])
        )
        unavailable = [
            x for x in status["shards"] if x["status"] != "Online"
        ]
        if unavailable:
            detail = ", ".join(
                f"{x['shard']}={x['status']}({x['ready']}/{x['desired']})"
                for x in unavailable
            )
            raise ControllerError(
                f"ShardedCluster '{deployment['display_name']}' is not ready for database work. "
                f"Shard status: {detail}. No change was attempted."
            )
        for component_name, component in (
            ("config servers", status["config_servers"]),
            ("mongos", status["mongos"]),
        ):
            if component["status"] != "Online":
                raise ControllerError(
                    f"ShardedCluster '{deployment['display_name']}' is not ready for database work. "
                    f"{component_name} status: {component['status']} "
                    f"({component['ready']}/{component['desired']}). No change was attempted."
                )


def _new_common(config: dict[str, Any], display: str, deployment_type: str) -> dict[str, Any]:
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
                f"An incomplete terraformController Kubernetes resource named '{key}' exists "
                f"as type '{spec_type}', not '{deployment_type}'."
            )


def add_replica_set(config: dict[str, Any], vault: VaultClient, name: str) -> None:
    key, display = normalize_deployment(name)
    inventory = vault.load_inventory()
    _check_new_name(config, inventory, key, display, "ReplicaSet")
    log_event("deployment.create.requested", deployment=display, deployment_type="ReplicaSet")

    item = _new_common(config, display, "ReplicaSet")
    item["members"] = config["default_members"]
    inventory[key] = item

    apply_inventory(config, inventory)
    print(f"Waiting for ReplicaSet '{display}' to become Running ...")
    kube.wait_phase(config, "mongodb", key, "Running", config["rs_ready_timeout"])
    kube.wait_phase(
        config, "mongodbuser", kube.controller_user(key), "Updated", config["rs_ready_timeout"]
    )

    log_event("deployment.create.succeeded", deployment=display, deployment_type="ReplicaSet")
    print(f"\nReplicaSet '{display}' was successfully created.")
    print(f"Members:   {item['members']}")
    print("Status:    Running")
    print("Databases: 0")


def add_sharded_cluster(
    config: dict[str, Any], vault: VaultClient, name: str, shards: int | None
) -> None:
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
        config, "mongodbuser", kube.controller_user(key), "Updated", config["sc_ready_timeout"]
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
    require_running(config, key, item)

    if item["databases"]:
        names = "\n".join(
            f"  {item['databases'][x]['display_name']}" for x in sorted(item["databases"])
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

    log_event("deployment.delete.requested", deployment=item["display_name"], deployment_type=expected_type)
    del inventory[key]
    apply_inventory(config, inventory)
    timeout = config["sc_ready_timeout"] if expected_type == "ShardedCluster" else config["rs_ready_timeout"]
    kube.wait_absent(config, "mongodb", key, timeout)
    log_event("deployment.delete.succeeded", deployment=item["display_name"], deployment_type=expected_type)
    print(f"\n{expected_type} '{item['display_name']}' was successfully deleted.")


def delete_replica_set(
    config: dict[str, Any], vault: VaultClient, name: str, confirmed: bool
) -> None:
    _delete_deployment(
        config, vault, name, confirmed, "ReplicaSet", "DeleteReplicaSet"
    )


def delete_sharded_cluster(
    config: dict[str, Any], vault: VaultClient, name: str, confirmed: bool
) -> None:
    _delete_deployment(
        config, vault, name, confirmed, "ShardedCluster", "DeleteShardedCluster"
    )


def add_shard(config: dict[str, Any], vault: VaultClient, name: str) -> None:
    inventory = vault.load_inventory()
    key, item = require_deployment(inventory, name, "ShardedCluster")
    require_running(config, key, item)

    old_count = int(item["shard_count"])
    new_count = old_count + 1
    log_event(
        "shard.add.requested",
        deployment=item["display_name"],
        previous_shards=old_count,
        requested_shards=new_count,
    )

    # Stage 1: prepare persistent storage before asking the Operator for the shard.
    item["storage_shard_count"] = new_count
    apply_inventory(config, inventory)

    # Stage 2: increase the actual MongoDB shard count.
    item["shard_count"] = new_count
    apply_inventory(config, inventory)
    print(
        f"Waiting for new shard '{key}-{old_count}' on ShardedCluster "
        f"'{item['display_name']}' to become online ..."
    )
    kube.wait_sharded_cluster_ready(
        config, key, int(item["shard_count"]), config["sc_ready_timeout"]
    )
    log_event(
        "shard.add.succeeded",
        deployment=item["display_name"],
        shard=f"{key}-{old_count}",
        shard_count=item["shard_count"],
    )
    print(
        f"\nShard '{key}-{old_count}' was successfully created on "
        f"ShardedCluster '{item['display_name']}'."
    )
    print(f"Total shards: {item['shard_count']}")
    print("Status:       Online")


def delete_shard(
    config: dict[str, Any], vault: VaultClient, name: str, confirmed: bool
) -> None:
    if not confirmed:
        raise ControllerError(
            "DeleteShard is destructive and requires '--confirm'. "
            f"Example: terraformController.py DeleteShard {name} --confirm"
        )
    inventory = vault.load_inventory()
    key, item = require_deployment(inventory, name, "ShardedCluster")
    require_running(config, key, item)

    if int(item["shard_count"]) <= 1:
        raise ControllerError(
            f"ShardedCluster '{item['display_name']}' has only one shard. "
            "DeleteShard will not remove the final shard."
        )

    if item["databases"]:
        names = "\n".join(
            f"  {item['databases'][x]['display_name']}" for x in sorted(item["databases"])
        )
        raise ControllerError(
            f"Shard deletion is blocked because ShardedCluster '{item['display_name']}' "
            f"contains managed databases:\n{names}\n"
            "For this first-pass implementation, remove all application databases before "
            "removing a shard. This prevents deleting a shard that may still contain data."
        )

    apply_inventory(
        config,
        inventory,
        {
            "action": "validate_deployment_empty",
            "deployment": key,
            "deployment_type": "ShardedCluster",
            "database": "",
            "members": int(item["members_per_shard"]),
        },
    )

    old_count = int(item["shard_count"])
    new_count = old_count - 1
    removed = f"{key}-{old_count - 1}"
    item["shard_count"] = new_count
    log_event(
        "shard.delete.requested",
        deployment=item["display_name"],
        shard=removed,
        requested_shards=new_count,
    )

    # Stage 1: remove the shard from MongoDB while its storage still exists.
    apply_inventory(config, inventory)
    kube.wait_sharded_cluster_ready(
        config, key, new_count, config["sc_ready_timeout"]
    )
    kube.wait_absent(config, "statefulset", removed, config["sc_ready_timeout"])

    # Stage 2: only after the shard is gone may Terraform clean its old PVs.
    item["storage_shard_count"] = new_count
    apply_inventory(config, inventory)
    log_event(
        "shard.delete.succeeded",
        deployment=item["display_name"],
        shard=removed,
        shard_count=item["shard_count"],
    )
    print(
        f"\nShard '{removed}' was successfully deleted from "
        f"ShardedCluster '{item['display_name']}'."
    )
    print(f"Total shards: {item['shard_count']}")


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
    inventory = vault.load_inventory()
    if not inventory:
        print("No terraformController-managed MongoDB deployments exist.")
        return
    rows = [_deployment_row(config, key, inventory[key]) for key in sorted(inventory)]
    print_table(
        ("DEPLOYMENT", "TYPE", "PHASE", "TOPOLOGY", "MONGODB", "DATABASES"), rows
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
        ("REPLICA SET", "TYPE", "PHASE", "TOPOLOGY", "MONGODB", "DATABASES"), rows
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
        ("SHARDED CLUSTER", "TYPE", "PHASE", "TOPOLOGY", "MONGODB", "DATABASES"), rows
    )


def _print_shards(config: dict[str, Any], key: str, item: dict[str, Any]) -> None:
    status = kube.sharded_cluster_status(config, key, int(item["shard_count"]))
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
    print(
        f"Config servers: {status['config_servers']['status']} "
        f"({status['config_servers']['ready']}/{status['config_servers']['desired']})"
    )
    print(
        f"mongos:         {status['mongos']['status']} "
        f"({status['mongos']['ready']}/{status['mongos']['desired']})"
    )


def list_deployment(config: dict[str, Any], vault: VaultClient, name: str) -> None:
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


def list_replica_set(config: dict[str, Any], vault: VaultClient, name: str) -> None:
    inventory = vault.load_inventory()
    _, item = require_deployment(inventory, name, "ReplicaSet")
    list_deployment(config, vault, item["display_name"])


def list_sharded_cluster(
    config: dict[str, Any], vault: VaultClient, name: str
) -> None:
    inventory = vault.load_inventory()
    _, item = require_deployment(inventory, name, "ShardedCluster")
    list_deployment(config, vault, item["display_name"])


def list_shards(config: dict[str, Any], vault: VaultClient, name: str) -> None:
    inventory = vault.load_inventory()
    key, item = require_deployment(inventory, name, "ShardedCluster")
    print(f"ShardedCluster: {item['display_name']}")
    print(f"Phase:          {kube.phase(config, key)}")
    print()
    _print_shards(config, key, item)
