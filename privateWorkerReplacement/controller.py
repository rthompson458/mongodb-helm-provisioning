"""Compatibility facade for privateWorkerReplacement lifecycle functions.

The implementation is intentionally split by responsibility:
- deployments.py: ReplicaSet, ShardedCluster, and shard lifecycle/status
- databases.py: database mutation, Vault credential, and rotation lifecycle
- database_status.py: read-only database and account status
- admin_status.py: administrator resource-inventory presentation
- maintenance.py: reconciliation and recovery
"""

from .databases import (
    add_database,
    delete_database,
    disable_owner,
    enable_owner,
    rotate_passwords,
)
from .database_status import (
    list_database,
    list_database_accounts,
    list_databases,
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
from .admin_status import list_managed_resources
from .maintenance import (
    reconcile,
    recover_deployment_lock,
    recover_orphaned_resources,
)

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
    "enable_owner",
    "list_database",
    "list_database_accounts",
    "list_databases",
    "list_deployment",
    "list_deployments",
    "list_replica_set",
    "list_replica_sets",
    "list_sharded_cluster",
    "list_sharded_clusters",
    "list_shards",
    "list_managed_resources",
    "reconcile",
    "recover_deployment_lock",
    "recover_orphaned_resources",
    "rotate_passwords",
]
