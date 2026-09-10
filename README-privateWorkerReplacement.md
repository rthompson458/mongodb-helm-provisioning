# privateWorkerReplacement Customer Guide

`privateWorkerReplacement.py` is the normal MongoDB Database as a Service (DBaaS) command-line interface. This document is the primary user manual for creating and managing MongoDB ReplicaSets, ShardedClusters, shards, databases, and database credentials.

Platform diagnostics, Terraform reconciliation, operation IDs, and recovery commands are intentionally separated into `privateWorkerReplacementAdmin.py`. See `README-privateWorkerReplacementAdmin.md` if you are operating the platform rather than consuming the DBaaS service.

---

## 1. Basic usage

Run the program with no command to show the full customer command list:

```bash
python3 privateWorkerReplacement.py
```

This is intentionally equivalent to:

```bash
python3 privateWorkerReplacement.py --help
```

Both forms print help and exit successfully.

Show detailed help for one command:

```bash
python3 privateWorkerReplacement.py AddReplicaSet --help
python3 privateWorkerReplacement.py AddShardedCluster --help
python3 privateWorkerReplacement.py AddDatabase --help
python3 privateWorkerReplacement.py DeleteDatabase --help
python3 privateWorkerReplacement.py ListDatabase --help
python3 privateWorkerReplacement.py ListDatabaseAccounts --help
```

The controller uses this configuration file in the current working directory by default:

```text
./privateWorkerReplacement.config
```

To intentionally use another configuration file:

```bash
python3 privateWorkerReplacement.py --config ./alternate-privateWorkerReplacement.config ListDeployments
```

Do not store the Vault token in the configuration file. The local development environment expects the token in the configured environment variable, normally `VAULT_TOKEN`.

---

## 2. What a fresh installation contains

A fresh installation starts with no user-facing MongoDB deployments:

```text
ReplicaSets:      0
ShardedClusters:  0
Databases:        0
```

The platform does not automatically create a default RS1 or SC1. The DBaaS user creates and names the required deployment.

A **deployment** is either:

- a ReplicaSet; or
- a ShardedCluster.

The managed hierarchy is:

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

A ShardedCluster is managed as one deployment even though MongoDB implements each shard as a ReplicaSet internally.

---

## 3. Quick-start workflows

### ReplicaSet workflow

Request a ReplicaSet:

```bash
python3 privateWorkerReplacement.py AddReplicaSet RS1
```

The request is asynchronous. The command returns after the request has been accepted while provisioning continues in the background.

Check readiness:

```bash
python3 privateWorkerReplacement.py ListReplicaSet RS1
```

When the ReplicaSet is `Running`, request a database:

```bash
python3 privateWorkerReplacement.py AddDatabase RS1 HouseInfo
```

Database creation is also asynchronous. Use the status command printed by the controller while the request finishes:

```bash
python3 privateWorkerReplacement.py ListDatabase RS1 HouseInfo
```

When the database reports `Ready`, inspect its three managed accounts and Vault credential locations:

```bash
python3 privateWorkerReplacement.py ListDatabaseAccounts RS1 HouseInfo
```

Delete the database when it is no longer needed:

```bash
python3 privateWorkerReplacement.py DeleteDatabase RS1 HouseInfo --confirm
```

Database deletion is asynchronous. Confirm that it is gone with:

```bash
python3 privateWorkerReplacement.py ListDatabases RS1
```

After all user databases are removed, delete the ReplicaSet:

```bash
python3 privateWorkerReplacement.py DeleteReplicaSet RS1 --confirm
```

### ShardedCluster workflow

Request a ShardedCluster using the configured default shard count:

```bash
python3 privateWorkerReplacement.py AddShardedCluster SC9
```

Or specify the initial shard count:

```bash
python3 privateWorkerReplacement.py AddShardedCluster SC9 --shards 5
```

Check cluster and shard readiness:

```bash
python3 privateWorkerReplacement.py ListShardedCluster SC9
python3 privateWorkerReplacement.py ListShards SC9
```

Add shards:

```bash
python3 privateWorkerReplacement.py AddShard SC9
python3 privateWorkerReplacement.py AddShard SC9 2
```

Remove shards while retaining at least one:

```bash
python3 privateWorkerReplacement.py DeleteShard SC9 --confirm
python3 privateWorkerReplacement.py DeleteShard SC9 2 --confirm
```

Create a database after the cluster is fully ready:

```bash
python3 privateWorkerReplacement.py AddDatabase SC9 Orders
```

Inspect database status:

```bash
python3 privateWorkerReplacement.py ListDatabase SC9 Orders
```

Inspect account and Vault credential details:

```bash
python3 privateWorkerReplacement.py ListDatabaseAccounts SC9 Orders
```

Delete the database:

```bash
python3 privateWorkerReplacement.py DeleteDatabase SC9 Orders --confirm
```

After the cluster contains no managed user databases, delete the cluster:

```bash
python3 privateWorkerReplacement.py DeleteShardedCluster SC9 --confirm
```

---

## 4. Customer command reference

### Deployment inventory

```text
ListDeployments
ListDeployment DEPLOYMENT
ListReplicaSets
ListReplicaSet REPLICASET
ListShardedClusters
ListShardedCluster SHARDED_CLUSTER
ListShards [SHARDED_CLUSTER]
```

Examples:

```bash
python3 privateWorkerReplacement.py ListDeployments
python3 privateWorkerReplacement.py ListDeployment RS1
python3 privateWorkerReplacement.py ListReplicaSets
python3 privateWorkerReplacement.py ListReplicaSet RS1
python3 privateWorkerReplacement.py ListShardedClusters
python3 privateWorkerReplacement.py ListShardedCluster SC9
python3 privateWorkerReplacement.py ListShards
python3 privateWorkerReplacement.py ListShards SC9
```

### ReplicaSet lifecycle

```text
AddReplicaSet REPLICASET
DeleteReplicaSet REPLICASET --confirm
```

Examples:

```bash
python3 privateWorkerReplacement.py AddReplicaSet RS1
python3 privateWorkerReplacement.py DeleteReplicaSet RS1 --confirm
```

### ShardedCluster lifecycle

```text
AddShardedCluster SHARDED_CLUSTER [--shards N]
DeleteShardedCluster SHARDED_CLUSTER --confirm
AddShard SHARDED_CLUSTER [COUNT]
DeleteShard SHARDED_CLUSTER [COUNT] --confirm
```

Examples:

```bash
python3 privateWorkerReplacement.py AddShardedCluster SC9
python3 privateWorkerReplacement.py AddShardedCluster SC9 --shards 5
python3 privateWorkerReplacement.py AddShard SC9
python3 privateWorkerReplacement.py AddShard SC9 2
python3 privateWorkerReplacement.py DeleteShard SC9 --confirm
python3 privateWorkerReplacement.py DeleteShard SC9 2 --confirm
python3 privateWorkerReplacement.py DeleteShardedCluster SC9 --confirm
```

### Database lifecycle and status

```text
AddDatabase DEPLOYMENT DATABASE
AddDatabase DATABASE
DeleteDatabase DEPLOYMENT DATABASE --confirm
DeleteDatabase DATABASE --confirm
ListDatabases [DEPLOYMENT]
ListDatabase DEPLOYMENT DATABASE
ListDatabase DATABASE
ListDatabaseAccounts DEPLOYMENT DATABASE
ListDatabaseAccounts DATABASE
```

Examples:

```bash
python3 privateWorkerReplacement.py AddDatabase RS1 HouseInfo
python3 privateWorkerReplacement.py AddDatabase SC9 Orders
python3 privateWorkerReplacement.py AddDatabase HouseInfo
python3 privateWorkerReplacement.py ListDatabases
python3 privateWorkerReplacement.py ListDatabases SC9
python3 privateWorkerReplacement.py ListDatabase SC9 Orders
python3 privateWorkerReplacement.py ListDatabaseAccounts SC9 Orders
python3 privateWorkerReplacement.py DeleteDatabase SC9 Orders --confirm
```

