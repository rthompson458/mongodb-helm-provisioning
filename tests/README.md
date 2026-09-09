# terraformController Test Suite

The terraformController test suite has two different jobs:

1. **Fast unit/regression tests** validate controller logic without touching a live MongoDB environment.
2. **The live end-to-end harness** drives the real `terraformController.py` CLI against Kubernetes, Ops Manager, Vault, Terraform, and MongoDB.

The live harness is intentionally configurable so it can be used as a quick health check, a focused feature test, or a complete acceptance run.

---

## 1. Fast unit/regression tests

These run automatically in GitHub Actions and do **not** require a live Kubernetes, Vault, Ops Manager, or MongoDB environment.

Files are separated by responsibility:

- `test_cli.py` - user command parsing and defaults.
- `test_config.py` - configuration validation.
- `test_database_lifecycle.py` - database/account lifecycle ordering and safety.
- `test_deployment_lifecycle.py` - ReplicaSet/ShardedCluster/shard behavior.
- `test_deployment_lock.py` - ShardedCluster mutation locking.
- `test_kube_status.py` - Kubernetes status interpretation.
- `test_lifecycle_script.py` - lifecycle shell behavior and regression cases.
- `test_logging.py` - JSON Lines logging.
- `test_maintenance.py` - maintenance/reconcile behavior.
- `test_vault_inventory.py` - Vault metadata reconstruction.
- `helpers.py` - shared test fixtures only.

Run all unit/regression tests:

```bash
python3 -m unittest discover -s tests -p "test_*.py" -v
```

These tests are the fastest first check after changing Python, Terraform contract logic, or the lifecycle shell script.

---

# 2. Live end-to-end harness

The live harness runs:

```text
tests/run_harness.py
```

It launches the real:

```text
terraformController.py
```

commands exactly as an end user would.

For mutating profiles, that means the real path is exercised:

```text
Harness
  -> terraformController.py
  -> Python validation/orchestration
  -> Terraform
  -> lifecycle.sh where required
  -> Kubernetes / MongoDB Operator / Ops Manager / Vault / MongoDB
```

The live harness is intended for a development environment such as the local k3d environment. Do **not** point destructive harness profiles at production.

---

## Get help

The complete command-line option list is always available with:

```bash
python3 tests/run_harness.py --help
```

The supported profiles are:

```text
preflight
replicaset
sharded
locking
all
```

---

# 3. Quick command matrix

| Goal | Command |
|---|---|
| Safe read-only health check | `python3 tests/run_harness.py` |
| Explicit read-only health check | `python3 tests/run_harness.py --profile preflight` |
| Read-only health check with full command output | `python3 tests/run_harness.py --profile preflight --verbose` |
| ReplicaSet lifecycle only | `python3 tests/run_harness.py --profile replicaset --allow-mutations --allow-destructive` |
| ShardedCluster/shard lifecycle only | `python3 tests/run_harness.py --profile sharded --allow-mutations --allow-destructive` |
| Deployment-lock/concurrency test only | `python3 tests/run_harness.py --profile locking --allow-mutations --allow-destructive` |
| Complete acceptance run | `python3 tests/run_harness.py --profile all --allow-mutations --allow-destructive` |
| Complete run with all stdout/stderr | add `--verbose` |
| Use another config file | add `--config /path/to/config` |
| Use another Python interpreter | add `--python /path/to/python3` |
| Force predictable temporary names | add `--suffix SOMEVALUE` |

---

# 4. Profile: preflight

## Purpose

`preflight` is the **safe, read-only** profile.

It is also the default profile, so these are equivalent:

```bash
python3 tests/run_harness.py
```

and:

```bash
python3 tests/run_harness.py --profile preflight
```

## What it checks

Preflight verifies basic controller and environment availability, including:

- CLI help renders.
- `ListDeployments` is readable.
- global `ListShards` is readable.
- Terraform executable is available.
- `kubectl` executable is available.

