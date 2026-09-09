# terraformController

`terraformController.py` is the temporary local orchestration interface for the MongoDB DBaaS proof of concept.

The durable implementation is Terraform. The long-term execution path is Spacelift plus a private worker running the same Terraform lifecycle.

## Design rule

**Terraform performs all managed changes.**

Python performs orchestration only:

- parses and validates CLI input,
- reads Vault desired-state metadata,
- reads Kubernetes and MongoDB status,
- prevents unsafe operations before Terraform runs,
- invokes Terraform,
- waits for MongoDB Operator reconciliation,
- verifies expected state,
- formats user-facing results,
- writes structured operational logs.

Python does not directly create, delete, rotate, enable, disable, add shards, delete shards, or create/remove deployment locks.

Terraform performs managed mutations directly or through:

```text
terraform-dbaas/scripts/lifecycle.sh
```

## Fresh installation

A fresh installation starts with:

```text
ReplicaSets:      0
ShardedClusters:  0
Databases:        0
```

The nix-k3d/platform setup does **not** create a default RS1 or SC1.

The user explicitly creates and names a deployment:

```bash
python3 terraformController.py AddReplicaSet RS1
python3 terraformController.py AddShardedCluster SC9 --shards 3
```

## Managed hierarchy

```text
Deployment
  ReplicaSet
    Database
      <Database>_owner
      <Database>_readWrite
      <Database>_read

  ShardedCluster
    Shards
    Config Server ReplicaSet
    mongos routers
    Database
      <Database>_owner
      <Database>_readWrite
      <Database>_read
```

A ShardedCluster is a deployment. It is not treated as a ReplicaSet in terminology, even though each shard is implemented by MongoDB as a ReplicaSet.

## Command reference

### Deployment inventory

```text
ListDeployments
ListDeployment DEPLOYMENT

ListReplicaSets
ListReplicaSet REPLICASET

ListShardedClusters
ListShardedCluster SHARDED_CLUSTER

ListShards
ListShards SHARDED_CLUSTER

ListOperations
ListOperation OPERATION_ID

RecoverDeploymentLock SHARDED_CLUSTER --confirm
```

### ReplicaSet lifecycle

```text
AddReplicaSet REPLICASET
DeleteReplicaSet REPLICASET --confirm
```

### ShardedCluster lifecycle

```text
AddShardedCluster SHARDED_CLUSTER [--shards N]
DeleteShardedCluster SHARDED_CLUSTER --confirm

AddShard SHARDED_CLUSTER [COUNT]
DeleteShard SHARDED_CLUSTER [COUNT] --confirm
```

`COUNT` defaults to 1.

Examples:

```bash
python3 terraformController.py AddShard SC9
python3 terraformController.py AddShard SC9 2
python3 terraformController.py DeleteShard SC9 --confirm
python3 terraformController.py DeleteShard SC9 2 --confirm
```

### Database lifecycle

```text
AddDatabase DEPLOYMENT DATABASE
AddDatabase DATABASE

DeleteDatabase DEPLOYMENT DATABASE --confirm
DeleteDatabase DATABASE --confirm

ListDatabases [DEPLOYMENT]

ListDatabase DEPLOYMENT DATABASE
ListDatabase DATABASE
```

The one-argument database forms are valid only when exactly one managed MongoDB deployment exists.

### Password and Owner lifecycle

```text
RotatePasswords DEPLOYMENT DATABASE
RotatePasswords DATABASE

DisableOwner DEPLOYMENT DATABASE --confirm
DisableOwner DATABASE --confirm

Reconcile
```

Use command-level help:

```bash
python3 terraformController.py AddShardedCluster --help
python3 terraformController.py ListShards --help
python3 terraformController.py AddShard --help
python3 terraformController.py DeleteShard --help
python3 terraformController.py AddDatabase --help
python3 terraformController.py RotatePasswords --help
```


## Asynchronous long-running operations

The following deployment/topology commands are asynchronous from the user's shell:

