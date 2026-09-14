# Maintainer Guide

This guide explains **where behavior lives, who owns each resource, why the
boundaries exist, and how to review a change without reverse-engineering the
whole repository**.

Use it with the audience-specific manuals:

- `README-privateWorkerReplacement.md` — customer/operator service commands
- `README-privateWorkerReplacementAdmin.md` — platform diagnostics/recovery
- `tests/README.md` — unit/regression and live acceptance testing
- `terraform-dbaas/README.md` — Terraform root-module ownership
- `docs/CLI-INTERFACES.md` — customer/admin interface separation

## 1. Architecture in one page

Normal managed work follows this ownership chain:

```text
Customer/Admin CLI
        |
        v
Python validation/orchestration
        |
        v
Terraform desired state
        |
        +--> Kubernetes MongoDB/MongoDBUser resources
        |       |
        |       v
        |   MongoDB Kubernetes Operator / Ops Manager
        |
        +--> Vault metadata + credentials
        |
        +--> Helm database-management release
        |
        +--> lifecycle.sh for narrow storage/lock/auth checks
```

The important rule is: **Python coordinates; Terraform owns normal desired-state
mutation.** Python does not directly edit managed MongoDB CRs, MongoDBUsers,
database-account Secrets, or normal Vault lifecycle records.

There are narrow documented exceptions for cleanup of artifacts created by
systems outside Terraform state, such as MongoDB Operator auth Secrets, Helm hook
Jobs/Pods, and Ops Manager project/group artifacts.

## 2. Source-of-truth and ownership table

| Concern | Authoritative/owning layer | Why |
| --- | --- | --- |
| Deployment/database desired inventory | Vault metadata | Reconstructs complete Terraform input after process restart |
| Managed infrastructure changes | Terraform | One declarative mutation boundary |
| Terraform state | Kubernetes backend | Tracks Terraform resource ownership |
| MongoDB runtime convergence | MongoDB Operator / Ops Manager | Native MongoDB deployment controller |
| Database passwords | Vault + Kubernetes write-only sinks | Credential retrieval plus runtime Secret without plaintext Terraform state |
| Customer command parsing/status | Python | Validation, orchestration, readable service interface |
| Admin inventory/recovery | Python + Terraform | Cross-plane diagnosis plus guarded desired-state repair |
| Logical DB materialization/delete | Terraform -> Helm chart | Keeps DB operations under Terraform transaction ownership |
| Static-local PV preparation/cleanup | Terraform -> lifecycle.sh | Imperative host/storage work remains Terraform-triggered |
| Per-SC topology lock | Terraform/lifecycle.sh ConfigMap | Prevents conflicting mutations on one ShardedCluster |
| Controller-wide mutation lock | Python `flock` | Prevents stale complete-inventory applies on one worker host |

## 3. Python code map

The executable files are intentionally tiny:

```text
privateWorkerReplacement.py
    -> privateWorkerReplacement/cli.py

privateWorkerReplacementAdmin.py
    -> privateWorkerReplacement/admin_cli.py
```

The implementation is split by responsibility:

| Module | Owns | Does not own |
| --- | --- | --- |
| `cli.py` | Customer parser/help, async submission, dispatch | MongoDB/Terraform lifecycle rules |
| `admin_cli.py` | Admin parser/help, async submission, dispatch | Inventory classification/recovery internals |
| `controller.py` | Stable import facade | Business logic |
| `deployments.py` | ReplicaSet/ShardedCluster create/delete and shared readiness rules | Shard scale operations, status formatting |
| `shards.py` | AddShard/DeleteShard resume-safe topology orchestration | Direct CR/PV mutation |
| `databases.py` | DB create/delete, rotate, DisableOwner/EnableOwner | Read-only presentation |
| `deployment_status.py` | Deployment/shard read-only presentation | Mutations |
| `database_status.py` | DB/account read-only presentation | Mutations |
| `credential_display.py` | Vault paths/browser URLs | Credential writes |
| `maintenance.py` | Cross-plane inventory collection, Reconcile, guarded recovery | Terminal formatting |
| `admin_status.py` | Admin inventory formatting/status wording | Resource collection/mutation |
| `vault.py` | Read Vault inventory/credentials | Normal lifecycle writes |
| `terraform_runner.py` | Build Terraform input/cache and execute Terraform | Direct MongoDB/Kubernetes mutation |
| `deployment_lock.py` | One-ShardedCluster mutation lock orchestration | Controller-wide serialization |
| `mutation_lock.py` | Whole-controller desired-state serialization | Per-deployment topology lock |
| `async_operations.py` | Detached worker journal/submission/status | DBaaS mutation itself |
| `kube.py` | Read Kubernetes and wait for convergence | Managed resource creation/update |
| `ops_manager.py` | Read/delete DBaaS Ops Manager project artifacts | General MongoDB lifecycle |
| `operator_artifacts.py` | Narrow cleanup of Operator/Helm leftovers | Terraform-owned resource cleanup |
| `logging_component.py` | Structured controller/operation evidence | Operation semantics |
| `runtime_paths.py` | Predictable runtime path locations | Business logic |
| `config.py` | Parse/validate dev.config and expose normalized settings | Runtime mutation logic |
| `common.py` | Shared normalization/time/process/table helpers | Domain lifecycle logic |

