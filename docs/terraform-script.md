# Terraform / Helm MongoDB Management

The Terraform module in `terraform-dbaas/` is the desired-state engine used by
`privateWorkerReplacement`.

The integrated database-management path reuses the useful Helm pattern from the
parallel Terraform work while keeping the controller's existing service model:

```text
Deployment
  ReplicaSet or ShardedCluster
    Database
      <Database>_owner
      <Database>_readWrite
      <Database>_read
```

There is no separate mission object in the current implementation. If a
higher-level grouping is later confirmed as a requirement, it should be added
above databases without changing the database-specific account boundary.

## Responsibilities

`terraform-dbaas/mongodb-chart/` owns logical MongoDB database materialization
and controlled database operations.

- `mongodb-database-job.yaml` creates missing placeholder collections so
  MongoDB retains managed logical databases.
- `mongodb-database-operation-job.yaml` performs controlled destructive
  database operations before additive provisioning runs.
- The operation hook keeps the protected `admin`, `config`, and `local`
  databases out of the management path.
- Destructive operations require the Terraform
  `allow_destructive_mongodb_operations` gate. The public CLI sets that gate
  only after its normal `--confirm` check has accepted DeleteDatabase.

`terraform-dbaas/scripts/lifecycle.sh` remains responsible for work that fits
better as bounded infrastructure/runtime checks: local storage lifecycle,
deployment locks, deployment-empty validation, and real credential
authentication verification.

## One source of truth

The Helm chart does not carry a second database inventory. Terraform derives
`local.mongodb_databases` from the same `deployments` object populated from
the controller's Vault-backed desired state. This keeps ReplicaSets,
ShardedClusters, databases, accounts, and Vault metadata in one model.

The chart contract intentionally keeps the recognizable
`mongodbDatabases`, `mongodbDatabaseOperations`, and
`mongodb_management` names so future collection-management work can extend
the same path without creating another implementation.
