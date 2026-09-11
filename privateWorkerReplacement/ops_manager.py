"""Ops Manager project discovery and teardown helpers.

The MongoDB Kubernetes Operator creates one Ops Manager project for each
controller-managed deployment. The controller needs direct Ops Manager API
access to inventory projects and to remove a deployment's project during
teardown.

API requests run through curl inside the existing Ops Manager pod because that
pod already has network access to the in-cluster Ops Manager service. API
credentials are read from Kubernetes and passed to curl on standard input so
they do not appear in the process argument list.
"""

from __future__ import annotations

import base64
import json
import time
from typing import Any

from . import kube
from .common import ControllerError, run_process


def _connection_info(
    config: dict[str, Any],
) -> tuple[str, str, str, str, str]:
    """Return URL, org, permanent project, and Ops Manager API credentials.

    Keeping this lookup in one helper prevents project-list and project-delete
    paths from interpreting the same ConfigMap and Secret differently.
    """

    project_config = kube.get_json(
        config,
        "configmap",
        config["ops_manager_config_map"],
    )
    if not project_config:
        raise ControllerError(
            f"Ops Manager ConfigMap '{config['ops_manager_config_map']}' was not found."
        )

    data = project_config.get("data", {})
    base_url = str(data.get("baseUrl", "")).rstrip("/")
    org_id = str(data.get("orgId", ""))
    permanent_project = str(data.get("projectName", ""))

    if not base_url or not org_id or not permanent_project:
        raise ControllerError(
            "Ops Manager ConfigMap is missing baseUrl, orgId, or projectName."
        )

    credentials = kube.get_json(
        config,
        "secret",
        config["ops_manager_credentials_secret"],
    )
    if not credentials:
        raise ControllerError("Ops Manager API credential Secret was not found.")

    secret_data = credentials.get("data", {})
    try:
        public_key = base64.b64decode(secret_data["publicKey"]).decode("utf-8")
        private_key = base64.b64decode(secret_data["privateKey"]).decode("utf-8")
    except (KeyError, ValueError, UnicodeDecodeError) as exc:
        raise ControllerError(
            "Ops Manager API credential Secret is invalid."
        ) from exc

    return base_url, org_id, permanent_project, public_key, private_key


def _ops_manager_pod(config: dict[str, Any]) -> str:
    """Return a stable Ops Manager pod name for in-cluster API requests."""

    pod_names = sorted(
        str(pod.get("metadata", {}).get("name", ""))
        for pod in kube.list_json(config, "pod")
        if str(pod.get("metadata", {}).get("name", "")).startswith("ops-manager-")
    )
    if not pod_names:
        raise ControllerError("Ops Manager pod was not found.")
    return pod_names[0]


def _run_ops_manager_curl(
    config: dict[str, Any],
    pod_name: str,
    curl_config: str,
):
    """Run curl inside Ops Manager with its configuration supplied on stdin."""

    return run_process(
        kube.base(config)
        + [
            "-n",
            config["mongodb_namespace"],
            "exec",
            "-i",
            pod_name,
            "-c",
            "mongodb-ops-manager",
            "--",
            "curl",
            "--config",
            "-",
        ],
        input_text=curl_config,
        capture=True,
        check=False,
    )


def _delete_group_secret(
    config: dict[str, Any],
    project_name: str,
    project_id: str,
) -> None:
    """Remove and verify the Operator-created Secret for a deleted project."""

    group_secret = f"{project_id}-group-secret"

    # Ops Manager project deletion does not remove this Kubernetes Secret.
    # Leaving it behind creates a stale credential artifact and makes an empty
    # DBaaS environment look orphaned, so deployment teardown owns this step.
    result = run_process(
        kube.base(config)
        + [
            "-n",
            config["mongodb_namespace"],
            "delete",
            "secret",
            group_secret,
            "--ignore-not-found=true",
        ],
        capture=True,
        check=False,
    )

    if result.returncode:
        raise ControllerError(
            f"Ops Manager project '{project_name}' was deleted, "
            f"but Kubernetes Secret '{group_secret}' could not be removed: "
            + (result.stderr or result.stdout or "kubectl delete failed").strip()
        )

    if kube.get_json(config, "secret", group_secret) is not None:
        raise ControllerError(
            f"Ops Manager project '{project_name}' was deleted, "
            f"but Kubernetes Secret '{group_secret}' still exists."
        )


