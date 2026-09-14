#!/usr/bin/env python3
"""Executable entry point for privateWorkerReplacement platform administration.

Normal DBaaS users should run privateWorkerReplacement.py. This separate
executable exposes administrator-only diagnostics, reconciliation, and guarded
recovery. Read-only diagnostics run in the foreground. Potentially long-running
administrator mutations launch detached workers and return an Operation ID.
The administrator CLI itself owns no-argument help behavior.
"""

# MAINTAINER READING GUIDE
# This file is only the administrator entry point.
# 1. Python imports main() from privateWorkerReplacement/admin_cli.py.
# 2. admin_cli.py owns parsing and dispatch for diagnostics and recovery.
# 3. Keep normal customer lifecycle commands out of this entry point.


from privateWorkerReplacement.admin_cli import main


if __name__ == "__main__":
    raise SystemExit(main())
