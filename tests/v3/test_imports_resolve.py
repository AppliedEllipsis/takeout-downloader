"""Import every module, and catch the name-before-import mistake directly.

Written because I made the same mistake **three times in one session**: added code
using `re` to `mint.py`, then `transport.py`, then `ledger.py`, without importing
it. `python -m py_compile` accepted all three — it only checks syntax, not names.

The first two surfaced only by running the entry point; the third sat in a branch
nothing exercised (the filename rule) and was found by a test that happened to run
it. That is an expensive way to find a missing import, so this file finds them.

Two checks:
  1. every module imports cleanly (catches module-level breakage);
  2. every stdlib module *used* in a file is actually imported in that file
     (catches the `re.compile` with no `import re` shape).
"""
import ast
import importlib
import pkgutil
import sys
from pathlib import Path

import pytest

WT = Path(__file__).resolve().parents[2]
SRC = WT / "autopilot"
if str(WT) not in sys.path:
    sys.path.insert(0, str(WT))

import autopilot  # noqa: E402

#: Stdlib modules this package actually reaches for. Kept explicit rather than
#: inferred, so the check cannot silently become vacuous.
WATCHED = (
    "re", "os", "json", "asyncio", "sqlite3", "hashlib", "errno", "time",
    "shutil", "tempfile", "subprocess", "urllib", "pathlib", "dataclasses",
    "typing", "datetime", "contextlib", "collections", "itertools", "math",
)


def _module_names():
    return sorted(m.name for m in pkgutil.iter_modules(autopilot.__path__)
                  if not m.name.startswith("_"))


def _source_files():
    return sorted(SRC.glob("*.py"))


def test_every_module_imports():
    for name in _module_names():
        importlib.import_module(f"autopilot.{name}")


def _bound_names(tree: ast.AST) -> set:
    """Every name the file binds: imports, assignments, defs, params, targets."""
    bound = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                bound.add(alias.asname or alias.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound.add(node.id)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, ast.alias):
            bound.add((node.asname or node.name).split(".")[0])
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            pass
    return bound


@pytest.mark.parametrize("path", _source_files(), ids=lambda p: p.name)
def test_every_stdlib_module_used_is_imported(path):
    """The `re.compile(...)` with no `import re` shape, three times over."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    bound = _bound_names(tree)

    # A name is "used as a module" when it appears as `name.attr`.
    used_as_module = {
        node.value.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
    }

    missing = sorted((used_as_module & set(WATCHED)) - bound)
    assert not missing, (
        f"{path.name} uses {missing} as a module but never imports it. "
        "py_compile will not catch this — only running the branch does.")


def test_the_watchlist_actually_matches_reality():
    """Guard the guard: if none of the watched names were in use the check would
    pass vacuously and catch nothing. Assert a concrete, non-trivial subset."""
    in_use = set()
    for path in _source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        in_use |= {
            node.value.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
        }
    must_be_in_use = {"os", "re", "sqlite3", "json"}
    assert must_be_in_use <= in_use, (
        f"these should be used somewhere in autopilot/: {sorted(must_be_in_use - in_use)}")
