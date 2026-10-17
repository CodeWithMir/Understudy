"""Offline recall of learned AI answers ("learned skills" for the AI steps).

Every time Gemini (AI Studio key or Vertex AI) gives an answer that passes validation, Understudy keeps a compact
*memory* of it: which step asked (prompt id), a fingerprint of the question (normalised words, field names, step
shapes, numbers, the recording's SHA-256 - not the raw SOP or page text) and the validated answer as the AI gave it
(with secret values removed; it can quote SOP sentences, e.g. a rule's source_text).

When AI is NOT available - no key, AI switched off, quota used up, network down - ``llm.generate_json`` asks this
module first. If an earlier answer fits the new problem closely enough it is re-used, adapted to the new inputs,
and labelled ``LEARNED_SKILL`` (never ``ACTUAL_AI_API``) with the match score and where it was learned. Only when
nothing fits does the step fall back to the deterministic code (``LOCAL_HEURISTIC``) as before.

Matching is deliberately conservative and different for each step:

    step                       must be equal (gates)                       similar enough (score)           min
    WATCH_ANALYZE              target host, page path                      recording hash, form, SOP        0.72
    WATCH_REFINE               target host                                 understanding + corrections      0.90
    WATCH_URLS                 the same recording (SHA-256)                -                                1.00
    FRAMEWORK_RESOLVE          app type                                    tool name                        0.92
    BUILD_PARAMETERIZE         framework                                   step shape + parameter keys      0.85
    BUILD_GENERATE_SUITE       framework, parameter keys                   step shape (action/selector)     0.95
    GOVERN_EXTRACT_RULES       the same SOP (sentences, any order/case)    parameter keys                   0.85
    VERIFY_SELF_HEAL           target host                                 error, steps, page fields        0.80
    ENV_UPGRADE_ADVICE         finding title, OS, shell                    facts                            0.90
    GOVERN_COMPLIANCE_REVIEW,
    CODE_HEALTH_FIX            identical question only (value-dependent answers)                            1.00
    MODEL_ARMOR_CLASSIFIER     identical text only, and only a remembered BLOCK verdict is re-used (fail closed)

A recalled answer still goes through every safety gate the AI answer would (Model Armor on transcribed text,
parameter screening, the generated-code scanner and syntax check, rule normalisation, the self-heal guard).

Storage: ``<UNDERSTUDY_SKILLS_DIR>/_recall/<PROMPT_ID>/<id>.json`` on the machine, and with ``UNDERSTUDY_STORE=firestore``
also the shared store (collection ``recall``), so every Cloud Run instance recalls what any instance learned.
Secret values are never stored. ``UNDERSTUDY_RECALL=off`` disables recall; Mission Control > Learned skills lists and
deletes the memories.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlsplit

SOURCE = "LEARNED_SKILL"
MAX_PER_PROMPT = 150
_LOCK = threading.RLock()
_CACHE: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}
_HASH_CACHE: Dict[Tuple[str, int, float], str] = {}

_STOP = set("""a an the and or of to in on for with by at from is are be was were this that these those it its as
into than then must should can may will shall not no all any each every only per our your their his her you we they
please also when if else""".split())


def enabled() -> bool:
    return os.environ.get("UNDERSTUDY_RECALL", "on").lower() not in ("0", "off", "false", "no")


def root() -> str:
    from core.wizard.skills import skills_dir
    return os.path.join(skills_dir(), "_recall")


def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


# ---------------------------------------------------------------------------
# Feature helpers
# ---------------------------------------------------------------------------
def words(text: Any) -> Set[str]:
    t = text if isinstance(text, str) else json.dumps(text, sort_keys=True, default=str)
    return {w for w in re.findall(r"[a-z][a-z0-9_]{1,}", t.lower()) if w not in _STOP}


def numbers(text: Any) -> List[str]:
    """Every number / date / amount in the text, normalised ("$5,000" -> "5000")."""
    t = text if isinstance(text, str) else json.dumps(text, default=str)
    out = set()
    for m in re.findall(r"\d[\d,]*(?:\.\d+)?", t or ""):
        n = m.replace(",", "")
        if "." in n:
            n = n.rstrip("0").rstrip(".")
        out.add(n)
    return sorted(out)


def jaccard(a: Any, b: Any) -> float:
    a, b = set(a or []), set(b or [])
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# --- soft (semantic-lite) matching ---------------------------------------------------------------------------
# Plain Jaccard treats "supplier_name" and "vendor_name" as unrelated. The soft matcher stems words, maps a small
# vetted business-synonym table onto one canonical term and accepts near-spellings (character-trigram Dice >= 0.75),
# so a paraphrased form or SOP still finds the skill it should - fully offline, deterministic and explainable.
# It only feeds the *score*; the hard gates (host, SOP sentences, numbers ...) are unchanged, so it can never make
# an unsafe answer match.
_SYN = {"supplier": "vendor", "payee": "vendor", "seller": "vendor", "merchant": "vendor", "provider": "vendor",
        "amount": "total", "sum": "total", "price": "total", "cost": "total", "value": "total", "charge": "total",
        "bill": "invoice", "receipt": "invoice", "voucher": "invoice", "day": "date", "dated": "date",
        "limit": "max", "cap": "max", "ceiling": "max", "threshold": "max", "approver": "approve", "authoriser": "approve",
        "authorizer": "approve", "forbid": "deny", "prohibit": "deny", "ban": "deny", "reject": "deny",
        "client": "customer", "buyer": "customer", "mail": "email", "submit": "send", "post": "send", "commit": "send"}


def _stem(w: str) -> str:
    if len(w) <= 4:
        return w
    if w.endswith("ies"):
        return w[:-3] + "y"
    if re.search(r"(ss|x|z|ch|sh)es$", w):
        return w[:-2]
    if w.endswith("ing") and len(w) > 6:
        return w[:-3]
    if w.endswith("ed") and len(w) > 5:
        return w[:-2]
    if w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def _canon(w: str) -> str:
    w = _stem(w)
    return _SYN.get(w, w)


def _soft_tokens(items: Any) -> Set[str]:
    out: Set[str] = set()
    for x in items or []:
        for t in re.findall(r"[a-z][a-z0-9]+", str(x).lower()):
            if t not in _STOP:
                out.add(_canon(t))
    return out


def _tri(w: str) -> Set[str]:
    w = f"^{w}$"
    return {w[i:i + 3] for i in range(len(w) - 2)}


def _tok_sim(a: str, b: str) -> float:
    if a == b:
        return 1.0
    ta, tb = _tri(a), _tri(b)
    d = 2 * len(ta & tb) / (len(ta) + len(tb)) if ta and tb else 0.0
    return d if d >= 0.75 else 0.0


def soft_jaccard(a: Any, b: Any) -> float:
    A, B = _soft_tokens(a), _soft_tokens(b)
    if not A and not B:
        return 1.0
    if not A or not B:
        return 0.0
    inter = 0.0
    for x in A:
        inter += max((_tok_sim(x, y) for y in B), default=0.0)
    inter = min(inter, len(A), len(B))
    return inter / (len(A) + len(B) - inter)


def soft_pairs(a: Any, b: Any, limit: int = 6) -> List[List[str]]:
    """Terms that matched only because of the soft matcher (shown to the user as "why it matched")."""
    raw_a = {t for x in a or [] for t in re.findall(r"[a-z][a-z0-9]+", str(x).lower())}
    raw_b = {t for x in b or [] for t in re.findall(r"[a-z][a-z0-9]+", str(x).lower())}
    pairs = []
    for x in sorted(raw_a - raw_b):
        for y in sorted(raw_b - raw_a):
            if x not in _STOP and y not in _STOP and _tok_sim(_canon(x), _canon(y)) > 0:
                pairs.append([x, y])
                break
    return pairs[:limit]


def seq_sim(a: List[str], b: List[str]) -> float:
    if not a and not b:
        return 1.0
    return difflib.SequenceMatcher(None, list(a or []), list(b or [])).ratio()


def host_path(url: str) -> Tuple[str, str]:
    try:
        u = urlsplit(url or "")
        return (u.hostname or "").lower(), (u.path or "/").rstrip("/") or "/"
    except Exception:
        return "", "/"


def _host_from_inventory(inv_text: str) -> Tuple[str, str]:
    try:
        return host_path(json.loads(inv_text).get("url") or "")
    except Exception:
        m = re.search(r'"url":\s*"([^"]+)"', inv_text or "")
        return host_path(m.group(1) if m else "")


def form_fields(inv_text: Any) -> List[str]:
    """Field identities (id / name / label) of a DOM inventory - which form this is, not its values."""
    try:
        data = json.loads(inv_text) if isinstance(inv_text, str) else (inv_text or {})
        out = set()
        for e in data.get("elements") or []:
            for k in ("id", "name", "label", "kind", "testid"):
                if e.get(k):
                    out.add(f"{k}:{str(e[k]).lower()[:60]}")
        return sorted(out)
    except Exception:
        return sorted(words(inv_text))


def step_shape(steps: Any) -> List[str]:
    out = []
    for s in steps or []:
        if isinstance(s, dict):
            out.append(f"{s.get('action')}|{s.get('selector') or ''}|{s.get('param_key') or ''}")
    return out


def param_keys(params: Any) -> List[str]:
    return sorted({str(p.get("key")) for p in (params or []) if isinstance(p, dict) and p.get("key")})


def media_hash(path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    try:
        st = os.stat(path)
        key = (path, st.st_size, st.st_mtime)
        if key not in _HASH_CACHE:
            if len(_HASH_CACHE) > 256:
                _HASH_CACHE.clear()
            h = hashlib.sha256()
            with open(path, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
            _HASH_CACHE[key] = h.hexdigest()
        return _HASH_CACHE[key]
    except OSError:
        return None


def exact_digest(prompt_id: str, variables: Dict[str, Any], mhash: Optional[str]) -> str:
    blob = json.dumps({"p": prompt_id, "v": variables, "m": mhash}, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def _norm_tool(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(name or "").lower())


def _error_lines(logs: str) -> List[str]:
    """The error-looking lines of a run log, normalised like learned-skill signatures."""
    from core.wizard.skills import signature
    lines = [l for l in (logs or "").splitlines() if re.search(r"(?i)error|exception|timeout|not found|failed|cannot", l)]
    return [signature("", l)[2:] for l in lines[-12:]]


# ---------------------------------------------------------------------------
# Per-step policies: features(), gates (must be equal), score(), adapt()
# ---------------------------------------------------------------------------
def _f_watch(v: Dict[str, Any], mh: Optional[str]) -> Dict[str, Any]:
    host, path = host_path(v.get("target_url", ""))
    return {"host": host, "path": path, "media": mh, "sop": sorted(words(v.get("sop_text", ""))),
            "sop_numbers": numbers(v.get("sop_text", "")), "fields": form_fields(v.get("dom_inventory", ""))}


def _s_watch(q: Dict[str, Any], m: Dict[str, Any]) -> Tuple[float, Dict[str, Any]]:
    same_media = bool(q.get("media") and q.get("media") == m.get("media"))
    fields = soft_jaccard(q["fields"], m["fields"])
    sop = soft_jaccard(q["sop"], m["sop"])
    if same_media:
        # the very same recording of the same page: the earlier AI answer applies (SOP may only change values)
        score = 0.85 + 0.10 * fields + 0.05 * sop
    else:
        # another recording of the same page: the form decides the process; the SOP mostly decides values, which
        # come from this session (SOP lines) or from the user, never silently from the old recording
        score = 0.75 * fields + 0.25 * sop
    detail = {"same_recording": same_media, "form_match": round(fields, 2), "sop_match": round(sop, 2)}
    pairs = soft_pairs(q["fields"], m["fields"]) + soft_pairs(q["sop"], m["sop"], 3)
    if pairs:
        detail["soft_terms"] = pairs[:6]
    return round(score, 3), detail


def _f_refine(v: Dict[str, Any], mh: Optional[str]) -> Dict[str, Any]:
    host, path = host_path(v.get("target_url", ""))
    prev = v.get("previous_analysis") or {}
    return {"host": host, "path": path, "media": mh, "steps": step_shape(prev.get("steps")),
            "keys": param_keys(prev.get("parameters")),
            # corrections can contain anything the user typed: keep only hashes of their words
            "corr": sorted(hashlib.sha1(w.encode()).hexdigest()[:12] for w in words(v.get("corrections", ""))),
            "corr_numbers": numbers(v.get("corrections", ""))}


def _s_refine(q, m):
    corr = jaccard(q["corr"], m["corr"])
    steps = seq_sim(q["steps"], m["steps"])
    keys = jaccard(q["keys"], m["keys"])
    score = 0.5 * corr + 0.3 * steps + 0.2 * keys
    return round(score, 3), {"correction_match": round(corr, 2), "step_match": round(steps, 2)}


def _f_framework(v, mh):
    return {"tool": _norm_tool(v.get("tool_name", "")), "app_type": str(v.get("app_type") or "")}


def _s_framework(q, m):
    s = 1.0 if q["tool"] == m["tool"] else difflib.SequenceMatcher(None, q["tool"], m["tool"]).ratio()
    return round(s, 3), {"tool_match": round(s, 2)}


def _f_param(v, mh):
    return {"framework": str(v.get("framework_name") or ""), "steps": step_shape(v.get("steps")),
            "keys": param_keys(v.get("parameters")), "sop": sorted(words(v.get("sop_text", "")))}


def _s_param(q, m):
    steps, keys, sop = seq_sim(q["steps"], m["steps"]), soft_jaccard(q["keys"], m["keys"]), soft_jaccard(q["sop"], m["sop"])
    d = {"step_match": round(steps, 2), "key_match": round(keys, 2)}
    pairs = soft_pairs(q["keys"], m["keys"])
    if pairs:
        d["soft_terms"] = pairs
    return round(0.45 * steps + 0.45 * keys + 0.10 * sop, 3), d


def _f_suite(v, mh):
    fw = v.get("framework") or {}
    return {"framework": str(fw.get("id") or fw.get("name") or ""), "steps": step_shape(v.get("steps")),
            "keys": param_keys(v.get("parameter_keys")), "api": sorted(words(v.get("api_plan") or {})),
            "notes": sorted(words(v.get("healing_notes") or []))}


def _s_suite(q, m):
    steps = seq_sim(q["steps"], m["steps"])
    api = jaccard(q["api"], m["api"])
    return round(0.85 * steps + 0.10 * api + 0.05 * jaccard(q["notes"], m["notes"]), 3), {"step_match": round(steps, 2)}


def sop_sentences(text: Any) -> List[str]:
    """The SOP as a normalised, order-insensitive list of sentences: case, spacing and punctuation don't matter,
    but every word, name and number does (a vendor moved from the approved to the blocked list is another SOP)."""
    t = str(text or "").replace("\r\n", "\n")
    parts = re.split(r"(?<=[.!?])\s+|\n+", t)
    return sorted({re.sub(r"[^a-z0-9$%/.,:-]+", " ", p.lower()).strip(" .") for p in parts if p.strip()} - {""})


def _f_rules(v, mh):
    return {"sop": sorted(words(v.get("sop_text", ""))), "sop_numbers": numbers(v.get("sop_text", "")),
            "sop_sentences": hashlib.sha256(json.dumps(sop_sentences(v.get("sop_text", ""))).encode()).hexdigest(),
            "keys": param_keys(v.get("parameter_keys"))}


def _s_rules(q, m):
    sop, keys = jaccard(q["sop"], m["sop"]), jaccard(q["keys"], m["keys"])
    return round(0.8 * sop + 0.2 * keys, 3), {"sop_match": round(sop, 2), "key_match": round(keys, 2)}


def _f_heal(v, mh):
    host, path = _host_from_inventory(v.get("dom_inventory", ""))
    return {"host": host, "errors": sorted(set(_error_lines(v.get("logs", "")))), "steps": step_shape(v.get("steps")),
            "fields": form_fields(v.get("dom_inventory", ""))}


def _s_heal(q, m):
    err = jaccard(q["errors"], m["errors"]) if (q["errors"] or m["errors"]) else 0.0
    steps, fields = seq_sim(q["steps"], m["steps"]), jaccard(q["fields"], m["fields"])
    return round(0.5 * err + 0.3 * steps + 0.2 * fields, 3), {"error_match": round(err, 2),
                                                              "step_match": round(steps, 2), "page_match": round(fields, 2)}


def _f_env(v, mh):
    f = v.get("finding") or {}
    return {"title": str(f.get("title") or ""), "category": str(f.get("category") or ""), "os": str(v.get("os") or "").split(" ")[0],
            "shell": str(v.get("shell") or ""), "facts": sorted(words(v.get("facts") or {}))}


def _s_env(q, m):
    s = jaccard(q["facts"], m["facts"])
    return round(s, 3), {"facts_match": round(s, 2)}


def _f_pages(v, mh):
    return {"media": mh or ""}


def _s_pages(q, m):
    # the page addresses come from the recording alone: only the very same recording is a match
    return (1.0, {"same_recording": True}) if q.get("media") else (0.0, {})


def _f_exact(v, mh):
    return {}


def _s_exact(q, m):
    return 1.0, {}


# --- adapters: fit a remembered answer to the new inputs -------------------------------------------------------
def _a_param(data: Dict[str, Any], v: Dict[str, Any], detail: Dict[str, Any]) -> Dict[str, Any]:
    """Keep the learned parameter definitions (labels, types, descriptions) but THIS session's values and sources."""
    cur = {p.get("key"): p for p in (v.get("parameters") or []) if isinstance(p, dict) and p.get("key")}
    out = []
    for p in data.get("parameters") or []:
        q = dict(p)
        if q.get("key") in cur:
            q["value"] = cur[q["key"]].get("value")
            q["source"] = cur[q["key"]].get("source") or q.get("source")
        else:
            q["value"], q["source"] = "", "learned"
        if q.get("type") == "secret":
            q["value"] = ""
        out.append(q)
    return dict(data, parameters=out)


