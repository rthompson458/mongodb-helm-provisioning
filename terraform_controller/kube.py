"""Read Kubernetes state and wait for MongoDB resources to converge.

Important design boundary:
- This module READS Kubernetes objects and waits for status changes.
- It does not create or mutate managed MongoDB resources.
- Managed changes remain Terraform/lifecycle-script owned.

The status helpers translate raw Kubernetes fields into terms the CLI can show
clearly: Running, Online, Creating, Degraded, Failed, and Absent.
"""

from __future__ import annotations

import json
import time
from typing import Any

from .common import ControllerError, run_process
from .logging_component import log_event


def base(config: dict[str, Any]) -> list[str]:
    """Build the common kubectl command prefix for the configured environment."""
    command = ["kubectl", "--kubeconfig", config["kubeconfig"]]
    if config["kube_context"]:
        command += ["--context", config["kube_context"]]
    return command


def get_json(config: dict[str, Any], resource: str, name: str) -> dict[str, Any] | None:
    """Return one Kubernetes resource as JSON, or None when it does not exist."""
    result = run_process(
        base(config) + [
            "-n", config["mongodb_namespace"], "get", resource, name, "-o", "json"
        ],
        capture=True,
        check=False,
    )
    if result.returncode:
        error = (result.stderr or "").lower()
        if "notfound" in error or "not found" in error:
            return None
        raise ControllerError(
            (result.stderr or result.stdout or "kubectl get failed").strip()
        )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ControllerError(
            f"kubectl returned invalid JSON for {resource}/{name}."
        ) from exc


def phase(config: dict[str, Any], deployment_key: str) -> str:
    obj = get_json(config, "mongodb", deployment_key)
    return str(obj.get("status", {}).get("phase", "Unknown")) if obj else "Absent"


def phase_message(config: dict[str, Any], deployment_key: str) -> str:
    obj = get_json(config, "mongodb", deployment_key)
    if not obj:
        return ""
    return str(obj.get("status", {}).get("message", "") or "")


def wait_phase(
    config: dict[str, Any], resource: str, name: str, wanted: str, timeout: int
) -> None:
    """Wait until a resource reaches a requested status.phase.

    A MongoDB resource entering Failed is treated as an immediate failure so the
    user does not wait for the entire timeout when the Operator already knows
    the deployment cannot converge.
    """

    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        obj = get_json(config, resource, name)
        if obj:
            current = str(obj.get("status", {}).get("phase", "Unknown"))
            if current != last:
                log_event(
                    "kubernetes.phase.changed",
                    resource=resource,
                    name=name,
                    phase=current,
                    wanted=wanted,
                )
                last = current
            if current == wanted:
                return
            if current == "Failed" and resource == "mongodb":
                message = str(obj.get("status", {}).get("message", "") or "")
                raise ControllerError(
                    f"MongoDB deployment '{name}' entered Failed state."
                    + (f" Operator message: {message}" if message else "")
                )
        time.sleep(5)
    raise ControllerError(
        f"Timed out waiting for {resource}/{name} phase '{wanted}'. "
        f"Last phase: {last or 'unknown'}."
    )


def wait_absent(
    config: dict[str, Any], resource: str, name: str, timeout: int = 180
) -> None:
    """Wait until Kubernetes reports that a resource no longer exists."""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if get_json(config, resource, name) is None:
            log_event("kubernetes.resource.absent", resource=resource, name=name)
            return
        time.sleep(3)
    raise ControllerError(f"Timed out waiting for {resource}/{name} to be deleted.")


def controller_user(deployment_key: str) -> str:
    return f"tc-{deployment_key}-admin"


def controller_connection_secret(deployment_key: str) -> str:
    return f"tc-{deployment_key}-admin-connection"


