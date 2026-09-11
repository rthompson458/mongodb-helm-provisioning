# privateWorkerReplacement Administrator Guide

`privateWorkerReplacementAdmin.py` is the platform-administrator interface for the MongoDB DBaaS controller. It is intentionally separate from the customer-facing `privateWorkerReplacement.py` command.

Use the customer CLI for normal deployment, shard, database, credential, and service-status work. Use the administrator CLI for operation diagnostics, reconciliation, managed-resource inventory, and exceptional recovery.

The executable split is an interface boundary, not an authorization boundary. Production must also restrict administrator host access, Kubernetes privileges, Vault policy, and Terraform backend access.

---

## 1. Quick start

Run the program with no command to show the full administrator help screen:

```bash
python3 privateWorkerReplacementAdmin.py
```

This is intentionally equivalent to:

```bash
python3 privateWorkerReplacementAdmin.py --help
```

Both forms print help and exit successfully.

Show command-specific help:

```bash
python3 privateWorkerReplacementAdmin.py ListManagedResources --help
python3 privateWorkerReplacementAdmin.py ListOperations --help
python3 privateWorkerReplacementAdmin.py ListOperation --help
python3 privateWorkerReplacementAdmin.py Reconcile --help
python3 privateWorkerReplacementAdmin.py RecoverDeploymentLock --help
python3 privateWorkerReplacementAdmin.py RecoverOrphanedResources --help
```

The administrator interface uses the same Vault token as the public controller and assumes this configuration file in the current working directory:

```text
./dev.config
```

Use `--config FILE` only when another configuration file is intentionally selected. Do not store the Vault token in the config file.

---

## 2. Command reference

| Command | Purpose | Mutation |
| --- | --- | --- |
| `ListManagedResources` | List controller-managed resources and report whether managed resources are present | Read-only |
| `ListOperations` | List recent asynchronous controller operations | Read-only |
| `ListOperation OPERATION_ID` | Show detailed status for one asynchronous operation | Read-only |
| `Reconcile` | Reapply complete Vault-backed desired state through Terraform | Yes |
| `RecoverDeploymentLock SC --confirm` | Release a completed but stranded ShardedCluster topology lock after safety validation | Yes |
| `RecoverOrphanedResources --confirm` | Finish Terraform cleanup after desired-state inventory is already empty | Yes |

These commands are deliberately not accepted by `privateWorkerReplacement.py`.

---

## 3. ListManagedResources

```bash
python3 privateWorkerReplacementAdmin.py ListManagedResources
```

This is the read-only authoritative DBaaS inventory and zero-state check. It
correlates state across Vault, Kubernetes, Terraform backend state, and Ops
Manager rather than looking only at Kubernetes deployment objects.

The summary includes counts for:

```text
Managed deployments
ReplicaSets
ShardedClusters
Databases
Managed accounts
MongoDB resources
MongoDB users
DBaaS PVCs
DBaaS PVs
Controller Secrets
Controller ConfigMaps
Deployment locks
Ops Manager DBaaS projects
Ops Manager group secrets
Ops Manager orphan projects
Orphan group secrets
Missing Ops Manager projects
Controller infrastructure ConfigMaps
Terraform backend states
Ops Manager platform project
Ops Manager platform group secrets
```

Ops Manager project and group-secret entries include the project/group ID. For
example:

```text
Ops Manager platform project:
  mongodb-development (Project ID: 6a973d4e12c067880c465361)

Orphan group secrets:
  6aa408678225d417ab4d6929-group-secret
  (Project ID: 6aa408678225d417ab4d6929)
```

The exact line wrapping is terminal-dependent, but the project ID is always
included in the inventory value.

### Status meanings

`CLEAN` means there are no active DBaaS-managed resources and no detected
cross-plane leftovers or mismatches. Permanent controller/platform
infrastructure may still be listed. In particular, these can remain in a clean
zero-deployment environment:

```text
tc-ops-manager-projects
Terraform backend state Secret
mongodb-development Ops Manager platform project
platform group Secret, when present
```

`MANAGED RESOURCES PRESENT` means legitimate active DBaaS resources exist. It
is neutral inventory information, not a health failure.

`ATTENTION REQUIRED` means the command detected a cross-plane mismatch or
leftover that an administrator should investigate, including:

