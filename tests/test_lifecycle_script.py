"""Regression tests for the Terraform lifecycle shell script.

These tests execute the shell script with small fake external commands so bugs
in Bash expansion are caught before a live Kubernetes environment is needed.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


class LifecycleScriptTests(unittest.TestCase):
    """Exercise shell behavior that syntax-only checks cannot detect."""

    def test_runtime_job_preserves_container_environment_variables(self) -> None:
        """MONGODB_URI and TC_JS must expand inside the Job, not on the host.

        lifecycle.sh runs with `set -u`. The Kubernetes Job receives
        MONGODB_URI and TC_JS as container environment variables, so the outer
        lifecycle shell must leave those references untouched while generating
        the Job YAML.
        """

        repo_root = Path(__file__).resolve().parent.parent
        lifecycle = repo_root / "terraform-dbaas" / "scripts" / "lifecycle.sh"

        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            fake_kubectl = temp / "kubectl"
            manifest_path = temp / "job.yaml"

            fake_kubectl.write_text(
                """#!/usr/bin/env bash
set -eu

: "${FAKE_MANIFEST:?FAKE_MANIFEST is required}"

args=" $* "

if [[ "$args" == *" apply -f -"* ]]; then
  cat > "$FAKE_MANIFEST"
  exit 0
fi

if [[ "$args" == *" get job "* ]]; then
  if [[ "$args" == *".status.succeeded"* ]]; then
    printf '1'
  else
    printf '0'
  fi
  exit 0
fi

if [[ "$args" == *" logs job/"* ]]; then
  echo "TC_RESULT=OK"
  exit 0
fi

if [[ "$args" == *" delete job "* ]]; then
  exit 0
fi

echo "Unexpected fake kubectl invocation: $*" >&2
exit 1
""",
                encoding="utf-8",
            )
            fake_kubectl.chmod(0o755)

            env = os.environ.copy()
            env.update(
                {
                    "PATH": f"{temp}{os.pathsep}{env['PATH']}",
                    "FAKE_MANIFEST": str(manifest_path),
                    "TC_ACTION": "validate_deployment_empty",
                    "TC_NAMESPACE": "mongodb",
                    "TC_KUBECONFIG": "/tmp/fake-kubeconfig",
                    "TC_DEPLOYMENT": "test-rs",
                    "TC_DATABASE": "HouseInfo",
                    "TC_PLACEHOLDER_COLLECTION": "__dbaas_metadata",
                    "TC_MONGO_IMAGE": "mongo:8.0",
                }
            )

            result = subprocess.run(
                ["bash", str(lifecycle)],
                cwd=lifecycle.parent,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(
                result.returncode,
                0,
                msg=(
                    "lifecycle.sh failed while generating/executing the fake runtime Job.\n"
                    f"stdout:\n{result.stdout}\n"
                    f"stderr:\n{result.stderr}"
                ),
            )

            manifest = manifest_path.read_text(encoding="utf-8")
            self.assertIn(
                'mongosh "$MONGODB_URI" --quiet --eval "$TC_JS"',
                manifest,
            )
            self.assertIn("name: MONGODB_URI", manifest)
            self.assertIn("name: tc-test-rs-admin-connection", manifest)


    def test_controller_admin_readiness_retries_authentication(self) -> None:
        """Fresh deployments are not ready until controller login really works."""

        repo_root = Path(__file__).resolve().parent.parent
        lifecycle = repo_root / "terraform-dbaas" / "scripts" / "lifecycle.sh"

        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            fake_kubectl = temp / "kubectl"
            attempt_file = temp / "attempt"

            fake_kubectl.write_text(
                """#!/usr/bin/env bash
set -eu

: "${FAKE_ATTEMPT:?FAKE_ATTEMPT is required}"
args=" $* "

if [[ "$args" == *" apply -f -"* ]]; then
  cat >/dev/null
  n=0
  [[ -f "$FAKE_ATTEMPT" ]] && n=$(cat "$FAKE_ATTEMPT")
  n=$((n + 1))
  printf '%s' "$n" > "$FAKE_ATTEMPT"
  exit 0
fi

