"""Clean MongoDB Operator and Helm artifacts that are outside Terraform state.

Most DBaaS resources are normal Terraform desired-state objects. A few runtime
artifacts are created by other controllers instead:

- MongoDB Kubernetes Operator creates <deployment>-agent-auth-secret.
- The database-management Helm chart creates hook Jobs for provisioning and
  one-shot database operations.

Those objects are not reliably removed when the MongoDB CR or Helm release is
destroyed. The controller therefore performs narrow, name-based cleanup only
after the owning deployment is gone. This keeps successful delete/recovery
workflows from reporting success while deployment-specific Kubernetes artifacts
are still present.
"""

from __future__ import annotations

from typing import Any

from . import kube
from .common import ControllerError, run_process
from .logging_component import log_event


MANAGED_BY_SELECTOR = "app.kubernetes.io/managed-by=privateWorkerReplacement"


def _helm_hook_name(deployment_key: str, suffix: str) -> str:
    """Return the exact Helm hook Job name used by mongodb-chart.

    The chart constructs the release name as <deployment>-mongodb-management and
    truncates the final hook name to Kubernetes' 63-character DNS label limit.
    Mirror that rule here so cleanup stays deterministic for long deployment
    names too.
    """

    release = f"{deployment_key}-mongodb-management"
    return f"{release}-{suffix}"[:63].rstrip("-")


def deployment_operator_artifact_names(deployment_key: str) -> dict[str, list[str]]:
    """Return the non-Terraform Kubernetes artifacts for one deployment."""

    return {
        "job": [
            _helm_hook_name(deployment_key, "database-provisioner"),
            _helm_hook_name(deployment_key, "database-operation"),
        ],
        "secret": [f"{deployment_key}-agent-auth-secret"],
    }


def cleanup_deployment_operator_artifacts(
    config: dict[str, Any],
    deployment_key: str,
) -> None:
    """Delete and verify known Operator/Helm artifacts for an absent deployment.

    The caller must invoke this only after the MongoDB custom resource is absent.
    Deleting these artifacts while a deployment is live could interrupt Operator
    authentication or an active database-management hook.
    """

    if kube.get_json(config, "mongodb", deployment_key) is not None:
        raise ControllerError(
            f"Refusing Operator-artifact cleanup for '{deployment_key}' because "
            "the MongoDB resource still exists."
        )

    namespace = str(config["mongodb_namespace"])
    artifacts = deployment_operator_artifact_names(deployment_key)

    for resource in ("job", "secret"):
        for name in artifacts[resource]:
            result = run_process(
                kube.base(config)
                + [
                    "-n",
                    namespace,
                    "delete",
                    resource,
                    name,
                    "--ignore-not-found=true",
                    "--wait=true",
                    *(["--cascade=foreground"] if resource == "job" else []),
                ],
                capture=True,
                check=False,
            )
            if result.returncode:
                raise ControllerError(
                    f"Could not remove leftover Kubernetes {resource} '{name}': "
                    + (
                        result.stderr
                        or result.stdout
                        or "kubectl delete failed"
                    ).strip()
                )

            if kube.get_json(config, resource, name) is not None:
                raise ControllerError(
                    f"Kubernetes {resource} '{name}' still exists after cleanup."
                )

            if resource == "job":
                pods = kube.list_json(
                    config,
                    "pod",
                    label_selector=f"job-name={name}",
                )
                if pods:
                    pod_names = ", ".join(
                        str(item.get("metadata", {}).get("name", "<unknown>"))
                        for item in pods
                    )
                    raise ControllerError(
                        f"Pod(s) for Kubernetes job '{name}' still exist after "
                        f"cleanup: {pod_names}."
                    )

            log_event(
                "operator_artifact.cleanup.checked",
                deployment=deployment_key,
                resource=resource,
                name=name,
            )


