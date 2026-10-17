"""Google Cloud / Gemini set-up checker ("doctor").

Used by the start scripts (``scripts/gcp_check.py --quick``: configuration only, no network), by
``python scripts/gcp_check.py`` (full: real Gemini, Firestore and Cloud Storage round-trips) and by Mission Control >
AI, security & account > "Check Google Cloud set-up" (``POST /api/admin/gcp/check``).

Every result says what was checked, what happened, and - when something is wrong - the exact command that fixes it.
Nothing secret is ever returned (keys are masked, no tokens).
"""
from __future__ import annotations

import os
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

PASS, FAIL, WARN, SKIP = "PASS", "FAIL", "WARN", "SKIP"

APIS = {"vertex": "aiplatform.googleapis.com", "firestore": "firestore.googleapis.com",
        "storage": "storage.googleapis.com"}
ROLES = {"vertex": "roles/aiplatform.user", "firestore": "roles/datastore.user",
         "storage": "roles/storage.objectAdmin"}


def _r(cid: str, name: str, status: str, detail: str, fix: str = "") -> Dict[str, Any]:
    return {"id": cid, "name": name, "status": status, "detail": detail[:600], "fix": fix[:800]}


def _project() -> Optional[str]:
    from core.wizard import gemini
    return gemini.vertex_project()


def _explain(e: Exception, service: str) -> str:
    """Turns a Google error into the command that fixes it."""
    s = f"{type(e).__name__}: {e}"
    p = _project() or "<project-id>"
    if "not a valid json" in s or "File " in s and "was not found" in s:
        return ("The credentials file is missing or damaged. Run:  gcloud auth application-default login   "
                "(or point GOOGLE_APPLICATION_CREDENTIALS at a valid service-account key file).")
    if "DefaultCredentialsError" in s or "Could not automatically determine credentials" in s or "credentials were not found" in s:
        return ("No Google credentials on this machine. Run:  gcloud auth application-default login   "
                "(or set GOOGLE_APPLICATION_CREDENTIALS to a service-account key file).")
    if "SERVICE_DISABLED" in s or "has not been used in project" in s or "is disabled" in s or "API has not been enabled" in s:
        return f"Enable the API:  gcloud services enable {APIS.get(service, service)} --project {p}"
    if "BILLING" in s.upper() and "DISABLED" in s.upper():
        return f"Turn on billing for project {p}: https://console.cloud.google.com/billing/linkedaccount?project={p}"
    if service == "firestore" and ("does not exist" in s or ("NotFound" in s and "database" in s.lower())):
        return (f"Create the Firestore database (Native mode):  gcloud firestore databases create "
                f"--location={os.environ.get('GOOGLE_CLOUD_LOCATION') or 'us-central1'} --type=firestore-native --project {p}")
    if service == "storage" and ("NotFound" in s or "404" in s or "does not exist" in s):
        b = os.environ.get("UNDERSTUDY_GCS_BUCKET") or "<bucket>"
        return f"Create the bucket:  gcloud storage buckets create gs://{b} --location={os.environ.get('GOOGLE_CLOUD_LOCATION') or 'us-central1'} --project {p}"
    if "PERMISSION_DENIED" in s or "403" in s or "Forbidden" in s or "does not have" in s:
        return (f"Your account / service account lacks a role. Grant it:  gcloud projects add-iam-policy-binding {p} "
                f"--member=user:<you@example.com> --role={ROLES.get(service, 'roles/editor')}  "
                f"(use serviceAccount:<email> for a service account)")
    if "quota project" in s.lower():
        return f"Set the quota project:  gcloud auth application-default set-quota-project {p}"
    if "API_KEY_INVALID" in s or "API key not valid" in s:
        return "The Gemini API key is not valid - create a new one at https://aistudio.google.com/apikey and save it in Mission Control."
    if "RESOURCE_EXHAUSTED" in s or "429" in s:
        return "Quota exhausted - wait a minute, or raise the quota in the Google Cloud console (IAM & Admin > Quotas)."
    if "timed out" in s.lower() or "deadline" in s.lower() or "ConnectionError" in s or "proxy" in s.lower():
        return "Network problem reaching Google - check internet access / proxy settings (HTTPS_PROXY) and try again."
    return "See the error above; README > Google Cloud set-up > Troubleshooting lists the common causes."


def _timed(fn: Callable[[], Any]):
    t0 = time.time()
    out = fn()
    return out, int((time.time() - t0) * 1000)