def _a_watch(data: Dict[str, Any], v: Dict[str, Any], detail: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(data)
    if not detail.get("same_recording"):
        # Another recording that nobody watched: what was SEEN in the old one (transcript, moments, evidence, the
        # values typed) does not belong to it. Keep the learned process; values are filled in from this session
        # (SOP / page) by the caller, or left empty for the user.
        out.pop("observed_text", None)
        out["summary"] = ("Process learned from an earlier recording of this page (this recording was not watched): "
                          + str(data.get("summary") or ""))[:1500]
        out["steps"] = [{k: val for k, val in st.items() if k not in ("video_time", "evidence")}
                        for st in data.get("steps") or []]
        params = []
        for p in data.get("parameters") or []:
            q = dict(p)
            q["value"] = ""
            q["source"] = "learned"
            params.append(q)
        out["parameters"] = params
    return out


def _a_rules(data, v, detail):
    keys = set(param_keys(v.get("parameter_keys")))
    return dict(data, rules=[r for r in data.get("rules") or [] if not keys or r.get("field") in keys])


def _a_none(data, v, detail):
    return data


POLICIES: Dict[str, Dict[str, Any]] = {
    "WATCH_ANALYZE": {"features": _f_watch, "gates": ("host", "path"), "score": _s_watch, "min": 0.72, "adapt": _a_watch,
                      "label": lambda v, d: f"{d.get('process_name') or 'process'} on {host_path(v.get('target_url', ''))[0]}"
                                            f"{host_path(v.get('target_url', ''))[1]}"},
    "WATCH_REFINE": {"features": _f_refine, "gates": ("host", "path", "corr_numbers"), "score": _s_refine, "min": 0.90,
                     "adapt": _a_none,
                     "label": lambda v, d: f"correction applied ({len(str(v.get('corrections', '')).splitlines())} round(s))"},
    "WATCH_URLS": {"features": _f_pages, "gates": ("media",), "score": _s_pages, "min": 1.0, "adapt": _a_none,
                   "label": lambda v, d: f"{len(d.get('pages') or [])} page address(es) read from the recording"},
    "FRAMEWORK_RESOLVE": {"features": _f_framework, "gates": ("app_type",), "score": _s_framework, "min": 0.92,
                          "adapt": _a_none, "label": lambda v, d: f"tool '{v.get('tool_name')}' -> {d.get('name')}"},
    "BUILD_PARAMETERIZE": {"features": _f_param, "gates": ("framework",), "score": _s_param, "min": 0.85,
                           "adapt": _a_param,
                           "label": lambda v, d: f"{len(d.get('parameters') or [])} parameters for {v.get('framework_name')}"},
    "BUILD_GENERATE_SUITE": {"features": _f_suite, "gates": ("framework", "keys"), "score": _s_suite, "min": 0.95,
                             "adapt": _a_none,
                             "label": lambda v, d: f"{(v.get('framework') or {}).get('name')} suite, "
                                                   f"{len(v.get('steps') or [])} steps"},
    "GOVERN_EXTRACT_RULES": {"features": _f_rules, "gates": ("sop_sentences", "sop_numbers"), "score": _s_rules,
                             "min": 0.85,
                             "adapt": _a_rules, "label": lambda v, d: f"{len(d.get('rules') or [])} SOP rules"},
    "VERIFY_SELF_HEAL": {"features": _f_heal, "gates": ("host",), "score": _s_heal, "min": 0.80, "adapt": _a_none,
                         "label": lambda v, d: "fix: " + str(d.get("diagnosis") or d.get("failure_category") or "")[:70]},
    "ENV_UPGRADE_ADVICE": {"features": _f_env, "gates": ("title", "category", "os", "shell"), "score": _s_env,
                           "min": 0.90, "adapt": _a_none,
                           "label": lambda v, d: "advice: " + str((v.get("finding") or {}).get("title") or "")[:70]},
    "GOVERN_COMPLIANCE_REVIEW": {"features": _f_exact, "gates": (), "score": _s_exact, "min": 1.0, "exact": True,
                                 "adapt": _a_none, "label": lambda v, d: "compliance second opinion"},
    "CODE_HEALTH_FIX": {"features": _f_exact, "gates": (), "score": _s_exact, "min": 1.0, "exact": True,
                        "adapt": _a_none, "label": lambda v, d: "code health fix"},
    "MODEL_ARMOR_CLASSIFIER": {"features": _f_exact, "gates": (), "score": _s_exact, "min": 1.0, "exact": True,
                               "adapt": _a_none, "block_only": True,
                               "label": lambda v, d: f"Model Armor verdict ({v.get('channel')})"},
}

STEP_OF = {"WATCH_ANALYZE": "Watch", "WATCH_REFINE": "Watch", "WATCH_URLS": "Watch", "FRAMEWORK_RESOLVE": "Build",
           "BUILD_PARAMETERIZE": "Build", "BUILD_GENERATE_SUITE": "Build", "GOVERN_EXTRACT_RULES": "Govern",
           "GOVERN_COMPLIANCE_REVIEW": "Govern", "MODEL_ARMOR_CLASSIFIER": "Govern",
           "VERIFY_SELF_HEAL": "Verify", "CODE_HEALTH_FIX": "Mission Control", "ENV_UPGRADE_ADVICE": "Mission Control"}


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
def _shared():
    try:
        from core.wizard import store
        st = store.get_store()
        return st if getattr(st, "kind", "local") != "local" else None
    except Exception:
        return None


def _scrub(value: Any) -> Any:
    """Secret parameter values never enter the memory."""
    if isinstance(value, dict):
        out = {k: _scrub(v) for k, v in value.items()}
        if out.get("type") == "secret" and "value" in out:
            out["value"] = ""
        return out
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return value


_SHARED_TTL = 20.0


def _entries(prompt_id: str) -> List[Dict[str, Any]]:
    shared = _shared()
    if shared is not None:
        # Cloud Run: the shared store is the truth (a memory forgotten on one instance is gone on all of them);
        # cached briefly so a lookup doesn't read the whole collection every time.
        key = "shared:" + prompt_id
        cached = _CACHE.get(key)
        if cached and time.time() - cached[0] < _SHARED_TTL:
            return cached[1]
        prefix = prompt_id.lower().replace("_", "-") + "--"
        try:
            docs = [shared.get("recall", d) for d in shared.list("recall") if d.startswith(prefix)]
            lst = sorted((e for e in docs if e), key=lambda e: e.get("created_at", ""))
            _CACHE[key] = (time.time(), lst)
            return lst
        except Exception:
            pass                                   # store unreachable: fall back to this machine's copy
    d = os.path.join(root(), prompt_id)
    try:
        names = sorted(n for n in os.listdir(d) if n.endswith(".json"))
        stamp = max([os.path.getmtime(d)] + [os.path.getmtime(os.path.join(d, n)) for n in names])
    except (FileNotFoundError, ValueError):
        names, stamp = [], 0.0
    cached = _CACHE.get(d)
    if cached and cached[0] == stamp and len(cached[1]) == len(names):
        return cached[1]
    out: Dict[str, Dict[str, Any]] = {}
    for n in names:
        try:
            with open(os.path.join(d, n), encoding="utf-8") as fh:
                e = json.load(fh)
            out[e["id"]] = e
        except Exception:
            continue
    lst = sorted(out.values(), key=lambda e: e.get("created_at", ""))
    _CACHE[d] = (stamp, lst)
    return lst


def _invalidate(prompt_id: str) -> None:
    _CACHE.pop(os.path.join(root(), prompt_id), None)
    _CACHE.pop("shared:" + prompt_id, None)


def _write(e: Dict[str, Any]) -> None:
    d = os.path.join(root(), e["prompt_id"])
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, e["id"] + ".json")
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(e, fh, indent=1, default=str)
    os.replace(tmp, p)
    shared = _shared()
    if shared is not None:
        try:
            shared.put("recall", e["id"], e)
        except Exception:
            pass
    _invalidate(e["prompt_id"])


