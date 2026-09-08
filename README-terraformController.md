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

Python does not directly create, delete, rotate, enable, or disable managed MongoDB/Vault resources.

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
AddShard SHARDED_CLUSTER
DeleteShard SHARDED_CLUSTER --confirm
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
python3 terraformController.py AddDatabase --help
python3 terraformController.py DeleteShard --help
python3 terraformController.py RotatePasswords --help
```

## Deployment status

The MongoDB Kubernetes Operator exposes the deployment phase through `status.phase`.

The controller reports phases such as:

```text
Pending
Running
Failed
Absent
Unknown
```

For ShardedClusters, the controller also reports each expected shard as:

```text
Online
Creating
Degraded
Failed
Unknown
```

Example:

```text
ShardedCluster: SC9
Phase:          Running

SHARD  STATUS  READY  DESIRED  UPDATED
-----  ------  -----  -------  -------
sc9-0  Online  3      3        3
sc9-1  Online  3      3        3
sc9-2  Online  3      3        3

Config servers: Online (3/3)
mongos:         Online (2/2)
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
```

If any condition is not satisfied, the command fails before Terraform changes database state.

The error identifies the current phase/component status.

## AddReplicaSet

Example:

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

Example:

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

```bash
python3 terraformController.py AddShard SC9
```

Shard addition is staged:

1. Terraform prepares the new shard's persistent storage.
2. Terraform increases the ShardedCluster shard count.
3. The controller waits until the new shard and the complete cluster are online.
4. Success is reported.

This prevents MongoDB from being asked to create a persistent shard before its local-development storage exists.

## DeleteShard

```bash
python3 terraformController.py DeleteShard SC9 --confirm
```

This first-pass implementation is intentionally conservative.

Deletion is allowed only if:

1. the ShardedCluster is fully Running,
2. the cluster has more than one shard,
3. managed inventory contains zero application databases,
4. Terraform performs a live MongoDB check and finds zero non-system databases.

The highest-numbered shard is removed.

Deletion is staged:

1. Terraform lowers the MongoDB shard count while existing storage remains available.
2. The controller waits for the removed shard StatefulSet to disappear and the remaining cluster to return to full readiness.
3. Terraform removes the old shard's persistent storage.

This avoids deleting storage before MongoDB has removed the shard.

More advanced shard draining/data-distribution behavior is deferred until the customer defines its collection sharding policy.

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

Example:

```bash
python3 terraformController.py AddDatabase SC9 HouseInfo
```

The controller:

1. verifies SC9 exists in managed inventory,
2. verifies SC9 is fully ready,
3. verifies HouseInfo does not already exist,
4. asks Terraform to materialize HouseInfo,
5. only after materialization succeeds, adds database lifecycle state,
6. Terraform creates the three managed users and credentials,
7. the controller waits for MongoDBUser reconciliation,
8. Terraform performs actual credential verification,
9. only then does the command report success.

Expected success output is explicit:

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

ReplicaSet example:

```text
mongodb/RS1/HouseInfo/HouseInfo_owner
mongodb/RS1/HouseInfo/HouseInfo_readWrite
mongodb/RS1/HouseInfo/HouseInfo_read
```

ShardedCluster example:

```text
mongodb/SC9/HouseInfo/HouseInfo_owner
mongodb/SC9/HouseInfo/HouseInfo_readWrite
mongodb/SC9/HouseInfo/HouseInfo_read
```

Do not create user credential paths such as:

```text
mongodb/SC9/Shard1/HouseInfo/...
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

Existing metadata that predates the deployment-type field is treated as ReplicaSet metadata for backward compatibility.

The older `mongodb/replica-sets/...` layout is also still readable as a migration fallback.

## Password rotation

Default:

```text
30 days
```

Example:

```bash
python3 terraformController.py RotatePasswords SC9 HouseInfo
```

Before rotation, the target deployment must pass the same readiness checks used for database changes.

Terraform owns:

- password revision,
- rotation timestamp,
- Owner-disable decision,
- password generation,
- Vault write,
- Kubernetes Secret write.

The controller verifies actual MongoDB authentication before reporting success.

If a partial multi-provider apply occurs, the controller reloads committed lifecycle metadata and makes one recovery retry with a fresh revision so Vault and Kubernetes converge on a new password.