```text
Ops Manager project with no corresponding managed deployment
<PROJECT_ID>-group-secret with no corresponding live project/deployment
managed deployment with no corresponding Ops Manager project
```

The command does not delete, reconcile, or repair anything.

---

## 4. Asynchronous operation diagnostics

Customer operations that currently execute asynchronously include:

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

`RecoverOrphanedResources` is also asynchronous on the administrator interface because cleanup can contain bounded storage waits.

The customer CLI intentionally hides internal operation IDs. Administrators can inspect them here.

### ListOperations

```bash
python3 privateWorkerReplacementAdmin.py ListOperations
```

Shows up to 50 recent operations with:

```text
Operation ID
Command
Deployment/scope
Result
Elapsed time
```

Possible results include:

```text
In Progress
Succeeded
Failed
Interrupted
```

### ListOperation

```bash
python3 privateWorkerReplacementAdmin.py ListOperation 7c1349abc123
```

Shows detailed status for one operation:

```text
Operation ID
Command
Deployment/scope
Result
Submitted timestamp
Started timestamp
Completed timestamp
Elapsed time
Worker PID
Daily operations-log path
Recorded result/error message
```

Use this when a background request appears stalled, a test step fails, a detached worker is interrupted, or exact diagnostic evidence is needed.

---

## 5. Runtime log and state layout

Logging no longer uses a configurable `[Logging]` section.

Structured controller events are appended to:

```text
logs/controller/controller-YYYYMMDD.log
```

Detailed Git/Terraform/worker diagnostics are appended to:

```text
logs/operations/operations-YYYYMMDD.log
```

Machine-readable asynchronous status records are stored under:

```text
logs/operations/state/<operation-id>.json
```

A detached worker may temporarily buffer ordinary stdout/stderr under:

```text
logs/operations/work/<operation-id>.tmp
```

When the worker reaches a terminal state, that transcript is appended as one block to the daily operations log and the temporary file is removed. This keeps concurrent worker output from becoming unreadably interleaved.

The daily files are append-only and use the UTC date in the filename. Detailed Git/Terraform output is written here rather than to the customer or administrator terminal.

The JSON operation files are controller state, not logs. They are needed for operation status, interrupted-worker detection, test-harness polling, and safe recovery decisions.

The `logs/` tree is ignored by Git. Ordinary `git clean -fd` leaves ignored files in place. Deleting/recloning the repository or running a command that explicitly removes ignored files, such as `git clean -fdx`, removes local runtime history and operation state.

Controller code must not intentionally write Vault tokens or managed plaintext passwords to the logs or state files.

---

## 6. Reconcile

```bash
python3 privateWorkerReplacementAdmin.py Reconcile
```

`Reconcile` is the normal administrator convergence/repair command. It:

1. reloads the managed desired state from Vault;
2. refreshes the Terraform source;
3. reapplies the complete controller-managed desired state through Terraform;
4. waits for deployments and managed accounts to converge;
5. reports the resulting service state.

`Reconcile` does not invent desired state. It re-applies state already recorded by the controller.

For ShardedClusters, Reconcile refuses to run while a protected managed change is active so a broad Terraform apply cannot race with shard, database, or credential work.

Routine Git/Terraform output is captured in the daily operations log instead of being printed to the administrator terminal.

---

## 7. RecoverDeploymentLock

```bash
python3 privateWorkerReplacementAdmin.py RecoverDeploymentLock SC9 --confirm
```

This is exceptional recovery, not a normal completion command.

Use it only when an interrupted `AddShard` or `DeleteShard` has already reached its recorded target topology but a later bookkeeping/storage step failed and left the ShardedCluster deployment lock in place.

Before releasing the lock, the controller verifies:

```text
Active lock category          = topology
Active lock action            = AddShard or DeleteShard
Vault desired shard count     = lock target
Live MongoDB spec.shardCount  = lock target
MongoDB phase                 = Running
Every surviving shard         = Online
Config servers                = Online
mongos                        = Online
Removed StatefulSets          = Absent for DeleteShard
```

Only after those checks pass does Terraform release the exact existing lock. Do not manually delete the lock merely to clear an error condition.

---

## 8. RecoverOrphanedResources

```bash
python3 privateWorkerReplacementAdmin.py RecoverOrphanedResources --confirm
```

