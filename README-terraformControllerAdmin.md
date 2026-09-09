# terraformController Administrator Guide

`terraformControllerAdmin.py` is the **platform-administrator interface** for the MongoDB DBaaS controller.

It is intentionally separate from the customer-facing `terraformController.py` command.

The customer interface is for normal DBaaS work: deployment lifecycle, shard lifecycle, database lifecycle, credential lifecycle, and normal service status.

The administrator interface is for **operations, diagnostics, reconciliation, and exceptional recovery**.

> **Important:** separating the executables creates a clean product/interface boundary. It is not, by itself, an authorization boundary. Production deployment must still restrict administrator execution, Kubernetes access, Vault access, host access, and Terraform backend access to authorized operators.

---

## Quick start

Show administrator help:

~~~bash
python3 terraformControllerAdmin.py --help
~~~

Show command-specific help:

~~~bash
python3 terraformControllerAdmin.py ListOperations --help
python3 terraformControllerAdmin.py ListOperation --help
python3 terraformControllerAdmin.py Reconcile --help
python3 terraformControllerAdmin.py RecoverDeploymentLock --help
python3 terraformControllerAdmin.py RecoverOrphanedResources --help
~~~

The administrator interface uses the same controller configuration file and Vault token as the public controller:

~~~bash
export VAULT_TOKEN='<current-vault-token>'
~~~

Do not store the Vault token in `terraformController.config`.

---

## Command reference

| Command | Purpose | Mutation |
| --- | --- | --- |
| `ListOperations` | List recent asynchronous controller operations | Read-only |
| `ListOperation OPERATION_ID` | Show detailed status for one operation | Read-only |
| `Reconcile` | Reapply the complete Vault-backed desired state through Terraform | Yes |
| `RecoverDeploymentLock SC --confirm` | Release a completed but stranded ShardedCluster topology lock after safety validation | Yes |
| `RecoverOrphanedResources --confirm` | Finish Terraform cleanup after desired-state inventory is already empty | Yes |

These commands are deliberately **not accepted** by `terraformController.py`.

---

## ListOperations

~~~bash
python3 terraformControllerAdmin.py ListOperations
~~~

This is the operator view of the controller's local asynchronous operation journal.

Possible operation results:

~~~text
In Progress
Succeeded
Failed
Interrupted
~~~

The operation journal is diagnostic controller state. It is not part of the customer-facing DBaaS contract.

---

## ListOperation

~~~bash
python3 terraformControllerAdmin.py ListOperation 7c1349abc123
~~~

This command intentionally exposes deeper diagnostic detail than the customer CLI:

~~~text
Operation ID
Command
Deployment / scope
Result
Submitted timestamp
Started timestamp
Completed timestamp
Elapsed time
Worker PID
Operation log path
Recorded result/error message
~~~

Use it when a background create/delete/topology request appears stalled, an acceptance-test step fails, a detached worker was interrupted, or the exact operation log is needed.

Do not build customer procedures around Operation IDs. They are internal service diagnostics.

---

## Reconcile

~~~bash
python3 terraformControllerAdmin.py Reconcile
~~~

`Reconcile` is the normal administrator repair/convergence command.

It:

1. reloads managed desired state from Vault;
2. refreshes the configured Terraform source;
3. asks Terraform to reapply the complete managed state;
4. waits for managed MongoDB resources and accounts to converge;
5. reports the resulting service state.

`Reconcile` does **not invent desired state**. It reapplies state already recorded by the controller.

For ShardedClusters, Reconcile refuses to run while a protected managed change is active. This prevents a broad environment-wide apply from racing with shard, database, or credential mutations.

---

## RecoverDeploymentLock

~~~bash
python3 terraformControllerAdmin.py RecoverDeploymentLock SC9 --confirm
~~~

This is **exceptional recovery**, not a normal completion command.

Use it only when an interrupted `AddShard` or `DeleteShard` has already reached its recorded target topology but a later controller bookkeeping/storage step failed and left the ShardedCluster deployment lock in place.

Before releasing the lock, the controller verifies:

~~~text
Active lock category          = topology
Active lock action            = AddShard or DeleteShard
Vault desired shard count     = lock target
Live MongoDB spec.shardCount  = lock target
MongoDB phase                 = Running
Every surviving shard         = Online
Config servers                = Online
mongos                        = Online
Removed StatefulSets          = Absent for DeleteShard
~~~

Only after those checks pass does Terraform release the exact existing lock.

This gives the administrator a guarded Terraform-driven recovery path instead of manually deleting a lock or bypassing controller safety.

