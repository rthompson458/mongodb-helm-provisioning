# terraformController Test Suite

The terraformController test suite has two jobs:

1. **Fast unit/regression tests** validate controller logic without touching a live MongoDB environment.
2. **The live end-to-end harness** drives the real customer CLI against Kubernetes, Ops Manager, Vault, Terraform, and MongoDB.

The live harness is intended for a development environment such as the local k3d environment. Do not point destructive profiles at production.

---

## 1. Fast unit/regression tests

Run all unit tests:

```bash
python3 -m unittest discover -s tests -p "test_*.py" -v
```

The unit suite covers command parsing, configuration validation, deployment/database lifecycle ordering, database lifecycle/status presentation, database-account presentation, ShardedCluster locking, Kubernetes status interpretation, logging, asynchronous operation state, maintenance/recovery, Vault inventory, and lifecycle-script regressions.

The suite also contains end-to-end CLI-entry-point regression tests for the no-argument behavior:

```bash
python3 terraformController.py
python3 terraformControllerAdmin.py
```

Both commands must print their full help text, exit with code `0`, and avoid the argparse `COMMAND` error.

GitHub Actions runs this suite as part of the repository validation workflow.

---

## 2. Live harness

The live harness entry point is:

```bash
python3 tests/run_harness.py --help
```

For mutating profiles, the real execution path is:

```text
Harness
  -> terraformController.py
  -> Python validation/orchestration
  -> detached worker for asynchronous customer requests
  -> Terraform
  -> lifecycle.sh where required
  -> Kubernetes / MongoDB Operator / Ops Manager / Vault / MongoDB
```

The harness does not treat an asynchronous request acknowledgement as success. It correlates the private operation-state record and polls `terraformControllerAdmin.py ListOperation` until the operation reports a terminal result.

That rule applies to deployment, shard, and database create/delete requests.

Database status and account status are tested separately. `ListDatabase` verifies database-level lifecycle/service state. `ListDatabaseAccounts` verifies the three managed account rows and credential-facing output.

---

## 3. Profiles

Supported profiles:

```text
preflight
replicaset
sharded
locking
all
```

### Preflight

Safe, read-only environment check:

```bash
python3 tests/run_harness.py --profile preflight
```

It verifies:

- customer CLI help renders;
- `ListDeployments` is readable;
- global `ListShards` is readable;
- Terraform is available;
- kubectl is available.

### ReplicaSet

```bash
python3 tests/run_harness.py \
  --profile replicaset \
  --allow-mutations \
  --allow-destructive
```

The ReplicaSet scenario:

1. asynchronously creates a temporary ReplicaSet;
2. waits for operation success and verifies ReplicaSet status;
3. asynchronously creates a database;
4. waits for operation success and verifies `ListDatabase` reports the database `Ready`;
5. verifies `ListDatabaseAccounts` reports the three managed accounts;
6. verifies ReplicaSet deletion is blocked while the database exists;
7. rotates all three database passwords;
8. disables the Owner account;
9. asynchronously deletes the database and waits for success;
10. asynchronously deletes the ReplicaSet and waits for success.

### ShardedCluster

```bash
python3 tests/run_harness.py \
  --profile sharded \
  --allow-mutations \
  --allow-destructive
```

The ShardedCluster scenario exercises:

- three-shard cluster creation;
- targeted and global shard status;
- 3 -> 5 shard expansion;
- asynchronous database creation;
- 5 -> 4 shard contraction while the database remains present;
- password rotation;
- Owner disable;
- asynchronous database deletion;
- 4 -> 1 shard contraction;
- blocking deletion of the final shard;
- ShardedCluster deletion.

### Locking

```bash
python3 tests/run_harness.py \
  --profile locking \
  --allow-mutations \
  --allow-destructive
```

This scenario verifies that the Terraform-created ShardedCluster deployment lock is observable and blocks conflicting database work while a background AddShard operation is active. It then waits for the AddShard operation to finish, verifies readable cluster status, and deletes the temporary cluster.

### Complete acceptance run

