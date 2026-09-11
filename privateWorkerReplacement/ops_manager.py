"""Read and manage Ops Manager projects used by privateWorkerReplacement."""

from __future__ import annotations

import base64
import json
import time
from typing import Any

from . import kube
from .common import ControllerError, run_process


def list_projects(
    config: dict[str, Any],
) -> tuple[str, list[dict[str, str]]]:
    """Return the permanent platform project name and all Ops Manager projects."""

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
        public_key = base64.b64decode(
            secret_data["publicKey"]
        ).decode("utf-8")
        private_key = base64.b64decode(
            secret_data["privateKey"]
        ).decode("utf-8")
    except (KeyError, ValueError, UnicodeDecodeError) as exc:
        raise ControllerError(
            "Ops Manager API credential Secret is invalid."
        ) from exc

    pods = kube.list_json(config, "pod")
    pod_names = sorted(
        str(pod.get("metadata", {}).get("name", ""))
        for pod in pods
        if str(pod.get("metadata", {}).get("name", "")).startswith(
            "ops-manager-"
        )
    )
    if not pod_names:
        raise ControllerError("Ops Manager pod was not found.")

    curl_config = (
        f'user = "{public_key}:{private_key}"\n'
        f'url = "{base_url}/api/public/v1.0/orgs/'
        f'{org_id}/groups?itemsPerPage=500"\n'
        "digest\n"
        "silent\n"
        "show-error\n"
        "fail\n"
    )

    result = run_process(
        kube.base(config)
        + [
            "-n",
            config["mongodb_namespace"],
            "exec",
            "-i",
            pod_names[0],
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
    """Delete one DBaaS Ops Manager project and wait until it is absent."""

    permanent_project, projects = list_projects(config)

    if project_name.lower() == permanent_project.lower():
        raise ControllerError(
            f"Refusing to delete permanent Ops Manager project "
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

    # Deletion is idempotent. If the Operator or a previous retry already
    # removed the project, there is nothing left to do.
    if project is None:
        return

    project_config = kube.get_json(
        config,
        "configmap",
        config["ops_manager_config_map"],
    )
    credentials = kube.get_json(
        config,
        "secret",
        config["ops_manager_credentials_secret"],
    )

    if not project_config or not credentials:
        raise ControllerError(
            "Ops Manager connection information is unavailable."
        )

    base_url = str(
        project_config.get("data", {}).get("baseUrl", "")
    ).rstrip("/")

    secret_data = credentials.get("data", {})
    try:
        public_key = base64.b64decode(
            secret_data["publicKey"]
        ).decode("utf-8")
        private_key = base64.b64decode(
            secret_data["privateKey"]
        ).decode("utf-8")
    except (KeyError, ValueError, UnicodeDecodeError) as exc:
        raise ControllerError(
            "Ops Manager API credential Secret is invalid."
        ) from exc

    pods = kube.list_json(config, "pod")
    pod_names = sorted(
        str(pod.get("metadata", {}).get("name", ""))
        for pod in pods
        if str(pod.get("metadata", {}).get("name", "")).startswith(
            "ops-manager-"
        )
    )

    if not pod_names:
        raise ControllerError("Ops Manager pod was not found.")

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

    result = run_process(
        kube.base(config)
        + [
            "-n",
            config["mongodb_namespace"],
            "exec",
            "-i",
            pod_names[0],
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

    status = (result.stdout or "").strip()

    if result.returncode or status not in {"200", "202", "204"}:
        raise ControllerError(
            f"Unable to delete Ops Manager project '{project_name}': "
            + (result.stderr or status or "request failed").strip()
        )

    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        _, remaining = list_projects(config)

        if not any(
            item["name"].lower() == project_name.lower()
            for item in remaining
        ):
            # The MongoDB Operator creates one Kubernetes group Secret for the
            # Ops Manager project. Ops Manager project deletion does not remove
            # that Secret, so finish teardown explicitly and verify the stale
            # credential artifact is gone before reporting success.
            group_secret = f"{project['id']}-group-secret"

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
                    + (
                        result.stderr
                        or result.stdout
                        or "kubectl delete failed"
                    ).strip()
                )

            if kube.get_json(config, "secret", group_secret) is not None:
                raise ControllerError(
                    f"Ops Manager project '{project_name}' was deleted, "
                    f"but Kubernetes Secret '{group_secret}' still exists."
                )

            return

        time.sleep(3)

    raise ControllerError(
        f"Timed out waiting for Ops Manager project "
        f"'{project_name}' to be deleted."
    )

