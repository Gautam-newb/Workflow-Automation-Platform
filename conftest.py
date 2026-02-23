"""
conftest.py  (repo root)

Adds the repository root to sys.path so that pytest can import
workflow_lambda as a package without needing `pip install -e .`.

This file must live at the repo root alongside pytest.ini.
It is loaded by pytest before any test collection begins.
"""

import sys
from pathlib import Path

# Insert repo root so `import workflow_lambda` resolves from anywhere
sys.path.insert(0, str(Path(__file__).parent))

