#!/usr/bin/env python3
"""Customer-facing entry point for the MongoDB DBaaS private worker replacement.

Normal DBaaS users and customer demonstrations should use this executable.

Administrator diagnostics, Terraform reconciliation, operation journals, and
recovery commands are intentionally separated into privateWorkerReplacementAdmin.py.

Keep this file tiny. Parsing and public behavior live in
privateWorkerReplacement/cli.py so the interface can be unit tested without
spawning a new Python process.
"""

# MAINTAINER READING GUIDE
# This file is only the customer entry point.
# 1. Python imports main() from privateWorkerReplacement/cli.py.
# 2. Lines below call main() and return its exit code to the shell.
# 3. Do not put lifecycle, Terraform, Vault, or Kubernetes logic here.
# When tracing a customer command, continue immediately in cli.py.


from privateWorkerReplacement.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
