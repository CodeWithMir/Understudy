#!/usr/bin/env python3
"""Mission Control password - generated once, the same way for every start script (9.7.1).

    python scripts/admin_password.py ensure     generate + save it in .env if there is none yet (prints it ONCE)
    python scripts/admin_password.py show       print the saved password
    python scripts/admin_password.py reset      generate a new one (used from the next start)

The start scripts (run_demo.*, deploy_docker.*, the Docker entrypoint) run ``ensure``. On Cloud Run the deploy scripts
keep it in Secret Manager instead: gcloud secrets versions access latest --secret=understudy-admin-password
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.wizard import admin_password  # noqa: E402

USER = os.environ.get("UNDERSTUDY_ADMIN_USERNAME") or "admin"
SHOW = "python scripts/admin_password.py show"


def box(lines):
    print("=" * 80)
    for line in lines:
        print("  " + line)
    print("=" * 80)


def main() -> int:
    cmd = (sys.argv[1] if len(sys.argv) > 1 else "ensure").lower()
    if cmd == "ensure":
        try:
            r = admin_password.ensure()
        except OSError as e:
            print(f"[ADMIN] Could not save a generated password ({e}) - Mission Control uses the PUBLISHED default;"
                  " set UNDERSTUDY_ADMIN_PASSWORD.")
            return 0
        if r["created"]:
            box(["FIRST START - Mission Control login (shown ONCE):",
                 f"    {USER} / {r['password']}",
                 f"  Saved in {r['path']} as UNDERSTUDY_ADMIN_PASSWORD. Show it again: {SHOW}"])
        else:
            where = "the environment (UNDERSTUDY_ADMIN_PASSWORD)" if r["source"] == "environment" else \
                ".env (UNDERSTUDY_ADMIN_PASSWORD)"
            print(f"[ADMIN] Mission Control login: {USER} / (saved in {where} - show it: {SHOW})")
        return 0
    if cmd == "show":
        pw, src = admin_password.current()
        if not pw:
            print("No Mission Control password is saved yet - start Understudy once, or run: "
                  "python scripts/admin_password.py ensure")
            return 1
        print(f"{USER} / {pw}    (from {src})")
        return 0
    if cmd == "reset":
        if os.environ.get(admin_password.ENV):
            print("UNDERSTUDY_ADMIN_PASSWORD is set in the environment (shell / Docker / Cloud Run), which wins over .env -"
                  " change it there.")
            return 1
        r = admin_password.reset()
        box(["New Mission Control password (shown ONCE; used from the next start):",
             f"    {USER} / {r['password']}",
             f"  Saved in {r['path']}. Restart Understudy to use it."])
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