if [[ "$args" == *" get job "* ]]; then
  n=$(cat "$FAKE_ATTEMPT")
  if [[ "$n" -eq 1 ]]; then
    if [[ "$args" == *".status.failed"* ]]; then
      printf '1'
    else
      printf '0'
    fi
  else
    if [[ "$args" == *".status.succeeded"* ]]; then
      printf '1'
    else
      printf '0'
    fi
  fi
  exit 0
fi

if [[ "$args" == *" logs job/"* ]]; then
  n=$(cat "$FAKE_ATTEMPT")
  if [[ "$n" -eq 1 ]]; then
    echo "MongoServerError: Authentication failed."
  else
    echo "TC_RESULT=CONTROLLER_AUTH_OK"
  fi
  exit 0
fi

if [[ "$args" == *" delete job "* ]]; then
  exit 0
fi

echo "Unexpected fake kubectl invocation: $*" >&2
exit 1
""",
                encoding="utf-8",
            )
            fake_kubectl.chmod(0o755)

            env = os.environ.copy()
            env.update(
                {
                    "PATH": f"{temp}{os.pathsep}{env['PATH']}",
                    "FAKE_ATTEMPT": str(attempt_file),
                    "TC_ACTION": "verify_controller_admin",
                    "TC_NAMESPACE": "mongodb",
                    "TC_KUBECONFIG": "/tmp/fake-kubeconfig",
                    "TC_DEPLOYMENT": "test-rs",
                    "TC_MONGO_IMAGE": "mongo:8.0",
                }
            )

            result = subprocess.run(
                ["bash", str(lifecycle)],
                cwd=lifecycle.parent,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertIn("TC_RESULT=CONTROLLER_AUTH_OK", result.stdout)
            self.assertEqual(attempt_file.read_text(encoding="utf-8"), "2")


    def test_account_connection_secret_names_replace_database_underscores(self) -> None:
        """Account verification must use the same DNS-safe names as Terraform."""

        repo_root = Path(__file__).resolve().parent.parent
        lifecycle = repo_root / "terraform-dbaas" / "scripts" / "lifecycle.sh"

        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            fake_kubectl = temp / "kubectl"
            manifest_path = temp / "jobs.yaml"

            fake_kubectl.write_text(
                """#!/usr/bin/env bash
set -eu

: "${FAKE_MANIFEST:?FAKE_MANIFEST is required}"
args=" $* "

if [[ "$args" == *" apply -f -"* ]]; then
  cat >> "$FAKE_MANIFEST"
  printf '\\n---\\n' >> "$FAKE_MANIFEST"
  exit 0
fi

if [[ "$args" == *" get job "* ]]; then
  if [[ "$args" == *".status.succeeded"* ]]; then
    printf '1'
  else
    printf '0'
  fi
  exit 0
fi

if [[ "$args" == *" logs job/"* ]]; then
  echo "TC_RESULT=AUTH_OK"
  exit 0
fi

if [[ "$args" == *" delete job "* ]]; then
  exit 0
fi

echo "Unexpected fake kubectl invocation: $*" >&2
exit 1
""",
                encoding="utf-8",
            )
            fake_kubectl.chmod(0o755)

            env = os.environ.copy()
            env.update(
                {
                    "PATH": f"{temp}{os.pathsep}{env['PATH']}",
                    "FAKE_MANIFEST": str(manifest_path),
                    "TC_ACTION": "verify_database_accounts",
                    "TC_NAMESPACE": "mongodb",
                    "TC_KUBECONFIG": "/tmp/fake-kubeconfig",
                    "TC_DEPLOYMENT": "test-rs",
                    "TC_DATABASE": "House_Info",
                    "TC_AUTH_DATABASE": "admin",
                    "TC_MONGO_IMAGE": "mongo:8.0",
                }
            )

            result = subprocess.run(
                ["bash", str(lifecycle)],
                cwd=lifecycle.parent,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, msg=result.stderr)
            manifests = manifest_path.read_text(encoding="utf-8")
            self.assertNotIn("house_info", manifests)
            self.assertIn("tc-test-rs-house-info-owner-", manifests)
            self.assertIn("tc-test-rs-house-info-readwrite-", manifests)
            self.assertIn("tc-test-rs-house-info-read-", manifests)


    def test_storage_cleanup_refuses_pvc_still_used_by_running_pod(self) -> None:
        """Never delete a PV/PVC that Kubernetes says a live pod still uses."""

        repo_root = Path(__file__).resolve().parent.parent
        lifecycle = repo_root / "terraform-dbaas" / "scripts" / "lifecycle.sh"

        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            fake_kubectl = temp / "kubectl"
            delete_marker = temp / "delete-called"

            fake_kubectl.write_text(
                """#!/usr/bin/env bash
