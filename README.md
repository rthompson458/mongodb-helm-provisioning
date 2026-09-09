# MongoDB DBaaS Provisioning

Terraform-driven MongoDB DBaaS proof of concept for ReplicaSet and ShardedCluster deployments managed through the MongoDB Kubernetes Operator, Ops Manager, Vault, Kubernetes, and Terraform-owned lifecycle scripts.

## Start here

The user-facing local controller is:

```bash
python3 terraformController.py --help
```

A fresh installation contains **zero user-facing ReplicaSets and zero user-facing ShardedClusters**. The user creates and names the required MongoDB deployment.

Examples:

```bash
python3 terraformController.py AddReplicaSet RS1
python3 terraformController.py AddShardedCluster SC9
python3 terraformController.py ListDeployments
```

`AddShardedCluster` uses the initial shard count stored in controller
configuration when `--shards` is omitted. The current configured default is
**3 shards**. Use `--shards N` to override it for one request.

After a deployment is fully ready:

```bash
python3 terraformController.py AddDatabase RS1 HouseInfo
python3 terraformController.py AddDatabase SC9 Orders
```

If exactly one managed deployment exists, the deployment name can be omitted:

```bash
python3 terraformController.py AddDatabase HouseInfo
```

If more than one deployment exists, the controller requires an explicit target.



### Two intentionally separate command interfaces

| Interface | Audience | Purpose |
| --- | --- | --- |
| `terraformController.py` | DBaaS user / customer demo | Normal deployments, shards, databases, credentials, and service status |
| `terraformControllerAdmin.py` | Platform administrator | Managed-resource inventory, operation diagnostics, Terraform reconciliation, and guarded recovery |

Normal DBaaS users should not need to know about Terraform operation journals,
worker processes, recovery locks, or orphaned controller state.

Administrators can verify a clean controller zero-state with:

```bash
python3 terraformControllerAdmin.py ListManagedResources
```

## Background deployment and topology processing

Long-running deployment/topology requests return control to the DBaaS user after
the request is accepted:

```text
AddReplicaSet
DeleteReplicaSet
AddShardedCluster
DeleteShardedCluster
AddShard
DeleteShard
```

Example:

```bash
python3 terraformController.py DeleteShard SC9 1 --confirm
```

The public response is deliberately service-oriented:

```text
DeleteShard request accepted.

ShardedCluster: SC9
Status:         Topology change requested

The request is being processed in the background.

Check service status with:
  python3 terraformController.py ... ListShards SC9
```

Internal operation IDs, worker PIDs, journal paths, and recovery commands are
available only through `terraformControllerAdmin.py`.

Background execution does **not** change the architecture boundary: managed
MongoDB, Kubernetes, Vault, and storage mutations remain Terraform-driven.

## Managed database accounts

Every managed application database gets exactly three accounts:

```text
<Database>_owner      -> dbOwner
<Database>_readWrite  -> readWrite
<Database>_read       -> read
```

Current credentials are stored in Vault under:

```text
mongodb/<Deployment>/<Database>/<Username>
```

Example:

```text
mongodb/SC9/HouseInfo/HouseInfo_owner
mongodb/SC9/HouseInfo/HouseInfo_readWrite
mongodb/SC9/HouseInfo/HouseInfo_read
```

## ShardedCluster support

The controller provisions and manages ShardedCluster infrastructure, including shard count, members per shard, mongos routers, config servers, status, persistent storage, and safe lifecycle checks.

Collection shard-key design and collection-level sharding policy remain intentionally deferred until customer requirements are defined.

### Shard status

All shards across all managed ShardedClusters:

```bash
python3 terraformController.py ListShards
```

One ShardedCluster:

```bash
python3 terraformController.py ListShards SC9
```

The targeted view also reports config-server, mongos, and active managed-change status.

### Add shards

If no count is supplied, `AddShard` defaults to **1 shard**.

Add one shard:

```bash
python3 terraformController.py AddShard SC9
```

Add two shards:

```bash
python3 terraformController.py AddShard SC9 2
```

### Delete shards

If no count is supplied, `DeleteShard` defaults to **1 shard**.
`--confirm` remains required.

Delete one shard:

```bash
python3 terraformController.py DeleteShard SC9 --confirm
```

Delete two shards:

```bash
python3 terraformController.py DeleteShard SC9 2 --confirm
```

A ShardedCluster can never be reduced below **one shard**.

Shard deletion is supported while application databases remain on the ShardedCluster. The operation remains Terraform-driven: Terraform lowers the managed ShardedCluster `shardCount`, the MongoDB Kubernetes Operator/Ops Manager reconciles the supported scale-down, and Terraform cleans the removed shard storage only after the remaining cluster is fully ready and the removed shard StatefulSets are gone.

Python does not directly issue MongoDB shard-removal commands or directly delete MongoDB/Kubernetes topology resources.

### Managed-change locking

Each ShardedCluster uses one atomic deployment lock for mutating operations.

While a shard add/delete or database/credential mutation is active on SC9, conflicting changes on SC9 are blocked. Read-only commands remain available.

