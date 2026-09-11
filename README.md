# MongoDB DBaaS Provisioning

Terraform-driven MongoDB Database as a Service proof of concept for ReplicaSet and ShardedCluster deployments managed through the MongoDB Kubernetes Operator, Ops Manager, Vault, Kubernetes, Terraform, and an integrated Helm database-management chart.

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

After the ReplicaSet is `Running`, request a database:

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

The public CLI deliberately hides operation IDs, worker PIDs, Terraform plans, raw implementation diagnostics, Kubernetes implementation details used only for troubleshooting, and recovery mechanics.

The administrator CLI exposes the diagnostics needed to operate and recover the service, but detailed Terraform/external-command stdout and stderr are still written to the operations log instead of flooding the terminal.

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

Every command also has command-specific help:

```bash
python3 privateWorkerReplacement.py AddDatabase --help
python3 privateWorkerReplacement.py DeleteShard --help
python3 privateWorkerReplacementAdmin.py ListManagedResources --help
```

Regression tests require every supported public and administrator subcommand to retain a meaningful description and runnable example.

## Database status model

Database-level status can include:

```text
Creating
Ready
Deleting
Unavailable
```

`Creating` can be visible immediately after an asynchronous `AddDatabase` request, even before normal database inventory has been fully committed.

`Deleting` indicates an active asynchronous `DeleteDatabase` operation.

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

Deployment deletion success includes cross-plane cleanup. After the MongoDB resource is absent, the controller deletes the deployment's Ops Manager project, waits for Ops Manager to confirm its removal, and removes the matching `<PROJECT_ID>-group-secret` before the worker can report success.

Examples:

```bash
python3 privateWorkerReplacement.py AddDatabase RS1 HouseInfo
python3 privateWorkerReplacement.py ListDatabase RS1 HouseInfo

python3 privateWorkerReplacement.py DeleteDatabase RS1 HouseInfo --confirm
python3 privateWorkerReplacement.py ListDatabases RS1
```

`RotatePasswords`, `DisableOwner`, and `EnableOwner` remain synchronous because the user normally needs the resulting credential/account state immediately. Detailed Terraform/external-command implementation diagnostics are captured in the operations log instead of displayed on the terminal.

## ShardedCluster defaults and safety

`AddShardedCluster` uses the configured initial shard count when `--shards` is omitted. The current `dev.config` uses **3 shards**.

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

Before database creation, deletion, password rotation, Owner disable, or Owner enable:

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

The configured rotation interval is **30 days in the supplied `dev.config`**. `ListDatabaseAccounts` shows when rotation is due; this POC does not run an internal scheduler. Rotation occurs when `RotatePasswords` is invoked manually or by future external automation. At the first rotation at or after one full configured interval from database creation, the Owner account is disabled in MongoDB while its rotated credential remains managed in Vault. `EnableOwner` restores the Owner account using that existing managed credential; enabling the account does not rotate its password.

## Runtime source and cache

The current runtime does **not** fetch Terraform from Git or depend on a remote repository. The project-local source directory is authoritative:

```text
terraform-dbaas/
```

Before each Terraform transaction, the controller refreshes a disposable execution cache from that local source:

```text
.runtime/terraform-cache/
```

The cache preserves Terraform's `.terraform/` provider directory but refreshes the remaining source files so renamed/deleted files do not remain stale. Git is still useful for normal source control, but it is not a controller runtime prerequisite.

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

After testing, cleanup, or a recovery operation, an administrator can inspect the authoritative DBaaS inventory across Vault, Kubernetes, Terraform state, and Ops Manager with:

```bash
python3 privateWorkerReplacementAdmin.py ListManagedResources
```

The command reports active ReplicaSets/ShardedClusters, databases, managed accounts, MongoDB/MongoDBUser resources, DBaaS PVCs/PVs, controller Secrets/ConfigMaps, deployment locks, Ops Manager DBaaS projects and group Secrets, orphan/missing Ops Manager artifacts, and permanent controller infrastructure.

Ops Manager entries include the project/group ID so an administrator can match a project to its `<PROJECT_ID>-group-secret`.