It does not intentionally create, modify, or delete DBaaS resources.

## Best uses

Use preflight:

- before starting live acceptance testing;
- after restarting k3d, Vault, Ops Manager, or the MongoDB Operator;
- after changing configuration;
- as a quick morning environment check;
- when you only want to confirm the controller can talk to its dependencies.

## Verbose preflight

To see stdout/stderr from passing checks as well as failures:

```bash
python3 tests/run_harness.py \
  --profile preflight \
  --verbose
```

---

# 5. Profile: replicaset

## Command

```bash
python3 tests/run_harness.py \
  --profile replicaset \
  --allow-mutations \
  --allow-destructive
```

## What it exercises

The ReplicaSet profile performs a real lifecycle test:

1. Create a temporary ReplicaSet.
2. Wait for the MongoDB deployment to become Running.
3. Wait for the hidden controller administrator to reconcile.
4. Verify the controller administrator can perform a **real MongoDB authentication**.
5. Read the temporary ReplicaSet with `ListReplicaSet`.
6. Create a temporary database.
7. Create and verify the three fixed database accounts:
   - `<DB>_owner`
   - `<DB>_readWrite`
   - `<DB>_read`
8. Read the database with `ListDatabase`.
9. Verify ReplicaSet deletion is blocked while the managed database exists.
10. Rotate all three database passwords.
11. Disable the Owner account.
12. Delete the database.
13. Verify account cleanup.
14. Delete the temporary ReplicaSet.

## Best uses

Use this profile when changing:

- ReplicaSet provisioning;
- database creation/deletion;
- MongoDBUser handling;
- Vault credential handling;
- rotation logic;
- Owner disable logic;
- ReplicaSet cleanup;
- controller-admin authentication/readiness.

It is much faster and more focused than running the entire `all` profile when your change only affects ReplicaSets or shared database lifecycle code.

---

# 6. Profile: sharded

## Command

```bash
python3 tests/run_harness.py \
  --profile sharded \
  --allow-mutations \
  --allow-destructive
```

## What it exercises

The ShardedCluster profile performs a real sharded lifecycle:

1. Create a temporary ShardedCluster with two shards.
2. Wait for the cluster, shards, config servers, and mongos to become ready.
3. Verify the hidden controller administrator can authenticate.
4. Verify targeted shard status.
5. Verify global shard status.
6. Add two shards in one command.
7. Verify the resulting four-shard topology.
8. Create a database on the ShardedCluster.
9. Create and authenticate the Owner/ReadWrite/Read accounts.
10. Delete one shard **while the database still exists**.
11. Verify database credential rotation still works after topology change.
12. Disable the Owner account.
13. Delete the database.
14. Delete two more shards, leaving exactly one.
15. Verify deletion of the final shard is blocked.
16. Delete the ShardedCluster.

## Best uses

Use this profile when changing:

- ShardedCluster creation/deletion;
- shard status reporting;
- `AddShard`;
- `DeleteShard`;
- multi-shard operations;
- storage expansion/contraction;
- DB lifecycle on a ShardedCluster;
- minimum-shard safeguards;
- post-topology-change credential behavior.

---

# 7. Profile: locking

## Command

```bash
python3 tests/run_harness.py \
  --profile locking \
  --allow-mutations \
  --allow-destructive
```

## What it exercises

This is the concurrency/deployment-lock test.

The harness:

1. Creates a one-shard temporary ShardedCluster.
2. Starts `AddShard` in the background.
3. Watches for the Terraform-created deployment-lock ConfigMap.
4. While `AddShard` still owns the lock, attempts a concurrent `AddDatabase`.
5. Confirms the database mutation is rejected with the expected busy/managed-change error.
6. Waits for the background `AddShard` to finish.
7. Confirms the lock is released and shard status is readable again.
8. Deletes the temporary ShardedCluster.

## Best uses

Use `locking` when changing:

