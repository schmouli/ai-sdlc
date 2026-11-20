"""Seed the SkillBox overlay library with the best-practices skeleton (one-shot).

Stages the seed through the NORMAL pipeline (stage_version → pending) so its
activation requires explicit human approval — the same gate as telemetry-
derived overlays (spec FR-034). Re-running is idempotent (unchanged content
stages no new version).

Run:  & .venv\\Scripts\\python.exe scripts\\skillbox_seed_overlay.py [--approve --operator <id>]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.skillbox.overlays import OverlayLibrary  # noqa: E402
from src.skillbox.telemetry import default_store  # noqa: E402

SEED_FILE = REPO_ROOT / ".skillbox" / "generated" / "seed-testing-best-practices.md"
OVERLAY_ID = "testing/engineering-best-practices"
COLLECTION = "testing"
TITLE = "Engineering Best Practices (Seed)"
SEED_LEARNING = "LRN-SEED-BESTPRACTICES"


def seed(state_root: Path) -> dict:
    store = default_store(state_root)
    lib = OverlayLibrary(state_root).bind_store(store)
    lib.ensure_overlay(OVERLAY_ID, title=TITLE, collection=COLLECTION,
                       source_learning_ids=[SEED_LEARNING])
    content = SEED_FILE.read_text(encoding="utf-8")
    evidence = {
        "seed": True,
        "basis": "operator-authored best-practices skeleton (features 001-004 observed practice)",
        "frequency": None,
        "success_rate": None,
    }
    entry = lib.stage_version(OVERLAY_ID, content, [SEED_LEARNING], evidence)
    store.close()
    return {"overlay_id": OVERLAY_ID, "version": entry["version"],
            "status": entry["status"], "state_root": str(state_root)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Seed the SkillBox overlay library")
    parser.add_argument("--approve", action="store_true",
                        help="also approve the seeded version (requires --operator; "
                             "otherwise the version stays pending for review)")
    parser.add_argument("--operator", default=None)
    args = parser.parse_args(argv)
    from src.skillbox.config import ensure_state_root, state_root

    root = ensure_state_root(state_root())
    result = seed(root)
    print(f"seeded: {result['overlay_id']}@{result['version']} (status={result['status']})")
    if args.approve:
        from src.skillbox.review import ReviewManager

        rm = ReviewManager(root)
        try:
            record = rm.approve_version(OVERLAY_ID, result["version"], operator=args.operator,
                                        rationale="seed approved at seed time")
            print(f"approved by {record['operator']} -> active")
        except Exception as exc:
            from src.skillbox import OperatorIdentityError

            print(f"approval failed: {exc}")
            print("the seeded version remains PENDING — approve manually:")
            print(f"  python -m src.skillbox review approve {OVERLAY_ID} {result['version']} "
                  f"--operator <you>")
            return 1
    else:
        print("pending review — approve with:")
        print(f"  python -m src.skillbox review approve {OVERLAY_ID} {result['version']} --operator <you>")
        print("then refresh the IDE injection:")
        print("  python -m src.skillbox inject refresh")
    return 0


if __name__ == "__main__":
    sys.exit(main())
