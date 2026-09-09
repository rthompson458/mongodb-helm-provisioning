#!/usr/bin/env python3
"""Customer-facing entry point for the MongoDB DBaaS controller.

Normal DBaaS users and customer demonstrations should use this executable.

Administrator diagnostics, Terraform reconciliation, operation journals, and
recovery commands are intentionally separated into terraformControllerAdmin.py.

Keep this file tiny. Parsing and public controller behavior live in
terraform_controller/cli.py so the interface can be unit tested without
spawning a new Python process.
"""

from terraform_controller.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
