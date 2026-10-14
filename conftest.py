"""Make the checkout root importable and reject cross-checkout imports.

The repository is commonly tested beside an older checkout.  Put this checkout
first, then verify the package that pytest will actually import comes from here;
otherwise a stale checkout can make a green test result meaningless.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
_ROOT_TEXT = str(ROOT)
if _ROOT_TEXT in sys.path:
    sys.path.remove(_ROOT_TEXT)
sys.path.insert(0, _ROOT_TEXT)


# importlib collection keeps identically named test files distinct, but does
# not add test directories to sys.path. Existing v3 tests share these bare
# sibling fixture imports; derive their search path from this checkout only.
_V3_FIXTURES = str(ROOT / "tests" / "v3")
if _V3_FIXTURES in sys.path:
    sys.path.remove(_V3_FIXTURES)
sys.path.insert(1, _V3_FIXTURES)


def _assert_autopilot_origins() -> None:
    package_root = (ROOT / "autopilot").resolve()
    for name, module in tuple(sys.modules.items()):
        if name != "autopilot" and not name.startswith("autopilot."):
            continue
        origin = getattr(module, "__file__", None)
        if origin is None:
            continue
        try:
            Path(origin).resolve().relative_to(package_root)
        except ValueError as exc:
            raise ImportError(
                f"{name} was imported from {origin}, outside {package_root}"
            ) from exc


# Import the package now, rather than merely checking sys.path text.  This
# catches a package that was already loaded from another checkout before pytest
# read this conftest, and the hooks below extend the check to its submodules.
importlib.import_module("autopilot")
_assert_autopilot_origins()


def pytest_sessionstart(session):
    _assert_autopilot_origins()


def pytest_collection_finish(session):
    _assert_autopilot_origins()


def pytest_runtest_setup(item):
    _assert_autopilot_origins()