set -eu

: "${FAKE_DELETE_MARKER:?FAKE_DELETE_MARKER is required}"
args=" $* "

if [[ "$args" == *" get pv test-sc-shard-11 -o jsonpath={.spec.claimRef.name}"* ]]; then
  printf 'data-test-sc-2-0'
  exit 0
fi

if [[ "$args" == *" get pv test-sc-shard-11 -o jsonpath={.spec.claimRef.namespace}"* ]]; then
  printf 'mongodb'
  exit 0
fi

if [[ "$args" == *" get pvc data-test-sc-2-0 -o jsonpath={.spec.volumeName}"* ]]; then
  printf 'test-sc-shard-11'
  exit 0
fi

if [[ "$args" == *" get mongodb test-sc"* ]]; then
  exit 0
fi

if [[ "$args" == *" get pvc data-test-sc-2-0"* ]]; then
  exit 0
fi

if [[ "$args" == *" get pods -o json"* ]]; then
  cat <<'JSON'
{"items":[{"metadata":{"name":"test-sc-2-0"},"spec":{"volumes":[{"persistentVolumeClaim":{"claimName":"data-test-sc-2-0"}}]}}]}
JSON
  exit 0
fi

if [[ "$args" == *" delete pvc "* || "$args" == *" delete pv "* ]]; then
  touch "$FAKE_DELETE_MARKER"
  exit 99
fi

echo "Unexpected fake kubectl invocation: $*" >&2
exit 1
""",
                encoding="utf-8",
            )
            fake_kubectl.chmod(0o755)

            env = os.environ.copy()
            env.update(
                {
                    "PATH": f"{temp}{os.pathsep}{env['PATH']}",
                    "FAKE_DELETE_MARKER": str(delete_marker),
                    "TC_ACTION": "cleanup_sharded_cluster_volume",
                    "TC_NAMESPACE": "mongodb",
                    "TC_KUBECONFIG": "/tmp/fake-kubeconfig",
                    "TC_DEPLOYMENT": "test-sc",
                    "TC_PV_NAME": "test-sc-shard-11",
                    # Blank simulates legacy pre-fix Terraform state where the
                    # PV did not record its deterministic expected PVC.
                    "TC_PVC_NAME": "",
                    "TC_STORAGE_BASE_PATH": "/tmp/storage",
                    "TC_STORAGE_NODE_NAME": "fake-node",
                    "TC_STORAGE_CLEANUP_TIMEOUT": "0s",
                }
            )

            result = subprocess.run(
                ["bash", str(lifecycle)],
                cwd=lifecycle.parent,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 42, msg=result.stderr)
            self.assertIn("still used by pod(s) from the removed shard: test-sc-2-0", result.stderr)
            self.assertIn("Timed out waiting 0s", result.stderr)
            self.assertIn("No PVC/PV was deleted", result.stderr)
            self.assertFalse(delete_marker.exists())


    def test_live_shard_cleanup_waits_for_terminating_pod_to_release_pvc(self) -> None:
        """Live shard contraction waits for terminating pods before deleting storage."""

        repo_root = Path(__file__).resolve().parent.parent
        lifecycle = repo_root / "terraform-dbaas" / "scripts" / "lifecycle.sh"

        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            fake_kubectl = temp / "kubectl"
            fake_docker = temp / "docker"
            pod_checks = temp / "pod-checks"
            pvc_deleted = temp / "pvc-deleted"
            pv_deleted = temp / "pv-deleted"

            fake_kubectl.write_text(
                """#!/usr/bin/env bash
set -eu

: "${FAKE_POD_CHECKS:?}"
: "${FAKE_PVC_DELETED:?}"
: "${FAKE_PV_DELETED:?}"
args=" $* "