```text
AddReplicaSet
DeleteReplicaSet
AddShardedCluster
DeleteShardedCluster
AddShard
DeleteShard
```

The public command validates its command-line shape, creates a persistent local Operation ID, starts a detached worker, prints exact status commands, and returns the shell prompt. The detached worker then executes the same existing synchronous lifecycle implementation, so managed changes remain Terraform-driven.

Example:

```bash
python3 terraformController.py DeleteShard SC9 1 --confirm
```

returns:

```text
DeleteShard request accepted.

Operation ID:   7c1349abc123
Deployment:     SC9
Status:         In Progress

Check positive/negative result with:
  python3 terraformController.py --config ... ListOperation 7c1349abc123

Check live shard progress with:
  python3 terraformController.py --config ... ListShards SC9
```

Read one operation:

```bash
python3 terraformController.py ListOperation 7c1349abc123
```

Read recent operations:

```bash
python3 terraformController.py ListOperations
```

Possible operation results:

```text
In Progress
Succeeded
Failed
Interrupted
```

`ListOperation`, `ListOperations`, and `ListShards` are read-only. They do not finish or mutate an operation.

Operation journals are stored outside the repository under `XDG_STATE_HOME` (or `~/.local/state`) in a config-specific terraformController state directory, so `git clean` does not erase operation history.

For this local POC, the detached worker survives the invoking shell but not a stopped WSL/host environment. A stopped/interrupted ShardedCluster operation remains protected by the existing deployment lock and matching-command resume behavior.

## Shard status

### All ShardedClusters

```bash
python3 terraformController.py ListShards
```

Example:

```text
CLUSTER  SHARD   STATUS  READY  DESIRED  UPDATED  ACTIVE CHANGE
-------  ------  ------  -----  -------  -------  ---------------
SC1      sc1-0   Online  3      3        3        -
SC1      sc1-1   Online  3      3        3        -
SC9      sc9-0   Online  3      3        3        AddShard 3 -> 5
SC9      sc9-1   Online  3      3        3        AddShard 3 -> 5
SC9      sc9-2   Online  3      3        3        AddShard 3 -> 5
SC9      sc9-3   Creating 0     0        0        AddShard 3 -> 5
SC9      sc9-4   Creating 0     0        0        AddShard 3 -> 5
```

### One ShardedCluster

```bash
python3 terraformController.py ListShards SC9
```

The targeted view shows:

- each expected/current shard,
- shard status,
- ready/desired/updated member counts,
- active managed change,
- config-server status,
- mongos status.

Shard status can include:

```text
Online
Creating
Degraded
Removing
Removed
Failed
Unknown
```

## Readiness rules

Before database creation, deletion, password rotation, or Owner disable:

### ReplicaSet

The target ReplicaSet must be exactly:

```text
Running
```

### ShardedCluster

All of these conditions must be true:

```text
MongoDB resource phase = Running
Every expected shard   = Online
Config servers         = Online
mongos                 = Online
No conflicting managed change is active
```

If any condition is not satisfied, the command fails before the requested database/credential change begins.

## ShardedCluster deployment lock

Each managed ShardedCluster uses one atomic Kubernetes ConfigMap lock:

```text
tc-deployment-lock-<deployment>
```

The lock is **created and released by Terraform through the lifecycle script**. Python reads it but does not directly create or delete it.

The lock serializes ShardedCluster mutations across separate controller processes/shells.

Protected operations include:

```text
AddDatabase
DeleteDatabase
RotatePasswords
DisableOwner
AddShard
DeleteShard
```

`Reconcile` refuses to run while any ShardedCluster managed change is active.

Read-only commands remain available, including:

```text
ListDeployments
ListDeployment
ListShardedClusters
ListShardedCluster
ListShards
ListDatabases
ListDatabase
```

This is important during shard creation/removal because Kubernetes can still report the existing cluster as `Running` while Terraform is preparing storage for a topology change.

## AddReplicaSet

```bash
python3 terraformController.py AddReplicaSet RS1
```

Defaults come from `terraformController.config`:

```text
Members:       3
MongoDB:       8.0.29
Persistent:    true
StorageClass:  mongodb-data-local
Storage:       16Gi/member
```

The public command returns an Operation ID after the detached worker starts. `ListOperation OPERATION_ID` reports `Succeeded` only after the MongoDB resource is Running and the hidden controller account is actually usable.

## AddShardedCluster

```bash
python3 terraformController.py AddShardedCluster SC9 --shards 3
```

Default topology:

```text
Shards:             3
Members per shard:  3
mongos:             2
Config servers:     3
MongoDB:            8.0.29
```

The shard count can be overridden with `--shards N`.

The public command returns an Operation ID promptly. The detached worker waits for the ShardedCluster and all expected components, and `ListOperation OPERATION_ID` reports the eventual positive/negative result.

## AddShard

Syntax:

```text
AddShard SHARDED_CLUSTER [COUNT]
```

Examples:

```bash
python3 terraformController.py AddShard SC9
python3 terraformController.py AddShard SC9 2
```

For `AddShard SC9 2`, if SC9 starts with 3 shards, the target is 5 shards.

The detached worker performs the existing staged lifecycle:

1. verify COUNT is at least 1,
2. verify SC9 exists and is fully ready,
3. atomically acquire the SC9 deployment lock through Terraform,
4. Terraform prepares persistent storage for the target shard count,
5. Terraform changes the MongoDB `shardCount` to the target,
6. wait until every target shard is online,
7. release the deployment lock through Terraform,
8. record `Succeeded` or `Failed` in the operation journal.

The submitting shell does not wait for steps 3-8. Use `ListOperation` for the final result and `ListShards` for live topology progress.

If the controller process is interrupted after the lock is acquired, rerun the **same** AddShard command. The controller recognizes the matching lock and resumes toward the stored target instead of adding the count again.

## DeleteShard

Syntax:

```text
DeleteShard SHARDED_CLUSTER [COUNT] --confirm
```

Examples:

```bash
python3 terraformController.py DeleteShard SC9 --confirm
python3 terraformController.py DeleteShard SC9 2 --confirm
```

Safety rules:

1. COUNT must be at least 1.
2. The ShardedCluster must retain at least **one shard**.
3. The cluster must initially be fully ready.
4. The SC deployment lock must be acquired before topology changes begin.
5. Application databases may remain on the ShardedCluster during shard removal.
6. Terraform lowers the managed MongoDB ShardedCluster `spec.shardCount`; Python does not directly remove shards.
7. The MongoDB Kubernetes Operator/Ops Manager reconciles the supported ShardedCluster scale-down.
8. The detached worker waits for the remaining cluster to become fully ready and for removed shard StatefulSets to disappear before Terraform cleans the old shard storage. The submitting shell has already returned with an Operation ID.

For example, if SC9 has 3 shards:

```text
DeleteShard SC9 1 --confirm  -> allowed target: 2
DeleteShard SC9 2 --confirm  -> allowed target: 1
DeleteShard SC9 3 --confirm  -> BLOCKED
```

The highest-numbered shards are removed first.

For `DeleteShard SC9 2 --confirm` from five shards, Terraform changes the desired shard count from 5 to 3. The MongoDB Kubernetes Operator/Ops Manager then reconciles the supported ShardedCluster scale-down. The controller waits for shards `sc9-3` and `sc9-4` to disappear and verifies the remaining cluster is fully ready. Only after those checks pass does Terraform remove the old persistent storage.

Existing application databases are **not** a reason to block DeleteShard. Their managed database/account/Vault inventory remains unchanged while the ShardedCluster topology is reduced.

Python does not issue `removeShard`, `movePrimary`, `moveCollection`, direct `kubectl delete`, or other topology mutations for this operation. The desired topology change is expressed through Terraform, preserving the project's Terraform-driven architecture.

If an operation is interrupted after the deployment lock is acquired, rerun the **same** DeleteShard command to resume safely.

## Database targeting

If exactly one deployment exists:

