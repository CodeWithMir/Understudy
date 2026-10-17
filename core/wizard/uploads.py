"""Large recordings straight to Cloud Storage (9.9).

Cloud Run refuses an HTTP/1 request body over 32 MiB before it reaches Understudy (the browser only sees "413"), so a
recording larger than that could never be uploaded through the app on Cloud Run. With ``UNDERSTUDY_GCS_BUCKET`` set:

1. ``begin``  - the browser asks for an upload slot; Understudy checks the name, type and size, and opens a Cloud
   Storage *resumable upload session* for one staging object, bound to the page's origin (so the browser may PUT to
   it, and only from that origin). The session URL is the only credential the browser gets - no keys, no signed
   bucket access.
2. the browser PUTs the file directly to Cloud Storage (no size cap from Cloud Run, upload progress shown).
3. ``take``   - Step 1 names the ``upload_id``; Understudy checks it belongs to this user, downloads the object to a
   temp file (size cap re-checked), deletes the staging object, and the recording goes through the normal checks
   (``guardrails.screen_media``: signature sniffing, Model Armor) exactly like a direct upload.

Upload slots live in the state store (Firestore on Cloud Run), so ``begin`` and ``take`` may run on different
instances. Abandoned staging objects are removed by the bucket's lifecycle rule (``deploy_cloudrun.sh``: 7 days).
Without a bucket the browser uploads through the app as before (fine locally and in Docker - no 32 MiB cap there).
"""
import os
import re
import tempfile
import time
import uuid
from typing import Any, Dict, Optional, Tuple

from core.wizard import store
from core.wizard.guardrails import MAX_MEDIA_BYTES, MEDIA_SIGNATURES, safe_filename

COLLECTION = "uploads"
PREFIX = "understudy/uploads/"
TTL_S = 6 * 3600
CLOUD_RUN_BODY_LIMIT = 32 * 1024 * 1024
_ORIGIN = re.compile(r"^https?://[A-Za-z0-9.-]+(:\d{1,5})?$")
_MIME = {".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime", ".3gp": "video/3gpp",
         ".webm": "video/webm", ".mkv": "video/x-matroska", ".avi": "video/x-msvideo", ".m4a": "audio/mp4",
         ".wav": "audio/wav", ".mp3": "audio/mpeg", ".ogg": "audio/ogg", ".flac": "audio/flac"}


class UploadError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def bucket_name() -> str:
    return (os.environ.get("UNDERSTUDY_GCS_BUCKET") or "").strip()


def on_cloud_run() -> bool:
    return bool(os.environ.get("K_SERVICE"))


def direct_available() -> bool:
    if not bucket_name():
        return False
    try:
        import importlib.util
        return importlib.util.find_spec("google.cloud.storage") is not None
    except Exception:
        return False


def max_bytes() -> int:
    """9.9: the largest recording this deployment can take - Understudy's own limit (UNDERSTUDY_MAX_MEDIA_MB, 250 MB),
    or, on Cloud Run without Cloud Storage, what fits in one request (Cloud Run refuses bodies over 32 MiB; the form
    fields and multipart framing need a little room)."""
    if on_cloud_run() and not direct_available():
        return min(MAX_MEDIA_BYTES, CLOUD_RUN_BODY_LIMIT - 1024 * 1024)
    return MAX_MEDIA_BYTES


def limit_info() -> Dict[str, Any]:
    mb = max_bytes() // (1024 * 1024)
    if mb < MAX_MEDIA_BYTES // (1024 * 1024):
        why = "Google Cloud Run accepts at most 32 MB per upload, and Cloud Storage isn't set up for larger recordings"
    elif direct_available():
        why = "large recordings go straight to Cloud Storage"
    else:
        why = ""
    return {"max_media_mb": mb, "max_media_bytes": max_bytes(), "max_media_why": why}


def _bucket():
    from google.cloud import storage
    return storage.Client().bucket(bucket_name())


