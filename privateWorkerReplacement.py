#!/usr/bin/env python3
"""Customer-facing entry point for the MongoDB DBaaS private worker replacement.

Normal DBaaS users and customer demonstrations should use this executable.

Administrator diagnostics, Terraform reconciliation, operation journals, and
recovery commands are intentionally separated into privateWorkerReplacementAdmin.py.

Keep this file tiny. Parsing and public behavior live in
privateWorkerReplacement/cli.py so the interface can be unit tested without
spawning a new Python process.
"""

from privateWorkerReplacement.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
