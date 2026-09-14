"""Repository-hygiene regression tests.

These checks protect the source tree from generated files, local caches,
credentials, Terraform runtime artifacts, and editor debris that should never
be committed. .gitignore prevents ordinary accidental adds; this test also
catches a forced add so CI can reject it before merge.
"""

# MAINTAINER READING GUIDE
# Enforces repository-level cleanup rules so retired names, runtime files, and other unwanted artifacts do not return.
# Treat these tests as executable design documentation. A failing assertion
# should identify which controller contract changed, not merely that text moved.


from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent

FORBIDDEN_TRACKED_PATTERNS = (
    r"(^|/)__pycache__(/|$)",
    r"\.py[cod]$",
    r"(^|/)\.pytest_cache(/|$)",
    r"(^|/)\.mypy_cache(/|$)",
    r"(^|/)\.ruff_cache(/|$)",
    r"(^|/)\.venv(/|$)",
    r"(^|/)venv(/|$)",
    r"(^|/)env(/|$)",
    r"(^|/)logs(/|$)",
    r"(^|/)\.runtime(/|$)",
    r"(^|/)\.terraform(/|$)",
    r"(^|/)\.terraform\.lock\.hcl$",
    r"terraform\.tfstate(?:\..*)?$",
    r"\.tfplan$",
    r"(^|/)crash(?:\..*)?\.log$",
    r"(^|/)\.env(?:\..*)?$",
    r"\.sw[opn]$",
    r"~$",
    r"(^|/)\.DS_Store$",
    r"(^|/)Thumbs\.db$",
)


class RepositoryHygieneTests(unittest.TestCase):
    """Keep generated/local-only artifacts out of Git history."""

    def test_no_generated_or_sensitive_local_artifacts_are_tracked(self) -> None:
        tracked = subprocess.check_output(
            ["git", "ls-files", "-z"],
            cwd=REPO_ROOT,
        ).decode("utf-8").split("\0")

        offenders = sorted(
            path
            for path in tracked
            if path
            and any(
                re.search(pattern, path, flags=re.IGNORECASE)
                for pattern in FORBIDDEN_TRACKED_PATTERNS
            )
        )

        self.assertEqual(
            offenders,
            [],
            "Generated/local-only files must not be committed: "
            + ", ".join(offenders),
        )


if __name__ == "__main__":
    unittest.main()