- deployment locking;
- concurrent operation protection;
- `AddShard` orchestration;
- lock creation/release;
- interrupted or resumed topology operations.

---

# 8. Profile: all

## Command

```bash
python3 tests/run_harness.py \
  --profile all \
  --allow-mutations \
  --allow-destructive
```

This is the full live acceptance run.

It executes:

```text
preflight
  -> ReplicaSet lifecycle
  -> ShardedCluster lifecycle
  -> locking/concurrency lifecycle
```

Use `all`:

- before calling a development milestone complete;
- before merging a major lifecycle change;
- after changes that touch common Terraform/lifecycle code;
- after a significant environment rebuild;
- when you want the broadest live validation.

---

# 9. Safety flags

All live mutation profiles require **both**:

```text
--allow-mutations
--allow-destructive
```

This applies to:

```text
replicaset
sharded
locking
all
```

For example, this is intentionally rejected:

```bash
python3 tests/run_harness.py --profile replicaset
```

The correct command is:

```bash
python3 tests/run_harness.py \
  --profile replicaset \
  --allow-mutations \
  --allow-destructive
```

The flags are deliberately explicit because the harness creates and deletes real resources.

`preflight` does not require them because it is read-only.

---

# 10. Verbose mode

By default:

- every step prints `[PASS]` or `[FAIL]`;
- detailed stdout/stderr is printed automatically for failures;
- passing commands stay compact.

To print full stdout/stderr for **passing and failing** commands:

```bash
python3 tests/run_harness.py \
  --profile all \
  --allow-mutations \
  --allow-destructive \
  --verbose
```

Verbose mode is useful when:

- debugging a lifecycle issue;
- capturing detailed acceptance evidence;
- reviewing Terraform plans/applies;
- investigating timing or readiness behavior.

For routine acceptance runs, non-verbose mode is easier to read.

---

# 11. Custom configuration file

The default config is:

```text
<repository>/terraformController.config
```

To use another configuration:

```bash
python3 tests/run_harness.py \
  --profile preflight \
  --config /home/rich/some-other-controller.config
```

For a mutating profile:

```bash
python3 tests/run_harness.py \
  --profile replicaset \
  --config /home/rich/some-other-controller.config \
  --allow-mutations \
  --allow-destructive
```

This is useful when testing:

- another k3d cluster;
- another namespace;
- alternate storage configuration;
- alternate Vault settings;
- another development environment.

---

# 12. Custom Python interpreter

By default the harness uses the same Python executable that launched `run_harness.py`.

To force another interpreter:

```bash
python3 tests/run_harness.py \
  --profile preflight \
  --python /usr/bin/python3
```

Or, for example, a virtual environment:

```bash
python3 tests/run_harness.py \
  --profile replicaset \
  --python /home/rich/venv/bin/python \
  --allow-mutations \
  --allow-destructive
```

This option controls the interpreter used when the harness launches `terraformController.py`.

---

# 13. Temporary-name suffix

Every live run generates a suffix used to keep its temporary resources unique.

The default is the current local time in:

```text
MMDDHHMMSS
```

For example:

```text
0909123624
```

can produce names such as:

```text
THRS-0909123624
THSC-0909123624
THDB_0909123624
```

To choose the suffix yourself:

```bash
python3 tests/run_harness.py \
  --profile replicaset \
  --suffix MYTEST01 \
  --allow-mutations \
  --allow-destructive
```

A custom suffix is useful when:

- you want predictable test names;
- correlating a run with logs;
- repeating a specific test case;
- distinguishing two developers' runs.

Choose a suffix that keeps generated Kubernetes names valid and reasonably short.

---

# 14. Combining options

Options may be combined.

For example, a verbose focused ReplicaSet test using a custom config and predictable suffix:

```bash
python3 tests/run_harness.py \
  --profile replicaset \
  --config /home/rich/terraformController-test/terraformController.config \
  --python /usr/bin/python3 \
  --suffix RSDEBUG01 \
  --allow-mutations \
  --allow-destructive \
  --verbose
```