if [[ "$args" == *" get mongodb test-sc"* ]]; then
  exit 0
fi

if [[ "$args" == *" get pv test-sc-shard-11 -o jsonpath={.spec.claimRef.name}"* ]]; then
  printf 'data-test-sc-2-0'
  exit 0
fi

if [[ "$args" == *" get pv test-sc-shard-11 -o jsonpath={.spec.claimRef.namespace}"* ]]; then
  printf 'mongodb'
  exit 0
fi

if [[ "$args" == *" get pvc data-test-sc-2-0 -o jsonpath={.spec.volumeName}"* ]]; then
  printf 'test-sc-shard-11'
  exit 0
fi

if [[ "$args" == *" get pvc data-test-sc-2-0"* ]]; then
  [[ -f "$FAKE_PVC_DELETED" ]] && exit 1
  exit 0
fi

if [[ "$args" == *" get pods -o json"* ]]; then
  n=0
  [[ -f "$FAKE_POD_CHECKS" ]] && n=$(cat "$FAKE_POD_CHECKS")
  n=$((n + 1))
  printf '%s' "$n" > "$FAKE_POD_CHECKS"
  if [[ "$n" -eq 1 ]]; then
    printf '{"items":[{"metadata":{"name":"test-sc-2-0"},"spec":{"volumes":[{"persistentVolumeClaim":{"claimName":"data-test-sc-2-0"}}]}}]}'
  else
    printf '{"items":[]}'
  fi
  exit 0
fi

if [[ "$args" == *" delete pvc data-test-sc-2-0"* ]]; then
  touch "$FAKE_PVC_DELETED"
  exit 0
fi

if [[ "$args" == *" delete pv test-sc-shard-11"* ]]; then
  touch "$FAKE_PV_DELETED"
  exit 0
fi

if [[ "$args" == *" get pv test-sc-shard-11"* ]]; then
  [[ -f "$FAKE_PV_DELETED" ]] && exit 1
  exit 0
fi

