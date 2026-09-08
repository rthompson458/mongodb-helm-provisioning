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

The command does not report success until the MongoDB resource is Running and the hidden controller account is ready.

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

The command waits for the ShardedCluster and all expected components before reporting success.

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

The operation is staged:

1. verify COUNT is at least 1,
2. verify SC9 exists and is fully ready,
3. atomically acquire the SC9 deployment lock through Terraform,
4. Terraform prepares persistent storage for the target shard count,
5. Terraform changes the MongoDB `shardCount` to the target,
6. wait until every target shard is online,
7. release the deployment lock through Terraform,
8. report the previous and final shard counts.

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
4. Managed inventory must contain zero application databases.
5. Terraform performs a live MongoDB check and must find zero non-system application databases.
6. The SC deployment lock must be acquired before topology changes begin.

For example, if SC9 has 3 shards:

```text
DeleteShard SC9 1 --confirm  -> allowed target: 2
DeleteShard SC9 2 --confirm  -> allowed target: 1
DeleteShard SC9 3 --confirm  -> BLOCKED
```

The highest-numbered shards are removed first.

For `DeleteShard SC9 2 --confirm` from five shards, Terraform changes the desired shard count from 5 to 3, waits for shards `sc9-3` and `sc9-4` to disappear, verifies the remaining cluster is fully ready, and only then removes the old persistent storage.

If an operation is interrupted after the deployment lock is acquired, rerun the **same** DeleteShard command to resume safely.

This first-pass implementation remains intentionally conservative: if any application database exists on the ShardedCluster, shard deletion is blocked. More advanced shard draining/data-distribution behavior will be designed with the customer.

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

Deployment locking is implemented by:

```text
terraform_controller/deployment_lock.py
```

Business/lifecycle code sends events to the logging component. Passwords, Vault tokens, and secret values are not intentionally logged.

Default logging:

```ini
[Logging]
enabled = true
level = INFO
directory = ~/.local/state/terraformController/logs
mode = per-run
filename_pattern = terraformController-%Y%m%d-%H%M%S-{pid}.jsonl
retention_days = 30
```

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

For shard deletion, `shardCount` is reduced and the removed shard StatefulSets disappear before old shard storage is cleaned up.

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
ShardedCluster lifecycle, count-based shard add/delete, final-shard protection,
database-based shard-deletion blocking, global/targeted shard status, and an
actual concurrent-process deployment-lock test.

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
