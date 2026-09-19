"""Make the repo root importable so `import autopilot` works from pytest.

`takeout2` is imported the same way by the existing v2 suite, so this mirrors the
established layout rather than inventing one.
"""
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
