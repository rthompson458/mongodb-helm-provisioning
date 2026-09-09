# terraformController

**Customer / DBaaS user guide**

This document describes the normal service interface exposed by `terraformController.py`.
Platform diagnostics, Terraform reconciliation, operation journals, and recovery commands
are intentionally separated into `terraformControllerAdmin.py`; see
`README-terraformControllerAdmin.md`.

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
python3 terraformController.py AddShardedCluster SC9
```

For `AddShardedCluster`, the initial shard count is optional. The current
configured default is **3 shards**, and the controller reads that default from
its configuration. Use `--shards N` to override the configured value for one
request.

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

Defaults:

- `AddShardedCluster SHARDED_CLUSTER` uses the initial shard count stored in
  controller configuration. The current configured default is **3**.
- `AddShard SHARDED_CLUSTER` defaults to **1 shard** when `COUNT` is omitted.
- `DeleteShard SHARDED_CLUSTER --confirm` defaults to **1 shard** when
  `COUNT` is omitted.

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


## Background processing for long-running requests

Deployment and shard topology changes can take longer than a normal command-line
interaction because MongoDB, Ops Manager, Kubernetes, storage, and Terraform must
converge safely.

The following public commands therefore return after the request has been accepted
while processing continues in the background:

```text
AddReplicaSet
DeleteReplicaSet
AddShardedCluster
DeleteShardedCluster
AddShard
DeleteShard
```

The customer interface reports only service-oriented information. Internal
operation IDs, worker PIDs, journal paths, and recovery details are deliberately
kept on the administrator interface.

Example:

```bash
python3 terraformController.py DeleteShard SC9 1 --confirm
```

returns a customer-facing acknowledgement similar to:

```text
DeleteShard request accepted.

ShardedCluster: SC9
Status:         Topology change requested

The request is being processed in the background.

Check service status with:
  python3 terraformController.py ... ListShards SC9
```

Use the normal resource status commands to follow progress:

```text
ListReplicaSet
ListReplicaSets
ListShardedCluster
ListShardedClusters
ListShards
ListDeployments
```

If a request does not converge as expected, a platform administrator can inspect
the private operation journal without exposing those implementation details to
DBaaS users.

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

The command returns after the request is accepted. Use `ListReplicaSet RS1` or `ListReplicaSets` to monitor service readiness. A ReplicaSet is ready for normal database work only after it is Running and controller authentication is usable.

## AddShardedCluster

Use the configured default shard count:

```bash
python3 terraformController.py AddShardedCluster SC9
```

Override the configured default for one request:

```bash
python3 terraformController.py AddShardedCluster SC9 --shards 5
```

Default topology:

```text
Shards:             3
Members per shard:  3
mongos:             2
Config servers:     3
MongoDB:            8.0.29
```

The initial shard count is read from controller configuration when `--shards`
is omitted. The current configured default is **3 shards**. The CLI help reads
and displays the configured value dynamically so the help text stays aligned
with configuration changes. Use `--shards N` to override it for one request.

The command returns after the request is accepted. Use `ListShardedCluster SC9` and `ListShards SC9` to monitor readiness. The deployment is ready only when the ShardedCluster, shards, config servers, and mongos components are online.

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

If `COUNT` is omitted, `AddShard` adds **1 shard** by default.

For `AddShard SC9 2`, if SC9 starts with 3 shards, the target is 5 shards.

The controller performs the staged lifecycle in the background:

1. verify COUNT is at least 1,
2. verify SC9 exists and is fully ready,
3. atomically acquire the SC9 deployment lock through Terraform,
4. Terraform prepares persistent storage for the target shard count,
5. Terraform changes the MongoDB `shardCount` to the target,
6. wait until every target shard is online,
7. release the deployment lock through Terraform,
8. complete the managed topology request.

The submitting shell does not wait for steps 3-8. Use `ListShards` for live topology progress and the normal deployment status commands for service readiness.

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

If `COUNT` is omitted, `DeleteShard` removes **1 shard** by default.
`--confirm` is still required.

Safety rules:

1. COUNT must be at least 1.
2. The ShardedCluster must retain at least **one shard**.
3. The cluster must initially be fully ready.
4. The SC deployment lock must be acquired before topology changes begin.
5. Application databases may remain on the ShardedCluster during shard removal.
6. Terraform lowers the managed MongoDB ShardedCluster `spec.shardCount`; Python does not directly remove shards.
7. The MongoDB Kubernetes Operator/Ops Manager reconciles the supported ShardedCluster scale-down.
8. Background processing waits for the remaining cluster to become fully ready and for removed shard StatefulSets to disappear before Terraform cleans the old shard storage.

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

Re-enabling a disabled Owner account is an administrator-controlled service action rather than a customer command. The platform administrator follows the controller administration runbook in `README-terraformControllerAdmin.md` and performs any required reconciliation through the administrator interface.

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
terraformController.py                  # customer / DBaaS interface
terraformControllerAdmin.py             # platform administrator interface
terraform_controller/cli.py
terraform_controller/admin_cli.py
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

## Administrator operations

Administrator diagnostics, operation journals, Terraform reconciliation, and
recovery procedures are deliberately excluded from the customer command surface.

See:

```text
README-terraformControllerAdmin.md
```

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
global/targeted shard status, background-operation completion, and an actual
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
