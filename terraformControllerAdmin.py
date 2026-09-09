#!/usr/bin/env python3
"""Executable entry point for terraformController platform administration.

Normal DBaaS users should run terraformController.py.

This separate executable exposes administrator-only diagnostics, reconciliation,
and carefully guarded recovery commands. Keeping this entry point small makes
the interface easy to audit while the implementation remains unit testable in
terraform_controller/admin_cli.py.
"""

from terraform_controller.admin_cli import main


if __name__ == "__main__":
    raise SystemExit(main())
