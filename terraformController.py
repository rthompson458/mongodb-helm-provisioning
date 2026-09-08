#!/usr/bin/env python3
"""Executable entry point for terraformController.

Keep this file intentionally tiny.  All parsing and controller behavior lives
inside terraform_controller/cli.py so the implementation can be unit tested
without spawning a new Python process.
"""

from terraform_controller.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
