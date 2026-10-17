"""Gemini key and model selection (Mission Control > AI, security & account), on Google's ``google-genai`` SDK.

* The key (``GEMINI_API_KEY``, also accepted as ``GOOGLE_API_KEY``) is held in the process environment, so every
  AI call (``core/wizard/gemini.py``) uses it from the next call on, and is saved to ``.env`` when ``persist``.
* The model (``GEMINI_MODEL``) is chosen from the models the key can use (``fetch_available_models``, Gemini API
  ``models.list``), cached; without a key the built-in suggestions are shown. 9.9: on Vertex AI (no key) the list
  comes from Vertex AI's Google publisher models (``publishers/google/models``), filtered to Gemini models that
  generate text - the same list the admin sees locally with a key, for this project.
* ``validate_api_key`` lists the models and sends one tiny ``Respond with 'OK' only.`` request.

On Vertex AI no key is needed (Application Default Credentials); the selected model still applies there unless
``UNDERSTUDY_VERTEX_MODEL`` forces one. 9.5.0: moved here from ``core/api_key_manager.py`` and ported from the
deprecated ``google-generativeai`` SDK to ``google-genai``.
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional, Tuple

from core.wizard import env_file

DEFAULT_MODEL = "gemini-3.6-flash"
_RECOMMENDED = ("gemini-3.6-flash", "gemini-3.5-flash", "gemini-flash-latest")

# Shown before a key is set (and when the model list can't be fetched)
_DEFAULT_SUGGESTIONS: List[Dict[str, Any]] = [
    {"id": "gemini-3.6-flash", "displayName": "Gemini 3.6 Flash", "description": "High-speed multimodal intelligence & reasoning (Recommended)", "isRecommended": True, "category": "flash"},
    {"id": "gemini-3.5-flash", "displayName": "Gemini 3.5 Flash", "description": "Next-generation multimodal reasoning and tool dispatch", "isRecommended": True, "category": "flash"},
    {"id": "gemini-flash-latest", "displayName": "Gemini Flash Latest", "description": "Latest stable production Flash release", "isRecommended": True, "category": "flash"},
    {"id": "gemini-2.5-flash", "displayName": "Gemini 2.5 Flash", "description": "High-efficiency general purpose multimodal model", "isRecommended": False, "category": "flash"},
    {"id": "gemini-2.5-pro", "displayName": "Gemini 2.5 Pro", "description": "Deep reasoning & multi-step complex planning", "isRecommended": False, "category": "pro"},
]
_DISCOVERED_MODELS_CACHE: List[Dict[str, Any]] = []
_CACHE_SOURCE: Dict[str, str] = {"source": "", "error": ""}            # "studio" / "vertex": a list never serves the other platform
# Vertex AI's publisher list also holds Imagen, Veo, embeddings, speech and image-output models - not for JSON answers
_VERTEX_SKIP = re.compile(r"embedding|tts|image|live|audio|native|veo|imagen|robotics|computer-use|aqa|^gemini-1\.")

_INVALID_KEY = ("Google AI rejected this key: API key is invalid. Please verify at "
                "https://aistudio.google.com/app/apikey")


def mask_key(key: Optional[str]) -> Optional[str]:
    """Masks an API key for display (e.g. AIzaSy...7e4a)."""
    if not key or not isinstance(key, str):
        return None
    key = key.strip()
    if len(key) <= 8:
        return "********"
    return f"{key[:6]}...{key[-4:]}"


def clean_api_key_string(raw: Optional[str]) -> str:
    """Strips quotes, whitespace and shell ``export NAME=`` prefixes from a pasted key."""
    if not raw or not isinstance(raw, str):
        return ""
    cleaned = raw.strip()
    if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in "'\"":
        cleaned = cleaned[1:-1].strip()
    for prefix in ("export GEMINI_API_KEY=", "GEMINI_API_KEY=", "export GOOGLE_API_KEY=", "GOOGLE_API_KEY="):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix):].strip().strip("'\"")
    return cleaned


def _dotenv() -> Dict[str, str]:
    return {} if os.environ.get("UNDERSTUDY_SKIP_DOTENV") == "1" else env_file.parse()


def _persist(values: Dict[str, Optional[str]]) -> None:
    try:
        env_file.update(values)
    except Exception as e:  # pragma: no cover - a read-only file system must not break the setting itself
        print(f"[api_keys] could not update .env: {e}", flush=True)


def get_current_api_key() -> Optional[str]:
    return os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")


def set_api_key(raw_key: str, persist: bool = True) -> Dict[str, Any]:
    clean_key = clean_api_key_string(raw_key)
    if not clean_key:
        raise ValueError("API key cannot be empty.")
    os.environ["GEMINI_API_KEY"] = clean_key
    os.environ["GOOGLE_API_KEY"] = clean_key
    _DISCOVERED_MODELS_CACHE.clear()
    if persist:
        _persist({"GEMINI_API_KEY": clean_key, "GOOGLE_API_KEY": clean_key})
    return {"status": "SAVED", "has_key": True, "masked_key": mask_key(clean_key), "persisted": persist}


def clear_api_key() -> Dict[str, Any]:
    os.environ.pop("GEMINI_API_KEY", None)
    os.environ.pop("GOOGLE_API_KEY", None)
    _DISCOVERED_MODELS_CACHE.clear()
    _persist({"GEMINI_API_KEY": None, "GOOGLE_API_KEY": None})
    return {"status": "CLEARED", "has_key": False, "masked_key": None}


def _describe_model(m: Any) -> Optional[Dict[str, Any]]:
    actions = [str(a) for a in (getattr(m, "supported_actions", None) or [])]
    if actions and not any("generateContent" in a for a in actions):
        return None
    mid = str(getattr(m, "name", "") or "").strip()
    mid = re.sub(r"^(projects/[^/]+/locations/[^/]+/)?(publishers/google/)?models/", "", mid)
    if not mid:
        return None
    display = getattr(m, "display_name", None) or mid
    desc = (getattr(m, "description", None) or "")[:120] or f"Google AI {display}"
    cat = "flash" if "flash" in mid.lower() else ("pro" if "pro" in mid.lower() else "other")
    return {"id": mid, "displayName": display, "description": desc,
            "isRecommended": mid in _RECOMMENDED or "3.6-flash" in mid, "category": cat}


def _sort_key(item: Dict[str, Any]) -> int:
    score = -100 if item["isRecommended"] else 0
    score -= {"flash": 50, "pro": 20}.get(item["category"], 0)
    score -= 10 if "3.6" in item["id"] else 8 if "3.5" in item["id"] else 5 if "latest" in item["id"] else 0
    return score


def _vertex() -> bool:
    try:
        from core.wizard import gemini
        return gemini.mode() == "vertex"
    except Exception:
        return False


def _cached(source: str) -> List[Dict[str, Any]]:
    return _DISCOVERED_MODELS_CACHE if _CACHE_SOURCE["source"] == source else []


def _remember(source: str, found: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    found.sort(key=_sort_key)
    _DISCOVERED_MODELS_CACHE[:] = found
    _CACHE_SOURCE["source"] = source
    return found


def fetch_vertex_models() -> List[Dict[str, Any]]:
    """9.9: the Gemini models Vertex AI offers this project (service account, no key), recommended first."""
    try:
        from core.wizard import gemini
        client = gemini._client("vertex")                  # kept referenced while the pager is read
        found, seen = [], set()
        for m in client.models.list(config={"query_base": True, "page_size": 100}):
            d = _describe_model(m)
            if not d or not d["id"].startswith("gemini-") or _VERTEX_SKIP.search(d["id"]) or d["id"] in seen:
                continue
            seen.add(d["id"])
            if d["description"].startswith("Google AI "):
                d["description"] = f"Vertex AI {d['displayName']}"
            found.append(d)
        if found:
            _CACHE_SOURCE["error"] = ""
            return _remember("vertex", found)
        _CACHE_SOURCE["error"] = "Vertex AI listed no Gemini models for this project and region"
    except Exception as e:
        _CACHE_SOURCE["error"] = f"Vertex AI model list failed: {type(e).__name__}: {str(e)[:200]}"
        print(f"[api_keys] {_CACHE_SOURCE['error']}", flush=True)
    return _cached("vertex") or _DEFAULT_SUGGESTIONS


def fetch_available_models(api_key: Optional[str] = None) -> List[Dict[str, Any]]:
    """The models this key can generate content with (Gemini API ``models.list``), recommended first. 9.9: on Vertex
    AI, without an explicit key, Vertex AI's own list."""
    explicit = clean_api_key_string(api_key)
    if not explicit and _vertex():
        return fetch_vertex_models()
    target_key = explicit or clean_api_key_string(get_current_api_key())
    if not target_key:
        return _cached("studio") or _DEFAULT_SUGGESTIONS
    try:
        from core.wizard import gemini
        # Keep the client referenced while the pager is read: google-genai closes its HTTP connection when the
        # client object is garbage-collected, which (inline) happened before the first page was fetched.
        client = gemini.studio_client(target_key)
        found = [d for d in (_describe_model(m) for m in client.models.list()) if d]
        if found:
            return _remember("studio", found)
    except Exception as e:
        print(f"[api_keys] model discovery: {e}", flush=True)
    return _cached("studio") or _DEFAULT_SUGGESTIONS


