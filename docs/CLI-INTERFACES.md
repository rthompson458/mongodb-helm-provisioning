# Controller CLI Interface Separation

## Purpose

The MongoDB DBaaS controller exposes two intentionally separate command-line interfaces.

| Interface | Audience | Responsibility |
| --- | --- | --- |
| `privateWorkerReplacement.py` | DBaaS user / customer demo | Normal service lifecycle and status |
| `privateWorkerReplacementAdmin.py` | Platform administrator | Diagnostics, reconciliation, and exceptional recovery |

This separation keeps customer workflows service-oriented while preserving the deeper controls needed to operate and recover the platform.

Running either executable with no command prints that interface's full help screen and exits successfully. The no-argument form is intentionally equivalent to `--help`.

## Customer interface

The public interface exposes:

```text
AddReplicaSet
DeleteReplicaSet
ListReplicaSets
ListReplicaSet

AddShardedCluster
DeleteShardedCluster
ListShardedClusters
ListShardedCluster
ListShards

AddShard
DeleteShard

AddDatabase
DeleteDatabase
ListDatabases
ListDatabase
ListDatabaseAccounts

RotatePasswords
DisableOwner

ListDeployments
ListDeployment
```

Database status and database-account status are intentionally separate concerns:

```text
ListDatabases [DEPLOYMENT]
  -> database inventory and lifecycle/service status only

ListDatabase [DEPLOYMENT] DATABASE
  -> one database and database-level status/details only

ListDatabaseAccounts [DEPLOYMENT] DATABASE
  -> the three managed accounts, Enabled/Disabled state,
     rotation information, Vault paths, and browser-ready Vault URLs
```

Database-level status can include:

```text
Creating
Ready
Deleting
Unavailable
```

An active `AddDatabase` operation can appear as `Creating` before the new database is committed to the normal Vault-backed managed inventory. An active `DeleteDatabase` operation appears as `Deleting`. `Ready` requires a sufficiently healthy parent deployment. `Unavailable` means the database is known but the parent deployment is not sufficiently healthy for normal service.

These customer commands run asynchronously because they can take meaningful time:

```text
AddReplicaSet
DeleteReplicaSet
AddShardedCluster
DeleteShardedCluster
AddShard
DeleteShard
AddDatabase
DeleteDatabase
```

The customer receives a concise acknowledgement and a normal resource-status command. The public interface does not expose:

- internal operation IDs;
- worker PIDs;
- operation-state files;
- raw Git/Terraform output;
- Terraform recovery commands;
- deployment-lock recovery;
- orphaned-state recovery;
- controller-wide Reconcile.

`RotatePasswords` and `DisableOwner` remain synchronous, but their Git/Terraform implementation output is captured in the operations log rather than displayed on the customer terminal.

`ListDatabaseAccounts` provides the complete Vault browser URLs for each managed credential, along with the logical Vault paths.

## Administrator interface

The administrator interface exposes:

```text
ListManagedResources
ListOperations
ListOperation OPERATION_ID
Reconcile
RecoverDeploymentLock SHARDED_CLUSTER --confirm
RecoverOrphanedResources --confirm
```

`ListManagedResources` is the read-only managed-resource inventory and zero-state check for:

```text
Vault-backed managed deployments
managed MongoDB resources
managed MongoDBUser resources
managed PVCs
managed PVs
deployment locks
```

If all six categories are empty, it reports:

```text
Status: CLEAN
```

If legitimate controller-managed resources exist, it reports:

```text
Status: MANAGED RESOURCES PRESENT
```

That second status is informational. Resource presence alone is not treated as a health failure.

The shorter name `ListResources` is intentionally not supported because it could imply a cluster-wide resource listing.

`ListOperations` and `ListOperation` expose the private asynchronous operation journal used for troubleshooting and recovery. Detailed Git/Terraform output is still kept in the operations log rather than routinely printed on the admin terminal.

## Asynchronous execution model

