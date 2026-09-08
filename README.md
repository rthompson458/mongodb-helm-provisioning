# MongoDB DBaaS Provisioning

Terraform-driven MongoDB DBaaS proof of concept for ReplicaSet and ShardedCluster deployments managed through the MongoDB Kubernetes Operator, Ops Manager, Vault, and Kubernetes.

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

## ShardedCluster first-pass scope

The controller provisions and manages ShardedCluster infrastructure, including shard count, members per shard, mongos routers, config servers, status, and safe lifecycle checks.

Collection shard-key design and collection-level sharding policy are intentionally deferred until customer requirements are defined.

Database work on a ShardedCluster is blocked until:

- the MongoDB resource phase is `Running`,
- every expected shard is `Online`,
- config servers are `Online`,
- mongos is `Online`.

For this first pass, deleting a shard is allowed only when the ShardedCluster has zero managed application databases and a live MongoDB check also finds zero application databases.

## Documentation

See [README-terraformController.md](README-terraformController.md) for the complete command reference, lifecycle rules, logging configuration, storage model, Vault behavior, and Terraform architecture.