def get_available_models(api_key: Optional[str] = None) -> List[Dict[str, Any]]:
    source = "vertex" if not clean_api_key_string(api_key) and _vertex() else "studio"
    return _cached(source) or fetch_available_models(api_key)


def format_model_label(model_name: str) -> str:
    for m in get_available_models():
        if m["id"] == model_name:
            return m["displayName"]
    return model_name.replace("gemini-", "Gemini ").replace("-", " ").title()


def get_selected_model() -> str:
    """The model every AI call uses: GEMINI_MODEL (environment or .env), else the first recommended model."""
    val = os.environ.get("GEMINI_MODEL") or _dotenv().get("GEMINI_MODEL")
    if val and val.strip():
        return val.strip()
    available = get_available_models()
    return next((m["id"] for m in available if m.get("isRecommended")), available[0]["id"] if available else DEFAULT_MODEL)


def set_selected_model(model_name: str, persist: bool = True) -> str:
    model_name = (model_name or "").strip() or DEFAULT_MODEL
    os.environ["GEMINI_MODEL"] = model_name
    if persist:
        _persist({"GEMINI_MODEL": model_name})
    return model_name


def _is_quota(err: str) -> bool:
    return "RESOURCE_EXHAUSTED" in err or "Quota exceeded" in err or "429" in err


