"""Live test-harness package for privateWorkerReplacement.

The files in this package exercise the public CLI exactly as an end user would.
They are intentionally separate from the fast unit tests.
"""

# MAINTAINER READING GUIDE
# Package marker for the live end-to-end harness. No scenario logic should live here.
# Treat these tests as executable design documentation. A failing assertion
# should identify which controller contract changed, not merely that text moved.

