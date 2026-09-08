"""End-to-end ReplicaSet lifecycle scenario."""

from __future__ import annotations

from .runner import HarnessRunner


def run(runner: HarnessRunner) -> None:
    """Create, exercise, and clean up a temporary ReplicaSet and database."""

    ctx = runner.context
    rs = ctx.replica_set
    db = ctx.database

    runner.controller(
        "Create temporary ReplicaSet",
        "AddReplicaSet",
        rs,
        expected_text=f"ReplicaSet '{rs}' was successfully created",
        timeout=1800,
    )
    runner.controller(
        "List temporary ReplicaSet",
        "ListReplicaSet",
        rs,
        expected_text=rs,
    )
    runner.controller(
        "Create database on ReplicaSet",
        "AddDatabase",
        rs,
        db,
        expected_text=f"MongoDB database '{db}' was successfully created",
        timeout=900,
    )
    runner.controller(
        "List database on ReplicaSet",
        "ListDatabase",
        rs,
        db,
        expected_text=f"{db}_owner",
    )
    runner.controller(
        "Block ReplicaSet deletion while database exists",
        "DeleteReplicaSet",
        rs,
        "--confirm",
        expect_success=False,
        expected_text="contains managed databases",
    )
    runner.controller(
        "Rotate ReplicaSet database credentials",
        "RotatePasswords",
        rs,
        db,
        expected_text="Rotated all three passwords",
        timeout=900,
    )
    runner.controller(
        "Disable ReplicaSet Owner",
        "DisableOwner",
        rs,
        db,
        "--confirm",
        expected_text="is now Disabled",
        timeout=900,
    )
    runner.controller(
        "Delete ReplicaSet database",
        "DeleteDatabase",
        rs,
        db,
        "--confirm",
        expected_text="was successfully deleted",
        timeout=900,
    )
    runner.controller(
        "Delete temporary ReplicaSet",
        "DeleteReplicaSet",
        rs,
        "--confirm",
        expected_text=f"ReplicaSet '{rs}' was successfully deleted",
        timeout=1800,
    )
