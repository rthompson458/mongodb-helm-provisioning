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
EnableOwner

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
- raw Terraform/external-command output;
- Terraform recovery commands;
- deployment-lock recovery;
- orphaned-state recovery;
- controller-wide Reconcile.

`RotatePasswords`, `DisableOwner`, and `EnableOwner` remain synchronous, but their Terraform/external-command implementation output is captured in the operations log rather than displayed on the customer terminal. `EnableOwner` restores the Owner account using the existing managed credential and does not rotate its password.

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

`ListManagedResources` is the read-only authoritative DBaaS inventory across
Vault, Kubernetes, Terraform backend state, and Ops Manager. It reports:

```text
managed ReplicaSets and ShardedClusters
managed databases and fixed Owner/ReadWrite/Read accounts
managed MongoDB and MongoDBUser resources
DBaaS PVCs and PVs
controller Secrets, ConfigMaps, and deployment locks
Ops Manager DBaaS projects and group Secrets
orphan Ops Manager projects and group Secrets
managed deployments missing an Ops Manager project
permanent controller/platform infrastructure
```

Ops Manager projects and group Secrets include the project/group ID so an
administrator can correlate a project with its `<PROJECT_ID>-group-secret`.

Permanent controller infrastructure remains visible but does not make zero-state
dirty. Examples include `tc-ops-manager-projects`, the Terraform backend state
Secret, and the base `mongodb-development` Ops Manager project.

Status meanings are:

```text
CLEAN
  No active DBaaS-managed resources and no detected orphan/missing artifacts.
  Permanent controller/platform infrastructure may still be listed.

MANAGED RESOURCES PRESENT
  Legitimate active DBaaS-managed resources exist. This is informational.

ATTENTION REQUIRED
  Cross-plane leftovers or mismatches were detected, such as an orphan Ops
  Manager project, orphan group Secret, missing expected DBaaS project, or the
  permanent Ops Manager platform project itself being missing.
```


The shorter name `ListResources` is intentionally not supported because it could imply a cluster-wide resource listing.

`ListOperations` and `ListOperation` expose the private asynchronous operation journal used for troubleshooting and recovery. Detailed Terraform/external-command output is still kept in the operations log rather than routinely printed on the admin terminal.

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

Runtime evidence uses one predictable tree beside `dev.config`:

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

The controller log is structured JSON Lines. The operations log contains detailed Terraform/external-command and worker diagnostics. Both are append-only daily files using a UTC date.

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

Customer read-only status presentation:
  -> privateWorkerReplacement/deployment_status.py
  -> privateWorkerReplacement/database_status.py
  -> privateWorkerReplacement/credential_display.py

Both use shared lifecycle/support modules:
  privateWorkerReplacement/deployments.py
  privateWorkerReplacement/shards.py
  privateWorkerReplacement/databases.py
  privateWorkerReplacement/maintenance.py
  privateWorkerReplacement/ops_manager.py
  privateWorkerReplacement/deployment_lock.py
  privateWorkerReplacement/terraform_runner.py
  privateWorkerReplacement/async_operations.py
  privateWorkerReplacement/logging_component.py
  privateWorkerReplacement/runtime_paths.py
  privateWorkerReplacement/kube.py
  privateWorkerReplacement/vault.py
```

This avoids duplicating lifecycle logic and preserves the Terraform-driven mutation boundary. Normal DBaaS desired-state changes remain Terraform-owned. Deployment teardown separately removes the per-deployment Ops Manager project and Operator-created group Secret because those cross-plane artifacts are not Terraform desired-state resources.

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