Permanent controller infrastructure such as `tc-ops-manager-projects`, the Terraform backend state Secret, and the base `mongodb-development` Ops Manager project remains visible but does not prevent:

```text
Status: CLEAN
```

Legitimate active DBaaS resources report:

```text
Status: MANAGED RESOURCES PRESENT
```

Cross-plane leftovers or mismatches—including orphan Ops Manager projects, orphan group Secrets, managed deployments missing an Ops Manager project, **or the permanent Ops Manager platform project itself being missing**—report:

```text
Status: ATTENTION REQUIRED
```

The command is read-only; it does not delete or repair anything.

## Testing

Fast unit/regression suite:

```bash
python3 -m unittest discover -s tests -p "test_*.py" -v
```

Show complete live-harness help without running tests:

```bash
python3 tests/run_harness.py
```

Current live profile totals are derived from scenario definitions and protected by CLI regression tests:

```text
preflight    5
replicaset  16
sharded     19
locking     11
all         36
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

The complete live harness covers ReplicaSet lifecycle, ShardedCluster lifecycle, database/account lifecycle, password rotation, Owner disable/re-enable, shard expansion/contraction, the one-shard minimum, storage cleanup, asynchronous request polling, and deployment-lock concurrency. It is fail-fast and cleans successful temporary scenarios.

See `tests/README.md` for profile-by-profile details.

## Architecture rule

**Terraform owns normal DBaaS desired-state changes.**

Python parses/validates requests, reconstructs desired state from Vault, reads live status, coordinates lifecycle steps, waits for convergence, reports customer/admin results, and records logs. Normal managed MongoDB, Vault, storage, account, and lock mutations remain Terraform-driven directly or through:

```text
terraform-dbaas/scripts/lifecycle.sh
```

Deployment teardown has one deliberate cross-plane cleanup exception: Python removes the per-deployment Ops Manager project and verifies deletion of its Operator-created `<PROJECT_ID>-group-secret`. Those artifacts are created outside Terraform desired state, so teardown must explicitly retire them before reporting success.

Logical database materialization/deletion is invoked by Terraform through the integrated Helm chart in:

```text
terraform-dbaas/mongodb-chart/
```

### Python module responsibilities

| Module | Responsibility |
| --- | --- |
| `cli.py` | Public command definitions, help, parsing, async submission, dispatch |
| `admin_cli.py` | Administrator command definitions, help, parsing, dispatch |
| `deployments.py` | ReplicaSet/ShardedCluster/shard mutation workflows |
| `deployment_status.py` | Read-only deployment and shard status presentation |
| `databases.py` | Database/account/credential mutation workflows |
| `database_status.py` | Read-only database and account status presentation |
| `credential_display.py` | Shared read-only Vault path/URL presentation |
| `deployment_lock.py` | ShardedCluster mutation lock acquisition/release/validation |
| `maintenance.py` | Cross-plane inventory classification, Reconcile, guarded recovery |
| `admin_status.py` | Administrator inventory/status formatting |
| `ops_manager.py` | Ops Manager project inventory/deletion and group-secret cleanup |
| `terraform_runner.py` | Local Terraform source refresh, execution lock, init/apply |
| `kube.py` | Read-only Kubernetes status/query/wait helpers |
| `vault.py` | Read-only Vault inventory reconstruction |
| `async_operations.py` | Detached operation journal/worker coordination |
| `logging_component.py` | Structured controller logs and detailed operation diagnostics |
| `runtime_paths.py` | Predictable log/state/cache path conventions |

This separation is deliberate: mutation modules should not also become status/UI modules, and read-only status modules should not mutate managed service state.

## Documentation map

| File | Purpose |
| --- | --- |
| `README-privateWorkerReplacement.md` | Complete DBaaS customer/user manual |
| `README-privateWorkerReplacementAdmin.md` | Platform administrator and recovery manual |
| `docs/CLI-INTERFACES.md` | Public/admin interface architecture boundary |
| `tests/README.md` | Unit and live acceptance testing guide |
| `dev.config` | Environment-specific runtime configuration |

Use the customer manual as the authoritative guide for normal DBaaS operation and the administrator manual for diagnostics/recovery.