The one-argument database form is valid only when exactly one managed deployment exists. If more than one deployment exists, specify the deployment explicitly.

### Credential lifecycle

```text
RotatePasswords DEPLOYMENT DATABASE
RotatePasswords DATABASE
DisableOwner DEPLOYMENT DATABASE --confirm
DisableOwner DATABASE --confirm
```

Examples:

```bash
python3 privateWorkerReplacement.py RotatePasswords RS1 HouseInfo
python3 privateWorkerReplacement.py DisableOwner RS1 HouseInfo --confirm
```

---

## 5. Asynchronous operations

The following customer commands are asynchronous:

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

They return after the request has been accepted, then complete through a detached local worker. The customer does not need an internal operation ID.

An AddDatabase acknowledgement is service-oriented, for example:

```text
AddDatabase request accepted.

Deployment:     RS1
Database:       HouseInfo
Status:         Creation requested

The request is being processed in the background.

Check service status with:
  python3 privateWorkerReplacement.py --config ./privateWorkerReplacement.config ListDatabase RS1 HouseInfo
```

The worker resolves the configuration file to an absolute path internally so the detached process remains reliable. That internal path is not shown in normal customer instructions.

A DeleteDatabase acknowledgement follows the same pattern but directs the user to `ListDatabases` to confirm removal.

The public CLI intentionally does not expose:

- operation IDs;
- worker PIDs;
- operation-state JSON files;
- raw Git output;
- Terraform plans or apply output;
- Terraform state details;
- deployment-lock recovery mechanics.

If an asynchronous request appears not to complete, use the normal public status commands first. A platform administrator can inspect the private operation status and daily operation diagnostics with `privateWorkerReplacementAdmin.py`.

`RotatePasswords` and `DisableOwner` currently remain synchronous. Their underlying Terraform/Git output is captured in the operations log rather than printed to the terminal.

---

## 6. ReplicaSet behavior

`AddReplicaSet` creates an empty managed ReplicaSet using values from `privateWorkerReplacement.config`, including MongoDB version, member count, storage class, storage size, and storage model.

Typical current development defaults are:

```text
Members:       3
MongoDB:       8.0.29
Persistent:    true
StorageClass:  mongodb-data-local
Storage:       16Gi/member
```

Creation is not complete merely because the request was accepted. The ReplicaSet is usable for database work only after it reports `Running` and the hidden controller administrator account can authenticate.

Check:

```bash
python3 privateWorkerReplacement.py ListReplicaSet RS1
```

`DeleteReplicaSet` requires `--confirm` and is allowed only when the ReplicaSet has no managed user databases. MongoDB system databases such as `admin`, `config`, and `local` do not count as user databases.

---

## 7. ShardedCluster behavior

`AddShardedCluster` creates an empty managed ShardedCluster. If `--shards` is omitted, the initial shard count comes from `privateWorkerReplacement.config`. The current repository configuration uses three shards.

Typical topology defaults are:

```text
Shards:             3
Members per shard:  3
mongos routers:     2
Config servers:     3
MongoDB:            8.0.29
```

The deployment is not ready for database work until:

```text
MongoDB phase        = Running
Every expected shard = Online
Config servers       = Online
mongos                = Online
No conflicting managed change is active
```

### Adding shards

`AddShard` defaults to one shard when COUNT is omitted.

```bash
python3 privateWorkerReplacement.py AddShard SC9
python3 privateWorkerReplacement.py AddShard SC9 2
```

The controller safely stages storage and topology changes through Terraform and waits for the target topology to become healthy before releasing the deployment lock.

### Deleting shards

`DeleteShard` also defaults to one shard and requires `--confirm`.

```bash
python3 privateWorkerReplacement.py DeleteShard SC9 --confirm
python3 privateWorkerReplacement.py DeleteShard SC9 3 --confirm
```

A ShardedCluster must retain at least one shard. If SC9 currently has four shards:

```text
DeleteShard SC9 1 --confirm  -> target 3, allowed
DeleteShard SC9 3 --confirm  -> target 1, allowed
DeleteShard SC9 4 --confirm  -> target 0, blocked
```

Shard deletion is supported while managed application databases remain on the ShardedCluster. Terraform lowers the desired shard count, MongoDB Operator/Ops Manager reconciles the scale-down, the controller waits for the remaining topology to become healthy and for removed shard workloads to release storage, and Terraform then removes the old shard storage.

The operation is Terraform-driven. Python does not directly issue MongoDB `removeShard`, `movePrimary`, direct `kubectl delete`, or similar topology mutations.

### Deployment locking

Each ShardedCluster uses one Terraform-created deployment lock to serialize conflicting mutations. Protected work includes shard, database, and credential changes. Read-only status commands remain available while a mutation is active.

If a shard operation is interrupted after the deployment lock has been acquired, rerunning the same AddShard/DeleteShard command with the same count uses the resume safeguards rather than blindly applying the count again.

---

## 8. Database behavior

### Readiness checks

Before `AddDatabase`, `DeleteDatabase`, `RotatePasswords`, or `DisableOwner` proceeds:

- the target deployment must exist;
- a ReplicaSet must be `Running`;
- a ShardedCluster must be fully ready, including shards, config servers, and mongos;
- no conflicting ShardedCluster managed change may be active.

Unsafe requests fail rather than bypassing the service protections.

### Database status values

Database status is intentionally separate from account status. Database-level commands can report:

```text
Creating
Ready
Deleting
Unavailable
```

`Creating` can appear immediately after an asynchronous `AddDatabase` request, including before the database has been committed to the normal Vault-backed managed inventory.

`Deleting` appears while an asynchronous `DeleteDatabase` request is active.

`Ready` means no active add/delete operation exists and the parent deployment is healthy enough for database service. For a ShardedCluster, that includes the expected shards, config servers, and mongos being Online.

`Unavailable` means the database is known to the controller but its parent deployment is not sufficiently healthy for normal service.

### AddDatabase

```bash
python3 privateWorkerReplacement.py AddDatabase RS1 HouseInfo
```

The public request is asynchronous. Inside the worker, the controller:

1. resolves and validates the deployment;
2. verifies readiness and any ShardedCluster lock state;
3. materializes the MongoDB database through Terraform;
4. records the database in managed desired state;
5. creates the three fixed accounts and Vault credentials through Terraform;
6. waits for MongoDBUser reconciliation;
7. performs real authentication verification;
8. records a successful operation result only after the lifecycle has converged.

MongoDB does not retain a truly empty database, so the controller materializes the database before it commits managed account state.

Use `ListDatabase` or `ListDatabases` to follow database lifecycle status. After the database is `Ready`, use `ListDatabaseAccounts` to see account, rotation, and Vault credential details.

### DeleteDatabase

```bash
python3 privateWorkerReplacement.py DeleteDatabase RS1 HouseInfo --confirm
```

Deletion is destructive and asynchronous. `--confirm` authorizes deletion of:

- the database and its contents;
- the three managed MongoDB accounts;
- the corresponding password resources;
- the Vault credentials;
- the database lifecycle metadata.

The worker reports success only after the database lifecycle has been removed, managed MongoDB users are absent, and the Terraform verification step completes.

Use:

```bash
python3 privateWorkerReplacement.py ListDatabases RS1
```

to confirm that the database is gone.

---

## 9. Fixed database accounts

Every managed application database receives exactly three accounts:

```text
<Database>_owner      -> dbOwner on <Database>
<Database>_readWrite  -> readWrite on <Database>
<Database>_read       -> read on <Database>
```

There are no arbitrary AddUser/DeleteUser/ChangeRole commands in this MVP. The three-account model is intentionally fixed and predictable.

Account information is shown with:

```bash
python3 privateWorkerReplacement.py ListDatabaseAccounts RS1 HouseInfo
```

This command owns account-related status. It shows the three managed accounts, account type, Enabled/Disabled state, rotation information, logical Vault paths, and browser-ready Vault URLs.

