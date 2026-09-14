# privateWorkerReplacement Test Suite

The privateWorkerReplacement test suite has two jobs:

1. **Fast unit/regression tests** validate controller logic without touching a live MongoDB environment.
2. **The live end-to-end harness** drives the real customer and administrator CLIs against Kubernetes, Ops Manager, Vault, Terraform, and MongoDB.

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
python3 privateWorkerReplacement.py
python3 privateWorkerReplacementAdmin.py
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

No live tests run when no arguments are supplied. An actual harness run requires an explicit `--profile`, `--admin`, or both.

`--help` is global harness help. For example, this is safe and does not run the locking profile:

```bash
python3 tests/run_harness.py --profile locking --help
```

For lifecycle profiles, the real execution path is:

```text
Harness
  -> privateWorkerReplacement.py
  -> Python validation/orchestration
  -> detached worker for asynchronous customer requests
  -> Terraform
  -> lifecycle.sh where required
  -> Kubernetes / MongoDB Operator / Ops Manager / Vault / MongoDB
```

The administrator suite also drives `privateWorkerReplacementAdmin.py` directly. It deliberately creates recoverable drift, invalid/valid stranded deployment locks, and orphaned Terraform state so the real recovery paths are exercised instead of only mocked.

The harness does not treat an asynchronous request acknowledgement as success.
It correlates the private operation-state record and polls
`privateWorkerReplacementAdmin.py ListOperation` until the operation reports a
terminal result. This applies to customer lifecycle work and to all three
long-running administrator mutations: `Reconcile`,
`RecoverDeploymentLock`, and `RecoverOrphanedResources`.

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

### ReplicaSet — 16 total checks

```bash
python3 tests/run_harness.py --profile replicaset --allow-changes
```

The ReplicaSet scenario adds 11 lifecycle checks after preflight. It creates a temporary ReplicaSet and database, verifies database/account status, verifies blocked deletion while the database exists, rotates credentials, disables and re-enables Owner, deletes the database, and deletes the temporary ReplicaSet.

### ShardedCluster — 21 total checks

```bash
python3 tests/run_harness.py --profile sharded --allow-changes
```

The ShardedCluster scenario adds 16 lifecycle checks after preflight. It also verifies the configured `max_shards_per_cluster` safety ceiling: cluster creation above the maximum is refused, and `AddShard` is refused when the resulting shard count would exceed the maximum. The remaining checks exercise cluster creation, shard status, allowed shard expansion, database creation, shard contraction, password rotation, Owner disable/re-enable, database deletion, final-shard protection, and cluster deletion.

### Locking — 11 total checks

```bash
python3 tests/run_harness.py --profile locking --allow-changes
```

The locking scenario adds 6 checks after preflight. It verifies that the Terraform-created ShardedCluster deployment lock appears during an active topology change, blocks conflicting work, disappears after completion, and leaves the cluster readable before cleanup.

### Administrator recovery suite — 67 total checks

The administrator suite is selected with a flag rather than a normal lifecycle profile:

```bash
python3 tests/run_harness.py --admin --allow-changes
```

It runs the 5 read-only preflight checks plus 62 administrator checks. The suite requires a **clean DBaaS starting inventory** because it intentionally damages and repairs the test environment. It verifies:

- administrator help, operation-journal reads, and unknown-operation handling;
- zero-state `ListManagedResources` and asynchronous no-op `Reconcile`;
- required confirmation on destructive recovery commands;
- live ReplicaSet/database creation for Reconcile testing;
- detection of a manually deleted `MongoDBUser`;
- asynchronous `Reconcile` recreation of the missing user;
- preservation of the existing Vault and Kubernetes passwords;
- real MongoDB authentication after Reconcile;
- `ListManagedResources` reporting `ATTENTION REQUIRED` for runtime drift;
- Reconcile refusal while a ShardedCluster deployment lock exists;
- refusal to recover a lock whose recorded target does not match desired state;
- successful asynchronous recovery of validated stranded AddShard and DeleteShard locks;
- a manufactured partial-destroy condition with empty Vault inventory but Terraform-tracked leftovers;
- `RecoverOrphanedResources` refusal while managed inventory still exists;
- refusal when Vault inventory is empty but a live managed MongoDB resource still exists;
- successful asynchronous orphan recovery after both independent safety checks pass;
- operation-journal polling for every long-running administrator mutation;
- final Kubernetes/Vault cleanup and a final `Status: CLEAN` inventory.

