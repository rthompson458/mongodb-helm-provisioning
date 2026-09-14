"""Serialize controller-wide desired-state mutations on one worker host.

Why this exists:
Terraform receives the complete Vault-backed deployment inventory on every apply.
If two controller workers independently load that inventory and then overlap,
the older worker can later re-apply a stale snapshot and accidentally remove
resources created by the newer worker.

The Terraform execution lock in terraform_runner.py prevents two Terraform
processes from running at the same instant, but it cannot prevent that
read-modify-wait-apply stale-snapshot race. This broader lock therefore covers
the complete mutating controller operation, including its waits and any later
verification applies.

Read-only status commands do not take this lock.

The lock is scoped to the configured Terraform backend and uses Linux/WSL
flock. It is process-safe on the current single private-worker host and is
released automatically if a worker exits. A future multi-host worker design
must replace or supplement this local lock with a distributed equivalent.
"""

# MAINTAINER READING GUIDE
# This is the BROADEST local controller lock.
# It covers the complete read -> modify -> apply -> wait -> verify workflow.
# It is different from:
# - terraform_runner.py execution lock: protects the shared Terraform workdir.
# - deployment_lock.py lock: protects one ShardedCluster from conflicting work.
# This lock prevents an older worker from later applying a stale Vault inventory.


from __future__ import annotations

import fcntl
import os
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .logging_component import log_event


def _lock_path(config: dict[str, Any]) -> Path:
    """Return the backend-scoped local desired-state mutation lock path."""

    cache: Path = config["terraform_cache"]
    scope = (
        f"{config['backend_namespace']}-{config['backend_secret_suffix']}"
    )
    safe_scope = re.sub(r"[^A-Za-z0-9_.-]+", "-", scope)
    return cache.parent / f".privateWorkerReplacement-state-{safe_scope}.lock"


@contextmanager
def controller_state_mutation_lock(
    config: dict[str, Any],
    operation: str,
) -> Iterator[None]:
    """Serialize one complete controller mutation against the shared inventory.

    The caller must acquire this lock before loading mutable desired state.
    Holding it until the command finishes guarantees that no second mutating
    worker can commit a newer inventory while the first worker still retains an
    older snapshot that may be used by a later Terraform apply.
    """

    path = _lock_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("a+", encoding="utf-8") as handle:
        log_event(
            "controller_state_lock.waiting",
            operation=operation,
            lock_file=str(path),
        )
        # Wait here BEFORE the business function loads mutable Vault inventory.
        # Acquiring this lock later would still allow two workers to capture
        # different snapshots and replay an older snapshot after a newer apply.
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)

        try:
            handle.seek(0)
            handle.truncate()
            handle.write(f"pid={os.getpid()}\noperation={operation}\n")
            handle.flush()

            log_event(
                "controller_state_lock.acquired",
                operation=operation,
                lock_file=str(path),
                pid=os.getpid(),
            )
            yield
        finally:
            log_event(
                "controller_state_lock.released",
                operation=operation,
                lock_file=str(path),
                pid=os.getpid(),
            )
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