echo "Unexpected fake kubectl invocation: $*" >&2
exit 1
""",
                encoding="utf-8",
            )
            fake_kubectl.chmod(0o755)
            fake_docker.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            fake_docker.chmod(0o755)

            env = os.environ.copy()
            env.update(
                {
                    "PATH": f"{temp}{os.pathsep}{env['PATH']}",
                    "FAKE_POD_CHECKS": str(pod_checks),
                    "FAKE_PVC_DELETED": str(pvc_deleted),
                    "FAKE_PV_DELETED": str(pv_deleted),
                    "TC_ACTION": "cleanup_sharded_cluster_volume",
                    "TC_NAMESPACE": "mongodb",
                    "TC_KUBECONFIG": "/tmp/fake-kubeconfig",
                    "TC_DEPLOYMENT": "test-sc",
                    "TC_PV_NAME": "test-sc-shard-11",
                    "TC_PVC_NAME": "data-test-sc-2-0",
                    "TC_STORAGE_BASE_PATH": "/tmp/storage",
                    "TC_STORAGE_NODE_NAME": "fake-node",
                    "TC_STORAGE_CLEANUP_TIMEOUT": "5s",
                }
            )

            result = subprocess.run(
                ["bash", str(lifecycle)],
                cwd=lifecycle.parent,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertIn("still used by pod(s) from the removed shard", result.stderr)
            self.assertIn("Waiting up to 5s", result.stderr)
            self.assertGreaterEqual(int(pod_checks.read_text(encoding="utf-8")), 2)
            self.assertTrue(pvc_deleted.exists())
            self.assertTrue(pv_deleted.exists())


    def test_legacy_storage_mismatch_is_cleaned_only_after_cluster_is_absent(self) -> None:
        """Full teardown may use the PV's actual claim after MongoDB is gone."""

        repo_root = Path(__file__).resolve().parent.parent
        lifecycle = repo_root / "terraform-dbaas" / "scripts" / "lifecycle.sh"

        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            fake_kubectl = temp / "kubectl"
            fake_docker = temp / "docker"
            pvc_deleted = temp / "pvc-deleted"
            pv_deleted = temp / "pv-deleted"

            fake_kubectl.write_text(
                """#!/usr/bin/env bash
set -eu

: "${FAKE_PVC_DELETED:?}"
: "${FAKE_PV_DELETED:?}"
args=" $* "

if [[ "$args" == *" get pv test-sc-shard-8 -o jsonpath={.spec.claimRef.name}"* ]]; then
  printf 'data-test-sc-3-1'
  exit 0
fi

if [[ "$args" == *" get pv test-sc-shard-8 -o jsonpath={.spec.claimRef.namespace}"* ]]; then
  printf 'mongodb'
  exit 0
fi

if [[ "$args" == *" get mongodb test-sc"* ]]; then
  exit 1
fi

if [[ "$args" == *" get pvc data-test-sc-3-1 -o jsonpath={.spec.volumeName}"* ]]; then
  printf 'test-sc-shard-8'
  exit 0
fi

if [[ "$args" == *" get pvc data-test-sc-3-1"* ]]; then
  [[ -f "$FAKE_PVC_DELETED" ]] && exit 1
  exit 0
fi

if [[ "$args" == *" get pods -o json"* ]]; then
  printf '{"items":[]}'
  exit 0
fi

if [[ "$args" == *" delete pvc data-test-sc-3-1"* ]]; then
  touch "$FAKE_PVC_DELETED"
  exit 0
fi

if [[ "$args" == *" delete pv test-sc-shard-8"* ]]; then
  touch "$FAKE_PV_DELETED"
  exit 0
fi

if [[ "$args" == *" get pv test-sc-shard-8"* ]]; then
  [[ -f "$FAKE_PV_DELETED" ]] && exit 1
  exit 0
fi

echo "Unexpected fake kubectl invocation: $*" >&2
exit 1
""",
                encoding="utf-8",
            )
            fake_kubectl.chmod(0o755)
            fake_docker.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            fake_docker.chmod(0o755)

            env = os.environ.copy()
            env.update(
                {
                    "PATH": f"{temp}{os.pathsep}{env['PATH']}",
                    "FAKE_PVC_DELETED": str(pvc_deleted),
                    "FAKE_PV_DELETED": str(pv_deleted),
                    "TC_ACTION": "cleanup_sharded_cluster_volume",
                    "TC_NAMESPACE": "mongodb",
                    "TC_KUBECONFIG": "/tmp/fake-kubeconfig",
                    "TC_DEPLOYMENT": "test-sc",
                    "TC_PV_NAME": "test-sc-shard-8",
                    "TC_PVC_NAME": "data-test-sc-2-2",
                    "TC_STORAGE_BASE_PATH": "/tmp/storage",
                    "TC_STORAGE_NODE_NAME": "fake-node",
                }
            )

            result = subprocess.run(
                ["bash", str(lifecycle)],
                cwd=lifecycle.parent,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertIn("Legacy storage binding detected during full teardown", result.stderr)
            self.assertTrue(pvc_deleted.exists())
            self.assertTrue(pv_deleted.exists())

    def test_legacy_storage_mismatch_still_refuses_while_cluster_exists(self) -> None:
        """A live cluster must never reinterpret a mismatched PV as safe."""

        repo_root = Path(__file__).resolve().parent.parent
        lifecycle = repo_root / "terraform-dbaas" / "scripts" / "lifecycle.sh"

        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            fake_kubectl = temp / "kubectl"
            delete_marker = temp / "delete-called"

            fake_kubectl.write_text(
                """#!/usr/bin/env bash
set -eu

: "${FAKE_DELETE_MARKER:?}"
args=" $* "

if [[ "$args" == *" get pv test-sc-shard-8 -o jsonpath={.spec.claimRef.name}"* ]]; then
  printf 'data-test-sc-3-1'
  exit 0
fi

if [[ "$args" == *" get pv test-sc-shard-8 -o jsonpath={.spec.claimRef.namespace}"* ]]; then
  printf 'mongodb'
  exit 0
fi

if [[ "$args" == *" get mongodb test-sc"* ]]; then
  exit 0
fi

if [[ "$args" == *" delete pvc "* || "$args" == *" delete pv "* ]]; then
  touch "$FAKE_DELETE_MARKER"
  exit 99
fi

echo "Unexpected fake kubectl invocation: $*" >&2
exit 1
""",
                encoding="utf-8",
            )
            fake_kubectl.chmod(0o755)

            env = os.environ.copy()
            env.update(
                {
                    "PATH": f"{temp}{os.pathsep}{env['PATH']}",
                    "FAKE_DELETE_MARKER": str(delete_marker),
                    "TC_ACTION": "cleanup_sharded_cluster_volume",
                    "TC_NAMESPACE": "mongodb",
                    "TC_KUBECONFIG": "/tmp/fake-kubeconfig",
                    "TC_DEPLOYMENT": "test-sc",
                    "TC_PV_NAME": "test-sc-shard-8",
                    "TC_PVC_NAME": "data-test-sc-2-2",
                    "TC_STORAGE_BASE_PATH": "/tmp/storage",
                    "TC_STORAGE_NODE_NAME": "fake-node",
                }
            )

            result = subprocess.run(
                ["bash", str(lifecycle)],
                cwd=lifecycle.parent,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 42, msg=result.stderr)
            self.assertIn("Refusing storage cleanup", result.stderr)
            self.assertFalse(delete_marker.exists())


    def test_full_teardown_waits_for_terminating_pod_to_release_pvc(self) -> None:
        """After the MongoDB CR is gone, terminating pods get a bounded grace wait."""

        repo_root = Path(__file__).resolve().parent.parent
        lifecycle = repo_root / "terraform-dbaas" / "scripts" / "lifecycle.sh"

        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            fake_kubectl = temp / "kubectl"
            fake_docker = temp / "docker"
            pod_checks = temp / "pod-checks"
            pvc_deleted = temp / "pvc-deleted"
            pv_deleted = temp / "pv-deleted"

            fake_kubectl.write_text(
                """#!/usr/bin/env bash
set -eu

: "${FAKE_POD_CHECKS:?}"
: "${FAKE_PVC_DELETED:?}"
: "${FAKE_PV_DELETED:?}"
args=" $* "

if [[ "$args" == *" get mongodb test-sc"* ]]; then
  exit 1
fi

if [[ "$args" == *" get pv test-sc-shard-0 -o jsonpath={.spec.claimRef.name}"* ]]; then
  printf 'data-test-sc-0-0'
  exit 0
fi

if [[ "$args" == *" get pv test-sc-shard-0 -o jsonpath={.spec.claimRef.namespace}"* ]]; then
  printf 'mongodb'
  exit 0
fi

if [[ "$args" == *" get pvc data-test-sc-0-0 -o jsonpath={.spec.volumeName}"* ]]; then
  printf 'test-sc-shard-0'
  exit 0
fi

if [[ "$args" == *" get pvc data-test-sc-0-0"* ]]; then
  [[ -f "$FAKE_PVC_DELETED" ]] && exit 1
  exit 0
fi

if [[ "$args" == *" get pods -o json"* ]]; then
  n=0
  [[ -f "$FAKE_POD_CHECKS" ]] && n=$(cat "$FAKE_POD_CHECKS")
  n=$((n + 1))
  printf '%s' "$n" > "$FAKE_POD_CHECKS"
  if [[ "$n" -eq 1 ]]; then
    printf '{"items":[{"metadata":{"name":"test-sc-0-0"},"spec":{"volumes":[{"persistentVolumeClaim":{"claimName":"data-test-sc-0-0"}}]}}]}'
  else
    printf '{"items":[]}'
  fi
  exit 0
fi

if [[ "$args" == *" delete pvc data-test-sc-0-0"* ]]; then
  touch "$FAKE_PVC_DELETED"
  exit 0
fi

if [[ "$args" == *" delete pv test-sc-shard-0"* ]]; then
  touch "$FAKE_PV_DELETED"
  exit 0
fi

if [[ "$args" == *" get pv test-sc-shard-0"* ]]; then
  [[ -f "$FAKE_PV_DELETED" ]] && exit 1
  exit 0
fi

echo "Unexpected fake kubectl invocation: $*" >&2
exit 1
""",
                encoding="utf-8",
            )
            fake_kubectl.chmod(0o755)
            fake_docker.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            fake_docker.chmod(0o755)

            env = os.environ.copy()
            env.update(
                {
                    "PATH": f"{temp}{os.pathsep}{env['PATH']}",
                    "FAKE_POD_CHECKS": str(pod_checks),
                    "FAKE_PVC_DELETED": str(pvc_deleted),
                    "FAKE_PV_DELETED": str(pv_deleted),
                    "TC_ACTION": "cleanup_sharded_cluster_volume",
                    "TC_NAMESPACE": "mongodb",
                    "TC_KUBECONFIG": "/tmp/fake-kubeconfig",
                    "TC_DEPLOYMENT": "test-sc",
                    "TC_PV_NAME": "test-sc-shard-0",
                    "TC_PVC_NAME": "data-test-sc-0-0",
                    "TC_STORAGE_BASE_PATH": "/tmp/storage",
                    "TC_STORAGE_NODE_NAME": "fake-node",
                    "TC_STORAGE_CLEANUP_TIMEOUT": "5s",
                }
            )

            result = subprocess.run(
                ["bash", str(lifecycle)],
                cwd=lifecycle.parent,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertIn("Waiting up to 5s", result.stderr)
            self.assertGreaterEqual(int(pod_checks.read_text(encoding="utf-8")), 2)
            self.assertTrue(pvc_deleted.exists())
            self.assertTrue(pv_deleted.exists())

    def test_full_teardown_unbound_pv_does_not_adopt_expected_pvc(self) -> None:
        """An unbound PV may be removed, but it must not delete another PV's PVC."""

        repo_root = Path(__file__).resolve().parent.parent
        lifecycle = repo_root / "terraform-dbaas" / "scripts" / "lifecycle.sh"

        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            fake_kubectl = temp / "kubectl"
            fake_docker = temp / "docker"
            pvc_touched = temp / "pvc-touched"
            pv_deleted = temp / "pv-deleted"

            fake_kubectl.write_text(
                """#!/usr/bin/env bash
set -eu

: "${FAKE_PVC_TOUCHED:?}"
: "${FAKE_PV_DELETED:?}"
args=" $* "

if [[ "$args" == *" get mongodb test-sc"* ]]; then
  exit 1
fi

if [[ "$args" == *" get pv test-sc-shard-10 -o jsonpath={.spec.claimRef.name}"* ]]; then
  exit 0
fi

if [[ "$args" == *" get pv test-sc-shard-10 -o jsonpath={.spec.claimRef.namespace}"* ]]; then
  exit 0
fi

if [[ "$args" == *" pvc "* ]]; then
  touch "$FAKE_PVC_TOUCHED"
  exit 99
fi

if [[ "$args" == *" delete pv test-sc-shard-10"* ]]; then
  touch "$FAKE_PV_DELETED"
  exit 0
fi

if [[ "$args" == *" get pv test-sc-shard-10"* ]]; then
  [[ -f "$FAKE_PV_DELETED" ]] && exit 1
  exit 0
fi

echo "Unexpected fake kubectl invocation: $*" >&2
exit 1
""",
                encoding="utf-8",
            )
            fake_kubectl.chmod(0o755)
            fake_docker.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            fake_docker.chmod(0o755)

            env = os.environ.copy()
            env.update(
                {
                    "PATH": f"{temp}{os.pathsep}{env['PATH']}",
                    "FAKE_PVC_TOUCHED": str(pvc_touched),
                    "FAKE_PV_DELETED": str(pv_deleted),
                    "TC_ACTION": "cleanup_sharded_cluster_volume",
                    "TC_NAMESPACE": "mongodb",
                    "TC_KUBECONFIG": "/tmp/fake-kubeconfig",
                    "TC_DEPLOYMENT": "test-sc",
                    "TC_PV_NAME": "test-sc-shard-10",
                    "TC_PVC_NAME": "data-test-sc-3-1",
                    "TC_STORAGE_BASE_PATH": "/tmp/storage",
                    "TC_STORAGE_NODE_NAME": "fake-node",
                    "TC_STORAGE_CLEANUP_TIMEOUT": "5s",
                }
            )

            result = subprocess.run(
                ["bash", str(lifecycle)],
                cwd=lifecycle.parent,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertFalse(pvc_touched.exists())
            self.assertTrue(pv_deleted.exists())


if __name__ == "__main__":
    unittest.main()
