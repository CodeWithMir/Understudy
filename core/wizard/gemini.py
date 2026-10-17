"""Gemini transport for Understudy: Google's current ``google-genai`` SDK, on AI Studio or on Vertex AI.

Modes (``mode()``):
    vertex   Vertex AI on Google Cloud - GOOGLE_GENAI_USE_VERTEXAI=true (or UNDERSTUDY_VERTEX_PROJECT set) plus
             GOOGLE_CLOUD_PROJECT / GOOGLE_CLOUD_LOCATION. Authenticates with Application Default Credentials (the
             Cloud Run service account, ``gcloud auth application-default login``, or a service-account key file in
             GOOGLE_APPLICATION_CREDENTIALS) - no API key needed. UNDERSTUDY_VERTEX_MODEL forces a model; if the
             Mission Control model is not offered by Vertex AI in the region, UNDERSTUDY_VERTEX_FALLBACK_MODEL
             (default gemini-2.5-flash) is used.
    studio   Gemini Developer API with the key set in Mission Control (GEMINI_API_KEY / GOOGLE_API_KEY).
(9.5.0: the deprecated ``google-generativeai`` fallback is gone - google-genai is the only SDK.)

Recordings: uploaded ONCE per (mode, credentials, file hash) and reused by every later call - WATCH, each WATCH
correction, and self-healing - instead of re-uploading the video each time:
    studio   Files API handle (valid 48 h; the cache keeps it 40 h and re-uploads if Google no longer has it)
    vertex   ``gs://`` object in UNDERSTUDY_GCS_BUCKET (google-cloud-storage), else inline bytes when <= 18 MB
"""
from __future__ import annotations

import hashlib
import mimetypes
import os
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

INLINE_LIMIT = 18 * 1024 * 1024
FILE_TTL_S = 40 * 3600
_MEDIA_CACHE: Dict[Tuple[str, str, str], Tuple[float, Any]] = {}
_LOCK = threading.Lock()
MEDIA_STATS = {"uploads": 0, "reused": 0}


def api_key() -> Optional[str]:
    return (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "").strip() or None


def _truthy(v: Optional[str]) -> bool:
    return (v or "").strip().lower() in ("1", "true", "yes", "on")


def vertex_project() -> Optional[str]:
    return (os.environ.get("UNDERSTUDY_VERTEX_PROJECT") or os.environ.get("GOOGLE_CLOUD_PROJECT") or "").strip() or None


def vertex_location() -> str:
    return (os.environ.get("UNDERSTUDY_VERTEX_LOCATION") or os.environ.get("GOOGLE_CLOUD_LOCATION") or "us-central1").strip()


def sdk_installed() -> bool:
    try:
        from google import genai  # noqa: F401
        return True
    except Exception:
        return False


def mode() -> Optional[str]:
    """vertex | studio | None (no usable credentials / SDK)."""
    if not sdk_installed():
        return None
    vertex = _truthy(os.environ.get("GOOGLE_GENAI_USE_VERTEXAI")) or bool(os.environ.get("UNDERSTUDY_VERTEX_PROJECT"))
    if vertex and vertex_project():
        return "vertex"
    return "studio" if api_key() else None


def describe() -> Dict[str, Any]:
    m = mode()
    return {"mode": m, "sdk": "google-genai" if m else None,
            "platform": {"vertex": "Vertex AI (Google Cloud)", "studio": "Gemini API (AI Studio key)"}.get(m or "",
                                                                                                        "not configured"),
            "project": vertex_project() if m == "vertex" else None,
            "location": vertex_location() if m == "vertex" else None,
            "gcs_bucket": os.environ.get("UNDERSTUDY_GCS_BUCKET") or None, "media_cache": dict(MEDIA_STATS)}


def unavailable_reason() -> str:
    if _truthy(os.environ.get("GOOGLE_GENAI_USE_VERTEXAI")) and not vertex_project():
        return "Vertex AI is switched on but GOOGLE_CLOUD_PROJECT is not set."
    if not api_key() and not (_truthy(os.environ.get("GOOGLE_GENAI_USE_VERTEXAI")) and vertex_project()):
        return ("No Gemini API key is set. An admin can add one in Mission Control > AI, security & account "
                "(or run on Vertex AI with GOOGLE_GENAI_USE_VERTEXAI=true).")
    if not sdk_installed():
        return "The Gemini SDK (google-genai) is not installed on the backend (pip install google-genai)."
    return ""


def _client(m: str, key: Optional[str] = None):
    from google import genai
    if m == "vertex":
        return genai.Client(vertexai=True, project=vertex_project(), location=vertex_location())
    return genai.Client(api_key=key or api_key())


def studio_client(key: Optional[str] = None):
    """A Gemini API (AI Studio) client for the given key (default: the Mission Control key)."""
    return _client("studio", key)


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _mime(path: str) -> str:
    return mimetypes.guess_type(path)[0] or "video/mp4"


def _cache_key(m: str, path: str) -> Tuple[str, str, str]:
    cred = (vertex_project() or "") if m == "vertex" else hashlib.sha256((api_key() or "").encode()).hexdigest()[:16]
    return (m, cred, _sha256(path))


