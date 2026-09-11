"""Compatibility facade for public/admin controller functions.

The implementation is split by responsibility so entry points can import one
stable facade without turning one source file into a catch-all:

- deployments.py: ReplicaSet/ShardedCluster/shard mutation workflows
- deployment_status.py: read-only deployment and shard presentation
- databases.py: database/credential mutation workflows
- database_status.py: read-only database and account presentation
- admin_status.py: administrator managed-resource presentation
- maintenance.py: controller-wide reconciliation and guarded recovery
"""

from .admin_status import list_managed_resources
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
)
from .deployment_status import (
    list_deployment,
    list_deployments,
    list_replica_set,
    list_replica_sets,
    list_sharded_cluster,
    list_sharded_clusters,
    list_shards,
)
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