def _is_invalid_key(err: str) -> bool:
    return "API_KEY_INVALID" in err or "API key not valid" in err


def validate_api_key(key_to_test: Optional[str] = None, model_name: Optional[str] = None) -> Tuple[bool, str]:
    """Lists the key's models and sends one tiny request; on success the working model becomes the selected one."""
    target_key = clean_api_key_string(key_to_test) or clean_api_key_string(get_current_api_key())
    if not target_key:
        return False, "No Google AI API Key provided or found in environment."
    try:
        from google.genai import types

        from core.wizard import gemini
    except ImportError:
        return False, "The Gen AI SDK (google-genai) is not installed in the Python runtime."
    try:
        discovered_ids = [m["id"] for m in fetch_available_models(target_key)]
        preferred = model_name if (model_name and model_name in discovered_ids) else get_selected_model()
        candidates = [preferred] + [m for m in _RECOMMENDED + ("gemini-3.5-flash-lite",)
                                    if m in discovered_ids and m != preferred] \
            + [m for m in discovered_ids if m != preferred][:5]
        client = gemini.studio_client(target_key)
        cfg = types.GenerateContentConfig(max_output_tokens=5, temperature=0.0,
                                          http_options=types.HttpOptions(timeout=12_000))
        last_error = None
        for m_name in dict.fromkeys(candidates):
            try:
                client.models.generate_content(model=m_name,
                                               contents="Respond with 'OK' only.",
                                               config=cfg)
                set_selected_model(m_name)
                return True, f"Google AI connection verified successfully on '{m_name}' ({len(discovered_ids)} models available)."
            except Exception as model_err:
                err = str(model_err)
                if _is_quota(err):
                    set_selected_model(m_name)
                    return True, (f"Google AI API Key verified! ({len(discovered_ids)} models available; free quota "
                                  f"limit active on {m_name}).")
                if _is_invalid_key(err):
                    return False, _INVALID_KEY
                last_error = err
        return False, f"Google AI verification failed: {last_error or 'no model answered'}"
    except Exception as e:
        err = str(e)
        if _is_invalid_key(err):
            return False, _INVALID_KEY
        if _is_quota(err):
            return True, "Google AI API Key is VALID! (Free tier rate limit active)."
        return False, f"Google AI verification failed: {err}"


def get_key_status() -> Dict[str, Any]:
    """Safe status for Mission Control: masked key, where it comes from, the selected model and the model list."""
    current = clean_api_key_string(get_current_api_key())
    has_key = bool(current)
    dot = _dotenv()
    persisted = bool(dot.get("GEMINI_API_KEY") or dot.get("GOOGLE_API_KEY"))
    selected = get_selected_model()
    available = list(get_available_models())
    if selected and selected not in {m["id"] for m in available}:      # the chosen model is always selectable
        available.insert(0, {"id": selected, "displayName": format_model_label(selected),
                             "description": "Currently selected", "isRecommended": False, "category": "other"})
    return {
        "has_key": has_key,
        "masked_key": mask_key(current),
        "source": ("file" if persisted else "runtime_env") if has_key else "none",
        "persisted": persisted,
        "selected_model": selected,
        "available_models": available,
        "model_ids": [m["id"] for m in available],
        "provider": f"Google Gen AI ({format_model_label(selected)})",
        "total_models_count": len(available),
        # 9.9: where the list came from - "vertex" / "studio" (live) or "" (the built-in suggestions) - and why not live
        "models_source": _CACHE_SOURCE["source"] if available and _DISCOVERED_MODELS_CACHE else "",
        "models_error": _CACHE_SOURCE.get("error") or "",
        "capabilities": [
            "Multimodal recording understanding (Gemini Files API / Vertex AI)",
            "Agentic build, inspection and self-healing",
            "Semantic Model Armor classifier",
            "SOP rule extraction in any language",
        ],
    }
