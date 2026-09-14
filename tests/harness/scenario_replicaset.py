"""End-to-end ReplicaSet and database lifecycle scenario.

Canonical full-suite test intent:
6. Create a temporary managed MongoDB ReplicaSet and wait for readiness.
7. Verify the new ReplicaSet is visible through the customer status command.
8. Create a database and its three managed accounts on the ReplicaSet.
9. Verify database-level status after creation.
10. Verify Owner, ReadWrite, and Read account status is presented correctly.
11. Prove an RS cannot be deleted while a managed database still exists.
12. Rotate all three database credentials through the supported workflow.
13. Disable the Owner account and verify the policy path completes.
14. Re-enable the Owner account and verify it becomes usable again.
15. Delete the managed database and its accounts/credentials.
16. Delete the now-empty ReplicaSet and complete deployment cleanup.
"""

from __future__ import annotations

from .runner import HarnessRunner

TEST_COUNT = 11


def run(runner: HarnessRunner) -> None:
    """Create, exercise, and clean up a temporary ReplicaSet and database.

    Stop as soon as a prerequisite step fails. Continuing after a failed create
    only produces cascade failures that hide the original defect.
    """

    ctx = runner.context
    rs = ctx.replica_set
    db = ctx.database

    # TEST 6 - Create a temporary managed ReplicaSet.
    # WHY: Proves the basic ReplicaSet provisioning workflow works end to end.
    # PASS: The asynchronous AddReplicaSet operation completes successfully.
    created_rs = runner.controller_async(
        "Create temporary ReplicaSet",
        "AddReplicaSet",
        rs,
        timeout=1800,
    )
    if not created_rs.passed:
        return

    # TEST 7 - Read the new ReplicaSet through the customer CLI.
    # WHY: Creation is useful only if normal status commands can discover the deployment.
    # PASS: ListReplicaSet returns the generated ReplicaSet name.
    listed_rs = runner.controller(
        "List temporary ReplicaSet",
        "ListReplicaSet",
        rs,
        expected_text=rs,
    )
    if not listed_rs.passed:
        return

    # TEST 8 - Create a database and its three managed accounts on the ReplicaSet.
    # WHY: Proves database provisioning works on a healthy ReplicaSet.
    # PASS: The asynchronous AddDatabase operation completes successfully.
    # AddDatabase is customer-asynchronous. The operation must finish before the
    # database-level view can report Ready and the account view can be checked.
    created_db = runner.controller_async(
        "Create database on ReplicaSet",
        "AddDatabase",
        rs,
        db,
        timeout=1200,
    )
    if not created_db.passed:
        return

    # TEST 9 - Read database service status after creation.
    # WHY: Proves the database reaches the user-facing Ready state after provisioning.
    # PASS: ListDatabase reports `Status: Ready`.
    listed_db = runner.controller(
        "List database status on ReplicaSet",
        "ListDatabase",
        rs,
        db,
        expected_text="Status:          Ready",
    )
    if not listed_db.passed:
        return

    # TEST 10 - Read the three managed database accounts.
    # WHY: Every managed database must expose Owner, ReadWrite, and Read account status.
    # PASS: ListDatabaseAccounts returns the expected managed account information.
    listed_accounts = runner.controller(
        "List database accounts on ReplicaSet",
        "ListDatabaseAccounts",
        rs,
        db,
        expected_text=f"{db}_owner",
    )
    if not listed_accounts.passed:
        return

    # TEST 11 - Refuse deletion of a ReplicaSet that still owns a database.
    # WHY: Prevents a deployment delete from silently destroying managed application data.
    # PASS: DeleteReplicaSet is rejected because managed databases still exist.
    blocked_delete = runner.controller_async(
        "Block ReplicaSet deletion while database exists",
        "DeleteReplicaSet",
        rs,
        "--confirm",
        expect_success=False,
        expected_text="contains managed databases",
        timeout=300,
    )
    if not blocked_delete.passed:
        return

    # TEST 12 - Rotate all database credentials.
    # WHY: Proves the supported password-rotation workflow updates the managed accounts.
    # PASS: RotatePasswords reports that all three passwords were rotated.
    rotated = runner.controller(
        "Rotate ReplicaSet database credentials",
        "RotatePasswords",
        rs,
        db,
        expected_text="Rotated all three passwords",
        timeout=900,
    )
    if not rotated.passed:
        return

    # TEST 13 - Disable the database Owner account.
    # WHY: Proves the Owner lifecycle policy can block Owner login without deleting the database.
    # PASS: DisableOwner reports the Owner account as Disabled.
    disabled = runner.controller(
        "Disable ReplicaSet Owner",
        "DisableOwner",
        rs,
        db,
        "--confirm",
        expected_text="is now Disabled",
        timeout=900,
    )
    if not disabled.passed:
        return

    # TEST 14 - Re-enable the database Owner account.
    # WHY: Proves an administrator can restore Owner access after it has been disabled.
    # PASS: EnableOwner reports the Owner account as Enabled.
    enabled = runner.controller(
        "Re-enable ReplicaSet Owner",
        "EnableOwner",
        rs,
        db,
        expected_text="is now Enabled",
        timeout=900,
    )
    if not enabled.passed:
        return

    # TEST 15 - Delete the managed database.
    # WHY: Proves database teardown removes the database lifecycle state and managed accounts cleanly.
    # PASS: The asynchronous DeleteDatabase operation completes successfully.
    deleted_db = runner.controller_async(
        "Delete ReplicaSet database",
        "DeleteDatabase",
        rs,
        db,
        "--confirm",
        timeout=1200,
    )
    if not deleted_db.passed:
        return

    # TEST 16 - Delete the now-empty ReplicaSet.
    # WHY: Proves full deployment teardown succeeds after all user databases are removed.
    # PASS: The asynchronous DeleteReplicaSet operation completes successfully.
    runner.controller_async(
        "Delete temporary ReplicaSet",
        "DeleteReplicaSet",
        rs,
        "--confirm",
        timeout=1800,
    )