---

## RecoverOrphanedResources

~~~bash
python3 terraformControllerAdmin.py RecoverOrphanedResources --confirm
~~~

This is the most restrictive recovery command.

It exists for a partial deployment destroy where:

- the deployment has already been removed from Vault-backed desired-state inventory;
- the MongoDB custom resource is already gone;
- Terraform still tracks PVCs, PVs, secrets, or other resources that were not fully destroyed.

Before Terraform is allowed to mutate anything, the command independently requires:

~~~text
Vault-backed managed deployment inventory = empty
Live MongoDB CRs managed by terraformController = none
~~~

If either check fails, recovery stops.

When both checks pass, the controller sends **empty desired state** to Terraform so Terraform can finish converging its backend state to zero managed resources.

Storage cleanup still uses the normal safeguards: actual PV/PVC ownership checks, legacy-binding detection, live-pod/PVC-use checks, bounded waits for terminating pods, bounded PVC/PV deletion waits, and refusal instead of force-deleting unsafe storage.

### Asynchronous administrator operation

`RecoverOrphanedResources` can contain bounded storage waits, so it remains asynchronous.

Example:

~~~text
RecoverOrphanedResources request accepted.

Operation ID:   ab0c98165276
Scope:          controller-state
Status:         In Progress

Check administrator operation status with:
  python3 terraformControllerAdmin.py ... ListOperation ab0c98165276
~~~

Operation IDs are appropriate here because this is an administrator diagnostic workflow.

---

## Recovery decision guide

### Normal service is healthy

Use the public controller:

~~~bash
python3 terraformController.py ...
~~~

### Managed state exists and should simply be reapplied

Use:

~~~bash
python3 terraformControllerAdmin.py Reconcile
~~~

### A topology lock remains, but the target topology is already healthy

Inspect public service state and administrator operation history:

~~~bash
python3 terraformController.py ListShards SC9
python3 terraformControllerAdmin.py ListOperations
~~~

Then, only when the preconditions are understood:

~~~bash
python3 terraformControllerAdmin.py RecoverDeploymentLock SC9 --confirm
~~~

### Vault inventory is empty, MongoDB CRs are gone, but Terraform-managed resources remain

Use:

~~~bash
python3 terraformControllerAdmin.py RecoverOrphanedResources --confirm
~~~

### A live MongoDB deployment still exists

Do **not** use `RecoverOrphanedResources`. Investigate the existing desired state and use the normal lifecycle/Reconcile path.

---

## Public vs administrator interface

| Concern | Public CLI | Administrator CLI |
| --- | --- | --- |
| Executable | `terraformController.py` | `terraformControllerAdmin.py` |
| Audience | DBaaS consumer / demo user | Platform operator |
| Database lifecycle | Yes | No |
| Deployment lifecycle | Yes | No |
| Shard lifecycle | Yes | No |
| Normal service status | Yes | No |
| Operation journal | No | Yes |
| Worker PID/log path | No | Yes |
| Terraform reconciliation | No | Yes |
| Recovery commands | No | Yes |

The public CLI should read like a service product. The administrator CLI should read like an operations runbook.

---

## Source layout

Administrator-facing files:

~~~text
terraformControllerAdmin.py
terraform_controller/admin_cli.py
terraform_controller/maintenance.py
terraform_controller/async_operations.py
terraform_controller/deployment_lock.py
terraform_controller/terraform_runner.py
~~~

Customer-facing entry point:

~~~text
terraformController.py
terraform_controller/cli.py
~~~

Shared lifecycle/business modules remain shared so the two interfaces do not duplicate controller logic.

---

## Logging and evidence

Both interfaces use the shared structured logging component:

~~~text
terraform_controller/logging_component.py
~~~

The administrator operation journal is stored outside the Git working tree under the configured user's local state directory (`XDG_STATE_HOME` when set, otherwise the normal local-state location).

This prevents normal Git reset/clean/branch operations from erasing evidence needed to diagnose interrupted background work.

Controller code should never intentionally write Vault tokens or plaintext managed passwords into these logs or journals.

---

## Production hardening note

The POC separates the customer and administrator **interfaces**.

For production, enforce the same distinction operationally:

- restrict who can execute `terraformControllerAdmin.py`;
- restrict Terraform backend access;
- restrict Kubernetes mutation permissions;
- restrict Vault administrator policy;
- retain auditable administrator logs;
- define an approval/runbook for recovery commands.

A separate executable makes the intended boundary obvious to users and reviewers, while RBAC and deployment controls make the boundary enforceable.
