"""End-to-end ReplicaSet lifecycle scenario."""

from __future__ import annotations

from .runner import HarnessRunner


def run(runner: HarnessRunner) -> None:
    """Create, exercise, and clean up a temporary ReplicaSet and database.

    Stop as soon as a prerequisite step fails. Continuing after a failed create
    only produces cascade failures that hide the original defect.
    """

    ctx = runner.context
    rs = ctx.replica_set
    db = ctx.database

    created_rs = runner.controller(
        "Create temporary ReplicaSet",
        "AddReplicaSet",
        rs,
        expected_text=f"ReplicaSet '{rs}' was successfully created",
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

    created_db = runner.controller(
        "Create database on ReplicaSet",
        "AddDatabase",
        rs,
        db,
        expected_text=f"MongoDB database '{db}' was successfully created",
        timeout=900,
    )
    if not created_db.passed:
        return

    listed_db = runner.controller(
        "List database on ReplicaSet",
        "ListDatabase",
        rs,
        db,
        expected_text=f"{db}_owner",
    )
    if not listed_db.passed:
        return

    blocked_delete = runner.controller(
        "Block ReplicaSet deletion while database exists",
        "DeleteReplicaSet",
        rs,
        "--confirm",
        expect_success=False,
        expected_text="contains managed databases",
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

    deleted_db = runner.controller(
        "Delete ReplicaSet database",
        "DeleteDatabase",
        rs,
        db,
        "--confirm",
        expected_text="was successfully deleted",
        timeout=900,
    )
    if not deleted_db.passed:
        return

    runner.controller(
        "Delete temporary ReplicaSet",
        "DeleteReplicaSet",
        rs,
        "--confirm",
        expected_text=f"ReplicaSet '{rs}' was successfully deleted",
        timeout=1800,
    )