---

## 10. Vault credential locations and browser URLs

Vault credentials are scoped by deployment, database, and username:

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
mongodb/SC9/Orders/Orders_owner
mongodb/SC9/Orders/Orders_readWrite
mongodb/SC9/Orders/Orders_read
```

`ListDatabaseAccounts`, password rotation results, and relevant Owner output provide complete browser-ready Vault URLs.

With this development configuration:

```text
Vault address: http://127.0.0.1:8200
KV mount:      secret
```

the owner credential URL for `RS1/HouseInfo` is:

```text
http://127.0.0.1:8200/ui/vault/secrets/secret/show/mongodb/RS1/HouseInfo/HouseInfo_owner
```

The controller builds the URL from the configured Vault address, mount, deployment, database, and username. The user does not have to manually construct it.

The controller also shows the logical Vault path because it is useful for Vault CLI/API administration.

Additional internal metadata paths include:

```text
mongodb/<Deployment>/_metadata
mongodb/<Deployment>/<Database>/_metadata
mongodb/<Deployment>/_internal/controller-admin
```

The hidden controller-admin credential is infrastructure state and is not part of the normal customer credential model.

---

## 11. Password rotation and Owner policy

The rotation interval comes from `privateWorkerReplacement.config`; the current development value is 30 days.

Rotate all three passwords:

```bash
python3 privateWorkerReplacement.py RotatePasswords RS1 HouseInfo
```

The controller rotates Owner, ReadWrite, and Read credentials and verifies authentication. `ListDatabaseAccounts` shows the last rotation time and the remaining time until the next rotation.

The Owner policy is:

```text
Owner:     rotate every 30 days; disable in MongoDB at first rotation at/after day 30
ReadWrite: rotate every 30 days
Read:      rotate every 30 days
```

Disabling the Owner prevents MongoDB login, but the Owner credential remains in Vault and continues to participate in rotation.

Administratively disable the Owner early:

```bash
python3 privateWorkerReplacement.py DisableOwner RS1 HouseInfo --confirm
```

The command prints the complete browser URL for the retained Owner credential. `ListDatabaseAccounts` then reports the Owner account as Disabled while the ReadWrite and Read accounts remain Enabled.

---

## 12. Reading service status

### ListDeployments

```bash
python3 privateWorkerReplacement.py ListDeployments
```

Shows each managed deployment, deployment type, live phase, topology summary, MongoDB version, and managed database count.

### ListReplicaSet / ListShardedCluster

```bash
python3 privateWorkerReplacement.py ListReplicaSet RS1
python3 privateWorkerReplacement.py ListShardedCluster SC9
```

Use these targeted views while a deployment is being created or deleted and for normal health checks.

### ListShards

All ShardedClusters:

```bash
python3 privateWorkerReplacement.py ListShards
```

One ShardedCluster:

```bash
python3 privateWorkerReplacement.py ListShards SC9
```

Shard status can include states such as:

```text
Online
Creating
Degraded
Removing
Removed
Failed
Unknown
```

The targeted view also reports config-server status, mongos status, and any active managed topology change. READY/DESIRED/UPDATED member counts are retained because they are useful when a shard is converging or degraded.

### ListDatabases

```bash
python3 privateWorkerReplacement.py ListDatabases
python3 privateWorkerReplacement.py ListDatabases RS1
```

`ListDatabases` is database inventory only. It shows:

```text
DEPLOYMENT   DATABASE     STATUS
RS1          HouseInfo    Ready
RS1          Inventory    Creating
SC9          Orders       Deleting
```

It does not list account rows.

### ListDatabase

```bash
python3 privateWorkerReplacement.py ListDatabase RS1 HouseInfo
```

`ListDatabase` shows one database and database-level information such as:

```text
Deployment:       RS1
Deployment Type:  ReplicaSet
Database:         HouseInfo
Status:           Ready
Created:          <timestamp>
```

It does not serve as the account-detail view.

### ListDatabaseAccounts

```bash
python3 privateWorkerReplacement.py ListDatabaseAccounts RS1 HouseInfo
```

`ListDatabaseAccounts` shows the three managed accounts, their Enabled/Disabled state, password-rotation timing, last-rotation information, logical Vault paths, and complete browser-ready Vault URLs.

Use this command when the question is about database credentials or account state rather than database lifecycle state.

---

## 13. Customer-facing output philosophy

The public CLI is meant to behave like a service product rather than a Terraform console.

Normal users should see:

- whether a request was accepted;
- which deployment/database it applies to;
- whether work is Creating, Ready, Deleting, Unavailable, Running, blocked, or failed as appropriate to the resource;
- which normal status command to run next;
- account and credential information when they explicitly request account details;
- complete Vault browser URLs where credentials can be retrieved.

Normal users should not see routine:

- Git fetch/reset output;
- Terraform provider initialization;
- Terraform plans/state refreshes;
- resource IDs and state addresses;
- worker PIDs;
- internal operation IDs;
- raw Kubernetes implementation details used only for recovery.

Detailed implementation diagnostics are preserved in the operations log for administrators.

---

## 14. Runtime logs

Logging is convention-based and does not have a `[Logging]` section in `privateWorkerReplacement.config`.

Daily structured controller log:

```text
logs/controller/controller-YYYYMMDD.log
```

Daily detailed operation log:

```text
logs/operations/operations-YYYYMMDD.log
```

Asynchronous status state:

```text
logs/operations/state/<operation-id>.json
```

Temporary detached-worker transcripts may briefly exist in:

```text
logs/operations/work/
```

Both human-readable logs are append-only and roll to a new filename by UTC date. Git/Terraform stdout and stderr are captured in the operations log instead of being displayed on normal user terminals.

The JSON files are controller state, not customer log files. They support administrator `ListOperation`, interrupted-worker detection, automated acceptance-test polling, and recovery decisions.

The entire `logs/` tree is ignored by Git. Ordinary `git clean -fd` leaves ignored logs alone; deleting/recloning the repository or explicitly cleaning ignored files with `git clean -fdx` removes local runtime history/state.

Vault tokens and managed plaintext passwords must not intentionally be written to logs or operation state.

---

## 15. Errors and escalation

Expected validation failures are shown as concise `ERROR:` messages. Examples include:

- deployment does not exist;
- deployment is not ready;
- multiple deployments exist but the database target was omitted;
- destructive command is missing `--confirm`;
- ShardedCluster is busy with another managed change;
- shard deletion would reduce the cluster below one shard;
- a deployment still contains managed databases.

If a request fails because Git/Terraform execution failed, the customer receives a concise failure while the detailed diagnostic output is retained in:

```text
logs/operations/operations-YYYYMMDD.log
```

Normal customers should use service status commands rather than operation IDs. Platform administrators can use `privateWorkerReplacementAdmin.py ListOperations` and `ListOperation` when deeper diagnosis is needed.

---

## 16. Architecture boundary

**Terraform performs all managed changes.**

Python performs orchestration:

- parses and validates customer input;
- reads Vault-backed desired-state metadata;
- reads Kubernetes/MongoDB status;
- blocks unsafe operations;
- invokes Terraform;
- waits for reconciliation;
- verifies expected state and authentication;
- formats customer-facing results;
- records structured and diagnostic logs.

Managed mutations remain Terraform-driven directly or through:

```text
terraform-dbaas/scripts/lifecycle.sh
```

Python does not bypass Terraform to directly create/delete MongoDB deployments, users, databases, shards, deployment locks, or persistent storage.

---

## 17. Getting administrator help

Normal DBaaS work should remain in `privateWorkerReplacement.py`.

If deeper platform diagnostics or recovery are required, an authorized administrator can run:

```bash
python3 privateWorkerReplacementAdmin.py
```

or:

```bash
python3 privateWorkerReplacementAdmin.py --help
```

See `README-privateWorkerReplacementAdmin.md` for the administrator command reference and recovery rules.