## Owner lifecycle

At the first rotation at or after the configured rotation age, Terraform disables the Owner MongoDBUser.

The current Owner credential remains in Vault and continues rotating.

For demonstration or administration:

```bash
python3 terraformController.py DisableOwner SC9 HouseInfo --confirm
```

To re-enable Owner, an authorized administrator updates:

```text
mongodb/<Deployment>/<Database>/_metadata
owner_disabled = false
```

then runs:

```bash
python3 terraformController.py Reconcile
```

## Database deletion

```bash
python3 terraformController.py DeleteDatabase SC9 HouseInfo --confirm
```

The target deployment must be fully ready.

`--confirm` authorizes destruction of the database and its contents.

Terraform drops the MongoDB database before desired account state is removed.

Then the lifecycle removes:

- Owner,
- ReadWrite,
- Read,
- Kubernetes password resources,
- Vault credentials,
- database lifecycle metadata.

The controller verifies the MongoDB users are absent before reporting success.

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

These MongoDB system databases do not block deletion:

```text
admin
config
local
```

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

Business/lifecycle code sends events to this component. Business logic does not open log files directly.

Default format:

```text
JSON Lines (.jsonl)
```

Each line is one JSON object. Example fields include:

```text
timestamp
level
logger
event
deployment
deployment_type
database
action
phase
```

Passwords, Vault tokens, and secret values are not intentionally logged.

Configuration:

```ini
[Logging]
enabled = true
level = INFO
directory = ~/.local/state/terraformController/logs
mode = per-run
filename_pattern = terraformController-%Y%m%d-%H%M%S-{pid}.jsonl
retention_days = 30
```

Modes:

```text
per-run  New timestamped log file for each controller execution.
append   Append to the rendered file name.
replace  Replace the rendered file when the run starts.
```

Default retention removes old `.jsonl` files in the configured log directory after 30 days.

## Local static storage

Current local mode:

```text
static-local
```

ReplicaSet storage uses the existing host-backed local PV model.

ShardedCluster local storage is Terraform-managed per persistent volume:

- one PV for each shard member,
- one PV for each config-server member,
- no persistent volume is required for mongos.

This per-volume model lets AddShard prepare only new volumes and lets DeleteShard remove only the deleted shard's volumes.

Future/customer mode can use:

```text
dynamic
```

where a cluster StorageClass dynamically provisions storage.

## Ops Manager project isolation

The controller reuses the known working Ops Manager connection information from:

```text
my-project
organization-secret
```

It creates/uses:

```text
tc-ops-manager-projects
```

without a fixed `projectName`, allowing each MongoDB resource to use a distinct Ops Manager project.

Do not casually delete or replace the known-good Ops Manager integration artifacts.

## Source layout

The Python entry point is intentionally small:

```text
terraformController.py
```

Python responsibilities are split across:

```text
terraform_controller/cli.py
terraform_controller/deployments.py
terraform_controller/databases.py
terraform_controller/maintenance.py
terraform_controller/kube.py
terraform_controller/vault.py
terraform_controller/terraform_runner.py
terraform_controller/logging_component.py
terraform_controller/config.py
terraform_controller/common.py
```

Terraform implementation:

```text
terraform-dbaas/
```

The controller refreshes Terraform from GitHub before applying managed changes.

## Reconcile

```bash
python3 terraformController.py Reconcile
```

Reconcile:

1. reconstructs desired deployment state from Vault,
2. refreshes Terraform from GitHub,
3. reapplies all managed deployment/database/account state,
4. waits for ReplicaSets and ShardedClusters to become ready,
5. waits for controller and database accounts to reconcile.

If there are zero managed deployments, Reconcile reports that there is nothing to do.

## Vault token

Do not store the Vault token in `terraformController.config`.

Use:

```bash
export VAULT_TOKEN='<current-vault-token>'
```

The local Vault port-forward must be available when using the default local Vault address.

## Validation

GitHub Actions validates:

- Python syntax,
- Python unit tests,
- Bash lifecycle syntax,
- Terraform formatting,
- Terraform provider initialization,
- Terraform validation.

Live Kubernetes/Ops Manager/Vault behavior still requires an end-to-end smoke test against the local environment after code validation succeeds.