## 4. Public command flow

A long-running customer command such as `AddShardedCluster` follows:

```text
cli.py parses command
  -> validates immediate request shape/safety
  -> records async operation
  -> launches detached privateWorkerReplacement.py worker
  -> foreground returns customer status command

worker
  -> loads config/Vault
  -> takes controller-wide mutation lock
  -> deployments.py or shards.py validates current state
  -> terraform_runner.py applies complete desired inventory
  -> kube.py waits for convergence
  -> operation journal becomes Succeeded/Failed
```

The customer CLI intentionally hides operation IDs and raw Terraform output.
Those are implementation/operator details, not service-level output.

## 5. Administrator flow

Read-only admin commands stay in the foreground:

```text
ListManagedResources
ListOperations
ListOperation
```

Mutating admin commands are detached operations:

```text
Reconcile
RecoverDeploymentLock
RecoverOrphanedResources
```

`ListManagedResources` compares **Vault + Kubernetes + Ops Manager + Terraform
backend/platform objects**. It intentionally does not trust one plane by itself.

Status meanings:

- `CLEAN` — no active managed customer resources and no detected drift/orphans.
- `MANAGED RESOURCES PRESENT` — legitimate managed deployments exist.
- `ATTENTION REQUIRED` — missing/orphan/mismatched cross-plane state exists.

Permanent platform objects may exist while status is `CLEAN`.

## 6. ShardedCluster status meanings

The overall `Phase` shown by PWR comes directly from the MongoDB custom
resource, normally `Pending`, `Running`, or `Failed`.

PWR separately summarizes each shard/config-server/mongos StatefulSet:

- `Creating` — zero desired replicas are Ready yet, or the workload is not visible while the deployment is still creating.
- `Degraded` — at least one replica is Ready, but not all desired replicas are both Ready and Updated.
- `Online` — all desired replicas are Ready and Updated.
- `Failed` — the overall MongoDB deployment has entered Failed.
- `Absent/Removed/Removing` — used where topology deletion/status context requires it.

A ShardedCluster can therefore show all shard ReplicaSets healthy while the
overall MongoDB CR remains `Pending` briefly because config servers, mongos, or
Ops Manager/Operator reconciliation has not finished.

## 7. Locking: two different problems, two different locks

### Controller-wide mutation lock

Every mutation applies a complete Vault-backed Terraform inventory. Two workers
could otherwise read different snapshots and the older worker could later
reapply stale state. `mutation_lock.py` holds a host-local `flock` across the
whole read/modify/apply/wait operation.

### ShardedCluster deployment lock

A ShardedCluster can still report `Running` while AddShard/DeleteShard work is
in progress. `deployment_lock.py` therefore uses a Terraform-created Kubernetes
ConfigMap to prevent conflicting mutations on that one cluster.

Read-only commands do not take either lock.

## 8. Database/account invariants

Every managed database has exactly three identities:

```text
<DB>_owner      -> dbOwner
<DB>_readWrite  -> readWrite
<DB>_read       -> read
```

Owner disablement removes the Owner MongoDBUser but keeps its managed credential
and password Secret. Rotation always rotates all three credentials. Re-enabling
Owner recreates the MongoDBUser using the existing current credential; it does
not rotate solely because of enablement.

System databases `admin`, `config`, and `local` are not customer-managed
databases and do not block deployment deletion.