```bash
python3 terraformController.py AddDatabase HouseInfo
```

If multiple deployments exist:

```bash
python3 terraformController.py AddDatabase RS1 HouseInfo
python3 terraformController.py AddDatabase SC9 HouseInfo
```

When multiple deployments exist and the target is omitted, the command fails and lists the available deployment names, types, and phases.

## AddDatabase lifecycle

```bash
python3 terraformController.py AddDatabase SC9 HouseInfo
```

For a ShardedCluster, the controller:

1. verifies SC9 exists,
2. verifies no conflicting SC9 managed change is active,
3. verifies SC9 is fully ready,
4. atomically acquires the SC9 deployment lock through Terraform,
5. asks Terraform to materialize HouseInfo,
6. only after materialization succeeds, adds database lifecycle state,
7. Terraform creates the three managed users and credentials,
8. waits for MongoDBUser reconciliation,
9. Terraform performs actual credential verification,
10. releases the SC9 deployment lock,
11. reports success.

Expected success output remains explicit:

```text
MongoDB database 'HouseInfo' was successfully created on ShardedCluster 'SC9'.
Managed accounts created:
  HouseInfo_owner      (dbOwner)
  HouseInfo_readWrite  (readWrite)
  HouseInfo_read       (read)

Please go to Vault at http://127.0.0.1:8200/ui/ to get your credentials.
```

## Fixed database accounts

Every managed application database gets exactly:

```text
<DB>_owner      -> dbOwner on <DB>
<DB>_readWrite  -> readWrite on <DB>
<DB>_read       -> read on <DB>
```

There are no arbitrary AddUser/DeleteUser/ChangeRole commands in this MVP.

## Vault layout

Vault is organized by deployment, not by individual shard:

```text
mongodb/<Deployment>/<Database>/<Username>
```

ReplicaSet:

```text
mongodb/RS1/HouseInfo/HouseInfo_owner
mongodb/RS1/HouseInfo/HouseInfo_readWrite
mongodb/RS1/HouseInfo/HouseInfo_read
```

ShardedCluster:

```text
mongodb/SC9/HouseInfo/HouseInfo_owner
mongodb/SC9/HouseInfo/HouseInfo_readWrite
mongodb/SC9/HouseInfo/HouseInfo_read
```

Users authenticate to the deployment. Individual shard credentials are not part of the DBaaS user model.

Deployment metadata:

```text
mongodb/<Deployment>/_metadata
```

Database lifecycle metadata:

```text
mongodb/<Deployment>/<Database>/_metadata
```

Hidden controller credential:

```text
mongodb/<Deployment>/_internal/controller-admin
```

## Password rotation

Default interval:

```text
30 days
```

Example:

```bash
python3 terraformController.py RotatePasswords SC9 HouseInfo
```

On a ShardedCluster, rotation uses the same deployment lock as other mutations. This prevents shard topology changes from starting during a credential rotation.

Terraform owns the password revision, timestamp, Owner-disable decision, password generation, Vault write, and Kubernetes Secret write.

The existing recovery retry for partial Vault/Kubernetes rotation remains in place.

## Owner lifecycle

```bash
python3 terraformController.py DisableOwner SC9 HouseInfo --confirm
```

On a ShardedCluster, the command acquires the same deployment lock before changing Owner state.

To re-enable Owner, an authorized administrator updates:

```text
mongodb/<Deployment>/<Database>/_metadata
owner_disabled = false
```

then runs:

```bash
python3 terraformController.py Reconcile
```

Reconcile is blocked while another ShardedCluster managed change is active.

## Database deletion

```bash
python3 terraformController.py DeleteDatabase SC9 HouseInfo --confirm
```

The target deployment must be fully ready.

On a ShardedCluster, DeleteDatabase acquires the deployment lock before Terraform drops the database.

`--confirm` authorizes destruction of the database and its contents.

Terraform drops the MongoDB database before desired account state is removed, then removes the three managed users, Kubernetes password resources, Vault credentials, and lifecycle metadata.

