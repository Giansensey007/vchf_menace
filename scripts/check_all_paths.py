#!/usr/bin/env python3
"""CLI wrapper: force DRY_RUN then run src.check_paths."""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ["DRY_RUN"] = "true"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.check_paths import main

if __name__ == "__main__":
    sys.exit(main())
