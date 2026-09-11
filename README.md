# MongoDB DBaaS Provisioning

Terraform-driven MongoDB Database as a Service proof of concept for ReplicaSet and ShardedCluster deployments managed through the MongoDB Kubernetes Operator, Ops Manager, Vault, Kubernetes, and Terraform-owned lifecycle scripts.

## Start here

Normal DBaaS users can run either form to show the full public help screen:

```bash
python3 privateWorkerReplacement.py
python3 privateWorkerReplacement.py --help
```

Platform administrators can run either form to show the full administrator help screen:

```bash
python3 privateWorkerReplacementAdmin.py
python3 privateWorkerReplacementAdmin.py --help
```

Running either executable with no command is intentional. It prints the full help text and exits successfully instead of returning an argparse `COMMAND` error.

Both controller CLIs assume the configuration file is in the current working directory:

```text
./dev.config
```

Use `--config FILE` only when a different configuration file is intentionally selected.

The complete customer/operator manual is:

```text
README-privateWorkerReplacement.md
```

The administrator/recovery manual is:

```text
README-privateWorkerReplacementAdmin.md
```

A fresh installation contains **zero user-facing ReplicaSets and zero user-facing ShardedClusters**. The user explicitly creates and names the MongoDB deployment they need.

## Quick example

Create a ReplicaSet:

```bash
python3 privateWorkerReplacement.py AddReplicaSet RS1
```

The request returns promptly while provisioning continues in the background. Check readiness with:

```bash
python3 privateWorkerReplacement.py ListReplicaSet RS1
```

After the ReplicaSet is Running, request a database:

```bash
python3 privateWorkerReplacement.py AddDatabase RS1 HouseInfo
```

Database creation is also asynchronous. Check database lifecycle status with:

```bash
python3 privateWorkerReplacement.py ListDatabase RS1 HouseInfo
```

`ListDatabase` reports the database itself, including lifecycle/service status such as `Creating`, `Ready`, `Deleting`, or `Unavailable` as applicable.

After the database is `Ready`, inspect its three managed accounts and Vault credential locations with:

```bash
python3 privateWorkerReplacement.py ListDatabaseAccounts RS1 HouseInfo
```

Every managed database receives exactly three accounts:

```text
HouseInfo_owner      -> dbOwner
HouseInfo_readWrite  -> readWrite
HouseInfo_read       -> read
```

`ListDatabaseAccounts` shows account status, password-rotation information, Vault secret paths, and complete Vault browser URLs.

## Two intentionally separate command interfaces

| Interface | Audience | Purpose |
| --- | --- | --- |
| `privateWorkerReplacement.py` | DBaaS user / customer demo | Deployments, shards, databases, credentials, and service status |
| `privateWorkerReplacementAdmin.py` | Platform administrator | Managed-resource inventory, operation diagnostics, Terraform reconciliation, and guarded recovery |

The public CLI deliberately hides operation IDs, worker PIDs, Terraform plans, Git activity, Kubernetes implementation details used only for troubleshooting, and recovery mechanics.

The administrator CLI exposes the diagnostics needed to operate and recover the service, but detailed Git/Terraform stdout and stderr are still written to the operations log instead of flooding the terminal.

The executable split is an interface boundary, not an authorization boundary. Production must still restrict administrator host, Kubernetes, Vault, and Terraform access.

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

A ShardedCluster is a deployment. Its individual shards are implemented by MongoDB as ReplicaSets, but customers manage the ShardedCluster as one DBaaS deployment.

## Public command summary

Deployment and shard lifecycle:

```text
AddReplicaSet REPLICASET
DeleteReplicaSet REPLICASET --confirm
ListReplicaSets
ListReplicaSet REPLICASET

AddShardedCluster SHARDED_CLUSTER [--shards N]
DeleteShardedCluster SHARDED_CLUSTER --confirm
ListShardedClusters
ListShardedCluster SHARDED_CLUSTER

ListDeployments
ListDeployment DEPLOYMENT

AddShard SHARDED_CLUSTER [COUNT]
DeleteShard SHARDED_CLUSTER [COUNT] --confirm
ListShards [SHARDED_CLUSTER]
```

