from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from .common import ControllerError, run_process


def _require(*names: str) -> None:
    missing = [name for name in names if not shutil.which(name)]
    if missing: raise ControllerError(?