def _remove(e: Dict[str, Any]) -> None:
    d = os.path.join(root(), e["prompt_id"])
    try:
        os.remove(os.path.join(d, e["id"] + ".json"))
    except FileNotFoundError:
        pass
    shared = _shared()
    if shared is not None:
        try:
            shared.delete("recall", e["id"])
        except Exception:
            pass
    _invalidate(e["prompt_id"])


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def remember(prompt_id: str, variables: Dict[str, Any], media_path: Optional[str], data: Any, model: str,
             platform: str = "") -> Optional[str]:
    """Keeps a validated AI answer. Returns the memory id (None when not kept)."""
    pol = POLICIES.get(prompt_id)
    if not enabled() or pol is None or data is None:
        return None
    if pol.get("block_only") and not (isinstance(data, dict) and data.get("is_adversarial")):
        return None
    try:
        mh = media_hash(media_path) if media_path else None
        feats = pol["features"](variables, mh)
        digest = exact_digest(prompt_id, variables, mh)
        try:
            label = str(pol["label"](variables, data if isinstance(data, dict) else {}))[:140]
        except Exception:
            label = prompt_id
        with _LOCK:
            existing = _entries(prompt_id)
            same = next((e for e in existing if e.get("digest") == digest), None)
            eid = same["id"] if same else prompt_id.lower().replace("_", "-") + "--" + digest[:16]
            entry = {"id": eid, "prompt_id": prompt_id, "step": STEP_OF.get(prompt_id, ""), "label": label,
                     "digest": digest, "features": feats, "answer": _scrub(data), "learned_with": model,
                     "platform": platform, "created_at": (same or {}).get("created_at") or _now(), "updated_at": _now(),
                     "hits": (same or {}).get("hits", 0), "last_used": (same or {}).get("last_used")}
            _write(entry)
            # keep the newest MAX_PER_PROMPT memories per step
            rest = [e for e in existing if e["id"] != eid]
            for old in sorted(rest, key=lambda e: e.get("last_used") or e.get("updated_at") or "")[:max(0, len(rest) + 1 - MAX_PER_PROMPT)]:
                _remove(old)
        return eid
    except Exception:
        return None