Database and credential lifecycle:

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
RotatePasswords DEPLOYMENT DATABASE
RotatePasswords DATABASE
DisableOwner DEPLOYMENT DATABASE --confirm
DisableOwner DATABASE --confirm
EnableOwner DEPLOYMENT DATABASE
EnableOwner DATABASE
```

The one-argument database forms are valid only when exactly one managed deployment exists.

`ListDatabases` and `ListDatabase` report database-level lifecycle/service status. Account rows are intentionally excluded from those commands. `ListDatabaseAccounts` owns the three-account, rotation, Enabled/Disabled, and Vault credential view.

## Database status model

Database-level status can include:

```text
Creating
Ready
Deleting
Unavailable
```

`Creating` can be visible immediately after an asynchronous AddDatabase request, even before normal database inventory has been fully committed.

`Deleting` indicates an active asynchronous DeleteDatabase operation.

`Ready` requires the parent deployment to be healthy enough to serve the database. For a ShardedCluster, that includes all expected shards, config servers, and mongos being Online.

`Unavailable` means the database is known to the controller but its parent deployment is not sufficiently healthy for normal service.

## Asynchronous customer requests

These public commands return after the request is accepted and continue in a detached worker:

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

The acknowledgement tells the user what was requested and which normal service-status command to run. It does **not** expose an internal operation ID. Normal follow-up instructions use the friendly `./dev.config` path; detached workers resolve that path internally before running.

Examples:

```bash
python3 privateWorkerReplacement.py AddDatabase RS1 HouseInfo
python3 privateWorkerReplacement.py ListDatabase RS1 HouseInfo

python3 privateWorkerReplacement.py DeleteDatabase RS1 HouseInfo --confirm
python3 privateWorkerReplacement.py ListDatabases RS1
```

`RotatePasswords`, `DisableOwner`, and `EnableOwner` currently remain synchronous because the user normally needs the resulting credential/account state immediately. Their Terraform/Git implementation output is captured in the operations log rather than displayed on the terminal.

## ShardedCluster defaults and safety

`AddShardedCluster` uses the configured initial shard count when `--shards` is omitted. The current repository configuration uses **3 shards**.

```bash
python3 privateWorkerReplacement.py AddShardedCluster SC9
python3 privateWorkerReplacement.py AddShardedCluster SC9 --shards 5
```

`AddShard` and `DeleteShard` default to **1 shard** when COUNT is omitted.

```bash
python3 privateWorkerReplacement.py AddShard SC9
python3 privateWorkerReplacement.py AddShard SC9 2
python3 privateWorkerReplacement.py DeleteShard SC9 --confirm
python3 privateWorkerReplacement.py DeleteShard SC9 2 --confirm
```

A managed ShardedCluster can never be reduced below one shard. Shard deletion is allowed while application databases remain on the cluster. Terraform changes the desired shard count, MongoDB Operator/Ops Manager performs the supported reconciliation, and old shard storage is removed only after the remaining topology is healthy and removed shard workloads have released it.

Each ShardedCluster uses a Terraform-created deployment lock to serialize conflicting mutations. Read-only status commands remain available while a change is in progress.

## Database readiness

Before database creation, deletion, password rotation, or Owner disable:

- a ReplicaSet must be `Running`;
- a ShardedCluster must be `Running`;
- every expected shard must be `Online`;
- config servers must be `Online`;
- mongos must be `Online`;
- no conflicting managed ShardedCluster change may be active.

Unsafe requests fail before the requested managed change proceeds.

## Database credentials and Vault

Every database receives exactly:

```text
<Database>_owner      -> dbOwner on <Database>
<Database>_readWrite  -> readWrite on <Database>
<Database>_read       -> read on <Database>
```

Vault inventory is scoped by deployment, database, and user:

```text
mongodb/<Deployment>/<Database>/<Username>
```

For example:

```text
mongodb/RS1/HouseInfo/HouseInfo_owner
mongodb/RS1/HouseInfo/HouseInfo_readWrite
mongodb/RS1/HouseInfo/HouseInfo_read
```

For a Vault address of `http://127.0.0.1:8200` and the `secret` KV mount, the owner credential is displayed by `ListDatabaseAccounts` as a complete browser URL:

```text
http://127.0.0.1:8200/ui/vault/secrets/secret/show/mongodb/RS1/HouseInfo/HouseInfo_owner
```

The URL is generated from the configured Vault address and mount; users do not need to manually construct it.

Passwords rotate every configured rotation interval, currently 30 days. The Owner account is disabled in MongoDB at the first rotation at or after day 30, while its rotated credential remains managed in Vault. `EnableOwner` restores the Owner account using that existing managed credential; enabling the account does not rotate its password.

## Runtime logs

Logging uses a fixed convention rather than configuration-file options.

Daily structured controller events:

```text
logs/controller/controller-YYYYMMDD.log
```

Daily implementation/operation diagnostics:

```text
logs/operations/operations-YYYYMMDD.log
```

Asynchronous operation status records:

```text
logs/operations/state/<operation-id>.json
```

Temporary worker transcripts may briefly appear under:

```text
logs/operations/work/
```

The controller and operations logs are append-only for the UTC date. There is no `[Logging]` section, overwrite mode, configurable log directory, or configurable filename format in `dev.config`.

The JSON state files are not user logs; they are small machine-readable records used by `ListOperation`, interrupted-worker detection, the acceptance harness, and guarded recovery. The `logs/` tree is ignored by Git. Ordinary `git clean -fd` does not remove ignored files, but deleting/recloning the repository or explicitly cleaning ignored files such as with `git clean -fdx` removes local runtime history/state.

Controller code must not intentionally write Vault tokens or managed plaintext passwords to these logs.

## Administrator managed-resource check

After testing, cleanup, or a recovery operation, an administrator can inspect controller-managed deployment resources with:

```bash
python3 privateWorkerReplacementAdmin.py ListManagedResources
```

A clean environment reports:

```text
Managed deployments:             0
MongoDB resources:               0
MongoDB users:                   0
PVCs (Persistent Volume Claims): 0
PVs (Persistent Volumes):        0
Deployment locks:                0

Status: CLEAN
```

An active environment with legitimate managed resources reports:

```text
Status: MANAGED RESOURCES PRESENT
```

That status is informational. It does not, by itself, indicate a health problem.

## Testing

Show complete live-harness help without running tests:

```bash
python3 tests/run_harness.py
```

Fast unit/regression suite:

```bash
python3 -m unittest discover -s tests -p "test_*.py" -v
```

Safe read-only live preflight:

```bash
python3 tests/run_harness.py --profile preflight
```

Lifecycle profiles that create, modify, or delete temporary test resources require the explicit `--allow-changes` safety acknowledgement.

Complete live acceptance run:

```bash
python3 tests/run_harness.py --profile all --allow-changes
```

The complete live harness covers ReplicaSet lifecycle, ShardedCluster lifecycle, database/account lifecycle, password rotation, Owner disable, shard expansion/contraction, the one-shard minimum, storage cleanup, asynchronous request polling, and deployment-lock concurrency. It is fail-fast and cleans successful temporary scenarios.

See `tests/README.md` for profile-by-profile details.

## Architecture rule

**Terraform performs managed changes.**

Python parses/validates requests, reads desired state and live status, coordinates lifecycle steps, waits for convergence, reports customer/admin results, and records logs. Managed MongoDB, Kubernetes, Vault, storage, account, and lock mutations remain Terraform-driven directly or through:

```text
terraform-dbaas/scripts/lifecycle.sh
```

## Documentation map

| File | Purpose |
| --- | --- |
| `README-privateWorkerReplacement.md` | Complete DBaaS customer/user manual |
| `README-privateWorkerReplacementAdmin.md` | Platform administrator and recovery manual |
| `docs/CLI-INTERFACES.md` | Public/admin interface architecture boundary |
| `tests/README.md` | Unit and live acceptance testing guide |
| `dev.config` | Environment-specific runtime configuration |

Use the customer manual as the authoritative guide for normal DBaaS operation and the administrator manual for diagnostics/recovery.
