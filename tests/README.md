# terraformController Test Suite

The tests are intentionally split into two layers.

## 1. Fast unit/regression tests

These run automatically in GitHub Actions and do **not** require a live
Kubernetes, Vault, Ops Manager, or MongoDB environment.

Files are separated by responsibility:

- `test_cli.py` - user command parsing and defaults.
- `test_config.py` - configuration validation.
- `test_database_lifecycle.py` - database/account lifecycle ordering and safety.
- `test_deployment_lifecycle.py` - ReplicaSet/ShardedCluster/shard behavior.
- `test_deployment_lock.py` - ShardedCluster mutation locking.
- `test_kube_status.py` - Kubernetes status interpretation.
- `test_logging.py` - JSON Lines logging.
- `test_vault_inventory.py` - Vault metadata reconstruction.
- `helpers.py` - shared test fixtures only.

Run them with:

```bash
python3 -m unittest discover -s tests -p "test_*.py" -v
```

## 2. Live end-to-end harness

The live harness executes `terraformController.py` exactly as an end user
would.  It is intended for the local k3d/Ops Manager/Vault environment.

### Safe read-only preflight

This is the default and does not create or delete resources:

```bash
python3 tests/run_harness.py
```

or:

```bash
python3 tests/run_harness.py --profile preflight
```

### Full live test

This creates temporary resources with names beginning with `TH`, exercises
them, and deletes them at the end:

```bash
python3 tests/run_harness.py \
  --profile all \
  --allow-mutations \
  --allow-destructive
```

The two safety flags are required intentionally.

### Individual live profiles

ReplicaSet only:

```bash
python3 tests/run_harness.py \
  --profile replicaset \
  --allow-mutations \
  --allow-destructive
```

ShardedCluster/shard lifecycle only:

```bash
python3 tests/run_harness.py \
  --profile sharded \
  --allow-mutations \
  --allow-destructive
```

Concurrent ShardedCluster lock test only:

```bash
python3 tests/run_harness.py \
  --profile locking \
  --allow-mutations \
  --allow-destructive
```

## What the full live harness covers

The harness verifies:

- CLI/status preflight.
- ReplicaSet creation and deletion.
- Database creation and deletion.
- Fixed Owner/ReadWrite/Read accounts.
- Password rotation.
- Owner disable.
- Blocking deployment deletion while a database exists.
- ShardedCluster creation and deletion.
- Targeted and global shard status.
- Adding multiple shards in one command.
- Deleting multiple shards in one command.
- Deleting a shard while a managed database remains on the ShardedCluster, then verifying database lifecycle operations still work.
- Preventing shard count from going below one.
- Concurrent-operation locking during a real AddShard operation.
- Readability of the cluster again after the lock is released.

## Important safety behavior

The harness never performs live mutations unless the user explicitly supplies
both `--allow-mutations` and `--allow-destructive`.

Use a development environment only.  Do not point this harness at a production
MongoDB environment.
