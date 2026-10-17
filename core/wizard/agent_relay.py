"""Gemini for agentic runs, through the organisation's AI (9.9).

An agentic tool (Understudy's agent) asks Gemini what to do at every step. Inside Understudy it uses the Gemini the
**admin** set up - Vertex AI with the service account on Google Cloud, or the Mission Control API key - so users are
never asked for a key of their own.

How the run reaches Gemini without holding a credential
-------------------------------------------------------
* For each agentic run Understudy starts a small **relay** on ``127.0.0.1`` (an ephemeral port), bound to that run
  only. The run gets two variables: ``UNDERSTUDY_AGENT_RELAY`` (the relay's address) and
  ``UNDERSTUDY_SECRET_AGENT_RELAY_TOKEN`` (a random token for this run). No Google API key, OAuth token or service
  account credential is ever given to the automation process.
* The relay accepts only ``POST /generate`` with that token (constant-time compare), bounded sizes
  (``MAX_PROMPT_BYTES``, ``MAX_IMAGE_BYTES``) and a call budget per run (``MAX_CALLS``). It forwards the call with
  ``gemini.generate`` - the same client, model and platform as every other Understudy AI call - and returns the text.
* The relay stops when the run ends; a token is useless afterwards. Loopback is exempt from the run's egress proxy, so
  the run's network still reaches only its target pages - Google's API host is no longer on its allow-list.
* Exported ZIPs run outside Understudy: there the agent uses the user's own ``GEMINI_API_KEY`` (README).
"""
from __future__ import annotations

import base64
import hmac
import json
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional

URL_ENV = "UNDERSTUDY_AGENT_RELAY"
TOKEN_ENV = "UNDERSTUDY_SECRET_AGENT_RELAY_TOKEN"
MODEL_ENV = "UNDERSTUDY_AGENT_MODEL"
PROMPT_ID = "AGENT_STEP"
MAX_PROMPT_BYTES = 400_000
MAX_IMAGE_BYTES = 6 * 1024 * 1024
MAX_BODY_BYTES = MAX_PROMPT_BYTES + MAX_IMAGE_BYTES * 2
MAX_CALLS = int(os.environ.get("UNDERSTUDY_AGENT_RELAY_MAX_CALLS", "400"))
TIMEOUT_S = 90


def available() -> bool:
    """True when the organisation's Gemini is set up (Vertex AI or the Mission Control key) - or a test stand-in."""
    from core.wizard import llm
    return bool(llm.ai_available())


def unavailable_reason() -> str:
    from core.wizard import llm
    return llm.unavailable_reason() or "Gemini is not set up."


def info() -> Dict[str, Any]:
    """What Step 4 shows: whether agentic runs can use Gemini, on which platform and model."""
    from core.wizard import gemini, llm
    if not available():
        return {"available": False, "reason": "Your admin hasn't set up Gemini in Mission Control yet - agentic tools "
                                              "need it while they run."}
    plat = "test stand-in" if llm._TEST_BACKEND is not None else gemini.describe().get("platform")
    return {"available": True, "platform": plat, "model": model()}


def model() -> str:
    from core.wizard import llm
    if llm._TEST_BACKEND is not None:
        return llm.standin_label()
    m = os.environ.get("UNDERSTUDY_VERTEX_MODEL") if _vertex() else ""
    return m or llm.selected_model()


def _vertex() -> bool:
    try:
        from core.wizard import gemini
        return gemini.mode() == "vertex"
    except Exception:
        return False


def _generate(system: str, prompt: str, image: Optional[bytes], model_name: str) -> str:
    """One agent decision through the organisation's Gemini (JSON mode, temperature 0)."""
    from core.wizard import gemini, llm
    if llm._TEST_BACKEND is not None:                       # tests: the stand-in decides
        r = llm._TEST_BACKEND(PROMPT_ID, system, prompt, None)
        return r if isinstance(r, str) else json.dumps(r)
    return gemini.generate(model_name, system, prompt, None, 0.0, TIMEOUT_S, image=image)


