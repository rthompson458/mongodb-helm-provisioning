# terraformController Test Suite

The terraformController test suite has two jobs:

1. **Fast unit/regression tests** validate controller logic without touching a live MongoDB environment.
2. **The live end-to-end harness** drives the real customer CLI against Kubernetes, Ops Manager, Vault, Terraform, and MongoDB.

The live harness is intended for a development environment such as the local k3d environment. Do not point lifecycle profiles at production.

---

## 1. Fast unit/regression tests

Run all unit tests:

```bash
python3 -m unittest discover -s tests -p "test_*.py" -v
```

The unit suite covers command parsing, configuration validation, deployment/database lifecycle ordering, database lifecycle/status presentation, database-account presentation, ShardedCluster locking, Kubernetes status interpretation, logging, asynchronous operation state, maintenance/recovery, Vault inventory, lifecycle-script regressions, and live-harness CLI behavior.

The suite also contains end-to-end CLI-entry-point regression tests for no-argument help behavior:

```bash
python3 terraformController.py
python3 terraformControllerAdmin.py
python3 tests/run_harness.py
```

All three commands must print their help text and exit successfully instead of returning an argparse error or starting work.

GitHub Actions runs the unit suite as part of the repository validation workflow.

---

## 2. Live harness

Run the harness with no arguments to show its complete help screen:

```bash
python3 tests/run_harness.py
```

This is intentionally equivalent to:

```bash
python3 tests/run_harness.py --help
```

No live tests run when no arguments are supplied. An actual harness run requires an explicit `--profile`.

For lifecycle profiles, the real execution path is:

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

Database status and account status are tested separately. `ListDatabase` verifies database-level lifecycle/service state. `ListDatabaseAccounts` verifies the three managed account rows and credential-facing output.

---

## 3. Profiles

Every profile starts with the same 5 read-only preflight checks. The selected profile then adds its own live checks.

### Preflight — 5 total checks

Safe and read-only:

```bash
python3 tests/run_harness.py --profile preflight
```

It verifies:

- customer CLI help renders;
- `ListDeployments` is readable;
- global `ListShards` is readable;
- Terraform is available;
- kubectl is available.

It creates, changes, and deletes nothing.

### ReplicaSet — 15 total checks

```bash
python3 tests/run_harness.py --profile replicaset --allow-changes
```

The ReplicaSet scenario adds 10 lifecycle checks after preflight. It creates a temporary ReplicaSet and database, verifies database/account status, verifies blocked deletion while the database exists, rotates credentials, disables Owner, deletes the database, and deletes the temporary ReplicaSet.

### ShardedCluster — 18 total checks

```bash
python3 tests/run_harness.py --profile sharded --allow-changes
```

The ShardedCluster scenario adds 13 lifecycle checks after preflight. It exercises cluster creation, shard status, shard expansion, database creation, shard contraction, password rotation, Owner disable, database deletion, final-shard protection, and cluster deletion.

### Locking — 11 total checks

```bash
python3 tests/run_harness.py --profile locking --allow-changes
```

The locking scenario adds 6 checks after preflight. It verifies that the Terraform-created ShardedCluster deployment lock appears during an active topology change, blocks conflicting work, disappears after completion, and leaves the cluster readable before cleanup.

### Complete acceptance run — 34 total checks

Run the full gauntlet only when broad end-to-end acceptance is needed:

```bash
python3 tests/run_harness.py --profile all --allow-changes
```

This runs preflight, ReplicaSet, ShardedCluster, and locking scenarios.

---

## 4. Safety flag

Every lifecycle profile (`replicaset`, `sharded`, `locking`, `all`) requires:

```text
--allow-changes
```

`--allow-changes` explicitly acknowledges that the harness may create, modify, and delete temporary test resources in the configured environment.

The flag is a deliberate safety gate. Naming a lifecycle profile by itself does **not** start that profile; the harness refuses to proceed until `--allow-changes` is present.

The read-only `preflight` profile does not require `--allow-changes`.

---

## 5. Remaining options

### `--config FILE`

Optional. The harness assumes the configuration file is in the current directory:

```text
./terraformController.config
```

Use `--config FILE` only when the configuration file is somewhere else:

```bash
python3 tests/run_harness.py --profile preflight --config /other/location/terraformController.config
```

### `--verbose`

Optional. Passing checks normally show concise PASS/FAIL information. Use `--verbose` when you also want stdout/stderr for successful commands:

```bash
python3 tests/run_harness.py --profile preflight --verbose
```

The old public `--python` and `--suffix` options were removed. The harness automatically uses the same Python interpreter that launched it and automatically generates a unique internal run ID.

Temporary live-test resources use readable generated names such as:

```text
RSTest-0910145230
SCTest-0910145230
LockTest-0910145230
DBTest_0910145230
```

The numeric portion is generated automatically for each run so interrupted-test leftovers do not collide with later runs.

---

## 6. Result behavior

The harness is fail-fast. If a prerequisite test fails, dependent work and later profiles do not continue.

Long asynchronous tests print periodic progress:

```text
[WAIT] Test X of Y - <name> - elapsed HH:MM:SS - operation <id> still In Progress
```

The operation ID is appropriate in harness output because the harness is internal engineering tooling, not the customer interface.

At the end, the harness reports pass/fail totals plus elapsed time for each profile and the complete run.

A successful complete run ends with:

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

Use the normal Terraform-driven lifecycle/recovery path. An administrator can inspect managed resource state with:

```bash
python3 terraformControllerAdmin.py ListManagedResources
```

A completely empty environment reports:

```text
Managed deployments:             0
MongoDB resources:               0
MongoDB users:                   0
PVCs (Persistent Volume Claims): 0
PVs (Persistent Volumes):        0
Deployment locks:                0

Status: CLEAN
```

An environment with legitimate managed resources reports `MANAGED RESOURCES PRESENT`; that status alone is not a failure.

---

## 9. Recommended validation workflow after changes

Do not run the complete 34-check harness after every small change.

Use this approach:

1. For documentation, display text, help text, or other presentation-only changes, rely on GitHub Actions/unit tests unless the change affects live behavior.
2. For a quick environment sanity check, run `--profile preflight`.
3. For narrow ReplicaSet/database lifecycle changes, run `--profile replicaset --allow-changes`.
4. For ShardedCluster/shard changes, run `--profile sharded --allow-changes`.
5. For deployment-lock/concurrency changes, run `--profile locking --allow-changes`.
6. Reserve `--profile all --allow-changes` for broad cross-cutting lifecycle changes, release/demo baselines, or other true acceptance milestones.

This keeps normal feedback fast while preserving the full 34-check run for the occasions when its broad coverage is actually valuable.
