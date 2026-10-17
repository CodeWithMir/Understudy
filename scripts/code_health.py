#!/usr/bin/env python3
"""See and undo the AI's own code fixes from a terminal - no running backend needed.

Use it when an AI code-health fix keeps Understudy from starting (then Mission Control isn't reachable), or to audit
what the AI changed:

    python scripts/code_health.py list                  # every AI code change, its status and who applied it
    python scripts/code_health.py show fix-2026...      # why, which lines changed, the diff, where the backup is
    python scripts/code_health.py revert fix-2026...    # restore the original file from its backup
    python scripts/code_health.py revert fix-2026... --force   # the file was edited afterwards: save it, then restore
    python scripts/code_health.py revert-all            # undo every change still in place (newest first)

Backups live in generated/code_health/backups/<fix id>/ (the original file at its relative path, plus CHANGE.md,
change.json and fix.diff). Restart the backend after a revert.
"""
from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("UNDERSTUDY_SKIP_DOTENV", "1")

from core.wizard import code_health as ch  # noqa: E402

STATUS = {"in_place": "IN PLACE (the file is exactly as the AI left it)", "rolled_back": "REVERTED",
          "modified_since": "EDITED SINCE (someone changed the file after the AI fix)", "file_missing": "FILE MISSING"}


def _print_change(h, full: bool) -> None:
    print(f"{h['id']}  {h['file']}  [{STATUS.get(h['status'], h['status'])}]")
    print(f"    applied {h['at']} by {h.get('applied_by') or h.get('by')}")
    w = h.get("warning") or {}
    if w:
        print(f"    triggered by: {w.get('category')} (line {w.get('line')}): {str(w.get('message'))[:160]}")
    if h.get("diagnosis"):
        print(f"    why: {str(h['diagnosis'])[:300]}")
    for line in h.get("summary") or []:
        print(f"    - {line}")
    print(f"    backup: {h.get('backup')}{'' if h.get('backup_exists') else '  (MISSING)'}")
    if h.get("rolled_back"):
        print(f"    reverted {h.get('rolled_back_at')} by {h.get('rolled_back_by')}"
              + (f"; the edited file was saved to {h['saved_before_revert']}" if h.get("saved_before_revert") else ""))
    if full:
        if h.get("explanation"):
            print("\n" + str(h["explanation"]))
        print("\n" + (h.get("diff") or "(no diff recorded)"))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    sh = sub.add_parser("show")
    sh.add_argument("id")
    rv = sub.add_parser("revert")
    rv.add_argument("id")
    rv.add_argument("--force", action="store_true", help="also when the file was edited after the fix")
    ra = sub.add_parser("revert-all")
    ra.add_argument("--force", action="store_true")
    a = p.parse_args(argv)
    if a.cmd == "list":
        hs = ch.history()
        if not hs:
            print("The AI has not changed any code.")
            return 0
        s = ch.changes_summary()
        print(f"{s['total']} AI code change(s): {s['in_place']} in place, {s['rolled_back']} reverted, "
              f"{s['modified_since']} edited since. Backups: {os.path.relpath(ch.backups_dir(), ROOT)}\n")
        for h in hs:
            _print_change(h, full=False)
            print()
        return 0
    if a.cmd == "show":
        try:
            _print_change(ch.history_detail(a.id), full=True)
        except KeyError:
            print(f"No AI code change with id {a.id}. Run: python scripts/code_health.py list")
            return 2
        return 0
    ids = [a.id] if a.cmd == "revert" else [h["id"] for h in ch.history() if h["status"] in ("in_place", "modified_since")]
    rc = 0
    for hid in ids:
        try:
            ch.rollback(hid, by="cli", force=a.force)
            h = ch.history_detail(hid)
            print(f"Reverted {hid}: {h['file']} restored from {h['backup']}"
                  + (f" (the edited file was saved to {h['saved_before_revert']})" if h.get("saved_before_revert") else ""))
        except ch.RollbackConflict as e:
            print(f"NOT reverted {hid}: {e}\n  -> run again with --force")
            rc = 1
        except (KeyError, ValueError) as e:
            print(f"NOT reverted {hid}: {e}")
            rc = 1
    if ids and rc == 0:
        print("Restart the backend to load the restored code.")
    return rc


if __name__ == "__main__":
    sys.exit(main())