class Relay:
    """One run's relay. ``with Relay(log) as r: env = r.env()`` - stops (and forgets the token) on exit."""

    def __init__(self, log: Callable[[str], None] = lambda m: None, model_name: Optional[str] = None,
                 generate: Optional[Callable[[str, str, Optional[bytes], str], str]] = None):
        """``model_name``: the tool's own default model (an admin-built agentic tool), else the organisation's."""
        self.log = log
        self.model = model_name or model()
        self.token = secrets.token_urlsafe(32)
        self.calls = 0
        self.errors = 0
        self.started = 0.0
        self._gen = generate or _generate
        self._lock = threading.Lock()
        self._srv: Optional[ThreadingHTTPServer] = None

    # -- lifecycle ----------------------------------------------------------------------------------------------------
    def start(self) -> "Relay":
        outer = self

        class H(BaseHTTPRequestHandler):
            def _send(self, code: int, obj: Dict[str, Any]) -> None:
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802
                self._send(404, {"error": "not found"})

            def do_POST(self):  # noqa: N802
                code, obj = outer._handle(self.path, self.headers.get("Authorization") or "",
                                          int(self.headers.get("Content-Length") or 0), self.rfile)
                self._send(code, obj)

            def log_message(self, *a):
                pass

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self._srv.daemon_threads = True
        threading.Thread(target=self._srv.serve_forever, daemon=True, name="agent-relay").start()
        self.started = time.time()
        return self

    def stop(self) -> None:
        if self._srv:
            self._srv.shutdown()
            self._srv.server_close()
            self._srv = None
        self.token = ""

    def __enter__(self) -> "Relay":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._srv.server_address[1]}" if self._srv else ""

    def env(self) -> Dict[str, str]:
        """The variables the run gets. The token is the only secret, and it only works while this run lasts."""
        return {URL_ENV: self.url, TOKEN_ENV: self.token, MODEL_ENV: self.model}

    def summary(self) -> Dict[str, Any]:
        return {"calls": self.calls, "errors": self.errors, "model": self.model,
                "seconds": round(time.time() - self.started, 1) if self.started else 0}

    # -- one request --------------------------------------------------------------------------------------------------
    def _handle(self, path: str, auth: str, length: int, rfile) -> tuple:
        if path.rstrip("/") != "/generate":
            return 404, {"error": "not found"}
        token = auth[7:] if auth.startswith("Bearer ") else ""
        if not self.token or not hmac.compare_digest(token.encode(), self.token.encode()):
            return 401, {"error": "this relay belongs to another run"}
        if length <= 0 or length > MAX_BODY_BYTES:
            return 413, {"error": "request too large"}
        try:
            req = json.loads(rfile.read(length).decode("utf-8"))
            system, prompt = str(req.get("system") or ""), str(req.get("prompt") or "")
            image = base64.b64decode(req["image"]) if req.get("image") else None
        except Exception:
            return 400, {"error": "bad request"}
        if not prompt or len(prompt.encode()) > MAX_PROMPT_BYTES or len(system.encode()) > MAX_PROMPT_BYTES:
            return 413, {"error": "prompt too large"}
        if image is not None and len(image) > MAX_IMAGE_BYTES:
            return 413, {"error": "screenshot too large"}
        with self._lock:
            if self.calls >= MAX_CALLS:
                return 429, {"error": f"this run used its budget of {MAX_CALLS} Gemini calls"}
            self.calls += 1
        try:
            return 200, {"text": self._gen(system, prompt, image, self.model)}
        except Exception as e:
            from core.wizard import llm
            with self._lock:
                self.errors += 1
            err = f"{type(e).__name__}: {e}"
            status = 429 if any(x in err for x in ("429", "RESOURCE_EXHAUSTED")) else \
                503 if any(x in err for x in ("503", "UNAVAILABLE", "overloaded", "Deadline")) else 502
            return status, {"error": llm.friendly_ai_error(err)[:400]}


def redact(text: str, hidden: List[str]) -> str:
    for s in hidden:
        if s and len(s) >= 8 and s in text:
            text = text.replace(s, "***")
    return text
