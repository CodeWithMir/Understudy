#!/usr/bin/env python3
"""Check the Google Cloud / Gemini set-up of this Understudy installation.

    python scripts/gcp_check.py           # full check: real Gemini call, Firestore and Cloud Storage round-trips
    python scripts/gcp_check.py --quick   # configuration + credentials only, no network calls (start scripts)
    python scripts/gcp_check.py --json    # machine-readable

Reads .env like the backend does. Exit code 1 when a check FAILs. Every problem comes with the command that fixes it.
"""
import argparse
import json
import os
import sys
import warnings

warnings.filterwarnings("ignore")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core.wizard import env_file  # noqa: E402

env_file.load()
from core.wizard import gcp_check  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--quick", action="store_true", help="no network calls")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    rep = gcp_check.check(network=not a.quick)
    if a.json:
        print(json.dumps(rep, indent=2))
    else:
        print(gcp_check.format_text(rep))
        if not a.quick:
            print(f"\n   Overall: {rep['status']}")
    return 1 if rep["status"] == gcp_check.FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
