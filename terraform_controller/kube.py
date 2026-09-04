from __future__ import annotations

import json
import time
import uuid
from typing import Any

from .common import ControllerError, run_process


def base(config: dict[str, Any]) -> list[str]:
    command = ["kubectl", "--kubeconfig", config["kubeconfig"]]
    if config["kube_context"]: command += ["--context", config["kube_context"]]
    return command


def get_json(config: dict[str, Any], resource: str, name: str) -> dict[str, Any] | None:
    result = run_process(
        base(config) + ["-n", config["mongodb_namespace"], "get", resource, name, "-o", "json"],
        capture=True, check=False,
    )
    if result.returncode:
        error = (result.stderr or "").lower()
        if "notfound" in error or "not found" in error: return None
        raise ControllerError((result.stderr or result.stdout or "kubectl get failed").strip())
    return json.loads(result.stdout)


def phase(config: dict[str, Any], rs_key: str) -> str:
    obj = get_json(config, "mongodb", rs_key)
    return str(obj.get("status", {}).get("phase", "Unknown")) if obj else "Absent"


def wait_phase(config: dict[str, Any], resource: str, name: str, wanted: str, timeout: int) -> None:
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        obj = get_json(config, resource, name)
        if obj:
            last = str(obj.get("status", {}).get("phase", ""))
            if last == wanted: return
        time.sleep(5)
    raise ControllerError(f"Timed out waiting for {resource}/{name} phase '{wanted}'. Last phase: {last or 'unknown'}.")


def wait_absent(config: dict[str, Any], resource: str, name: str, timeout: int = 180) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if get_json(config, resource, name) is None: return
        time.sleep(3)
    raise ControllerError(f"Timed out waiting for {resource}/{name} to be deleted.")


def ensure_replica_set_storage(config: dict[str, Any], rs_key: str, members: int, storage_size: str, storage_class: str) -> None:
    """Create the static local directories and PVs required by one ReplicaSet."""
    base_path = config["storage_base_path"].rstrip("/")
    node_name = config["storage_node_name"]

    # The K3D node name is also the Docker container name in this nix-k3d environment.
    # Creating the directory from inside the node guarantees the local-volume path exists
    # where kubelet will mount it; the parent path is bind-mounted from the WSL host.
    for ordinal in range(members):
        pv_name = f"{rs_key}-{ordinal}"
        local_path = f"{base_path}/{pv_name}"

        run_process(["docker", "exec", node_name, "mkdir", "-p", local_path], capture=True)

        manifest = {
            "apiVersion": "v1",
            "kind": "PersistentVolume",
            "metadata": {
                "name": pv_name,
                "labels": {
                    "app.kubernetes.io/managed-by": "terraformController",
                    "dbaas.replica-set": rs_key,
                    "dbaas.member": str(ordinal),
                },
            },
            "spec": {
                "accessModes": ["ReadWriteOnce"],
                "capacity": {"storage": storage_size},
                "persistentVolumeReclaimPolicy": "Retain",
                "storageClassName": storage_class,
                "volumeMode": "Filesystem",
                "local": {"path": local_path},
                "nodeAffinity": {
                    "required": {
                        "nodeSelectorTerms": [{
                            "matchExpressions": [{
                                "key": "kubernetes.io/hostname",
                                "operator": "In",
                                "values": [node_name],
                            }]
                        }]
                    }
                },
            },
        }
        run_process(base(config) + ["apply", "-f", "-"], input_text=json.dumps(manifest), capture=True)


def cleanup_replica_set_storage(config: dict[str, Any], rs_key: str, members: int) -> None:
    """Remove the retained PVCs/PVs and local directories after an empty ReplicaSet is deleted."""
    namespace = config["mongodb_namespace"]
    node_name = config["storage_node_name"]
    base_path = config["storage_base_path"].rstrip("/")

    for ordinal in range(members):
        pvc_name = f"data-{rs_key}-{ordinal}"
        run_process(
            base(config) + ["-n", namespace, "delete", "pvc", pvc_name, "--ignore-not-found=true", "--wait=true"],
            capture=True, check=False,
        )

    for ordinal in range(members):
        pv_name = f"{rs_key}-{ordinal}"
        run_process(
            base(config) + ["delete", "pv", pv_name, "--ignore-not-found=true", "--wait=true"],
            capture=True, check=False,
        )
        local_path = f"{base_path}/{pv_name}"
        run_process(["docker", "exec", node_name, "rm", "-rf", local_path], capture=True, check=False)


def controller_user(rs_key: str) -> str:
    return f"tc-{rs_key}-admin"


def controller_connection_secret(rs_key: str) -> str:
    return f"tc-{rs_key}-admin-connection"


