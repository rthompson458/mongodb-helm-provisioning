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

            log_event(
                "operator_artifact.cleanup.checked",
                deployment=deployment_key,
                resource=resource,
                name=name,
            )


def discover_managed_deployment_keys(config: dict[str, Any]) -> list[str]:
    """Discover deployment keys from still-live Terraform-managed K8s objects.

    RecoverOrphanedResources intentionally runs after Vault inventory is empty.
    Before Terraform destroys its remaining objects, their labels provide the
    safest available record of which deployment-specific Operator artifacts are
    ours to clean afterward.

    Only objects carrying privateWorkerReplacement's managed-by label are used.
    Unrelated MongoDB resources in the namespace are ignored.
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

    return sorted(keys)