def lookup(prompt_id: str, variables: Dict[str, Any], media_path: Optional[str] = None,
           touch: bool = True) -> Optional[Dict[str, Any]]:
    """Best remembered answer for this question, adapted to it - or None.

    Returns {data, memory_id, score, label, learned_with, learned_at, platform, exact, detail, threshold}."""
    pol = POLICIES.get(prompt_id)
    if not enabled() or pol is None:
        return None
    try:
        entries = _entries(prompt_id)
        if not entries:
            return None
        mh = media_hash(media_path) if media_path else None
        digest = exact_digest(prompt_id, variables, mh)
        q = pol["features"](variables, mh)
        best: Optional[Tuple[float, Dict[str, Any], Dict[str, Any], bool]] = None
        for e in entries:
            exact = e.get("digest") == digest
            if pol.get("exact") and not exact:
                continue
            if pol.get("block_only") and not (e.get("answer") or {}).get("is_adversarial"):
                continue
            m = e.get("features") or {}
            if any(q.get(g) != m.get(g) for g in pol["gates"]):
                continue
            score, detail = pol["score"](q, m)
            if exact:
                score, detail = 1.0, dict(detail, identical_question=True)
            if score >= pol["min"] and (best is None or score > best[0] or
                                        (score == best[0] and e.get("updated_at", "") > best[1].get("updated_at", ""))):
                best = (score, e, detail, exact)
        if best is None:
            return None
        score, e, detail, exact = best
        data = pol["adapt"](json.loads(json.dumps(e["answer"])), variables, detail)
        if not touch:                               # a check ("is there one?"), not a use: hits stay as they are
            return {"data": data, "memory_id": e["id"], "score": round(min(score, 1.0), 3), "exact": exact}
        with _LOCK:
            e = dict(e)
            e["hits"] = int(e.get("hits", 0)) + 1
            e["last_used"] = _now()
            try:
                _write(e)
            except Exception:
                pass
        return {"data": data, "memory_id": e["id"], "score": round(min(score, 1.0), 3), "label": e.get("label", ""),
                "learned_with": e.get("learned_with", ""), "learned_at": e.get("created_at", ""),
                "platform": e.get("platform", ""), "exact": exact, "detail": detail, "threshold": pol["min"],
                "step": e.get("step", "")}
    except Exception:
        return None


def list_memories() -> List[Dict[str, Any]]:
    out = []
    for pid in POLICIES:
        for e in _entries(pid):
            out.append({k: e.get(k) for k in ("id", "prompt_id", "step", "label", "learned_with", "platform",
                                               "created_at", "updated_at", "hits", "last_used")})
    return sorted(out, key=lambda e: e.get("updated_at") or "", reverse=True)


def read_memory(mid: str) -> Dict[str, Any]:
    if not re.fullmatch(r"[a-z0-9-]+--[0-9a-f]{16}", mid or ""):
        raise KeyError(mid)
    for pid in POLICIES:
        for e in _entries(pid):
            if e["id"] == mid:
                return e
    raise KeyError(mid)


def delete_memory(mid: str) -> None:
    _remove(read_memory(mid))


def clear() -> int:
    n = 0
    for pid in POLICIES:
        for e in list(_entries(pid)):
            _remove(e)
            n += 1
    return n


def stats() -> Dict[str, Any]:
    by: Dict[str, int] = {}
    hits = 0
    for m in list_memories():
        by[m["step"]] = by.get(m["step"], 0) + 1
        hits += int(m.get("hits") or 0)
    return {"enabled": enabled(), "total": sum(by.values()), "by_step": by, "reuses": hits}
