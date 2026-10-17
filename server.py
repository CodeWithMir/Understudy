"""Understudy - backend server (FastAPI, port 8000).

Mounts:
  * the Understudy wizard API        /api/wizard/*   (core/wizard/api.py)      - the 5-step Studio flow
  * Mission Control (admin) API      /api/admin/*    (core/wizard/admin_api.py)
  * Studio accounts / projects       /api/auth/*, /api/projects/*, /api/history/*  (core/wizard/user_api.py)
  * live events (WebSocket)          /api/admin/ws, /api/wizard/ws               (core/wizard/events.py)
  * Gemini key / model configuration /api/config/*   (below - Mission Control > AI, security & account)
  * Model Armor interception feed    /api/security/alerts (below)
  * the bundled demo ERP portals     /portal1, /portal2, /portal3, /api/erp/*    (erp/mock_erp.py)
  * liveness probe                   /api/health
"""
# Every setting in .env (Google Cloud project, Vertex AI, Firestore, Cloud Storage, ...) - not only the Gemini key -
# applies to native runs. Real environment variables (Docker, Cloud Run, the shell) take precedence.
from core.wizard import env_file as _env_file
_env_file.load()
# 9.7.1: the Mission Control password is generated on the first start and saved in .env (like Secret Manager on Cloud
# Run); must run before the admin login is set up (core/wizard/admin_auth.py reads UNDERSTUDY_ADMIN_PASSWORD).
from core.wizard import admin_password as _admin_password
_admin_password.startup()

import asyncio
import os
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from core.wizard import WIZARD_VERSION as _UNDERSTUDY_VERSION
from core.wizard import security_alerts
from core.wizard.api_keys import (
    clear_api_key,
    fetch_available_models,
    get_key_status,
    get_selected_model,
    set_api_key,
    set_selected_model,
    validate_api_key,
)
from core.wizard.admin_api import require_admin as _require_admin  # Mission Control login protects key/model/alerts
from core.wizard.admin_api import router as admin_router
from core.wizard.api import router as wizard_router
from core.wizard.events import router as events_router
from core.wizard.user_api import auth_router as studio_auth_router, history_router, projects_router
from erp.mock_erp import app as erp_app

@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """Start-up: banner, email-claim backfill and the code-health watcher (FastAPI lifespan; 9.5.2 replaced the
    deprecated ``on_event("startup")`` hook, whose warning code health reported on every start)."""
    from core.wizard import code_health, users as _users
    from core.wizard.banner import startup_banner
    print(startup_banner(get_key_status()), flush=True)
    try:                             # email claims for accounts created before 8.1.1 (idempotent)
        _users.backfill_email_claims()
    except Exception as e:           # pragma: no cover - never block start-up
        print(f"[users] email claim backfill skipped: {e}", flush=True)
    code_health.start_watcher()      # AI self-heal of start-up warnings once a Gemini key is set (Mission Control)
    from core.wizard import runtime_restore
    runtime_restore.start()          # 9.9: runtimes earlier tool setups downloaded (Cloud Run instances start empty)
    yield


app = FastAPI(title="Understudy Backend", version=_UNDERSTUDY_VERSION, lifespan=_lifespan)

# The Next.js UI runs on its own origin (port 3000) in local / Docker Compose setups.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(wizard_router)        # Studio: the 5-step Watch / Build / Govern / Execute / Export flow
app.include_router(admin_router)         # Mission Control
app.include_router(studio_auth_router)   # Studio users log in / sign up here
app.include_router(projects_router)      # User -> Projects -> Sessions
app.include_router(history_router)       # previous-session context search for Step 1
app.include_router(events_router)        # live events (WebSocket) for Mission Control and the Studio

# The bundled demo ERP (Portal 1 / 2 / 3 and its JSON API) is served by the same process.
for route in erp_app.routes:
    app.routes.append(route)


@app.get("/api/health", include_in_schema=False)
async def health():
    """Liveness/readiness probe (Cloud Run, docker-compose). No secrets, no external calls.

    9.7: also says where Understudy is hosted and which Google AI it uses, so a deployment can be checked
    (``python scripts/check_deployment.py <url>``)."""
    from core.wizard import gemini as _gemini, store as _store
    m = _gemini.mode()
    return {"status": "ok", "version": _UNDERSTUDY_VERSION, "store": _store.describe()["kind"],
            "hosting": {"platform": "cloud_run" if os.environ.get("K_SERVICE") else "self_hosted",
                        "service": os.environ.get("K_SERVICE"), "revision": os.environ.get("K_REVISION")},
            "ai": {"provider": "google", "mode": m,
                   "platform": {"vertex": "Vertex AI", "studio": "Gemini API (AI Studio)"}.get(m or "", None),
                   "model": get_selected_model() if m else None}}


