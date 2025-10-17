# Test configuration ensuring the application package is importable.
# Adds the repository root to sys.path so `import app` works when running pytest
# directly from the project directory (common in this repo).

import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
