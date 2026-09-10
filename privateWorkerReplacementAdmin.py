#!/usr/bin/env python3
"""Executable entry point for privateWorkerReplacement platform administration.

Normal DBaaS users should run privateWorkerReplacement.py. This separate
executable exposes administrator-only diagnostics, reconciliation, and guarded
recovery. The administrator CLI itself owns no-argument help behavior.
"""

from privateWorkerReplacement.admin_cli import main


if __name__ == "__main__":
    raise SystemExit(main())
