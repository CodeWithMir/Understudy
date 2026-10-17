#!/usr/bin/env python3
"""Checks a deployed Understudy against the submission requirements (9.7).

    python scripts/check_deployment.py https://understudy-xxxxx-uc.a.run.app

Requirements checked (from the deployment's public health endpoint - nothing secret is read):
  * it is a working prototype      - the Studio (/wizard), Mission Control (/) and the demo portal answer
  * it uses a Google AI model      - Gemini on Vertex AI (or the Gemini API) is configured and a model is selected
  * it is deployed on Google Cloud - the service runs on Cloud Run (K_SERVICE is set) over HTTPS
  * recommended                    - state in Cloud Firestore, so every Cloud Run instance shares it

Exit code 0 when every required check passes. Uses only the Python standard library.
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request


def get(url: str, timeout: float = 30.0):
    req = urllib.request.Request(url, headers={"User-Agent": "understudy-check-deployment"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(200_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception as e:                                   # network, DNS, TLS
        return None, f"{type(e).__name__}: {e}"


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    base = sys.argv[1].rstrip("/")
    rows = []

    def check(name, ok, detail, required=True):
        rows.append((name, ok, detail, required))

    check("HTTPS URL", base.startswith("https://"), base)
    status, body = get(base + "/healthz")
    if status != 200:
        status, body = get(base + "/api/health")
    health = {}
    if status == 200:
        try:
            health = json.loads(body)
        except ValueError:
            pass
    check("Backend answers (/healthz)", status == 200 and health.get("status") == "ok",
          f"HTTP {status} - version {health.get('version', '?')}" if status else body)
    for path, name in (("/wizard", "Understudy Studio (/wizard)"), ("/", "Mission Control (/)"),
                       ("/portal1", "Demo target app (/portal1)")):
        st, _ = get(base + path)
        check(name, st == 200, f"HTTP {st}")
    host = health.get("hosting") or {}
    check("Deployed on Google Cloud Run", host.get("platform") == "cloud_run",
          f"service {host.get('service')}, revision {host.get('revision')}" if host.get("platform") == "cloud_run"
          else "not running on Cloud Run (K_SERVICE is not set)")
    ai = health.get("ai") or {}
    check("Google AI model (Gemini) configured", ai.get("provider") == "google" and bool(ai.get("mode")),
          f"{ai.get('platform')} - model {ai.get('model')}" if ai.get("mode")
          else "no Gemini configured - run the deploy script (Vertex AI) or add a key in Mission Control")
    check("Gemini on Vertex AI (recommended on Google Cloud)", ai.get("mode") == "vertex",
          ai.get("platform") or "-", required=False)
    check("State in Cloud Firestore (recommended)", health.get("store") == "firestore",
          health.get("store") or "-", required=False)

    width = max(len(r[0]) for r in rows)
    failed = 0
    for name, ok, detail, required in rows:
        mark = "PASS" if ok else ("FAIL" if required else "WARN")
        failed += (not ok) and required
        print(f"  {mark:4}  {name:<{width}}  {detail}")
    print()
    print("All required checks pass - this deployment meets the submission requirements." if not failed
          else f"{failed} required check(s) failed.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