```bash
python3 tests/run_harness.py \
  --profile all \
  --allow-mutations \
  --allow-destructive
```

The current complete acceptance run contains **34 tests** across preflight, ReplicaSet, ShardedCluster, and locking profiles.

---

## 4. Safety flags

Mutating profiles require both:

```text
--allow-mutations
--allow-destructive
```

These flags are deliberately explicit. They prevent accidentally starting a destructive live scenario by typing only a profile name.

---

## 5. Useful options

Show stdout/stderr for passing tests as well as failures:

```bash
python3 tests/run_harness.py \
  --profile all \
  --allow-mutations \
  --allow-destructive \
  --verbose
```

Use another controller config:

```bash
python3 tests/run_harness.py \
  --profile preflight \
  --config /path/to/terraformController.config
```

Use another Python interpreter for harness-launched scripts:

```bash
python3 tests/run_harness.py \
  --profile preflight \
  --python /path/to/python3
```

Use a predictable temporary-name suffix:

```bash
python3 tests/run_harness.py \
  --profile replicaset \
  --suffix RSDEBUG01 \
  --allow-mutations \
  --allow-destructive
```

Without `--suffix`, the harness generates an `MMDDHHMMSS` value and uses temporary names such as:

```text
THRS-<suffix>
THSC-<suffix>
THDB_<suffix>
```

---

## 6. Result behavior

The harness is fail-fast. If a prerequisite test fails, dependent work and later profiles do not continue.

Long asynchronous tests print periodic progress:

```text
[WAIT] Test X of Y - <name> - elapsed HH:MM:SS - operation <id> still In Progress
```

The operation ID is appropriate in harness output because the harness is internal engineering tooling, not the customer interface.

At the end, the harness reports pass/fail totals plus elapsed time for each profile and the complete run.

A successful complete run should end with:

```text
HARNESS SUMMARY: 34 passed / 0 failed
```

---

## 7. Runtime evidence during testing

Structured controller events are written to:

```text
logs/controller/controller-YYYYMMDD.log
```

Detailed Git/Terraform and worker diagnostics are written to:

```text
logs/operations/operations-YYYYMMDD.log
```

Async operation state used by the harness is stored under:

```text
logs/operations/state/<operation-id>.json
```

Temporary worker transcripts may briefly exist under:

```text
logs/operations/work/
```

The daily files are append-only and use a UTC date. The harness reads the state records directly to correlate a public async request, then polls the operation through the administrator CLI.

---

## 8. Cleanup and failed runs

Successful lifecycle profiles delete the temporary resources they create.

After a failed run, temporary managed resources may intentionally remain so the failed state can be inspected. Do not manually delete controller-managed MongoDB/Vault/Kubernetes resources merely to make the next test start clean.

Use the normal Terraform-driven lifecycle/recovery path. An administrator can inspect zero-state with:

```bash
python3 terraformControllerAdmin.py ListManagedResources
```

Before a fresh complete acceptance run, the desired baseline is:

```text
Managed deployments:  0
MongoDB resources:    0
MongoDB users:        0
PVCs:                 0
PVs:                  0
Deployment locks:     0

Status: CLEAN
```

If managed resources intentionally exist, `ListManagedResources` reports `MANAGED RESOURCES PRESENT`; that status alone is not a failure. Destructive acceptance testing should still begin from the documented clean baseline so temporary test resources do not collide with existing managed state.

---

## 9. Recommended validation workflow after changes

For ordinary code/documentation changes:

```text
1. Run/observe GitHub Actions unit and static validation.
2. Confirm the development environment reports CLEAN before destructive testing.
3. Run the focused live profile if the change is narrow.
4. Run the complete 34-test `all` profile before calling a broad lifecycle or CLI-contract change accepted.
5. Verify ListManagedResources reports CLEAN after the run.
```

Changes to asynchronous execution, Terraform orchestration, logging, deployment locking, storage cleanup, database lifecycle, database status/account command semantics, or customer command behavior should receive a fresh complete live acceptance run because they cross multiple profiles.