def _studio_media(client, path: str, timeout_s: int):
    key = _cache_key("studio", path)
    with _LOCK:
        hit = _MEDIA_CACHE.get(key)
    if hit and time.time() - hit[0] < FILE_TTL_S:
        try:
            f = client.files.get(name=hit[1].name)
            if str(getattr(getattr(f, "state", None), "name", getattr(f, "state", "ACTIVE"))) in ("ACTIVE", "State.ACTIVE"):
                MEDIA_STATS["reused"] += 1
                return f
        except Exception:
            pass                                    # expired on Google's side: upload again
    f = client.files.upload(file=path)
    MEDIA_STATS["uploads"] += 1
    deadline = time.time() + timeout_s
    while "PROCESSING" in str(getattr(getattr(f, "state", None), "name", getattr(f, "state", ""))) and time.time() < deadline:
        time.sleep(2)
        f = client.files.get(name=f.name)
    state = str(getattr(getattr(f, "state", None), "name", getattr(f, "state", "ACTIVE")))
    if "ACTIVE" not in state:
        raise RuntimeError(f"Media processing did not complete (state={state})")
    with _LOCK:
        _MEDIA_CACHE[key] = (time.time(), f)
    return f


def _vertex_media(path: str):
    from google.genai import types
    key = _cache_key("vertex", path)
    with _LOCK:
        hit = _MEDIA_CACHE.get(key)
    if hit:
        MEDIA_STATS["reused"] += 1
        return hit[1]
    bucket = os.environ.get("UNDERSTUDY_GCS_BUCKET")
    if bucket:
        uri = upload_to_gcs(path, bucket, recording_object(path))
        part = types.Part.from_uri(file_uri=uri, mime_type=_mime(path))
    else:
        size = os.path.getsize(path)
        if size > INLINE_LIMIT:
            raise RuntimeError(f"Recording is {size // (1024 * 1024)} MB; on Vertex AI recordings over 18 MB need "
                               f"UNDERSTUDY_GCS_BUCKET (Cloud Storage) to be set.")
        with open(path, "rb") as fh:
            part = types.Part.from_bytes(data=fh.read(), mime_type=_mime(path))
    MEDIA_STATS["uploads"] += 1
    with _LOCK:
        _MEDIA_CACHE[key] = (time.time(), part)
    return part


def upload_to_gcs(path: str, bucket: str, object_name: str) -> str:
    """Uploads (once - skipped when the object already exists) and returns the gs:// URI."""
    from google.cloud import storage
    blob = storage.Client().bucket(bucket).blob(object_name)
    if not blob.exists():
        blob.upload_from_filename(path, content_type=_mime(path))
    return f"gs://{bucket}/{object_name}"


def recording_object(path: str) -> str:
    return f"understudy/recordings/{_sha256(path)}{os.path.splitext(path)[1] or '.mp4'}"


def download_from_gcs(uri: str, dest: str) -> str:
    """Fetches a recording another instance stored (gs://bucket/object) - used when a session is restored."""
    from google.cloud import storage
    bucket, _, obj = uri[len("gs://"):].partition("/")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    storage.Client().bucket(bucket).blob(obj).download_to_filename(dest)
    return dest


def generate(model_name: str, system: str, user_prompt: str, media_path: Optional[str], temperature: float,
             timeout_s: int, image: Optional[bytes] = None) -> str:
    """One JSON-mode generation. Returns the raw response text. Raises on SDK/network errors.
    ``image``: PNG bytes sent inline after the prompt (the agent's page screenshot, 9.9 relay)."""
    m = mode()
    if m is None:
        raise RuntimeError(unavailable_reason() or "Gemini is not configured")
    from google.genai import types
    client = _client(m)
    contents: List[Any] = []
    if media_path:
        contents.append(_vertex_media(media_path) if m == "vertex" else _studio_media(client, media_path, timeout_s))
    contents.append(user_prompt)
    if image:
        contents.append(types.Part.from_bytes(data=image, mime_type="image/png"))
    cfg = types.GenerateContentConfig(system_instruction=system, temperature=temperature,
                                      response_mime_type="application/json",
                                      http_options=types.HttpOptions(timeout=int(timeout_s * 1000)),
                                      automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True))
    if m == "vertex" and os.environ.get("UNDERSTUDY_VERTEX_MODEL"):
        model_name = os.environ["UNDERSTUDY_VERTEX_MODEL"]           # explicit Vertex model wins
    try:
        resp = client.models.generate_content(model=model_name, contents=contents, config=cfg)
    except Exception as e:
        fallback = vertex_fallback_model()
        if m == "vertex" and _is_not_found(e) and model_name != fallback:
            # The Mission Control model (often an AI Studio alias such as gemini-flash-latest) is not offered by
            # Vertex AI in this region: use a stable Vertex model instead of failing every call.
            LAST_FALLBACK.update(requested=model_name, used=fallback, at=time.time())
            resp = client.models.generate_content(model=fallback, contents=contents, config=cfg)
        else:
            raise
    return resp.text


LAST_FALLBACK: Dict[str, Any] = {}


def vertex_fallback_model() -> str:
    return (os.environ.get("UNDERSTUDY_VERTEX_FALLBACK_MODEL") or "gemini-2.5-flash").strip()


def _is_not_found(e: Exception) -> bool:
    s = f"{type(e).__name__} {e}"
    return "404" in s or "NOT_FOUND" in s or "was not found" in s or "does not have access" in s