def check(network: bool = True) -> Dict[str, Any]:
    from core.wizard import gemini, store
    from core.wizard.llm import selected_model
    res: List[Dict[str, Any]] = []
    g = gemini.describe()
    mode = g.get("mode")
    vertex_on = (os.environ.get("GOOGLE_GENAI_USE_VERTEXAI") or "").lower() in ("1", "true", "yes", "on") \
        or bool(os.environ.get("UNDERSTUDY_VERTEX_PROJECT"))
    want_fs = (os.environ.get("UNDERSTUDY_STORE") or "local").lower() == "firestore"
    bucket = os.environ.get("UNDERSTUDY_GCS_BUCKET") or ""
    needs_gcp = vertex_on or want_fs or bool(bucket)
    project = _project()

    # 1. what is configured --------------------------------------------------------------------------------------
    ai_desc = {"vertex": f"Vertex AI, project {g.get('project')}, location {g.get('location')}",
               "studio": "Gemini API key (AI Studio)"}.get(mode or "")
    if mode:
        res.append(_r("ai_config", "Gemini access", PASS, f"{ai_desc}; model {selected_model()}"))
    else:
        res.append(_r("ai_config", "Gemini access", WARN, gemini.unavailable_reason() or "Gemini is not configured.",
                      "Either save a Gemini API key in Mission Control (https://aistudio.google.com/apikey), or use Vertex AI: "
                      "set GOOGLE_GENAI_USE_VERTEXAI=true and GOOGLE_CLOUD_PROJECT=<project-id> in .env "
                      "(scripts/gcp_setup.sh / .ps1 does it for you)."))
    st = store.describe()
    if want_fs and st.get("degraded"):
        res.append(_r("store_config", "State store", FAIL,
                      f"Firestore is switched on but unavailable, so Understudy is using local files: {st['degraded']}",
                      _explain(Exception(st["degraded"]), "firestore")))
    else:
        res.append(_r("store_config", "State store", PASS,
                      "Cloud Firestore (shared by every instance)" if want_fs else f"local files ({st.get('location')})"))
    from core.wizard import cloud_armor
    armor = cloud_armor.config()
    if armor["enabled"]:
        res.append(_r("armor_config", "Google Cloud Model Armor", PASS,
                      f"on: {armor['template']} ({'fail-closed' if armor['fail_closed'] else 'fail-open'}; set in "
                      f"{'Mission Control' if armor['source'] == 'admin' else 'UNDERSTUDY_MODEL_ARMOR_TEMPLATE'})"))
    else:
        res.append(_r("armor_config", "Google Cloud Model Armor", SKIP,
                      "off - the built-in Model Armor stages screen every input",
                      "Optional: Mission Control > AI, security & account > Google Cloud Model Armor"))
    if needs_gcp and not project:
        res.append(_r("project", "Google Cloud project", FAIL, "GOOGLE_CLOUD_PROJECT is not set.",
                      "Add GOOGLE_CLOUD_PROJECT=<project-id> to .env (find it with: gcloud projects list)."))

    # 2. libraries ----------------------------------------------------------------------------------------------
    def _lib(cid, name, mod, needed):
        if not needed:
            return
        try:
            __import__(mod)
            res.append(_r(cid, name, PASS, f"{mod} installed"))
        except Exception:
            res.append(_r(cid, name, FAIL, f"{mod} is not installed.", "pip install -r requirements-gcp.txt"))
    _lib("lib_fs", "Firestore client", "google.cloud.firestore", want_fs)
    _lib("lib_gcs", "Cloud Storage client", "google.cloud.storage", bool(bucket))

    # 3. credentials (Application Default Credentials) -------------------------------------------------------------
    if needs_gcp:
        try:
            import google.auth
            creds, adc_project = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
            who = getattr(creds, "service_account_email", None) or getattr(creds, "_service_account_email", None)
            kind = f"service account {who}" if who and who != "default" else (
                "Cloud Run / Compute service account" if type(creds).__name__ == "Credentials" and "compute" in type(creds).__module__
                else "your Google account (gcloud application-default login)")
            src = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
            res.append(_r("adc", "Google credentials", PASS,
                          f"{kind}" + (f" from {os.path.basename(src)}" if src else "") +
                          (f"; default project {adc_project}" if adc_project else "")))
            if project and adc_project and adc_project != project:
                res.append(_r("adc_project", "Credentials project", WARN,
                              f"Credentials default to project {adc_project}, Understudy uses {project}.",
                              f"Usually fine. To bill API calls to {project}: gcloud auth application-default set-quota-project {project}"))
        except Exception as e:
            res.append(_r("adc", "Google credentials", FAIL, f"{type(e).__name__}: {e}", _explain(e, "vertex")))

    if not network:
        return _summary(res, network)
    no_creds = any(r["id"] in ("adc", "project") and r["status"] == FAIL for r in res)
    if no_creds:
        for cid, name, on in (("ai_call", "Gemini call", mode == "vertex"), ("firestore", "Firestore read/write", want_fs),
                              ("gcs", "Cloud Storage read/write", bool(bucket))):
            if on:
                res.append(_r(cid, name, SKIP, "skipped - fix the credentials / project first"))
        if mode != "vertex" and mode:
            pass                                              # an API-key Gemini call does not need Google credentials
        else:
            return _summary(res, network)

    # 4. live round-trips ------------------------------------------------------------------------------------------
    if mode and not (no_creds and mode == "vertex"):
        try:
            gemini.LAST_FALLBACK.clear()
            text, ms = _timed(lambda: gemini.generate(selected_model(), "You are a health check. Reply with JSON only.",
                                                      'Reply exactly: {"ok": true}', None, 0.0, 45))
            used = gemini.LAST_FALLBACK.get("used")
            res.append(_r("ai_call", "Gemini call", PASS if "ok" in (text or "") else WARN,
                          f"answered in {ms} ms" + (f" (model {selected_model()} is not offered by Vertex AI here; "
                                                    f"used {used})" if used else ""),
                          "" if not used else f"Pick a Vertex model in Mission Control or set UNDERSTUDY_VERTEX_MODEL={used}."))
        except Exception as e:
            res.append(_r("ai_call", "Gemini call", FAIL, f"{type(e).__name__}: {str(e)[:300]}", _explain(e, "vertex")))
    if no_creds:
        return _summary(res, network)
    if want_fs:
        try:
            st_obj = store.FirestoreStore()
            doc_id = "check-" + uuid.uuid4().hex[:8]

            def _rt():
                st_obj.put("healthcheck", doc_id, {"ok": True, "at": time.time()})
                got = st_obj.get("healthcheck", doc_id)
                st_obj.delete("healthcheck", doc_id)
                return got
            got, ms = _timed(_rt)
            res.append(_r("firestore", "Firestore read/write", PASS if got and got.get("ok") else FAIL,
                          f"write + read + delete in {ms} ms (database "
                          f"{os.environ.get('UNDERSTUDY_FIRESTORE_DATABASE') or '(default)'})"))
        except Exception as e:
            res.append(_r("firestore", "Firestore read/write", FAIL, f"{type(e).__name__}: {str(e)[:300]}",
                          _explain(e, "firestore")))
    if bucket:
        try:
            from google.cloud import storage

            def _rt():
                blob = storage.Client(project=project).bucket(bucket).blob(f"understudy/healthcheck/{uuid.uuid4().hex[:8]}.txt")
                blob.upload_from_string("ok", content_type="text/plain")
                data = blob.download_as_bytes()
                blob.delete()
                return data
            data, ms = _timed(_rt)
            res.append(_r("gcs", "Cloud Storage read/write", PASS if data == b"ok" else FAIL,
                          f"gs://{bucket}: upload + download + delete in {ms} ms"))
        except Exception as e:
            res.append(_r("gcs", "Cloud Storage read/write", FAIL, f"{type(e).__name__}: {str(e)[:300]}",
                          _explain(e, "storage")))
    if armor["enabled"]:
        t = cloud_armor.test_template(armor["template"])
        if t["ok"]:
            res.append(_r("armor", "Model Armor screening", PASS,
                          f"sample text passed ({t['clean']['ms']} ms), sample prompt injection blocked by "
                          f"{', '.join(t['attack']['filters'])} ({t['attack']['ms']} ms)"))
        else:
            res.append(_r("armor", "Model Armor screening", FAIL if not t["reachable"] else WARN,
                          t.get("error") or "the template's verdicts are not as expected", t.get("fix") or ""))
    return _summary(res, network)


def _summary(res: List[Dict[str, Any]], network: bool) -> Dict[str, Any]:
    worst = FAIL if any(r["status"] == FAIL for r in res) else WARN if any(r["status"] == WARN for r in res) else PASS
    return {"status": worst, "network": network, "checks": res, "checked_at": time.time()}


def format_text(report: Dict[str, Any]) -> str:
    icon = {PASS: "OK  ", FAIL: "FAIL", WARN: "WARN", SKIP: "skip"}
    lines = []
    for r in report["checks"]:
        lines.append(f"   [{icon[r['status']]}] {r['name']:<26} {r['detail']}")
        if r["fix"] and r["status"] != PASS:
            lines.append(f"          fix: {r['fix']}")
    return "\n".join(lines)
