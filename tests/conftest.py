"""Make the plugin importable as `dispatcharr_sports_filter` when running pytest
from inside the repo directory. The repo dir IS the package, so we put its
parent on sys.path.
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_DIR = os.path.dirname(_HERE)
_PARENT = os.path.dirname(_REPO_DIR)

if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)