Examples of blocked concurrent mutations include:

```text
AddDatabase
DeleteDatabase
RotatePasswords
DisableOwner
AddShard
DeleteShard
```

The lock itself is created and released by the Terraform lifecycle script. Python only requests the Terraform operation and reads lock/status information.

## Readiness

Database work on a ShardedCluster is blocked until:

- the MongoDB resource phase is `Running`,
- every expected shard is `Online`,
- config servers are `Online`,
- mongos is `Online`,
- no conflicting managed operation holds the ShardedCluster deployment lock.

## Controller logging

Logging is configured in `terraformController.config`:

```ini
[Logging]
enabled = true
level = INFO
directory = logs
mode = append
filename_format =
```

With a blank `filename_format`, the controller writes `Controller.log`.
A relative `directory` is resolved from the location of the config file, so
the default `logs` value means `<repository>/logs`.

`mode` may be `append` or `overwrite`.

A formatted name can use standard `strftime` tokens, for example:

```ini
filename_format = Controller-%Y%m%d-%H%M.log
```

where `%m` is month and `%M` is minute.

## Documentation

Customer / DBaaS user guide:

```text
README-terraformController.md
```

Platform administrator / recovery guide:

```text
README-terraformControllerAdmin.md
```

Interface-boundary architecture note:

```text
docs/CLI-INTERFACES.md
```

Test and acceptance-harness guide:

```text
tests/README.md
```

## Testing

The project has two testing layers:

1. fast unit/regression tests that do not require a live MongoDB environment;
2. a live end-to-end harness that drives the real `terraformController.py` CLI against the configured development environment.

### Fast unit/regression suite

```bash
python3 -m unittest discover -s tests -p "test_*.py" -v
```

### Live harness help

Show every supported harness option:

```bash
python3 tests/run_harness.py --help
```

### Live harness profiles

Safe read-only preflight, which is also the default:

```bash
python3 tests/run_harness.py
```

or:

```bash
python3 tests/run_harness.py --profile preflight
```

ReplicaSet lifecycle only:

```bash
python3 tests/run_harness.py \
  --profile replicaset \
  --allow-mutations \
  --allow-destructive
```

ShardedCluster and shard lifecycle only:

```bash
python3 tests/run_harness.py \
  --profile sharded \
  --allow-mutations \
  --allow-destructive
```

Concurrent ShardedCluster deployment-lock test only:

```bash
python3 tests/run_harness.py \
  --profile locking \
  --allow-mutations \
  --allow-destructive
```

Complete live acceptance run:

```bash
python3 tests/run_harness.py \
  --profile all \
  --allow-mutations \
  --allow-destructive
```

The mutating profiles intentionally require both safety flags:

```text
--allow-mutations
--allow-destructive
```

### Useful harness options

Print stdout/stderr for passing steps as well as failures:

```bash
python3 tests/run_harness.py \
  --profile all \
  --allow-mutations \
  --allow-destructive \
  --verbose
```

Use a different controller configuration file:

```bash
python3 tests/run_harness.py \
  --profile preflight \
  --config /path/to/terraformController.config
```

Use a specific Python interpreter when the harness launches `terraformController.py`:

```bash
python3 tests/run_harness.py \
  --profile preflight \
  --python /usr/bin/python3
```

Use a predictable suffix for temporary test resources:

```bash
python3 tests/run_harness.py \
  --profile replicaset \
  --suffix RSDEBUG01 \
  --allow-mutations \
  --allow-destructive
```

The default suffix is generated from the current time as `MMDDHHMMSS` and is used in temporary names such as:

```text
THRS-<suffix>
THSC-<suffix>
THDB_<suffix>
```

### What the full harness covers

The complete `all` profile runs:

```text
preflight
  -> ReplicaSet lifecycle
  -> ShardedCluster lifecycle
  -> deployment-lock/concurrency lifecycle
```

Coverage includes ReplicaSet and ShardedCluster creation/deletion, real controller-admin authentication readiness, database creation/deletion, Owner/ReadWrite/Read accounts, password rotation, Owner disable, blocking deployment deletion while databases exist, targeted/global shard status, a 3 -> 5 -> 4 -> 1 shard lifecycle, deleting a shard while a database remains, one-shard minimum enforcement, asynchronous-operation polling, and concurrent mutation locking.

The harness is **fail-fast**. If a prerequisite step fails, dependent steps and later profiles are not started. Each check is numbered as `Test X of Y`, long asynchronous checks print periodic `[WAIT]` progress, every result includes per-test elapsed time, and the final summary includes per-profile and total elapsed time.

Successful live profiles clean up their temporary resources. After a failed run, temporary resources may remain so the failed state can be inspected. Use normal `terraformController.py`/Terraform lifecycle commands to clean controller-managed resources rather than manually deleting them from Kubernetes.

See [tests/README.md](tests/README.md) for the full harness operator guide, including profile-by-profile behavior, all option combinations, cleanup guidance, result interpretation, and recommended testing workflows.