An asynchronous public request follows this model:

```text
Customer
  -> privateWorkerReplacement.py
     -> validate obvious command-line safety requirements
     -> create private operation-state record
     -> start detached worker
     -> return customer acknowledgement

Detached worker
  -> same privateWorkerReplacement.py lifecycle implementation
  -> Python validation/orchestration
  -> Terraform
  -> lifecycle.sh where required
  -> Kubernetes / MongoDB Operator / Ops Manager / Vault / MongoDB
  -> verify convergence
  -> mark operation Succeeded or Failed
```

The public response deliberately hides the operation ID. The acceptance harness is internal engineering tooling, so it correlates the private state entry and polls detailed status through `privateWorkerReplacementAdmin.py ListOperation`.

Database create/delete uses the same model as deployment and shard lifecycle. Moving those requests to a worker changes only how the user waits; it does not change the underlying Terraform-driven lifecycle or safety checks.

Read-only database status also consults the operation journal so the public CLI can represent in-progress `Creating` and `Deleting` states without exposing the internal operation ID.

## Logging and operation state

Runtime evidence uses one predictable tree beside `privateWorkerReplacement.config`:

```text
logs/
  controller/
    controller-YYYYMMDD.log
  operations/
    operations-YYYYMMDD.log
    state/
      <operation-id>.json
    work/
      <operation-id>.tmp       # temporary while a detached worker runs
```

The controller log is structured JSON Lines. The operations log contains detailed Git/Terraform and worker diagnostics. Both are append-only daily files using a UTC date.

Operation-state JSON files are not human log files. They provide durable-enough local state for operation result tracking, interrupted-worker detection, acceptance-harness polling, database lifecycle status, and guarded recovery.

Detached worker stdout/stderr is temporarily buffered per operation so concurrent workers do not mix ordinary transcript lines. The transcript is appended to the daily operations log as one block when the worker reaches a terminal result.

The `logs/` tree is ignored by Git. Ordinary `git clean -fd` leaves it alone; deleting/recloning the repository or explicitly cleaning ignored files removes the local runtime history/state.

## Shared implementation

The two CLIs are separate interfaces over shared controller modules:

```text
privateWorkerReplacement.py
  -> privateWorkerReplacement/cli.py

privateWorkerReplacementAdmin.py
  -> privateWorkerReplacement/admin_cli.py
     -> privateWorkerReplacement/admin_status.py

Customer database status/account presentation:
  -> privateWorkerReplacement/database_status.py

Both use shared lifecycle/support modules:
  privateWorkerReplacement/deployments.py
  privateWorkerReplacement/databases.py
  privateWorkerReplacement/maintenance.py
  privateWorkerReplacement/deployment_lock.py
  privateWorkerReplacement/terraform_runner.py
  privateWorkerReplacement/async_operations.py
  privateWorkerReplacement/logging_component.py
  privateWorkerReplacement/runtime_paths.py
  privateWorkerReplacement/kube.py
  privateWorkerReplacement/vault.py
```

This avoids duplicating lifecycle logic and preserves the Terraform-driven mutation boundary.

## Security boundary

Two executable names improve clarity and reduce accidental exposure, but they do not replace authorization.

Production should additionally restrict:

- who can execute the administrator CLI;
- Vault administrator policies;
- Kubernetes mutation permissions;
- Terraform backend access;
- host access;
- administrator logs and operation state.

The intended operating model is:

```text
DBaaS customer
  -> python3 privateWorkerReplacement.py ...

Authorized service operator
  -> python3 privateWorkerReplacementAdmin.py ...
  -> privileged supporting infrastructure
```

## Documentation ownership

Customer manual:

```text
README-privateWorkerReplacement.md
```

Administrator runbook:

```text
README-privateWorkerReplacementAdmin.md
```

Testing guide:

```text
tests/README.md
```

Keeping these audiences separate is part of the product design, not merely a documentation preference.
