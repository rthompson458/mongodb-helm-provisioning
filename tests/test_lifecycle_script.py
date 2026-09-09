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
                    "TC_ACTION": "create_database",
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


if __name__ == "__main__":
    unittest.main()
