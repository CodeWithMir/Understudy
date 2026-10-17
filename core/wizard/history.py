"""Context of previous sessions for STEP 1 (WATCH).

In Step 1 the user can pick earlier sessions - one by one (search by name) or a whole project at once (every
session of that project that got as far as an analysis). Understudy turns each picked session into a compact,
value-free digest and gives the digests to the WATCH agent (prompts ``WATCH_ANALYZE`` / ``WATCH_REFINE``,
variable ``history_context``), so the new automation strategy can build on what already worked:

    * selectors that were proven (or repaired by self-healing) on the same application,
    * parameter names / types used before (consistent payloads across runs),
    * the verification approach that proved the change,
    * self-healing lessons (what broke, what fixed it) and the user's own WATCH corrections,
    * the SOP rules that applied and the framework that was used.

Safety
    * Owner-scoped: only the user's own sessions and projects can be referenced (anything else is "not found").
    * No payload VALUES and no secrets are ever included - a previous run's data must never leak into this one.
      The prompt tells the model the same: values come from this session's recording / SOP / page.
    * Every digest passes Model Armor (it contains text an AI wrote earlier from untrusted pages); a digest that
      fails is left out and reported as a security event. In the prompt the whole block is untrusted data.
    * Size-bounded: at most MAX_SESSIONS sessions, MAX_DIGEST_CHARS each, MAX_TOTAL_CHARS in total.

Offline (no AI) ``apply_offline`` re-uses proven selectors and parameter names deterministically.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from core.wizard import projects, store
from core.wizard.guardrails import screen_text

MAX_REFS = 20
MAX_SESSIONS = 20
MAX_DIGEST_CHARS = 3000
MAX_TOTAL_CHARS = 14000


class HistoryError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def _load_session(sid: str) -> Optional[Dict[str, Any]]:
    from core.wizard import session as wz
    s = wz.SESSIONS.get(sid)
    if s is not None:
        return s
    try:
        return store.get_store().get("sessions", sid)
    except Exception:
        return None


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except Exception:
        return ""


def _one_line(v: Any, n: int = 200) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip()[:n]


def _url_only(u: str) -> str:
    """scheme://host/path - query strings and fragments can carry ids or tokens of the earlier run."""
    try:
        p = urlsplit(u or "")
        return f"{p.scheme}://{p.netloc}{p.path}" if p.scheme and p.netloc else ""
    except ValueError:
        return ""


def _scrubber(s: Dict[str, Any]):
    """Removes the earlier run's payload values (and any secret it still has in memory) from free text such as
    self-healing diagnoses, healing notes and WATCH corrections: each becomes {parameter_key}."""
    vals = []
    for p in (s.get("parameters") or []) + list(((s.get("analysis") or {}).get("parameters")) or []):
        v = str(p.get("value") if p.get("value") is not None else "").strip()
        if len(v) >= 3 and v.lower() not in ("true", "false"):
            vals.append((v, p.get("key") or "value"))
    vals.sort(key=lambda x: -len(x[0]))

    def scrub(text: Any) -> str:
        out = str(text or "")
        for v, k in vals:
            out = re.sub(re.escape(v), "{" + k + "}", out, flags=re.I)
        return out
    return scrub


