# terraformController Administrator Guide

`terraformControllerAdmin.py` is the platform-administrator interface for the MongoDB DBaaS controller. It is intentionally separate from the customer-facing `terraformController.py` command.

Use the customer CLI for normal deployment, shard, database, credential, and service-status work. Use the administrator CLI for operation diagnostics, reconciliation, zero-state verification, and exceptional recovery.

The executable split is an interface boundary, not an authorization boundary. Production must also restrict administrator host access, Kubernetes privileges, Vault policy, and Terraform backend access.

---

## 1. Quick start

Show administrator help:

```bash
python3 terraformControllerAdmin.py --help
```

Show command-specific help:

```bash
python3 terraformControllerAdmin.py ListManagedResources --help
python3 terraformControllerAdmin.py ListOperations --help
python3 terraformControllerAdmin.py ListOperation --help
python3 terraformControllerAdmin.py Reconcile --help
python3 terraformControllerAdmin.py RecoverDeploymentLock --help
python3 terraformControllerAdmin.py RecoverOrphanedResources --help
```

The administrator interface uses the same `terraformController.config` and Vault token as the public controller. Do not store the Vault token in the config file.

---

## 2. Command reference

| Command | Purpose | Mutation |
| --- | --- | --- |
| `ListManagedResources` | List controller-managed resources and report zero-state cleanliness | Read-only |
| `ListOperations` | List recent asynchronous controller operations | Read-only |
| `ListOperation OPERATION_ID` | Show detailed status for one asynchronous operation | Read-only |
| `Reconcile` | Reapply complete Vault-backed desired state through Terraform | Yes |
| `RecoverDeploymentLock SC --confirm` | Release a completed but stranded ShardedCluster topology lock after safety validation | Yes |
| `RecoverOrphanedResources --confirm` | Finish Terraform cleanup after desired-state inventory is already empty | Yes |

These commands are deliberately not accepted by `terraformController.py`.

---

## 3. ListManagedResources

```bash
python3 terraformControllerAdmin.py ListManagedResources
```

This is the read-only zero-state and leftover-resource check. It combines Vault-backed deployment inventory with controller-managed Kubernetes resources:

```text
Managed deployments
MongoDB resources
MongoDB users
PVCs
PVs
Deployment locks
```

A clean environment reports:

```text
terraformController Managed Resource Inventory

Managed deployments:  0
MongoDB resources:    0
MongoDB users:        0
PVCs:                 0
PVs:                  0
Deployment locks:     0

Status: CLEAN
```

If anything remains, the command reports `ATTENTION REQUIRED` and lists the remaining names by category. The command does not delete or reconcile anything.

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
python3 terraformControllerAdmin.py ListOperations
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
python3 terraformControllerAdmin.py ListOperation 7c1349abc123
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
python3 terraformControllerAdmin.py Reconcile
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
python3 terraformControllerAdmin.py RecoverDeploymentLock SC9 --confirm
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
python3 terraformControllerAdmin.py RecoverOrphanedResources --confirm
```

This is the most restrictive recovery command. It exists for a partial deployment destroy where desired-state inventory is already empty but Terraform still tracks controller-managed resources that need cleanup.

Before mutation is allowed, the controller independently requires:

```text
Vault-backed managed deployment inventory = empty
Live terraformController-managed MongoDB CRs = none
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
  python3 terraformControllerAdmin.py --config /path/to/terraformController.config ListOperation ab0c98165276
```

---

## 9. Recovery decision guide

Verify zero-state:

```bash
python3 terraformController.py ListDeployments
python3 terraformControllerAdmin.py ListManagedResources
```

If normal managed desired state exists and should simply be reapplied:

```bash
python3 terraformControllerAdmin.py Reconcile
```

If an AddShard/DeleteShard topology lock remains but the target topology is already healthy, first inspect:

```bash
python3 terraformController.py ListShards SC9
python3 terraformControllerAdmin.py ListOperations
```

Then, only after understanding the state:

```bash
python3 terraformControllerAdmin.py RecoverDeploymentLock SC9 --confirm
```

If Vault inventory is empty, managed MongoDB CRs are gone, but Terraform-managed leftovers remain:

```bash
python3 terraformControllerAdmin.py RecoverOrphanedResources --confirm
```

If a live managed MongoDB deployment still exists, do not use orphan recovery. Investigate the desired state and use the normal lifecycle/Reconcile path.

---

## 10. Public vs administrator interface

| Concern | Public CLI | Administrator CLI |
| --- | --- | --- |
| Executable | `terraformController.py` | `terraformControllerAdmin.py` |
| Audience | DBaaS consumer | Platform operator |
| Deployment lifecycle | Yes | No |
| Shard lifecycle | Yes | No |
| Database lifecycle | Yes | No |
| Credential lifecycle | Yes | No |
| Normal service status | Yes | No |
| Managed-resource zero-state inventory | No | Yes |
| Operation IDs/journal | No | Yes |
| Worker PID/log path | No | Yes |
| Terraform reconciliation | No | Yes |
| Guarded recovery | No | Yes |

The public CLI should read like a service product. The administrator CLI should read like an operations runbook.

---

## 11. Architecture and source layout

Customer entry point:

```text
terraformController.py
terraform_controller/cli.py
```

Administrator entry point:

```text
terraformControllerAdmin.py
terraform_controller/admin_cli.py
```

Shared lifecycle/support modules include:

```text
terraform_controller/deployments.py
terraform_controller/databases.py
terraform_controller/maintenance.py
terraform_controller/deployment_lock.py
terraform_controller/terraform_runner.py
terraform_controller/async_operations.py
terraform_controller/logging_component.py
terraform_controller/runtime_paths.py
terraform_controller/kube.py
terraform_controller/vault.py
```

Managed infrastructure mutations remain Terraform-driven. The Python controller validates, coordinates, waits, reports, and logs; it does not bypass Terraform to directly mutate managed MongoDB/Vault/Kubernetes lifecycle state.

---

## 12. Production hardening considerations

This proof of concept establishes a clear interface and recovery model. A production implementation should additionally define:

- who may execute `terraformControllerAdmin.py`;
- durable centralized log retention/forwarding;
- Terraform backend access controls;
- Kubernetes RBAC appropriate to customer vs administrator workflows;
- Vault policies appropriate to credential consumers vs administrators;
- operational approval/runbook requirements for destructive recovery;
- backup/retention policy for operation state if local worker state remains part of the production design.

The local `logs/` convention makes development/support evidence easy to find; production can later map the same controller/operations distinction onto durable worker storage or centralized logging without changing the customer CLI contract.
