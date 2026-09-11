"""End-to-end ReplicaSet lifecycle scenario."""

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

    created_rs = runner.controller_async(
        "Create temporary ReplicaSet",
        "AddReplicaSet",
        rs,
        timeout=1800,
    )
    if not created_rs.passed:
        return

    listed_rs = runner.controller(
        "List temporary ReplicaSet",
        "ListReplicaSet",
        rs,
        expected_text=rs,
    )
    if not listed_rs.passed:
        return

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

    listed_db = runner.controller(
        "List database status on ReplicaSet",
        "ListDatabase",
        rs,
        db,
        expected_text="Status:          Ready",
    )
    if not listed_db.passed:
        return

    listed_accounts = runner.controller(
        "List database accounts on ReplicaSet",
        "ListDatabaseAccounts",
        rs,
        db,
        expected_text=f"{db}_owner",
    )
    if not listed_accounts.passed:
        return

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

    runner.controller_async(
        "Delete temporary ReplicaSet",
        "DeleteReplicaSet",
        rs,
        "--confirm",
        timeout=1800,
    )