def digest(s: Dict[str, Any]) -> Dict[str, Any]:
    """A value-free summary of one session: text for the prompt + structured parts for the offline path."""
    a = s.get("analysis") or {}
    fw = s.get("framework") or {}
    scrub = _scrubber(s)
    pillars = s.get("pillars") or {}
    passed = [p for p in ("WATCH", "BUILD", "GOVERN", "BUILD-EXECUTE", "EXPORT", "VERIFY") if (pillars.get(p) or {}).get("status") == "PASS"]
    steps = [{"order": st.get("order"), "action": st.get("action"), "selector": st.get("selector") or "",
              "fallback_selectors": list(st.get("fallback_selectors") or [])[:3], "label": st.get("label") or "",
              "param_key": st.get("param_key") or "", "description": st.get("description") or ""}
             for st in a.get("steps") or []]
    params = [{"key": p.get("key"), "label": p.get("label"), "type": p.get("type"),
               "constraints": {k: v for k, v in (p.get("constraints") or {}).items() if k != "options"} |
                              ({"options": list((p.get("constraints") or {}).get("options") or [])[:12]}
                               if (p.get("constraints") or {}).get("options") else {})}
              for p in s.get("parameters") or a.get("parameters") or []]
    heal = []
    for h in (s.get("retry") or {}).get("history") or []:
        ch = "; ".join(f"step {c.get('step')}: {_one_line(c.get('from'), 60)} -> {_one_line(c.get('to'), 60)}"
                       for c in (h.get("changes") or [])[:4])
        heal.append(f"attempt {h.get('attempt')}: {h.get('category')} - {_one_line(scrub(h.get('diagnosis')), 160)}"
                    f"{(' [' + ch + ']') if ch else ''} -> {h.get('outcome')}")
    corrections = [_one_line(scrub(c.get("text")), 200) for c in s.get("clarifications") or []][-5:]
    rules = [_one_line(r.get("plain_english"), 160) for r in s.get("rules") or [] if r.get("enabled", True)][:10]
    ver = a.get("verification") or {}
    lines = [
        f'SESSION "{_one_line(s.get("session_name") or s.get("session_id"), 80)}" in project '
        f'"{_one_line(s.get("project_name") or "-", 80)}" - {str(s.get("created_at") or "")[:10]}, status '
        f'{s.get("status")}, {len(passed)}/5 steps passed',
        f"target: {_url_only(s.get('target_url') or '') or '-'}",
        f"process: {_one_line(scrub(a.get('process_name')), 120)} - {_one_line(scrub(a.get('summary')), 400)}",
        f"framework: {fw.get('name') or '-'}"
        + (f" ({(fw.get('packages') or [{}])[0].get('name')} {(fw.get('packages') or [{}])[0].get('version')})"
           if fw.get("packages") else ""),
        "steps:",
    ]
    for st in steps:
        lines.append(f"  {st['order']}. {st['action']} selector={st['selector'] or '-'}"
                     + (f" fallbacks={st['fallback_selectors']}" if st["fallback_selectors"] else "")
                     + (f' label="{_one_line(st["label"], 60)}"' if st["label"] else "")
                     + (f" param={st['param_key']}" if st["param_key"] else "")
                     + f" - {_one_line(scrub(st['description']), 120)}")
    if params:
        lines.append("parameters (names and types only, values deliberately omitted): " + ", ".join(
            f"{p['key']} ({p['type']}{', ' + str(p['constraints']) if p['constraints'] else ''})" for p in params))
    if ver:
        lines.append(f"verification: {ver.get('method')} on {_url_only(ver.get('url') or '') or '-'}"
                     + (f" expecting {ver.get('expect_param_keys')}" if ver.get("expect_param_keys") else ""))
    if rules:
        lines.append("SOP rules: " + " | ".join(rules))
    if heal:
        lines.append("self-healing lessons: " + " | ".join(heal[-6:]))
    if s.get("healing_notes"):
        lines.append("healing notes: " + " | ".join(_one_line(scrub(n), 160) for n in s["healing_notes"][-5:]))
    if corrections:
        lines.append("user corrections of the AI's understanding: " + " | ".join(corrections))
    text = "\n".join(lines)
    if len(text) > MAX_DIGEST_CHARS:
        text = text[:MAX_DIGEST_CHARS - 20] + "\n  ... (truncated)"
    return {"text": text, "steps": steps, "parameters": params, "host": _host(s.get("target_url") or "")}


