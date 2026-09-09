"""Compatibility facade for terraformController lifecycle functions.

The implementation is intentionally split by responsibility:
- deployments.py: ReplicaSet, ShardedCluster, and shard lifecycle/status
- databases.py: database, account, Vault credential, and rotation lifecycle
- maintenance.py: reconciliation
"""

from .databases import (
    add_database,
    delete_database,
    disable_owner,
    list_database,
    list_databases,
    rotate_passwords,
)
from .deployments import (
    add_replica_set,
    add_shard,
    add_sharded_cluster,
    delete_replica_set,
    delete_shard,
    delete_sharded_cluster,
    list_deployment,
    list_deployments,
    list_replica_set,
    list_replica_sets,
    list_sharded_cluster,
    list_sharded_clusters,
    list_shards,
)
from .maintenance import reconcile, recover_deployment_lock

__all__ = [
    "add_database",
    "add_replica_set",
    "add_shard",
    "add_sharded_cluster",
    "delete_database",
    "delete_replica_set",
    "delete_shard",
    "delete_sharded_cluster",
    "disable_owner",
    "list_database",
    "list_databases",
    "list_deployment",
    "list_deployments",
    "list_replica_set",
    "list_replica_sets",
    "list_sharded_cluster",
    "list_sharded_clusters",
    "list_shards",
    "reconcile",
    "recover_deployment_lock",
    "rotate_passwords",
]