The suite stops on the first failure. A failed destructive test may intentionally leave its broken state available for diagnosis. A **passing** administrator run finishes clean.

`--admin` may also be added to a specific lifecycle profile. For example:

```bash
python3 tests/run_harness.py --profile replicaset --admin --allow-changes
```

The `all` profile already includes the full administrator suite, so adding `--admin` to `--profile all` does not duplicate the tests.

### Complete acceptance run — 100 total checks

Run the full gauntlet only when broad end-to-end acceptance is needed:

```bash
python3 tests/run_harness.py --profile all --allow-changes
```

This runs preflight, ReplicaSet, ShardedCluster, locking, and the complete administrator recovery suite.

The harness help derives displayed totals from the scenario `TEST_COUNT` constants. The CLI regression suite verifies the current 5/16/21/11/67/100 totals so documentation drift is caught quickly.

---

## 4. Safety flag

Every mutating lifecycle selection and the administrator suite require:

```text
--allow-changes
```

`--allow-changes` explicitly acknowledges that the harness may create, modify, deliberately damage, recover, and delete temporary test resources in the configured environment.

The flag is a deliberate safety gate. A mutating profile or `--admin` does **not** start until `--allow-changes` is present.

The read-only `preflight` profile does not require `--allow-changes`.

---

## 5. Remaining options

### `--config FILE`

Optional. The harness assumes the configuration file is in the current working directory:

```text
./dev.config
```

Use `--config FILE` only when another configuration file is intentionally selected:

```bash
python3 tests/run_harness.py --profile preflight --config ./alternate-dev.config
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
AdminRSTest-0910145230
AdminSCTest-0910145230
AdminDB_0910145230
OrphanRSTest-0910145230
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
HARNESS SUMMARY: 100 passed / 0 failed
```

---

## 7. Runtime evidence during testing

Structured controller events are written to:

```text
logs/controller/controller-YYYYMMDD.log
```

Detailed Terraform/external-command and worker diagnostics are written to:

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

Successful lifecycle profiles delete the temporary resources they create. A successful administrator suite additionally proves its test resources are absent from Kubernetes and Vault and finishes with `ListManagedResources` reporting `Status: CLEAN`.

The administrator suite requires a clean DBaaS inventory before it starts. This prevents its destructive recovery tests from adopting or deleting unrelated managed deployments.

After a failed run, temporary or deliberately damaged resources may intentionally remain so the failed state can be inspected. Do not manually delete controller-managed MongoDB/Vault/Kubernetes resources merely to make the next test start clean.

Use the normal Terraform-driven lifecycle/recovery path. An administrator can inspect managed resource state with:

```bash
python3 privateWorkerReplacementAdmin.py ListManagedResources
```

A zero-deployment environment can still show permanent controller infrastructure such as `tc-ops-manager-projects`, the Terraform backend state Secret, and the base `mongodb-development` Ops Manager project. Those entries are informational and do not prevent:

```text
Status: CLEAN
```

An environment with legitimate active DBaaS resources reports `MANAGED RESOURCES PRESENT`; that status alone is not a failure.

An orphan Ops Manager project, orphan `<PROJECT_ID>-group-secret`, a managed deployment missing its expected Ops Manager project, or a missing permanent Ops Manager platform project reports `ATTENTION REQUIRED`. Ops Manager inventory lines include project IDs so cleanup failures can be correlated across Kubernetes and Ops Manager.

---

## 9. Recommended validation workflow after changes

Do not run the complete 100-check harness after every small change.

Use this approach:

1. For documentation, display text, help text, or other presentation-only changes, rely on GitHub Actions/unit tests unless the change affects live behavior.
2. For a quick environment sanity check, run `--profile preflight`.
3. For narrow ReplicaSet/database lifecycle changes, run `--profile replicaset --allow-changes`.
4. For ShardedCluster/shard changes, run `--profile sharded --allow-changes`.
5. For deployment-lock/concurrency changes, run `--profile locking --allow-changes`.
6. For administrator inventory, Reconcile, recovery, or cross-plane consistency changes, run `--admin --allow-changes` from a clean DBaaS starting inventory.
7. Reserve `--profile all --allow-changes` for broad cross-cutting lifecycle changes, release/demo baselines, or other true acceptance milestones.

This keeps normal feedback fast while preserving the full 100-check run for the occasions when its broad coverage is actually valuable.
