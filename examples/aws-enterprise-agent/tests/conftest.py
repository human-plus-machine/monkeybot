"""Pytest bootstrap for the example-level smoke tests.

The example ships an empty ``src/`` package (``src/__init__.py``) purely for
its Docker image layout. When these tests run they need the monorepo's real
``src/`` package on ``sys.path``. Deleting
``examples/aws-enterprise-agent/tests/__init__.py`` keeps pytest from walking
up to the example root and shadowing the monorepo — this conftest just adds
the repo root so ``from src.gateway...`` imports resolve.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]

if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
