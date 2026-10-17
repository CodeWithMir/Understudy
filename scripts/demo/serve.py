#!/usr/bin/env python3
"""Starts the Understudy backend for recording the demo video.

    python scripts/demo/serve.py              # normal backend: real Gemini/Vertex AI if configured, else offline
    python scripts/demo/serve.py --standin    # "session 1" on a machine without internet: a clearly labelled
                                              # stand-in model answers instead of Gemini (scripts/demo/standin_model.py)
    python scripts/demo/serve.py --offline    # "session 2": AI switched off (UNDERSTUDY_FORCE_HEURISTIC=1), so only
                                              # learned skills and the built-in rules can answer

Same app as `uvicorn server:app` (port 8000); the UI (port 3000) is started separately.
"""
import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
os.chdir(ROOT)


def main() -> None:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--standin", action="store_true", help="answer with the labelled stand-in model")
    g.add_argument("--offline", action="store_true", help="switch AI off")
    ap.add_argument("--port", type=int, default=8000)
    a = ap.parse_args()
    if a.offline:
        os.environ["UNDERSTUDY_FORCE_HEURISTIC"] = "1"
    if a.standin:
        os.environ.setdefault("UNDERSTUDY_STANDIN_LABEL", "stand-in model (no internet)")
    import uvicorn
    import server
    if a.standin:
        from core.wizard import llm
        from scripts.demo.standin_model import standin
        llm.set_test_backend(standin)
        print("[demo] AI answers come from the labelled stand-in model:", llm.standin_label())
    uvicorn.run(server.app, host="127.0.0.1", port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