@app.get("/", include_in_schema=False)
async def root_redirect():
    """The backend's root opens the demo ERP's Portal 1 (the UI lives on port 3000)."""
    return RedirectResponse(url="/portal1")


# ---------------------------------------------------------------------------
# Model Armor interceptions (Mission Control > AI, security & account)
# ---------------------------------------------------------------------------
@app.get("/api/security/alerts", dependencies=[Depends(_require_admin)])
async def get_security_alerts_endpoint():
    """Every input Model Armor blocked: trigger / pattern, channel and target."""
    alerts = security_alerts.alerts()
    return {"status": "SUCCESS", "alerts": alerts, "count": len(alerts), "active_model": get_selected_model()}


@app.delete("/api/security/alerts", dependencies=[Depends(_require_admin)])
async def clear_security_alerts_endpoint():
    security_alerts.clear()
    return {"status": "SUCCESS", "message": "Security threat history cleared.", "count": 0}


# ---------------------------------------------------------------------------
# Gemini key and model (Mission Control > AI, security & account)
# ---------------------------------------------------------------------------
class SetApiKeyRequest(BaseModel):
    api_key: str
    selected_model: Optional[str] = None
    validate_on_save: Optional[bool] = False
    persist: Optional[bool] = True


class SetModelRequest(BaseModel):
    selected_model: str
    persist: Optional[bool] = True


class ValidateKeyRequest(BaseModel):
    api_key: Optional[str] = None
    selected_model: Optional[str] = None


@app.get("/api/config/api-key", dependencies=[Depends(_require_admin)])
async def get_google_api_key_status():
    """Gemini key status (masked), plus where Understudy's AI runs (Vertex AI with Application Default Credentials
    needs no key) and where state is stored."""
    from core.wizard import gemini as _gemini, llm as _llm, store as _store
    st = get_key_status()
    g = _gemini.describe()
    st["platform"] = {k: g.get(k) for k in ("mode", "sdk", "platform", "project", "location", "media_cache")}
    st["platform"]["gcs_bucket"] = bool(g.get("gcs_bucket"))
    _sd = _store.describe()
    st["platform"]["store"] = _sd["kind"]
    st["platform"]["store_degraded"] = _sd.get("degraded")
    st["platform"]["cloud_run"] = bool(os.environ.get("K_SERVICE"))
    # a labelled stand-in/test backend answers instead of Gemini (demo recording without internet): say so
    st["standin"] = _llm.standin_label() if _llm._TEST_BACKEND is not None else None
    return st


@app.get("/api/config/models")
async def get_available_gemini_models(api_key: Optional[str] = None):
    """Gemini models discovered live - from the key's Gemini API list, or (9.9) Vertex AI's list on Vertex AI -
    falling back to the built-in suggestions."""
    from core.wizard import api_keys as _ak
    models = await asyncio.to_thread(fetch_available_models, api_key)
    live = bool(_ak._DISCOVERED_MODELS_CACHE) and models == _ak._DISCOVERED_MODELS_CACHE
    return {
        "models": models,
        "source": _ak._CACHE_SOURCE["source"] if live else "",
        "error": _ak._CACHE_SOURCE.get("error") or "",
        "total_count": len(models),
        "selected_model": get_selected_model(),
        "recommended": [m["id"] for m in models if m.get("isRecommended")],
    }


@app.post("/api/config/api-key", dependencies=[Depends(_require_admin)])
async def configure_google_api_key(req: SetApiKeyRequest):
    """Sets (optionally validates) and persists the Gemini key and the selected model."""
    clean_key = (req.api_key or "").strip()
    if not clean_key:
        raise HTTPException(status_code=400, detail="API key string cannot be empty.")
    persist = req.persist if req.persist is not None else True
    if req.selected_model:
        set_selected_model(req.selected_model, persist=persist)
    if req.validate_on_save:
        is_valid, msg = validate_api_key(clean_key, model_name=req.selected_model)
        if not is_valid:
            raise HTTPException(status_code=400, detail=f"Validation failed: {msg}")
    res = set_api_key(clean_key, persist=persist)
    return {"status": "SUCCESS", "message": "Google AI API Key configured successfully.", **res}


@app.post("/api/config/model", dependencies=[Depends(_require_admin)])
async def configure_google_model(req: SetModelRequest):
    m_name = set_selected_model(req.selected_model, persist=req.persist if req.persist is not None else True)
    return {"status": "SUCCESS", "selected_model": m_name, "message": f"Active Gemini model set to '{m_name}'."}


@app.post("/api/config/api-key/validate", dependencies=[Depends(_require_admin)])
async def validate_google_api_key(req: ValidateKeyRequest):
    is_valid, message = validate_api_key(req.api_key, model_name=req.selected_model)
    return {"valid": is_valid, "message": message, "model": req.selected_model or get_selected_model()}


@app.delete("/api/config/api-key", dependencies=[Depends(_require_admin)])
async def delete_google_api_key():
    return clear_api_key()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
