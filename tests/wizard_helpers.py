"""Shared helpers for the Understudy test suites.

* ``FakeLLM``       - deterministic stand-in for Gemini, plugged in through
                      ``core.wizard.llm.set_test_backend`` so the ACTUAL_AI_API code paths
                      (prompt rendering, schema validation, normalisation) are exercised offline.
* ``LiveServer``    - runs the real FastAPI app (mock ERP portals + wizard API) on a free port in a
                      background thread, for integration tests that need real HTTP + a real browser.
* ``fake_mp4``      - writes a tiny file with a valid ISO-BMFF ('ftyp') signature.
"""
import os
import socket
import sys
import tempfile
import threading
import time
from typing import Any, Callable, Dict, List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Isolated admin policy file for tests (URL exceptions + tool whitelist), never the real one.
os.environ.setdefault("UNDERSTUDY_ADMIN_POLICY_PATH", os.path.join(tempfile.mkdtemp(prefix="understudy-policy-"),
                                                                    "admin_policy.json"))

# No background code-health scans while tests run (they have their own tests).
os.environ.setdefault("UNDERSTUDY_CODE_HEALTH", "off")
# 9.0: no live-page probe (two Chromium loads per session) unless a test switches it on itself.
os.environ.setdefault("UNDERSTUDY_PAGE_PROBE", "off")
# Offline recall of learned AI answers is off by default so one test's FakeLLM answers can't leak into another test;
# tests/test_offline_recall.py switches it on with its own folder.
os.environ.setdefault("UNDERSTUDY_RECALL", "off")
# Isolated learned-skills folder and telemetry log for tests.
os.environ.setdefault("UNDERSTUDY_STORE_DIR", tempfile.mkdtemp(prefix="understudy-store-"))
os.environ.setdefault("UNDERSTUDY_SKILLS_DIR", tempfile.mkdtemp(prefix="understudy-skills-"))
os.environ.setdefault("UNDERSTUDY_ACTIVITY_LOG", os.path.join(tempfile.mkdtemp(prefix="understudy-activity-"), "a.jsonl"))

# A developer's own .env (keys, Google Cloud settings) must not leak into the tests.
os.environ["UNDERSTUDY_SKIP_DOTENV"] = "1"
# Never talk to a real model from the test suite.
os.environ.pop("GEMINI_API_KEY", None)
os.environ.pop("GOOGLE_API_KEY", None)


def fake_mp4(path: Optional[str] = None, payload: bytes = b"") -> str:
    path = path or tempfile.mktemp(suffix=".mp4")
    with open(path, "wb") as fh:
        fh.write(b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + payload + b"\x00" * 64)
    return path


class FakeLLM:
    """Routes prompt ids to canned responses (dicts, callables or exceptions) and records calls."""

    def __init__(self, responses: Dict[str, Any]):
        self.responses = responses
        self.calls: List[Dict[str, Any]] = []

    def __call__(self, prompt_id: str, system: str, user: str, media: Optional[str]):
        self.calls.append({"prompt_id": prompt_id, "system": system, "user": user, "media": media})
        r = self.responses.get(prompt_id)
        if r is None:
            raise RuntimeError(f"FakeLLM: no response configured for {prompt_id}")
        if isinstance(r, Exception):
            raise r
        if callable(r):
            return r(system, user, media)
        return r

    def called(self, prompt_id: str) -> List[Dict[str, Any]]:
        return [c for c in self.calls if c["prompt_id"] == prompt_id]


def install_fake(fake: Optional[FakeLLM]):
    from core.wizard import llm
    llm.set_test_backend(fake)


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class LiveServer:
    _instance: Optional["LiveServer"] = None

    def __init__(self):
        import uvicorn
        import server  # noqa: F401  (the real app: mock ERP + wizard router)
        self.port = free_port()
        self.base = f"http://localhost:{self.port}"
        config = uvicorn.Config(server.app, host="127.0.0.1", port=self.port, log_level="error", lifespan="off")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.time() + 20
        import requests
        while time.time() < deadline:
            try:
                if requests.get(self.base + "/api/wizard/info", timeout=1).ok:
                    return
            except Exception:
                time.sleep(0.2)
        raise RuntimeError("Live server did not start")

    @classmethod
    def get(cls) -> "LiveServer":
        if cls._instance is None:
            cls._instance = LiveServer()
        return cls._instance


def browser_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True)
            b.close()
        return True
    except Exception:
        return False


def wait_job(get_session: Callable[[], Dict[str, Any]], timeout: float = 120) -> Dict[str, Any]:
    deadline = time.time() + timeout
    s = get_session()
    while s.get("job", {}).get("status") == "running" and time.time() < deadline:
        time.sleep(0.3)
        s = get_session()
    return s


# ---------------------------------------------------------------------------
# Mission Control login (default credentials; every test process starts with them)
# ---------------------------------------------------------------------------
ADMIN_USER, ADMIN_PASSWORD = "admin", "Understudy@2026"


def admin_client():
    """A FastAPI TestClient logged in to Mission Control (X-Admin-Token header set)."""
    from fastapi.testclient import TestClient
    import server
    from core.wizard import admin_auth
    admin_auth.reset_to_defaults()
    c = TestClient(server.app)
    r = c.post("/api/admin/login", json={"username": ADMIN_USER, "password": ADMIN_PASSWORD})
    assert r.status_code == 200, r.text
    c.headers.update({"X-Admin-Token": r.json()["token"]})
    return c


def admin_headers(base: str) -> Dict[str, str]:
    """Logs in to a running server (LiveServer) and returns the header to send."""
    import requests
    from core.wizard import admin_auth
    admin_auth.reset_to_defaults()          # the LiveServer runs in this process
    r = requests.post(base + "/api/admin/login", json={"username": ADMIN_USER, "password": ADMIN_PASSWORD}, timeout=30)
    r.raise_for_status()
    return {"X-Admin-Token": r.json()["token"]}


def govern(sid: str, client=None):
    """9.9: Step 2 (Govern) before Build - confirm the extracted SOP rules and run the compliance check. With ``client``
    (a TestClient) through the API, else in-process. Returns the session."""
    from core.wizard import session as wz
    s = wz.get(sid)
    if client is not None:
        r = client.put(f"/api/wizard/sessions/{sid}/govern/rules", json={"rules": s.get("rules") or [], "confirmed": True})
        assert r.status_code == 200, r.text
        r = client.post(f"/api/wizard/sessions/{sid}/govern/check")
        assert r.status_code == 200, r.text
        return wz.get(sid)
    wz.update_rules(sid, s.get("rules") or [], True)
    return wz.compliance_check(sid)