A full acceptance run with a predictable identifier:

```bash
python3 tests/run_harness.py \
  --profile all \
  --suffix ACCEPT01 \
  --allow-mutations \
  --allow-destructive
```

---

# 15. Fail-fast behavior

The live harness is intentionally **fail-fast**.

If a prerequisite fails, dependent steps are not executed.

Example:

```text
Create ReplicaSet -> PASS
Create database   -> FAIL

STOP
```

The harness will **not** continue with:

```text
ListDatabase
RotatePasswords
DisableOwner
DeleteDatabase
ShardedCluster scenario
locking scenario
```

This matters because those later checks would only be cascade failures caused by the original problem.

For `--profile all`, a failed ReplicaSet scenario prevents the ShardedCluster and locking scenarios from starting. A failed ShardedCluster scenario prevents the locking scenario from starting.

The final summary therefore points to the earliest meaningful defect.

---

# 16. What happens to resources after a failed run?

On a successful lifecycle profile, the harness deletes the temporary resources it created.

On a **failed** run, resources may remain because fail-fast intentionally stops before unrelated cleanup commands can hide or complicate the original state.

After a failed run, inspect the environment before starting another live test:

```bash
python3 terraformController.py ListDeployments
```

and:

```bash
kubectl get mongodb,mongodbusers -n mongodb
```

If a temporary test deployment remains, clean it up through the normal terraformController/Terraform lifecycle rather than manually deleting controller-managed Kubernetes objects.

Examples:

```bash
python3 terraformController.py DeleteReplicaSet THRS-<suffix> --confirm
```

or:

```bash
python3 terraformController.py DeleteShardedCluster THSC-<suffix> --confirm
```

If a failed run physically materialized a database but did not finish recording it in controller inventory, inspect the state carefully before deleting the deployment. Do not bypass Terraform-managed cleanup unless there is a specific recovery reason.

---

# 17. Reading the result

A successful run ends with a summary similar to:

```text
====================================================================
HARNESS SUMMARY: 20 passed / 0 failed
====================================================================
```

A failure ends with something similar to:

```text
====================================================================
HARNESS SUMMARY: 5 passed / 1 failed
====================================================================
Failed steps:
  - Create temporary ReplicaSet
```

The process returns:

- exit code `0` when all executed checks pass;
- exit code `1` when one or more checks fail.

That makes the harness usable manually and from higher-level automation.

---

# 18. Recommended testing workflows

## Quick environment check

```bash
python3 tests/run_harness.py
```

Use this when you only need to know whether the controller and basic dependencies are reachable.

## Focused ReplicaSet development

```bash
python3 tests/run_harness.py \
  --profile replicaset \
  --allow-mutations \
  --allow-destructive
```

## Focused sharding development

```bash
python3 tests/run_harness.py \
  --profile sharded \
  --allow-mutations \
  --allow-destructive
```

## Focused concurrency/locking development

```bash
python3 tests/run_harness.py \
  --profile locking \
  --allow-mutations \
  --allow-destructive
```

## Final acceptance

First run the fast tests:

```bash
python3 -m unittest discover -s tests -p "test_*.py" -v
```

Then run the full live harness:

```bash
python3 tests/run_harness.py \
  --profile all \
  --allow-mutations \
  --allow-destructive
```

---

# 19. Important safety behavior

The harness never performs live lifecycle mutations unless the user explicitly selects a mutating profile and supplies both safety flags.

Even with the flags, use the harness only against a development/test DBaaS environment.

Before a full live run, a clean application baseline is preferred:

```text
ReplicaSets:      0
ShardedClusters:  0
Managed DBs:      0
MongoDBUsers:     0
```

Platform infrastructure such as the MongoDB Operator, Ops Manager, Vault, Terraform backend state infrastructure, and StorageClasses is expected to remain running.