## Deployment deletion

ReplicaSet:

```bash
python3 terraformController.py DeleteReplicaSet RS1 --confirm
```

ShardedCluster:

```bash
python3 terraformController.py DeleteShardedCluster SC9 --confirm
```

Deletion is blocked when managed databases exist.

Terraform also performs a live MongoDB database check before deleting the deployment.

System databases ignored by the emptiness check:

```text
admin
config
local
```

A ShardedCluster deletion is also blocked while its deployment lock is active.

## Sharding scope

This version provisions and manages ShardedCluster infrastructure.

It does **not** expose:

- shard-key selection,
- ShardCollection,
- ReshardCollection,
- UnshardCollection,
- per-database shard placement,
- per-user shard selection.

MongoDB collection-level sharding policy will be added after customer requirements are defined.

## Structured logging

Logging is implemented by:

```text
terraform_controller/logging_component.py
```

Business/lifecycle code sends events to the logging component. Passwords, Vault
tokens, and secret values are not intentionally logged.

The controller reads all log-file behavior from `terraformController.config`.

Default configuration:

```ini
[Logging]
enabled = true
level = INFO
directory = logs
mode = append
filename_format =
```

### Logging directory

`directory` may be relative or absolute.

Relative example:

```ini
directory = logs
```

A relative path is resolved from the directory containing
`terraformController.config`, not from the shell's current working directory.
With the repository's default config, the log directory is therefore:

```text
<repository>/logs
```

Absolute example:

```ini
directory = /var/log/terraformController
```

The directory is created automatically when logging starts.

### Append versus overwrite

Supported values:

```text
append
overwrite
```

`append` preserves an existing selected log file and writes new JSON log events
at the end.

`overwrite` truncates the selected log file when the controller starts and
writes a fresh run to that file.

### Log file name

If `filename_format` is blank:

```ini
filename_format =
```

the controller uses:

```text
Controller.log
```

If a format is supplied, standard Python/Unix `strftime` tokens are expanded
when the controller starts.

Common tokens:

```text
%Y = four-digit year
%m = two-digit month
%d = two-digit day
%H = hour on a 24-hour clock
%M = minute
%S = second
```

Important:

```text
%m = MONTH
%M = MINUTE
```

Example:

```ini
filename_format = Controller-%Y%m%d-%H%M.log
```

could produce:

```text
Controller-20260908-1607.log
```

Another example:

```ini
filename_format = %Y%m%d-%H%M%S-Controller.log
```

could produce:

```text
20260908-160742-Controller.log
```

`filename_format` is a file name only. Directory information belongs in
`directory`.

The log content remains structured JSON Lines: one JSON object per log event,
even when the configured file name uses a normal `.log` extension.

## Local static storage

Current local mode:

```text
static-local
```

ReplicaSet storage uses the existing host-backed local PV model.

ShardedCluster local storage is Terraform-managed per persistent volume:

- one PV for each shard member,
- one PV for each config-server member,
- no persistent volume for mongos.

For shard addition, storage is increased before `shardCount`.

For shard deletion, Terraform reduces `shardCount`. The MongoDB Kubernetes Operator/Ops Manager reconciles the supported scale-down, the removed shard StatefulSets disappear, and the remaining cluster must be fully ready before Terraform cleans the old shard storage. This ordering is the same whether or not application databases exist on the ShardedCluster.

## Ops Manager project isolation

The controller reuses:

```text
my-project
organization-secret
```

and uses:

```text
tc-ops-manager-projects
```

without a fixed `projectName`, allowing each managed MongoDB resource to use a distinct Ops Manager project.

## Source layout

```text
terraformController.py
terraform_controller/cli.py
terraform_controller/deployments.py
terraform_controller/databases.py
terraform_controller/deployment_lock.py
terraform_controller/maintenance.py
terraform_controller/kube.py
terraform_controller/vault.py
terraform_controller/terraform_runner.py
terraform_controller/logging_component.py
terraform_controller/config.py
terraform_controller/common.py
terraform-dbaas/
```

