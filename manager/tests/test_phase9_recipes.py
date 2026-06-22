"""Phase 9: recipe store + replay (repeat-without-LLM).

No browser/CDP needed: we inject a fake trigger and assert the store records a
completed job, lists/gets it, and replay calls the trigger with the recipe.
Run: .venv-manager/Scripts/python -m manager.tests.test_phase9_recipes
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from manager.recipes import RecipeStore, Recipe  # noqa: E402


def main():
    tmp = Path(tempfile.mkdtemp(prefix="mgr-p9-"))
    triggered = []

    store = RecipeStore(tmp / ".recipes", trigger=lambda rec: triggered.append(rec.name) or True)

    # A completed-job snapshot like Job.snapshot() produces.
    job_snap = {
        "job_id": "20260616T154500-braincreation",
        "workflow": "braincreation",
        "status": "complete",
        "output_dir": str(tmp / "google-takeout" / "braincreation" / "2026-06-16-04-01-04"),
        "parallel": 4,
        "max_exports": 290,
        "meta": {"account_label": "braincreation", "email": "braincreation@gmail.com",
                 "user": "11530", "authuser": "0"},
        "totals": {"parts_total": 290, "parts_done": 290,
                   "bytes_total": 3110000000000, "bytes_done": 3110000000000},
    }

    # 1. Record.
    rec = store.record_from_job(job_snap)
    assert rec is not None, "record_from_job returned None"
    assert rec.name == "braincreation", rec.name
    assert rec.parallel == 4 and rec.max_exports == 290
    print("[OK] recorded recipe from completed job")

    # 2. Persisted to disk.
    p = (tmp / ".recipes" / "braincreation.json")
    assert p.exists(), "recipe not persisted"
    print("[OK] recipe persisted to .recipes/braincreation.json")

    # 3. List + get.
    assert "braincreation" in store.list_names()
    got = store.get("braincreation")
    assert got is not None and got.account_label == "braincreation"
    print("[OK] list + get")

    # 4. A fresh store loads the persisted recipe from disk.
    store2 = RecipeStore(tmp / ".recipes", trigger=lambda rec: triggered.append(rec.name) or True)
    assert "braincreation" in store2.list_names(), "recipe not reloaded from disk"
    print("[OK] recipe reload from disk")

    # 5. Run -> trigger called with the recipe.
    ok = store.run("braincreation")
    assert ok is True, "run returned False"
    assert triggered == ["braincreation"], triggered
    print("[OK] run() invokes the replay trigger with the recipe")

    # 6. Run a missing recipe -> False, no trigger.
    triggered.clear()
    assert store.run("nope") is False
    assert triggered == []
    print("[OK] running a missing recipe is a no-op")

    # 7. Schedule set/clear.
    assert store.set_schedule("braincreation", "0 3 * * *") is True
    assert store.get("braincreation").schedule_cron == "0 3 * * *"
    assert store.set_schedule("braincreation", None) is True
    assert store.get("braincreation").schedule_cron in (None, "")
    print("[OK] schedule set + clear")

    # 8. No-trigger store: run returns False but doesn't crash.
    store_nt = RecipeStore(tmp / ".recipes", trigger=None)
    assert store_nt.run("braincreation") is False
    print("[OK] run with no trigger configured is a safe no-op")

    print("\n[PASS] Phase 9: recipe record + persist + reload + replay verified")


if __name__ == "__main__":
    main()