def plan(filename: str, size: int) -> Dict[str, Any]:
    """What the browser should do with a recording of ``size`` bytes."""
    if direct_available():
        return {"mode": "gcs"}
    out: Dict[str, Any] = {"mode": "direct"}
    if on_cloud_run():
        out["direct_limit_bytes"] = CLOUD_RUN_BODY_LIMIT
        if size > CLOUD_RUN_BODY_LIMIT:
            out["message"] = (f"{filename} is {size // (1024 * 1024)} MB. On Google Cloud Run a file over 32 MB can only be "
                              "uploaded through Cloud Storage, which isn't set up on this server - ask your admin to set "
                              "UNDERSTUDY_GCS_BUCKET (deploy_cloudrun.sh does it), or upload a shorter recording.")
    return out


def _check(filename: str, size: int) -> str:
    name = safe_filename(filename or "")
    ext = os.path.splitext(name)[1].lower()
    if ext not in MEDIA_SIGNATURES:
        raise UploadError("Unsupported file type. Use MP4, WebM, MOV, MKV, AVI, MP3, WAV, M4A, OGG or FLAC.", 415)
    if not isinstance(size, int) or size <= 0:
        raise UploadError("The file is empty.")
    if size > MAX_MEDIA_BYTES:
        raise UploadError(f"File is larger than {MAX_MEDIA_BYTES // (1024 * 1024)} MB", 413)
    return name


def begin(owner: Optional[str], filename: str, size: int, origin: str) -> Dict[str, Any]:
    """Opens a resumable upload session for one staging object. Returns {mode, upload_id, upload_url, content_type}."""
    if not direct_available():
        raise UploadError("Direct uploads need Cloud Storage (UNDERSTUDY_GCS_BUCKET).", 409)
    name = _check(filename, size)
    origin = (origin or "").strip().rstrip("/")
    if not _ORIGIN.match(origin):
        raise UploadError("Unknown page origin for the upload.")
    upload_id = uuid.uuid4().hex
    obj = f"{PREFIX}{upload_id}/{name}"
    ctype = _MIME.get(os.path.splitext(name)[1].lower(), "application/octet-stream")
    try:
        url = _bucket().blob(obj).create_resumable_upload_session(content_type=ctype, size=size, origin=origin)
    except Exception as e:
        raise UploadError(f"Cloud Storage refused the upload ({type(e).__name__}: {str(e)[:200]}). Check that the "
                          "service account can write to the bucket.", 502)
    store.get_store().put(COLLECTION, upload_id, {"id": upload_id, "owner": (owner or "").lower(), "object": obj,
                                                  "filename": name, "size": size, "created_at": time.time()})
    return {"mode": "gcs", "upload_id": upload_id, "upload_url": url, "content_type": ctype}


def take(upload_id: str, owner: Optional[str]) -> Tuple[str, str]:
    """Moves a finished upload to a local temp file (the caller deletes it). Returns (tmp_path, filename)."""
    if not re.fullmatch(r"[0-9a-f]{32}", upload_id or ""):
        raise UploadError("Unknown upload.", 404)
    st = store.get_store()
    rec = st.get(COLLECTION, upload_id)
    if not rec or rec.get("owner", "") != (owner or "").lower():
        raise UploadError("Unknown upload.", 404)
    if time.time() - float(rec.get("created_at") or 0) > TTL_S:
        st.delete(COLLECTION, upload_id)
        raise UploadError("This upload has expired - choose the recording again.", 410)
    blob = _bucket().blob(rec["object"])
    try:
        blob.reload()
    except Exception:
        raise UploadError("The recording hasn't finished uploading - wait for it, or choose it again.", 409)
    if not blob.size or blob.size > MAX_MEDIA_BYTES:
        raise UploadError(f"File is larger than {MAX_MEDIA_BYTES // (1024 * 1024)} MB", 413)
    fd, tmp = tempfile.mkstemp(prefix="wizard-upload-")
    os.close(fd)
    try:
        blob.download_to_filename(tmp)
    except Exception as e:
        os.remove(tmp)
        raise UploadError(f"Could not read the uploaded recording ({type(e).__name__}).", 502)
    if os.path.getsize(tmp) > MAX_MEDIA_BYTES:
        os.remove(tmp)
        raise UploadError(f"File is larger than {MAX_MEDIA_BYTES // (1024 * 1024)} MB", 413)
    try:
        blob.delete()
    except Exception:
        pass                                    # the bucket's lifecycle rule removes it later
    st.delete(COLLECTION, upload_id)
    return tmp, rec["filename"]
