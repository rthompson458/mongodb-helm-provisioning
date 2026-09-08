"""Unit tests for Kubernetes status interpretation helpers."""

from __future__ import annotations

import unittest
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