def list_projects(
    config: dict[str, Any],
) -> tuple[str, list[dict[str, str]]]:
    """Return the permanent platform project name and all Ops Manager projects."""

    base_url, org_id, permanent_project, public_key, private_key = (
        _connection_info(config)
    )
    pod_name = _ops_manager_pod(config)

    # Ops Manager calls projects "groups" in this API. itemsPerPage=500 keeps
    # this prototype inventory in one deterministic response.
    curl_config = (
        f'user = "{public_key}:{private_key}"\n'
        f'url = "{base_url}/api/public/v1.0/orgs/'
        f'{org_id}/groups?itemsPerPage=500"\n'
        "digest\n"
        "silent\n"
        "show-error\n"
        "fail\n"
    )
    result = _run_ops_manager_curl(config, pod_name, curl_config)

    if result.returncode:
        raise ControllerError(
            "Unable to read Ops Manager project inventory: "
            + (result.stderr or result.stdout or "curl failed").strip()
        )

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ControllerError(
            "Ops Manager returned invalid JSON while listing projects."
        ) from exc

    projects = sorted(
        [
            {
                "id": str(project.get("id", "")),
                "name": str(project.get("name", "")),
            }
            for project in payload.get("results", [])
        ],
        key=lambda project: project["name"].lower(),
    )
    return permanent_project, projects


def delete_project(
    config: dict[str, Any],
    project_name: str,
    timeout: int = 180,
) -> None:
    """Delete one DBaaS Ops Manager project and all of its local residue.

    The permanent platform project is protected. DBaaS project deletion is
    idempotent, waits until Ops Manager no longer lists the project, and then
    removes the Operator-created PROJECT_ID-group-secret.
    """

    permanent_project, projects = list_projects(config)

    if project_name.lower() == permanent_project.lower():
        raise ControllerError(
            "Refusing to delete permanent Ops Manager project "
            f"'{permanent_project}'."
        )

    project = next(
        (
            item
            for item in projects
            if item["name"].lower() == project_name.lower()
        ),
        None,
    )

    # A retry can arrive after Ops Manager already removed the project. Treat
    # that state as success rather than turning cleanup into a fragile one-shot.
    if project is None:
        return

    base_url, _, _, public_key, private_key = _connection_info(config)
    pod_name = _ops_manager_pod(config)

    curl_config = (
        f'user = "{public_key}:{private_key}"\n'
        f'url = "{base_url}/api/public/v1.0/groups/{project["id"]}"\n'
        'request = "DELETE"\n'
        "digest\n"
        "silent\n"
        "show-error\n"
        'output = "/dev/null"\n'
        'write-out = "%{http_code}"\n'
    )
    result = _run_ops_manager_curl(config, pod_name, curl_config)
    status = (result.stdout or "").strip()

    # Ops Manager commonly returns 202 because project deletion is asynchronous.
    if result.returncode or status not in {"200", "202", "204"}:
        raise ControllerError(
            f"Unable to delete Ops Manager project '{project_name}': "
            + (result.stderr or status or "request failed").strip()
        )

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _, remaining = list_projects(config)
        still_present = any(
            item["name"].lower() == project_name.lower()
            for item in remaining
        )
        if not still_present:
            _delete_group_secret(config, project_name, project["id"])
            return
        time.sleep(3)

    raise ControllerError(
        "Timed out waiting for Ops Manager project "
        f"'{project_name}' to be deleted."
    )