## RecoverDeploymentLock

Use this command only for an interrupted ShardedCluster topology operation that
already reached its recorded target topology but failed during a later
bookkeeping or storage step:

```bash
python3 terraformController.py RecoverDeploymentLock SC9 --confirm
```

The recovery is intentionally narrow. Before releasing anything, the controller
requires:

```text
Active lock category             = topology
Active lock action               = AddShard or DeleteShard
Vault desired shard count        = lock target
Live MongoDB spec.shardCount     = lock target
MongoDB phase                    = Running
Every surviving shard            = Online
Config servers                   = Online
mongos                           = Online
Removed shard StatefulSets       = Absent (DeleteShard)
```

The release remains Terraform-driven. The recovery uses a targeted Terraform
apply for the lifecycle-operation resource so unrelated legacy storage is not
reconciled while the lock is being released.

For old static-local ShardedClusters created before deterministic PV/PVC
pre-binding, full deployment teardown can clean a mismatched legacy PV only
after the MongoDB resource is absent. Cleanup then follows the PV's actual
Kubernetes claim and still refuses to remove any PVC that is referenced by a
pod. While the MongoDB deployment still exists, PV/PVC identity mismatches
remain a hard refusal.

## RecoverOrphanedResources

Use this command only when a failed Terraform destroy has already removed all
Vault-backed controller inventory, but Terraform still tracks orphaned
Kubernetes/storage resources:

```bash
python3 terraformController.py RecoverOrphanedResources --confirm
```

The command is asynchronous because storage teardown may need bounded waits.
Before Terraform is allowed to mutate anything, the controller requires both:

```text
Vault-backed managed deployment inventory           = empty
Live MongoDB CRs labeled managed-by=terraformController = none
```

If either check fails, recovery stops without making a change.

When both checks pass, Python sends an empty desired-state inventory to
Terraform. Terraform then finishes destroying any remaining controller-managed
resources in its backend state. Storage cleanup remains protected by the normal
PVC/PV ownership, live-use, and bounded-wait checks.

This command is intentionally different from `Reconcile`. Reconcile protects
normal managed desired state; RecoverOrphanedResources exists only for the
exceptional case where desired state is already empty but a previous Terraform
destroy ended partway through.

## Reconcile

```bash
python3 terraformController.py Reconcile
```

Reconcile reconstructs desired state from Vault, refreshes Terraform, reapplies managed state, and waits for convergence.

If any ShardedCluster deployment lock is active, Reconcile stops before applying Terraform and lists the active change.

## Vault token

Do not store the Vault token in `terraformController.config`.

Use:

```bash
export VAULT_TOKEN='<current-vault-token>'
```

## Test harness

Fast unit/regression tests are separated by subsystem under `tests/`.

```bash
python3 -m unittest discover -s tests -p "test_*.py" -v
```

The default live harness profile is read-only:

```bash
python3 tests/run_harness.py
```

For a complete local development lifecycle test:

```bash
python3 tests/run_harness.py \
  --profile all \
  --allow-mutations \
  --allow-destructive
```

The full profile exercises ReplicaSet lifecycle, database/account lifecycle,
a 3 -> 5 -> 4 -> 1 ShardedCluster lifecycle, count-based shard add/delete,
final-shard protection, shard deletion while a managed database remains present,
global/targeted shard status, asynchronous operation polling, and an actual
concurrent-process deployment-lock test.

Harness output identifies `Test X of Y`, prints periodic `[WAIT]` lines for
long asynchronous operations, records per-test elapsed time, and finishes with
per-profile plus total elapsed time.

See `tests/README.md` for scenario details.

## Validation

GitHub Actions validates:

- Python syntax,
- Python unit tests,
- Bash lifecycle syntax,
- Terraform formatting,
- Terraform provider initialization,
- Terraform validation.

After static/CI validation, new ShardedCluster shard lifecycle behavior still requires a controlled live smoke test against the local Kubernetes/Ops Manager/Vault environment.