## 9. Testing: two layers

### `tests/test_*.py`

Fast unit/regression/contract tests. They validate code behavior, documentation
contracts, safety rules, Terraform structure, CLI help, and repository hygiene
without building a live MongoDB environment.

GitHub Actions runs these plus syntax/Terraform validation on PRs and pushes to
`main`.

### `tests/harness/`

Live end-to-end acceptance scenarios against the configured k3d/Kubernetes,
Vault, Ops Manager, Terraform, MongoDB Operator, and MongoDB environment.

Canonical numbering:

```text
1-5     Preflight
6-16    ReplicaSet
17-32   ShardedCluster
33-38   Locking/concurrency
39-100  Administrator/recovery
```

Every canonical live test has an adjacent `TEST / WHY / PASS` comment so its
purpose and success evidence can be defended directly from the source.

## 10. Terraform code map

`terraform-dbaas/` is one root module. Terraform loads all `*.tf` files
together; filenames are organizational only.

See `terraform-dbaas/README.md` for the detailed map. The key rule is that
moving a block between files does not change its Terraform address. Renaming a
resource or changing a `for_each` key is different and must be treated as a
state migration.

## 11. Runtime files that are intentionally not source

These are generated/local-only and are ignored by Git:

```text
__pycache__/
*.pyc
.runtime/
logs/
terraform-dbaas/.terraform/
Terraform state/plan/local tfvars files
local .env files
editor/OS temporary files
```

`tests/test_repository_hygiene.py` also fails if common generated/runtime
artifacts are force-added to Git.

## 12. Review checklist

Before approving a change, a reviewer should be able to answer:

1. **Which module owns this behavior?** Use the code map above.
2. **Is a managed mutation still Terraform-owned?** If not, the exception must be explicit and justified.
3. **Does Vault desired state remain reconstructable?**
4. **Could two mutations overlap?** Confirm the correct controller/deployment lock boundary.
5. **Does customer output stay service-oriented?** Raw Terraform/internal IDs belong in admin/logs.
6. **Does administrator recovery remain guarded and diagnosable?**
7. **Were help text and the correct README updated if the interface changed?**
8. **Were unit/contract tests updated?**
9. **Does the relevant live harness profile still cover the behavior?**
10. **Did the change create a Terraform address/state migration?** If yes, handle it explicitly.

## 13. Common reviewer questions

**Why two CLIs?**  
Customer service operations and privileged diagnostics/recovery have different
audiences and output contracts. The executable split reduces accidental exposure
of operator internals, but host/Kubernetes/Vault permissions remain the actual
security boundary.

**Why Vault plus Terraform state?**  
They answer different questions. Vault stores recoverable desired metadata and
credentials. Terraform state tracks resource ownership/provider state.

**Why not let Python directly create MongoDB users/resources?**  
That would create a second independent mutation owner. Normal desired-state
changes stay behind Terraform so reconciliation and recovery have one path.

**Why are some cleanup actions direct?**  
Only because those artifacts were created outside Terraform state by the MongoDB
Operator, Helm hooks, or Ops Manager. Cleanup is narrow, named, guarded, and
performed after the owning deployment is gone.

**Why asynchronous operations?**  
MongoDB/Operator/Terraform convergence can take minutes. Detached workers keep
the CLI responsive while preserving an administrator-visible operation journal.

**Why 100 live checks if unit tests exist?**  
Unit tests prove code decisions. The live harness proves the integrated stack
actually behaves that way across Terraform, Kubernetes, MongoDB Operator, Ops
Manager, Vault, Helm, and MongoDB.

## 14. Documentation ownership

When behavior changes, update the document for the audience that experiences it:

| Change | Documentation |
| --- | --- |
| Customer command/syntax/status | `README-privateWorkerReplacement.md` + CLI help |
| Administrator command/recovery | `README-privateWorkerReplacementAdmin.md` + Admin help |
| Cross-interface architecture | `README.md`, `docs/CLI-INTERFACES.md`, this guide |
| Live/unit testing | `tests/README.md` |
| Terraform structure/ownership | `terraform-dbaas/README.md` |
| Vault admin policy | `docs/vault-dbaas-admin-policy.hcl` where policy scope changes |

The regression suite checks command/help/documentation contracts so missing
updates fail before merge.
