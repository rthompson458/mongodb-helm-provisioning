#!/usr/bin/env python3
"""Executable entry point for terraformController platform administration.

Normal DBaaS users should run terraformController.py. This separate executable
exposes administrator-only diagnostics, reconciliation, and guarded recovery.
Running it with no arguments prints the full administrator help screen.
"""

from __future__ import annotations

import sys

from terraform_controller.admin_cli import build_parser, main


if __name__ == "__main__":
    if len(sys.argv) == 1:
        build_parser().print_help()
        raise SystemExit(0)
    raise SystemExit(main())