def resolve(owner: Optional[str], refs: List[Dict[str, Any]], exclude_session: str = "") -> Dict[str, Any]:
    """refs: [{type: session|project, id}] -> {refs, sessions:[{session_id,name,project_name,...}], text, chars,
    skipped, blocked}. Raises HistoryError for a reference the owner cannot use."""
    if not isinstance(refs, list):
        raise HistoryError("context_refs must be a list.")
    if len(refs) > MAX_REFS:
        raise HistoryError(f"Pick at most {MAX_REFS} sessions or projects.")
    rows = {r["session_id"]: r for r in projects.index_rows(owner)}
    chosen: List[str] = []
    clean_refs: List[Dict[str, Any]] = []
    for ref in refs:
        kind, rid = str((ref or {}).get("type") or ""), str((ref or {}).get("id") or "")
        if kind == "session":
            if rid not in rows:
                raise HistoryError("One of the chosen sessions was not found.", 404)
            clean_refs.append({"type": "session", "id": rid, "name": rows[rid].get("name"),
                               "project_name": rows[rid].get("project_name")})
            if rid not in chosen:
                chosen.append(rid)
        elif kind == "project":
            try:
                prj = projects.get_project(owner, rid)
            except projects.ProjectError:
                raise HistoryError("One of the chosen projects was not found.", 404)
            members = sorted([r for r in rows.values() if r.get("project_id") == rid],
                             key=lambda r: r.get("created_at") or "", reverse=True)
            clean_refs.append({"type": "project", "id": rid, "name": prj["name"], "sessions": len(members)})
            for r in members:
                if r["session_id"] not in chosen:
                    chosen.append(r["session_id"])
        else:
            raise HistoryError("Each context reference needs type 'session' or 'project'.")
    out: Dict[str, Any] = {"refs": clean_refs, "sessions": [], "skipped": [], "blocked": [], "text": "", "chars": 0}
    parts: List[str] = []
    for sid in chosen:
        if sid == exclude_session:
            continue
        row = rows[sid]
        s = _load_session(sid)
        if not s or not s.get("analysis"):
            out["skipped"].append({"session_id": sid, "name": row.get("name"),
                                   "why": "it has no analysis yet (Step 1 never finished)"})
            continue
        if len(out["sessions"]) >= MAX_SESSIONS:
            out["skipped"].append({"session_id": sid, "name": row.get("name"), "why": f"more than {MAX_SESSIONS} sessions"})
            continue
        d = digest(s)
        tv = screen_text(d["text"], "HISTORY")      # every Model Armor stage (sanitise, regex, cloud/semantic)
        if not tv.is_safe:
            out["blocked"].append({"session_id": sid, "name": row.get("name"), "pattern": tv.pattern,
                                   "message": f"The context of '{row.get('name')}' was left out: {tv.friendly_message}",
                                   "excerpt": d["text"][:300]})
            continue
        if out["chars"] + len(d["text"]) > MAX_TOTAL_CHARS:
            out["skipped"].append({"session_id": sid, "name": row.get("name"),
                                   "why": f"the context is limited to {MAX_TOTAL_CHARS} characters"})
            continue
        d["text"] = tv.sanitized                    # zero-width / control characters removed
        parts.append(d["text"])
        out["chars"] += len(d["text"])
        out["sessions"].append({"session_id": sid, "name": row.get("name"), "project_name": row.get("project_name"),
                                "status": row.get("status"), "target_url": row.get("target_url"),
                                "host": d["host"], "steps": d["steps"], "parameters": d["parameters"]})
    out["text"] = "\n\n".join(parts)
    return out


def prompt_text(ctx: Optional[Dict[str, Any]]) -> str:
    if not ctx or not ctx.get("text"):
        return "(none - the user did not add previous sessions)"
    return ctx["text"]


def _norm(t: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(t or "").lower()).strip()


def apply_offline(raw: Dict[str, Any], ctx: Optional[Dict[str, Any]], inventory, target_url: str) -> List[str]:
    """Without AI: re-use selectors and parameter names from the chosen sessions when they fit THIS page.
    Mutates ``raw`` (steps/parameters) and returns what was applied, in plain English."""
    if not ctx or not ctx.get("sessions"):
        return []
    host = _host(target_url)
    applied: List[str] = []
    params = {p["key"]: p for p in raw.get("parameters") or []}
    used_keys = set(params)
    renames: Dict[str, str] = {}
    for st in raw.get("steps") or []:
        if st.get("action") not in ("fill", "select", "check", "click"):
            continue
        for sess in ctx["sessions"]:
            if sess.get("host") and host and sess["host"] != host:
                continue                              # lessons from another application don't transfer
            match = next((h for h in sess["steps"] if h.get("action") == st.get("action") and (
                (h.get("label") and _norm(h["label"]) == _norm(st.get("label"))) or
                (h.get("selector") and h["selector"] == st.get("selector")))), None)
            if not match:
                continue
            sel = match.get("selector")
            if sel and sel != st.get("selector") and inventory is not None and inventory.selector_exists(sel):
                st["fallback_selectors"] = [st.get("selector")] + [x for x in st.get("fallback_selectors") or [] if x != sel]
                st["selector"] = sel
                applied.append(f"step {st.get('order')}: re-used selector {sel} proven in '{sess['name']}'")
            old, new = st.get("param_key"), match.get("param_key")
            if old and new and old != new and old in params and new not in used_keys and old not in renames:
                renames[old] = new
                used_keys.discard(old)
                used_keys.add(new)
                applied.append(f"step {st.get('order')}: parameter named '{new}' like in '{sess['name']}'")
            break
    for old, new in renames.items():          # rename everywhere the old key is referenced
        params[old]["key"] = new
        for st in raw.get("steps") or []:
            if st.get("param_key") == old:
                st["param_key"] = new
        ver = raw.get("verification") or {}
        if ver.get("expect_param_keys"):
            ver["expect_param_keys"] = [new if k == old else k for k in ver["expect_param_keys"]]
    raw["parameters"] = [params[k] for k in params]
    return applied
