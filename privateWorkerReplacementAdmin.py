#!/usr/bin/env python3
"""Executable entry point for terraformController platform administration.

Normal DBaaS users should run terraformController.py. This separate executable
exposes administrator-only diagnostics, reconciliation, and guarded recovery.
The administrator CLI itself owns no-argument help behavior.
"""

from terraform_controller.admin_cli import main


if __name__ == "__main__":
    raise SystemExit(main())
