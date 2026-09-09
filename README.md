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
python3 terraformController.py AddShardedCluster SC9 --shards 3
python3 terraformController.py ListDeployments
```

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

Add one shard:

```bash
python3 terraformController.py AddShard SC9
```

Add two shards:

```bash
python3 terraformController.py AddShard SC9 2
```

### Delete shards

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
Reconcile
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

See [README-terraformController.md](README-terraformController.md) for the complete command reference, lifecycle rules, logging configuration, storage model, Vault behavior, deployment locking, and Terraform architecture.


## Testing

Tests are split by responsibility instead of being kept in one large Python file.

Fast unit/regression suite:

```bash
python3 -m unittest discover -s tests -p "test_*.py" -v
```

Safe read-only live preflight:

```bash
python3 tests/run_harness.py
```

Full live lifecycle test against the configured development environment:

```bash
python3 tests/run_harness.py \
  --profile all \
  --allow-mutations \
  --allow-destructive
```

The live harness creates temporary resources with `TH...` names and covers
ReplicaSet lifecycle, ShardedCluster lifecycle, multi-shard add/delete,
database/account lifecycle, password rotation, Owner disable, shard removal while a database remains on the cluster, final-shard safety, global/targeted shard status, and concurrent ShardedCluster locking.

See [tests/README.md](tests/README.md) for the complete test map and safety rules.