def runtime_job(config: dict[str, Any], rs_key: str, javascript: str) -> str:
    job = f"tc-runtime-{rs_key[:12]}-{uuid.uuid4().hex[:8]}"
    manifest = {
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": {"name": job, "namespace": config["mongodb_namespace"], "labels": {"app.kubernetes.io/managed-by": "terraformController", "dbaas.replica-set": rs_key}},
        "spec": {"backoffLimit": 0, "ttlSecondsAfterFinished": 120, "template": {
            "metadata": {"labels": {"app.kubernetes.io/managed-by": "terraformController", "dbaas.replica-set": rs_key}},
            "spec": {"restartPolicy": "Never", "containers": [{
                "name": "mongosh", "image": config["mongo_image"],
                "env": [
                    {"name": "MONGODB_URI", "valueFrom": {"secretKeyRef": {"name": controller_connection_secret(rs_key), "key": "connectionString.standard"}}},
                    {"name": "TC_JS", "value": javascript},
                ],
                "command": ["/bin/bash", "-lc", 'mongosh "$MONGODB_URI" --quiet --eval "$TC_JS"'],
            }]},
        }},
    }
    run_process(base(config) + ["apply", "-f", "-"], input_text=json.dumps(manifest), capture=True)
    deadline = time.monotonic() + config["job_timeout"]
    try:
        while time.monotonic() < deadline:
            obj = get_json(config, "job", job)
            status = (obj or {}).get("status", {})
            if int(status.get("succeeded", 0) or 0): break
            if int(status.get("failed", 0) or 0):
                logs = _logs(config, job)
                raise ControllerError(f"MongoDB runtime operation failed on ReplicaSet '{rs_key}'.\n{logs}")
            time.sleep(2)
        else:
            raise ControllerError(f"MongoDB runtime operation timed out on ReplicaSet '{rs_key}'.\n{_logs(config, job)}")
        return _logs(config, job)
    finally:
        run_process(base(config) + ["-n", config["mongodb_namespace"], "delete", "job", job, "--ignore-not-found=true", "--wait=false"], capture=True, check=False)


def _logs(config: dict[str, Any], job: str) -> str:
    result = run_process(base(config) + ["-n", config["mongodb_namespace"], "logs", f"job/{job}"], capture=True, check=False)
    return (result.stdout or result.stderr or "").strip()


def list_databases(config: dict[str, Any], rs_key: str) -> list[str]:
    js = '''const p=new Set(["admin","config","local"]);const n=db.adminCommand({listDatabases:1,nameOnly:true}).databases.map(x=>x.name).filter(x=>!p.has(x)).sort();print("TC_RESULT="+JSON.stringify(n));'''
    logs = runtime_job(config, rs_key, js)
    for line in reversed(logs.splitlines()):
        if line.startswith("TC_RESULT="): return list(json.loads(line.split("=", 1)[1]))
    raise ControllerError("Runtime database check returned no result marker.")


def ensure_database(config: dict[str, Any], rs_key: str, database: str) -> None:
    d, p = json.dumps(database), json.dumps(config["placeholder_collection"])
    js = f'''const d={d},p={p},t=db.getSiblingDB(d),c=t.getCollectionNames();if(!c.includes(p))t.createCollection(p);print("TC_RESULT=OK");'''
    runtime_job(config, rs_key, js)


def delete_database_if_empty(config: dict[str, Any], rs_key: str, database: str) -> None:
    d, p = json.dumps(database), json.dumps(config["placeholder_collection"])
    js = f'''const d={d},p={p};const n=db.adminCommand({{listDatabases:1,nameOnly:true}}).databases.map(x=>x.name);if(!n.includes(d)){{print("TC_RESULT=ALREADY_ABSENT");quit(0);}}const t=db.getSiblingDB(d);const b=t.getCollectionInfos().map(x=>x.name).filter(x=>x!==p&&!x.startsWith("system."));if(b.length){{print("TC_BLOCKED="+JSON.stringify(b.sort()));quit(42);}}const r=t.dropDatabase();if(!r||r.ok!==1)quit(43);print("TC_RESULT=DELETED");'''
    try:
        runtime_job(config, rs_key, js)
    except ControllerError as exc:
        text = str(exc)
        if "TC_BLOCKED=" in text:
            payload = text.split("TC_BLOCKED=", 1)[1].splitlines()[0]
            try: collections = json.loads(payload)
            except json.JSONDecodeError: collections = [payload]
            raise ControllerError("Database is not empty. DeleteDatabase was blocked.\nCollections still present:\n" + "\n".join(f"  {x}" for x in collections)) from exc
        raise


def drop_all_databases(config: dict[str, Any], rs_key: str) -> list[str]:
    js = '''const p=new Set(["admin","config","local"]);const n=db.adminCommand({listDatabases:1,nameOnly:true}).databases.map(x=>x.name).filter(x=>!p.has(x)).sort();for(const x of n){const r=db.getSiblingDB(x).dropDatabase();if(!r||r.ok!==1)quit(42);}print("TC_RESULT="+JSON.stringify(n));'''
    logs = runtime_job(config, rs_key, js)
    for line in reversed(logs.splitlines()):
        if line.startswith("TC_RESULT="): return list(json.loads(line.split("=", 1)[1]))
    return []