def _artifact_deployment_key(resource: str, item: dict[str, Any]) -> str:
    """Return a deployment key encoded in one known runtime artifact."""

    metadata = item.get("metadata", {}) or {}
    name = str(metadata.get("name", ""))
    labels = metadata.get("labels", {}) or {}

    # New controller-created Helm hook objects carry an explicit deployment
    # label. Prefer it because Kubernetes name truncation can remove part of a
    # long suffix.
    labeled_key = str(labels.get("dbaas.deployment", "")).strip()
    if labeled_key:
        return labeled_key

    if resource == "secret" and name.endswith("-agent-auth-secret"):
        return name.removesuffix("-agent-auth-secret")

    if resource == "job":
        for suffix in (
            "-mongodb-management-database-provisioner",
            "-mongodb-management-database-operation",
        ):
            if name.endswith(suffix):
                return name.removesuffix(suffix)

    if resource == "pod":
        labels = metadata.get("labels", {}) or {}
        job_name = str(labels.get("job-name", ""))
        for suffix in (
            "-mongodb-management-database-provisioner",
            "-mongodb-management-database-operation",
        ):
            if job_name.endswith(suffix):
                return job_name.removesuffix(suffix)

    return ""


def list_orphan_operator_artifacts(
    config: dict[str, Any],
    managed_deployment_keys: set[str],
) -> list[str]:
    """Return stale Operator/Helm artifacts with no desired or live deployment.

    A runtime artifact is considered orphaned only when:
    - its name/labels identify a deployment key;
    - that key is not in the controller desired-state inventory; and
    - no live MongoDB custom resource exists with that key.

    This prevents active deployments, including unrelated MongoDB resources in
    the same namespace, from being classified as cleanup debris.
    """

    artifacts: list[str] = []
    live_cache: dict[str, bool] = {}

    for resource in ("job", "pod", "secret"):
        for item in kube.list_json(config, resource):
            key = _artifact_deployment_key(resource, item)
            if not key or key in managed_deployment_keys:
                continue

            if key not in live_cache:
                live_cache[key] = (
                    kube.get_json(config, "mongodb", key) is not None
                )
            if live_cache[key]:
                continue

            name = str(item.get("metadata", {}).get("name", "<unknown>"))
            artifacts.append(f"{resource}/{name}")

    return sorted(set(artifacts))


def discover_managed_deployment_keys(config: dict[str, Any]) -> list[str]:
    """Discover keys needed for safe post-Terraform artifact cleanup.

    RecoverOrphanedResources intentionally runs after Vault inventory is empty.
    Normally the still-live Terraform-managed objects carry deployment labels.
    A previous partial cleanup may already have removed all Terraform objects,
    leaving only MongoDB Operator auth Secrets or Helm hook Jobs/Pods. Those
    artifact names also encode the deployment key, so include them as a recovery
    fallback.

    Cleanup still refuses to touch a key while a MongoDB CR with that name is
    live. That guard prevents stale-artifact recovery from deleting credentials
    belonging to an active deployment.
    """

    keys: set[str] = set()

    for resource, namespaced in (
        ("mongodbuser", True),
        ("secret", True),
        ("configmap", True),
        ("pvc", True),
        ("pv", False),
    ):
        items = kube.list_json(
            config,
            resource,
            label_selector=MANAGED_BY_SELECTOR,
            namespaced=namespaced,
        )
        for item in items:
            labels = item.get("metadata", {}).get("labels", {}) or {}
            for label in ("dbaas.deployment", "dbaas.replica-set"):
                value = str(labels.get(label, "")).strip()
                if value:
                    keys.add(value)

    # Recovery fallback for a prior run that already destroyed every
    # Terraform-managed object but left Operator/Helm artifacts behind.
    for resource in ("job", "pod", "secret"):
        for item in kube.list_json(config, resource):
            key = _artifact_deployment_key(resource, item)
            if key:
                keys.add(key)

    return sorted(keys)