def _statefulset_status(
    config: dict[str, Any],
    name: str,
    *,
    fallback_phase: str = "Unknown",
) -> dict[str, Any]:
    """Translate StatefulSet replica counts into a DBaaS-friendly status."""

    obj = get_json(config, "statefulset", name)
    if not obj:
        status = "Failed" if fallback_phase == "Failed" else (
            "Creating" if fallback_phase in {"Pending", "Creating"} else "Unknown"
        )
        return {
            "name": name,
            "status": status,
            "desired": 0,
            "ready": 0,
            "updated": 0,
        }

    desired = int(obj.get("spec", {}).get("replicas", 0) or 0)
    raw = obj.get("status", {})
    ready = int(raw.get("readyReplicas", 0) or 0)
    updated = int(raw.get("updatedReplicas", 0) or 0)

    if desired > 0 and ready >= desired and updated >= desired:
        status = "Online"
    elif fallback_phase == "Failed":
        status = "Failed"
    elif ready > 0:
        status = "Degraded"
    else:
        status = "Creating"

    return {
        "name": name,
        "status": status,
        "desired": desired,
        "ready": ready,
        "updated": updated,
    }


def sharded_cluster_status(
    config: dict[str, Any],
    deployment_key: str,
    shard_count: int,
) -> dict[str, Any]:
    """Return one combined status snapshot for a ShardedCluster.

    MongoDB creates separate StatefulSets for each shard, config servers, and
    mongos.  The controller combines them so callers do not need to understand
    every Kubernetes object name.
    """

    overall = phase(config, deployment_key)
    shards = [
        {
            "shard": f"{deployment_key}-{index}",
            **_statefulset_status(
                config, f"{deployment_key}-{index}", fallback_phase=overall
            ),
        }
        for index in range(shard_count)
    ]
    return {
        "phase": overall,
        "message": phase_message(config, deployment_key),
        "shards": shards,
        "config_servers": _statefulset_status(
            config, f"{deployment_key}-config", fallback_phase=overall
        ),
        "mongos": _statefulset_status(
            config, f"{deployment_key}-mongos", fallback_phase=overall
        ),
    }


def wait_sharded_cluster_ready(
    config: dict[str, Any],
    deployment_key: str,
    shard_count: int,
    timeout: int,
) -> None:
    """Wait until the cluster and every required component are fully online."""

    deadline = time.monotonic() + timeout
    last_signature = ""
    while time.monotonic() < deadline:
        status = sharded_cluster_status(config, deployment_key, shard_count)
        signature = json.dumps(
            {
                "phase": status["phase"],
                "shards": [
                    (x["shard"], x["status"], x["ready"], x["desired"])
                    for x in status["shards"]
                ],
                "config": (
                    status["config_servers"]["status"],
                    status["config_servers"]["ready"],
                    status["config_servers"]["desired"],
                ),
                "mongos": (
                    status["mongos"]["status"],
                    status["mongos"]["ready"],
                    status["mongos"]["desired"],
                ),
            },
            sort_keys=True,
        )
        if signature != last_signature:
            log_event(
                "sharded_cluster.status.changed",
                deployment=deployment_key,
                phase=status["phase"],
                shards=[
                    {
                        "name": x["shard"],
                        "status": x["status"],
                        "ready": x["ready"],
                        "desired": x["desired"],
                    }
                    for x in status["shards"]
                ],
            )
            last_signature = signature

        if status["phase"] == "Failed":
            raise ControllerError(
                f"ShardedCluster '{deployment_key}' entered Failed state."
                + (
                    f" Operator message: {status['message']}"
                    if status["message"]
                    else ""
                )
            )

        shards_ready = all(x["status"] == "Online" for x in status["shards"])
        config_ready = status["config_servers"]["status"] == "Online"
        mongos_ready = status["mongos"]["status"] == "Online"
        if status["phase"] == "Running" and shards_ready and config_ready and mongos_ready:
            return
        time.sleep(5)

    status = sharded_cluster_status(config, deployment_key, shard_count)
    details = ", ".join(
        f"{x['shard']}={x['status']}({x['ready']}/{x['desired']})"
        for x in status["shards"]
    )
    raise ControllerError(
        f"Timed out waiting for ShardedCluster '{deployment_key}' to become fully online. "
        f"Cluster phase: {status['phase']}. Shards: {details or 'none found'}."
    )