This is the most restrictive recovery command. It exists for a partial deployment destroy where desired-state inventory is already empty but Terraform still tracks controller-managed resources that need cleanup.

Before mutation is allowed, the controller independently requires:

```text
Vault-backed managed deployment inventory = empty
Live privateWorkerReplacement-managed MongoDB CRs = none
```

If either check fails, recovery stops.

When both checks pass, Terraform receives empty desired state and finishes converging tracked resources toward zero. Existing storage safeguards remain active: ownership checks, live-pod/PVC-use checks, bounded waits, and refusal instead of unsafe force deletion.

`RecoverOrphanedResources` is asynchronous. A successful submission prints the internal operation ID because this is an administrator workflow:

```text
RecoverOrphanedResources request accepted.

Operation ID:   ab0c98165276
Scope:          controller-state
Status:         In Progress

Check administrator operation status with:
  python3 privateWorkerReplacementAdmin.py --config ./dev.config ListOperation ab0c98165276
```

The detached recovery worker resolves the configuration file to an absolute path internally. Routine administrator instructions continue to show the friendlier `./dev.config` path.

---

## 9. Recovery decision guide

Verify managed state:

```bash
python3 privateWorkerReplacement.py ListDeployments
python3 privateWorkerReplacementAdmin.py ListManagedResources
```

If normal managed desired state exists and should simply be reapplied:

```bash
python3 privateWorkerReplacementAdmin.py Reconcile
```

If an AddShard/DeleteShard topology lock remains but the target topology is already healthy, first inspect:

```bash
python3 privateWorkerReplacement.py ListShards SC9
python3 privateWorkerReplacementAdmin.py ListOperations
```

Then, only after understanding the state:

```bash
python3 privateWorkerReplacementAdmin.py RecoverDeploymentLock SC9 --confirm
```

If Vault inventory is empty, managed MongoDB CRs are gone, but Terraform-managed leftovers remain:

```bash
python3 privateWorkerReplacementAdmin.py RecoverOrphanedResources --confirm
```

If a live managed MongoDB deployment still exists, do not use orphan recovery. Investigate the desired state and use the normal lifecycle/Reconcile path.

---

## 10. Public vs administrator interface

| Concern | Public CLI | Administrator CLI |
| --- | --- | --- |
| Executable | `privateWorkerReplacement.py` | `privateWorkerReplacementAdmin.py` |
| Audience | DBaaS consumer | Platform operator |
| No-argument behavior | Full public help | Full administrator help |
| Deployment lifecycle | Yes | No |
| Shard lifecycle | Yes | No |
| Database lifecycle | Yes | No |
| Credential lifecycle | Yes | No |
| Normal service status | Yes | No |
| Managed-resource inventory | No | Yes |
| Operation IDs/journal | No | Yes |
| Worker PID/log path | No | Yes |
| Terraform reconciliation | No | Yes |
| Guarded recovery | No | Yes |

The public CLI should read like a service product. The administrator CLI should read like an operations runbook.

---

## 11. Architecture and source layout

Customer entry point:

```text
privateWorkerReplacement.py
privateWorkerReplacement/cli.py
```

Administrator entry point:

```text
privateWorkerReplacementAdmin.py
privateWorkerReplacement/admin_cli.py
privateWorkerReplacement/admin_status.py
```

Shared lifecycle/support modules include:

```text
privateWorkerReplacement/deployments.py
privateWorkerReplacement/databases.py
privateWorkerReplacement/database_status.py
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

Managed infrastructure mutations remain Terraform-driven. The Python controller validates, coordinates, waits, reports, and logs; it does not bypass Terraform to directly mutate managed MongoDB/Vault/Kubernetes lifecycle state.

---

## 12. Production hardening considerations

This proof of concept establishes a clear interface and recovery model. A production implementation should additionally define:

- who may execute `privateWorkerReplacementAdmin.py`;
- durable centralized log retention/forwarding;
- Terraform backend access controls;
- Kubernetes RBAC appropriate to customer vs administrator workflows;
- Vault policies appropriate to credential consumers vs administrators;
- operational approval/runbook requirements for destructive recovery;
- backup/retention policy for operation state if local worker state remains part of the production design.

The local `logs/` convention makes development/support evidence easy to find; production can later map the same controller/operations distinction onto durable worker storage or centralized logging without changing the customer CLI contract.
