"""Unit tests for Kubernetes status interpretation helpers."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from terraform_controller import kube


class KubeStatusTests(unittest.TestCase):
    """Verify raw Kubernetes objects are translated into useful DBaaS states."""

    def setUp(self) -> None:
        self.config = {
            "kubeconfig": "/tmp/kubeconfig",
            "kube_context": "test",
            "mongodb_namespace": "mongodb",
        }

    def test_list_json_cluster_scoped_resource_omits_namespace_flag(self) -> None:
        completed = SimpleNamespace(
            returncode=0,
            stdout='{"items": []}',
            stderr="",
        )
        with patch.object(kube, "run_process", return_value=completed) as run_mock:
            kube.list_json(
                self.config,
                "pv",
                label_selector="app.kubernetes.io/managed-by=terraformController",
                namespaced=False,
            )

        command = run_mock.call_args.args[0]
        self.assertNotIn("-n", command)
        self.assertIn("pv", command)
        self.assertIn("-l", command)

    def test_list_json_namespaced_resource_uses_configured_namespace(self) -> None:
        completed = SimpleNamespace(
            returncode=0,
            stdout='{"items": []}',
            stderr="",
        )
        with patch.object(kube, "run_process", return_value=completed) as run_mock:
            kube.list_json(self.config, "pvc")

        command = run_mock.call_args.args[0]
        self.assertIn("-n", command)
        self.assertIn("mongodb", command)

    def test_phase_returns_absent_for_missing_mongodb_resource(self) -> None:
        with patch.object(kube, "get_json", return_value=None):
            self.assertEqual(kube.phase(self.config, "sc9"), "Absent")

    def test_statefulset_online_when_all_replicas_ready(self) -> None:
        obj = {
            "spec": {"replicas": 3},
            "status": {"readyReplicas": 3, "updatedReplicas": 3},
        }
        with patch.object(kube, "get_json", return_value=obj):
            status = kube._statefulset_status(self.config, "sc9-0")
        self.assertEqual(status["status"], "Online")

    def test_statefulset_degraded_when_some_replicas_ready(self) -> None:
        obj = {
            "spec": {"replicas": 3},
            "status": {"readyReplicas": 1, "updatedReplicas": 2},
        }
        with patch.object(kube, "get_json", return_value=obj):
            status = kube._statefulset_status(self.config, "sc9-0")
        self.assertEqual(status["status"], "Degraded")

    def test_missing_statefulset_is_creating_while_cluster_pending(self) -> None:
        with patch.object(kube, "get_json", return_value=None):
            status = kube._statefulset_status(
                self.config, "sc9-0", fallback_phase="Pending"
            )
        self.assertEqual(status["status"], "Creating")

    def test_sharded_cluster_status_reports_each_component(self) -> None:
        def fake_get(config, resource, name):
            if resource == "mongodb":
                return {"status": {"phase": "Running"}}
            replicas = 2 if name.endswith("-mongos") else 3
            return {
                "spec": {"replicas": replicas},
                "status": {
                    "readyReplicas": replicas,
                    "updatedReplicas": replicas,
                },
            }

        with patch.object(kube, "get_json", side_effect=fake_get):
            status = kube.sharded_cluster_status(
                self.config, "sc9", 2
            )

        self.assertEqual(len(status["shards"]), 2)
        self.assertEqual(status["shards"][0]["status"], "Online")
        self.assertEqual(status["config_servers"]["status"], "Online")
        self.assertEqual(status["mongos"]["status"], "Online")


if __name__ == "__main__":
    unittest.main()
