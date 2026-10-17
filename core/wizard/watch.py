"""STEP 1 - WATCH: analyse the demo video + target URL + SOP text, suggest frameworks.

AI path (ACTUAL_AI_API): the media file is uploaded to the Gemini File API and analysed together with
the SOP and a live inventory of the target page's form fields (``WATCH_ANALYZE`` prompt). The model
returns a step plan, parameters, a verification plan and 3-6 framework suggestions (any tool).

Offline path (LOCAL_HEURISTIC): without an AI key the video cannot be interpreted. The media is still
validated, hashed and stored, and the plan is inferred from the target page's own form, with values
taken from the SOP where possible. The UI states this plainly.

Both paths finish with the same normalisation + security screening:
  * step/parameter keys are normalised and cross-checked, values are removed from step text,
  * the AI's transcription of on-screen/audio text is screened (indirect prompt injection),
  * every proposed URL is screened and must stay on the target host,
  * every framework option is validated by ``frameworks.validate_spec``.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin

from core.wizard import dom_discovery, history as history_mod, payload_schema
from core.wizard.frameworks import (CATALOG, FrameworkSpec, FrameworkValidationError, suggest_offline, validate_spec)
from core.wizard.governance import parse_sop_rules, parse_date
from core.wizard.guardrails import (GuardrailViolation, normalize_param_key, screen_parameters, screen_text, screen_url)
from core.wizard.llm import generate_json, record_fallback
from core.wizard.prompts import catalog_hint_text

ACTIONS = {"navigate", "fill", "select", "check", "click", "press", "wait", "assert_text"}
_SUBMIT_WORDS = re.compile(r"(?i)\b(submit|save|post|send|create|add|confirm|continue|next|apply|book|pay|place|sign\s*up|register|log\s*in)\b")


def _type_for(el: dom_discovery.DomElement) -> str:
    t = el.type
    if el.kind == "select" and el.tag == "select":
        return "select"
    if el.kind == "select":                      # ARIA combobox: typed into, options unknown statically
        return "text"
    if el.kind == "check":
        return "boolean"
    if t in ("date", "datetime-local", "month"):
        return "date"
    if t in ("number", "range"):
        return "number"
    if t == "email":
        return "email"
    if t == "password":
        return "secret"
    return "text"


def _sop_value_for(key: str, label: str, sop_text: str) -> Optional[str]:
    """Looks for 'Label: value' / 'key = value' lines in the SOP text."""
    if not sop_text:
        return None
    names = {key, key.replace("_", " "), label.lower()} - {""}
    for line in sop_text.splitlines():
        m = re.match(r"^\s*[-*]?\s*([A-Za-z][A-Za-z0-9 _/()-]{1,40}?)\s*[:=]\s*(.+?)\s*$", line)
        if not m:
            continue
        name = m.group(1).strip().lower()
        if name in names or name.replace(" ", "_") == key:
            val = m.group(2).strip().strip("\"'")
            if 0 < len(val) <= 200 and "," not in val:
                return val
    return None


def _default_value(el: dom_discovery.DomElement, ptype: str) -> Tuple[Any, str]:
    if el.value:
        return el.value, "page"
    if el.kind == "select" and el.options:
        opts = [o for o in el.options if not re.match(r"(?i)^(select|choose|--)", o)]
        if opts:
            return opts[0], "page"
    if el.placeholder:
        ph = re.sub(r"(?i)^(e\.?g\.?|ex\.?|example|for example)[:,]?\s*", "", el.placeholder).strip()
        if ph and ptype != "secret" and not re.search(r"(?i)\b(enter|type|your|search)\b", ph):
            return ph, "page"
    if ptype == "date":
        return date.today().isoformat(), "default"
    if ptype == "number":
        return "100", "default"
    if ptype == "boolean":
        return True, "default"
    return "", "default"


def _heuristic_analysis(target_url: str, inv: dom_discovery.PageInventory, sop_text: str,
                        ai_reason: str = "") -> Dict[str, Any]:
    # file-upload controls can't be typed into (the recording's file isn't available to a replay either)
    fields = [f for f in inv.fields() if f.type not in ("file", "image", "reset")]
    params: List[Dict[str, Any]] = []
    steps: List[Dict[str, Any]] = [{"order": 1, "action": "navigate", "selector": "", "value": "",
                                    "description": "Open the target page"}]
    used = set()
    for el in fields:
        raw_key = el.name or el.id or el.label or el.placeholder or f"field_{len(params) + 1}"
        key = normalize_param_key(raw_key)
        while key in used:
            key = key + "_2"
        used.add(key)
        label = el.label or el.aria_label or el.placeholder or raw_key.replace("_", " ").title()
        ptype = _type_for(el)
        params.append({"key": key, "label": label[:80], "type": ptype, "required": el.required,
                       "description": f"Value typed into '{label[:60]}'", "sop_dependent": False,
                       "verify": ptype in ("text", "number", "date", "select", "email"), "_el": el})
        action = "select" if ptype == "select" else ("check" if el.kind == "check" else "fill")
        sels = el.selectors()
        steps.append({"order": len(steps) + 1, "action": action, "selector": sels[0] if sels else el.best_selector(),
                      "fallback_selectors": sels[1:], "label": label[:80], "param_key": key,
                      "description": f"Enter the {label[:60]}"})
    # constraint-driven starting values
    rules = parse_sop_rules(sop_text, [{k: v for k, v in p.items() if k != "_el"} for p in params])
    for p in params:
        el = p.pop("_el")
        sop_val = _sop_value_for(p["key"], p["label"], sop_text)
        if sop_val is not None:
            p["value"], p["source"] = sop_val, "sop"
        else:
            p["value"], p["source"] = _default_value(el, p["type"])
        for r in rules:
            if r["field"] != p["key"]:
                continue
            p["sop_dependent"] = True
            if r["rule_type"] in ("allowed_values", "role_access") and r["values"] and sop_val is None:
                p["value"], p["source"] = r["values"][0], "sop"
            elif r["rule_type"] == "date_range" and r.get("start_date"):
                cur = parse_date(p["value"])
                s, e = parse_date(r.get("start_date")), parse_date(r.get("end_date"))
                if not cur or (s and cur < s) or (e and cur > e):
                    p["value"], p["source"] = (e if e and date.today() > e else s or date.today()).isoformat(), "sop"
            elif r["rule_type"] == "max" and r.get("limit"):
                try:
                    if float(p["value"]) > float(r["limit"]):
                        p["value"], p["source"] = str(round(float(r["limit"]) / 2, 2)), "sop"
                except (TypeError, ValueError):
                    pass
    buttons = inv.buttons()
    submit = next((b for b in buttons if b.type == "submit"), None) or \
        next((b for b in buttons if _SUBMIT_WORDS.search(b.text or b.name or b.id)), None) or (buttons[-1] if buttons else None)
    if submit:
        sels = submit.selectors()
        steps.append({"order": len(steps) + 1, "action": "click", "selector": sels[0] if sels else submit.best_selector(),
                      "fallback_selectors": sels[1:], "label": (submit.text or submit.name or "Submit")[:80],
                      "description": f"Click '{(submit.text or 'Submit')[:50]}' to save the record"})
    for p in params:
        if p["type"] == "boolean":
            p["verify"] = False
    verify_keys = [p["key"] for p in params if p.get("verify") and str(p.get("value", "")).strip()][:4]
    api = None
    if inv.api_calls:
        c = inv.api_calls[0]
        api = {"method": c["method"], "path": c["path"], "url": urljoin(target_url, c["path"]),
               "body_keys": c.get("body_keys", []), "header_env": c.get("header_env", {})}
        for header, env in (c.get("header_env") or {}).items():
            key = normalize_param_key(env.replace("UNDERSTUDY_SECRET_", ""))
            if key not in {p["key"] for p in params}:
                params.append({"key": key, "label": f"{header} header (API secret)", "type": "secret", "value": "",
                               "source": "page", "required": False, "sop_dependent": False, "verify": False,
                               "description": f"Only needed by API frameworks: value of the '{header}' header."})
    title = inv.title or "Recorded business process"
    submit_txt = f" and clicks '{(submit.text or 'Submit')[:40]}'" if submit else ""
    return {
        "process_name": title[:120],
        "summary": (f"AI video analysis was not used ({(ai_reason or 'AI unavailable').rstrip('.')}), so Understudy did not "
                    f"watch the video itself. Instead it read the form on "
                    f"{target_url}: the process fills {len([s for s in steps if s['action'] in ('fill', 'select', 'check')])} "
                    f"field(s){submit_txt}. Values come from your SOP where it mentions them; please review them in Step 2."),
        "app_type": "web_ui",
        "steps": steps,
        "parameters": params,
        "verification": {"method": "dom_text", "url": target_url, "expect_param_keys": verify_keys},
        "api": api,
        "observed_text": "",
        "framework_options": [f.to_dict() for f in suggest_offline("web_ui", bool(api))],
        "security_flags": [],
    }


def _strip_values_from_text(text: str, params: List[Dict[str, Any]]) -> str:
    out = str(text or "")
    for p in params:
        v = str(p.get("value") or "").strip()
        if len(v) < 3:
            continue
        try:
            num = float(v.replace(",", ""))
        except ValueError:
            num = None
        if num is not None:
            # 450.0 must also hide "450.00" / "450" / "1,250.00" - whole numbers only, never a part of one
            def _sub(m, num=num, key=p["key"]):
                try:
                    return "{" + key + "}" if float(m.group(0).replace(",", "")) == num else m.group(0)
                except ValueError:
                    return m.group(0)
            out = re.sub(r"(?<![\d.,])\d[\d,]*(?:\.\d+)?(?![\d])", _sub, out)
        else:
            out = out.replace(v, "{" + p["key"] + "}")
    return out[:200]


_VIDEO_TIME = re.compile(r"^\d{1,2}:[0-5]\d(:[0-5]\d)?$")


def normalize_analysis(raw: Dict[str, Any], target_url: str,
                       extra_urls: Optional[List[str]] = None) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Cleans AI or heuristic output. Returns (analysis, security_problems). 9.4: ``extra_urls`` are the other pages
    the user listed - their hosts are allowed for navigate steps and the verification, like the start page's."""
    problems: List[Dict[str, Any]] = []
    hosts_of = [target_url] + [u for u in (extra_urls or []) if u]
    # parameters
    params_in = []
    for p in raw.get("parameters") or []:
        q = dict(p)
        q["key"] = normalize_param_key(q.get("key") or q.get("label") or "param")
        q["type"] = q.get("type") if q.get("type") in ("text", "number", "date", "email", "select", "boolean", "secret") else "text"
        q.setdefault("label", q["key"].replace("_", " ").title())
        q.setdefault("source", "video")
        q.setdefault("required", True)
        q.setdefault("verify", q["type"] in ("text", "number", "date", "select", "email"))
        q.setdefault("sop_dependent", False)
        q.setdefault("description", "")
        if q["type"] == "secret":
            q["value"], q["verify"] = "", False
        elif q.get("value") is None:
            q["value"] = ""
        params_in.append(q)
    params, param_problems = screen_parameters(params_in)
    problems.extend(param_problems)
    keys = {p["key"] for p in params}
    # steps
    steps: List[Dict[str, Any]] = []
    for s in sorted(raw.get("steps") or [], key=lambda x: x.get("order", 0)):
        action = str(s.get("action", "")).lower()
        if action not in ACTIONS:
            continue
        st = {"order": len(steps) + 1, "action": action, "selector": str(s.get("selector") or "")[:200],
              "fallback_selectors": [str(x)[:200] for x in (s.get("fallback_selectors") or [])][:5],
              "label": str(s.get("label") or "")[:80], "value": str(s.get("value") or "")[:200],
              "description": _strip_values_from_text(s.get("description") or action, params)}
        vt = str(s.get("video_time") or "").strip()
        if _VIDEO_TIME.match(vt):                  # moment-by-moment grounding in the recording (optional)
            st["video_time"] = vt
            st["evidence"] = _strip_values_from_text(str(s.get("evidence") or "")[:200], params)
        if action in ("fill", "select", "check"):
            pk = normalize_param_key(s.get("param_key") or "")
            if pk not in keys:
                continue
            st["param_key"] = pk
        if action == "assert_text" and s.get("param_key"):
            st["param_key"] = normalize_param_key(s["param_key"]) if normalize_param_key(s["param_key"]) in keys else None
        if action == "navigate":
            val = st["value"]
            if val and val.startswith("http"):
                uv = screen_url(val, same_host_as=hosts_of)
                if not uv.is_safe:
                    problems.append({"key": "steps", "message": uv.friendly_message, "pattern": "UNSAFE_URL"})
                    st["value"] = ""
        for fld in ("selector", "label", "value", "description", "evidence"):
            tv = screen_text(st[fld], "PAYLOAD", use_semantic=False) if st.get(fld) else None
            if tv and not tv.is_safe:
                problems.append({"key": f"step {st['order']}", "message": tv.friendly_message, "pattern": tv.pattern})
                st[fld] = ""
        steps.append(st)
    if not steps or steps[0]["action"] != "navigate":
        steps.insert(0, {"order": 0, "action": "navigate", "selector": "", "fallback_selectors": [], "label": "",
                         "value": "", "description": "Open the target page"})
    for i, st in enumerate(steps, 1):
        st["order"] = i
    # verification
    ver = dict(raw.get("verification") or {})
    method = ver.get("method") if ver.get("method") in ("dom_text", "api_json", "url_change", "visual") else "dom_text"
    ver_url = ver.get("url") or target_url
    uv = screen_url(ver_url, same_host_as=hosts_of)
    ver_url = uv.normalized_url if uv.is_safe else target_url
    api_url = ver.get("api_url")
    if api_url:
        if not api_url.startswith("http"):
            api_url = urljoin(target_url, api_url)
        uv = screen_url(api_url, same_host_as=hosts_of)
        api_url = uv.normalized_url if uv.is_safe else None
        if not api_url and method == "api_json":
            method = "dom_text"
    expect = [k for k in (ver.get("expect_param_keys") or []) if k in keys] or \
             [p["key"] for p in params if p.get("verify") and str(p.get("value", "")).strip()][:4]
    verification = {"method": method, "url": ver_url, "api_url": api_url, "expect_param_keys": expect,
                    "success_text": str(ver.get("success_text") or "")[:120]}
    # API plan
    api = raw.get("api") or None
    if api and api.get("url"):
        u = api["url"] if api["url"].startswith("http") else urljoin(target_url, api["url"])
        uv = screen_url(u, same_host_as=hosts_of)
        if uv.is_safe:
            api = {"method": str(api.get("method") or "POST").upper()[:7], "url": uv.normalized_url,
                   "path": api.get("path") or re.sub(r"^https?://[^/]+", "", uv.normalized_url),
                   "body_keys": [normalize_param_key(k) for k in (api.get("body_keys") or [])][:40],
                   "header_env": {str(h)[:40]: re.sub(r"[^A-Z0-9_]", "_", str(e).upper())[:60]
                                  for h, e in (api.get("header_env") or {}).items()}}
        else:
            api = None
    else:
        api = None
    # frameworks
    options: List[Dict[str, Any]] = []
    for f in raw.get("framework_options") or []:
        try:
            spec = FrameworkSpec.from_dict(f)
            if spec.id in CATALOG and not spec.template:
                spec.template = CATALOG[spec.id].template
            options.append(validate_spec(spec).to_dict())
        except (FrameworkValidationError, TypeError, ValueError):
            continue
    if not options:
        options = [f.to_dict() for f in suggest_offline(raw.get("app_type", "web_ui"), bool(api))]
    if not any(o.get("recommended") for o in options):
        options[0]["recommended"] = True
    analysis = {
        "process_name": str(raw.get("process_name") or "Recorded process")[:120],
        "summary": str(raw.get("summary") or "")[:1500],
        "app_type": raw.get("app_type") if raw.get("app_type") in ("web_ui", "api", "desktop", "mixed") else "web_ui",
        "steps": steps, "parameters": params, "verification": verification, "api": api,
        "framework_options": options[:8],
        "security_flags": [str(x)[:200] for x in (raw.get("security_flags") or [])][:10],
        "history_applied": [],
    }
    for x in (raw.get("history_applied") or [])[:20]:
        tv = screen_text(str(x)[:300], "PAYLOAD", use_semantic=False)
        if tv.is_safe:
            analysis["history_applied"].append(_strip_values_from_text(str(x), params))   # never a value
    req = raw.get("sop_tool_requirement")
    if isinstance(req, dict) and str(req.get("name") or "").strip():
        analysis["sop_tool_requirement"] = {k: str(req.get(k) or "")[:200] for k in ("name", "version", "quote")}
    return analysis, problems


# ---------------------------------------------------------------------------
# Grounding: every step's element is looked up on the real page (any website)
# ---------------------------------------------------------------------------
_TOKEN_RXS = (re.compile(r"#([A-Za-z][\w-]*)"), re.compile(r"\[(?:name|id|data-testid|data-test|data-cy|aria-label)\s*[*^$]?=\s*['\"]?([^'\"\]]+)"))


def _norm(t: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(t or "").lower()).strip()


def _kind_ok(action: str, el: dom_discovery.DomElement) -> bool:
    if action == "click":
        return el.kind in ("button", "link", "check")
    if action == "select":
        return el.kind in ("select", "input")
    if action == "check":
        return el.kind == "check"
    return el.kind in ("input", "select")


def _match_element(step: Dict[str, Any], inv: dom_discovery.PageInventory, used: set) -> Optional[dom_discovery.DomElement]:
    """The page element a step means when its selector is not on the page: same id/name/test-id token, or the same
    visible label/text."""
    from difflib import SequenceMatcher
    tokens = set()
    for sel in [step.get("selector") or ""] + list(step.get("fallback_selectors") or []):
        for rx in _TOKEN_RXS:
            tokens |= {m.lower() for m in rx.findall(sel)}
    label = _norm(step.get("label"))
    best, best_score = None, 0.0
    for el in inv.elements:
        if id(el) in used or not _kind_ok(step.get("action", ""), el):
            continue
        ids = {x.lower() for x in (el.id, el.name, el.testid, el.aria_label) if x}
        names = [_norm(x) for x in (el.label, el.aria_label, el.placeholder, el.text) if x]
        score = 0.0
        if tokens & ids:
            score = 0.95
        elif label and label in names:
            score = 0.9
        elif label and names:
            score = max(SequenceMatcher(None, label, n).ratio() for n in names) * 0.85
        if score > best_score:
            best, best_score = el, score
    return best if best_score >= 0.72 else None


def ground_analysis(analysis: Dict[str, Any], inv: dom_discovery.PageInventory, target_url: str) -> Dict[str, Any]:
    """Validates every step against the live page inventory and derives the payload schema from it.

    * selector on the page -> kept; the element's other selectors become fallbacks;
    * a fallback on the page, or the same element found by id/name/label -> the page's real selector becomes the
      primary one (the AI's guess is kept as a fallback) and the repair is reported;
    * not on the page -> kept but marked ``grounded: false`` (another page of a multi-page flow, or content that only
      appears after an earlier step); the runner falls back to the visible label, and self-healing re-reads the page;
    * parameters of grounded steps get the page's ``constraints`` and the matching type (select/boolean/number/...)."""
    report: Dict[str, Any] = {"page": inv.url, "renderer": inv.renderer, "checked": 0, "on_page": 0, "repaired": [],
                              "not_found": [], "constraints": 0, "type_fixes": [], "value_fixes": []}
    if not inv.fetched or not inv.elements:
        report["skipped"] = inv.error or "the target page has no interactive elements to check against"
        analysis["grounding"] = report
        return report
    params = {p["key"]: p for p in analysis.get("parameters") or []}
    used: set = set()
    left_start_page = False
    for st in analysis.get("steps") or []:
        a = st.get("action")
        if a == "navigate":
            v = st.get("value") or ""
            if st["order"] > 1 and v and urljoin(target_url, v).rstrip("/") != (inv.url or target_url).rstrip("/"):
                left_start_page = True
            continue
        if a not in ("fill", "select", "check", "click"):
            continue
        report["checked"] += 1
        el, how = (inv.find(st.get("selector") or ""), "selector")
        if el is None:
            for fb in st.get("fallback_selectors") or []:
                el = inv.find(fb)
                if el is not None:
                    how = "fallback"
                    break
        if el is None:
            el, how = _match_element(st, inv, used), "match"
        if el is None:
            st["grounded"] = False
            report["not_found"].append({"step": st["order"], "selector": st.get("selector"),
                                        "why": "not on the start page (later page of the flow)" if left_start_page
                                        else "not found on the target page"})
            continue
        used.add(id(el))
        st["grounded"] = True
        report["on_page"] += 1
        sels = el.selectors() or [el.best_selector()]
        old = st.get("selector") or ""
        fallbacks = [x for x in st.get("fallback_selectors") or [] if x]
        if old not in sels and old != el.best_selector():
            st["selector"] = sels[0]
            fallbacks = sels[1:] + ([old] if old else []) + fallbacks
            report["repaired"].append({"step": st["order"], "from": old or None, "to": sels[0],
                                       "why": {"fallback": "the primary selector is not on the page, a fallback is",
                                               "match": f"no such selector on the page; same field by "
                                                        f"{'id/name' if old else 'label'}: '{el.descriptor()[:60]}'"}.get(how, "")})
        else:
            fallbacks += [x for x in sels if x != old]
        seen: List[str] = []
        for x in fallbacks:
            if x and x != st["selector"] and x not in seen:
                seen.append(x)
        st["fallback_selectors"] = seen[:5]
        if not st.get("label"):
            st["label"] = (el.label or el.aria_label or el.placeholder or el.text or "")[:80]
        if a == "fill" and el.kind == "select" and el.tag == "select":
            st["action"] = "select"
        elif a == "fill" and el.kind == "check":
            st["action"] = "check"
        pk = st.get("param_key")
        if pk in params:
            p = params[pk]
            c = el.constraints()
            if c:
                p["constraints"] = c
                report["constraints"] += 1
            nt = payload_schema.type_for_element(el.to_dict(), p.get("type", "text"))
            if nt != p.get("type"):
                report["type_fixes"].append(f"{p.get('label') or pk}: {p.get('type')} -> {nt} (the page's control)")
                p["type"] = nt
                if nt == "secret":
                    p["value"], p["verify"] = "", False
                if nt == "boolean":
                    p["verify"] = False
            note = payload_schema.coerce_option(p)
            if note:
                report["value_fixes"].append(note)
    analysis["grounding"] = report
    return report


# ---------------------------------------------------------------------------
# 9.4: an automation that visits several pages
# ---------------------------------------------------------------------------
def urls_for_prompt(target_url: str, extra_urls: Optional[List[str]] = None) -> str:
    extra = [u for u in (extra_urls or []) if u and u != target_url]
    if not extra:
        return target_url
    return "\n".join([f"1. {target_url}  (start page)"] + [f"{i}. {u}" for i, u in enumerate(extra, 2)])


def discover_pages(target_url: str, extra_urls: Optional[List[str]] = None
                   ) -> Tuple[dom_discovery.PageInventory, List[Tuple[str, dom_discovery.PageInventory]]]:
    """The start page's inventory + one per other page the user listed (each read once)."""
    inv = dom_discovery.discover(target_url)
    others: List[Tuple[str, dom_discovery.PageInventory]] = []
    for u in [u for u in (extra_urls or []) if u and u != target_url]:
        try:
            others.append((u, dom_discovery.discover(u)))
        except Exception as e:                                  # one unreadable page never stops WATCH
            others.append((u, dom_discovery.PageInventory(url=u, error=f"{type(e).__name__}: {e}")))
    return inv, others


def inventories_for_prompt(inv: dom_discovery.PageInventory,
                           others: List[Tuple[str, dom_discovery.PageInventory]], budget: int = 15000) -> str:
    if not others:
        return dom_discovery.inventory_for_prompt(inv)
    first = max(4000, budget // 2)
    each = max(1500, (budget - first) // len(others))
    parts = [f"=== PAGE 1 (start page): {inv.url} ===\n" + dom_discovery.inventory_for_prompt(inv, max_chars=first)]
    for i, (u, other) in enumerate(others, 2):
        body = dom_discovery.inventory_for_prompt(other, max_chars=each) if not other.error else \
            f"(could not read this page: {other.error[:200]} - it may only open after earlier steps, e.g. a login)"
        parts.append(f"=== PAGE {i}: {u} ===\n{body}")
    return "\n\n".join(parts)


def _merge_other_pages(raw: Dict[str, Any], others: List[Tuple[str, dom_discovery.PageInventory]],
                       sop_text: str) -> List[str]:
    """Offline WATCH for a multi-page process: after the start page's steps, open each other page and fill its form
    (the same page reading, payload keys made unique per page)."""
    notes: List[str] = []
    keys = {p["key"] for p in raw.get("parameters") or []}
    for i, (u, other) in enumerate(others, 2):
        if other.error or not other.fields():
            notes.append(f"Page {i} ({u}) has no form fields Understudy could read without AI"
                         + (f" ({other.error[:120]})" if other.error else "") + " - only a step that opens it was added.")
            raw["steps"].append({"order": 0, "action": "navigate", "selector": "", "value": u,
                                 "description": f"Open page {i}"})
            continue
        h = _heuristic_analysis(u, other, sop_text)
        rename: Dict[str, str] = {}
        for p in h.get("parameters") or []:
            k = p["key"]
            while k in keys:
                k = f"{p['key']}_p{i}"
                if k in keys:
                    k = f"{p['key']}_p{i}_{len(keys)}"
            rename[p["key"]] = k
            keys.add(k)
            p["key"] = k
            raw["parameters"].append(p)
        for st in h.get("steps") or []:
            st = dict(st)
            if st.get("action") == "navigate":
                st.update(value=u, description=f"Open page {i}")
            if st.get("param_key") in rename:
                st["param_key"] = rename[st["param_key"]]
            raw["steps"].append(st)
    for n, st in enumerate(raw["steps"], 1):
        st["order"] = n
    return notes


_SCHEMELESS_HOST = re.compile(r"^(localhost|[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+)(:\d+)?(/|$)", re.I)


def complete_url(raw: str, known: List[str]) -> str:
    """An address read from a recording: 'erp.example.com/x' -> 'https://erp.example.com/x'; a bare path '/x' is
    completed from the first known address (same site)."""
    u = str(raw or "").strip().rstrip(".,;)").strip("<>\"'`").rstrip(".,;)")
    if not u:
        return ""
    if re.match(r"(?i)^https?://", u):
        return u
    if u.startswith("/"):
        base = next((k for k in known if re.match(r"(?i)^https?://", k)), "")
        return urljoin(base, u) if base else ""
    if _SCHEMELESS_HOST.match(u):
        host = u.split("/")[0].split(":")[0].lower()
        local = host in ("localhost", "127.0.0.1") or host.endswith(".local") or re.match(r"^\d+\.\d+\.\d+\.\d+$", host)
        return ("http://" if local else "https://") + u
    return ""


def _pages_vars(user_urls: List[str], sop_text: str = "", note: str = "") -> Dict[str, Any]:
    return {"user_pages": "\n".join(f"{i}. {u}" for i, u in enumerate(user_urls, 1)) or "(none)",
            "page_note": note or "(none)", "sop": (sop_text or "")[:1500]}


def learned_pages(media_path: Optional[str], sop_text: str = "") -> bool:
    """Without AI: True when an earlier AI session already read the page addresses of this recording (a learned
    skill), so the session can start without a typed address."""
    if not media_path:
        return False
    try:
        from core.wizard import recall
        return bool(recall.lookup("WATCH_URLS", _pages_vars([], sop_text), media_path, touch=False))
    except Exception:
        return False


def pages_from_video(media_path: Optional[str], user_urls: List[str], sop_text: str = "",
                     note: str = "") -> Dict[str, Any]:
    """9.4: the WATCH_URLS agent lists the page addresses it sees in the recording, in order.
    {found[{url, video_time, evidence, purpose}], rejected[{url, reason, video_time}], notes, agent_source, model,
    error}. Raises GuardrailViolation when the transcribed screen text is an attack. Every address is screened like a
    typed one (Model Armor, SSRF, the admin's lists)."""
    out: Dict[str, Any] = {"found": [], "rejected": [], "notes": "", "agent_source": "LOCAL_HEURISTIC",
                           "model": None, "error": None}
    if not media_path:
        out["error"] = "no recording"
        return out
    res = generate_json("WATCH_URLS", _pages_vars(user_urls, sop_text, note),
                        untrusted=["user_pages", "page_note", "sop"], media_path=media_path)
    if not (res.ok and isinstance(res.data, dict)):
        out["error"] = res.error or "the AI did not answer"
        return out
    out.update(agent_source=res.agent_source, model=res.model, notes=str(res.data.get("notes") or "")[:600])
    observed = str(res.data.get("observed_text") or "")
    if observed:
        tv = screen_text(observed, "MEDIA_TRANSCRIPT")
        if not tv.is_safe:
            tv.channel = "MEDIA_TRANSCRIPT"
            raise GuardrailViolation(tv)
    known = list(user_urls)
    seen = set()
    for item in (res.data.get("pages") or [])[:20]:
        if not isinstance(item, dict):
            continue
        raw = str(item.get("url") or "")[:500]
        vt = str(item.get("video_time") or "").strip()
        vt = vt if _VIDEO_TIME.match(vt) else ""
        url = complete_url(raw, known + [f["url"] for f in out["found"]])
        if not url:
            out["rejected"].append({"url": raw[:200], "video_time": vt, "kind": "partial",
                                    "reason": "only part of the address was readable - add the full address yourself"})
            continue
        uv = screen_url(url)
        if not uv.is_safe:
            out["rejected"].append({"url": url[:200], "video_time": vt, "kind": "blocked",
                                    "reason": uv.friendly_message[:300]})
            continue
        if uv.normalized_url in seen:
            continue
        seen.add(uv.normalized_url)
        ev = screen_text(str(item.get("evidence") or "")[:240], "PAYLOAD", use_semantic=False)
        pu = screen_text(str(item.get("purpose") or "")[:120], "PAYLOAD", use_semantic=False)
        out["found"].append({"url": uv.normalized_url, "video_time": vt,
                             "evidence": ev.sanitized if ev.is_safe else "",
                             "purpose": pu.sanitized if pu.is_safe else ""})
    return out


def analyze_inputs(target_url: str, sop_text: str, media_path: Optional[str], media_kind: str = "video",
                   history: Optional[Dict[str, Any]] = None, extra_urls: Optional[List[str]] = None,
                   page_note: str = "") -> Dict[str, Any]:
    """Runs WATCH. Returns {analysis, inventory, agent_source, model, notes, security_problems}. Raises GuardrailViolation.
    ``history`` is the resolved context of previous sessions the user picked (core/wizard/history.py).
    9.4: ``extra_urls`` - the other pages the process visits, in order; each is read and shown to the AI."""
    inv, others = discover_pages(target_url, extra_urls)
    extra_urls = [u for u, _ in others]
    notes: List[str] = []
    if inv.error:
        notes.append(f"Could not read the target page ({inv.error}).")
    res = generate_json(
        "WATCH_ANALYZE",
        {"target_url": urls_for_prompt(target_url, extra_urls), "dom_inventory": inventories_for_prompt(inv, others),
         "sop_text": sop_text or "", "page_note": page_note or "(none)",
         "history_context": history_mod.prompt_text(history), "catalog_hint": catalog_hint_text()},
        untrusted=["dom_inventory", "sop_text", "history_context", "page_note"], media_path=media_path)
    agent_source, model = "LOCAL_HEURISTIC", "DOM discovery + SOP parser"
    raw: Optional[Dict[str, Any]] = None
    if res.ok:
        raw, agent_source, model = res.data, res.agent_source, res.model
        observed = str(raw.get("observed_text") or "")
        if observed:
            tv = screen_text(observed, "MEDIA_TRANSCRIPT")
            if not tv.is_safe:
                tv.channel = "MEDIA_TRANSCRIPT"
                raise GuardrailViolation(tv)
        if res.recall:
            notes.append(_recall_note(res.recall))
            if not res.recall["exact"] and not (res.recall.get("detail") or {}).get("same_recording"):
                # another recording of the same form: the values come from THIS session (its SOP, else the page's
                # own defaults/examples, exactly like the offline page analysis) - never from the old recording
                here = {p["key"]: p for p in _heuristic_analysis(target_url, inv, sop_text).get("parameters", [])}
                for p in raw.get("parameters") or []:
                    h = here.get(p.get("key"))
                    if h is not None and p.get("type") != "secret":
                        p["value"], p["source"] = h.get("value", ""), h.get("source") or "page"
    else:
        notes.append(f"AI video analysis unavailable: {res.error}")
        if not inv.fields():
            record_fallback("WATCH_ANALYZE", res.error or "AI unavailable", None,
                            "No form fields on the target page either - nothing to infer the process from.")
            return {"analysis": None, "inventory": inv.to_dict(), "agent_source": agent_source, "model": model,
                    "ai_error": res.error,
                    "notes": notes + ["No form fields were found on the target page, so the process can't be inferred "
                                      "without AI video analysis. Check the URL, or fix the AI problem above."],
                    "security_problems": []}
        raw = _heuristic_analysis(target_url, inv, sop_text, res.error or "")
        if others:
            notes.extend(_merge_other_pages(raw, others, sop_text))
        raw["history_applied"] = history_mod.apply_offline(raw, history, inv, target_url)
        record_fallback("WATCH_ANALYZE", res.error or "AI unavailable",
                        {k: raw.get(k) for k in ("process_name", "steps", "parameters", "verification", "history_applied")},
                        "DOM discovery of the target page + SOP value parser"
                        + (" + selectors/parameter names re-used from the chosen previous sessions" if history else "") + ".")
    analysis, problems = normalize_analysis(raw, target_url, extra_urls)
    ground_analysis(analysis, inv, target_url)
    analysis["media_interpreted"] = agent_source == "ACTUAL_AI_API" or bool(
        res.recall and (res.recall.get("detail") or {}).get("same_recording"))
    analysis["media_kind"] = media_kind
    return {"analysis": analysis, "inventory": inv.to_dict(), "agent_source": agent_source, "model": model,
            "notes": notes, "security_problems": problems, "ai_error": None if res.ok else res.error,
            "recall": res.recall}


def _recall_note(r: Dict[str, Any]) -> str:
    how = "the same recording" if (r.get("detail") or {}).get("same_recording") or r.get("exact") else \
        f"a similar earlier session ({r['score']:.0%} match)"
    return (f"No AI right now ({r.get('why_no_ai') or 'offline'}). Understudy re-used what it learned from {how}: "
            f"{r.get('label')} (learned {str(r.get('learned_at'))[:10]} with {r.get('learned_with')}). Check the plan "
            "and the values below before you continue.")


# ---------------------------------------------------------------------------
# WATCH clarification loop: the user explains what the AI missed, until it is right
# ---------------------------------------------------------------------------
def previous_for_prompt(analysis: Dict[str, Any], parameters: List[Dict[str, Any]]) -> Dict[str, Any]:
    """The understanding the user saw, without secret values (never re-sent to the model)."""
    params = [{k: ("" if (p.get("type") == "secret" and k == "value") else v) for k, v in p.items()
               if k in ("key", "label", "type", "value", "verify", "sop_dependent", "description")} for p in parameters]
    return {"process_name": analysis.get("process_name"), "summary": analysis.get("summary"),
            "app_type": analysis.get("app_type"), "steps": analysis.get("steps"), "parameters": params,
            "verification": analysis.get("verification"), "api": analysis.get("api")}


_KV_RX = re.compile(r"(?im)^\s*[-*]?\s*([A-Za-z][A-Za-z0-9 _()/.-]{1,60}?)\s*(?:[:=]|\bshould be\b|\bis\b)\s*(.+?)\s*$")
_VERIFY_RX = re.compile(r"(?i)\b(?:verify|check|confirm)\w*\b[^\n]*?\b(?:on|at|in)\s+(https?://\S+)")


def _norm_words(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(t or "").lower()).strip()


def _heuristic_refine(previous: Dict[str, Any], parameters: List[Dict[str, Any]], corrections: List[str],
                      target_url: str, reason: str) -> Tuple[Dict[str, Any], List[str]]:
    """Offline refinement: apply 'Field: value' statements and 'verify on <url>' to the previous understanding."""
    raw = {k: v for k, v in previous.items()}
    params = [dict(p) for p in parameters]
    raw["steps"] = [dict(s) for s in previous.get("steps") or []]
    applied: List[str] = []
    for text in corrections:                              # oldest first: later corrections win
        for m in _KV_RX.finditer(text):
            name, value = _norm_words(m.group(1)), m.group(2).strip().strip("\"'").rstrip(".")
            if not name or not value or len(value) > 200:
                continue
            for p in params:
                if p.get("type") == "secret":
                    continue
                if name in (_norm_words(p["key"]), _norm_words(p.get("label"))) or \
                        (len(name) > 3 and name in _norm_words(p.get("label"))):
                    if str(p.get("value")) != value:
                        p["value"], p["source"] = value, "user"
                        applied.append(f"{p.get('label') or p['key']} = {value}")
                    break
        mv = _VERIFY_RX.search(text)
        if mv:
            raw.setdefault("verification", {})
            raw["verification"] = dict(raw["verification"] or {}, url=mv.group(1).rstrip(".,)"))
            applied.append(f"verify on {mv.group(1)}")
    raw["parameters"] = params
    raw["framework_options"] = []
    if applied:
        raw["summary"] = ("Updated: " + "; ".join(applied[-6:]) + ". " + str(previous.get("summary") or ""))[:1500]
        notes = []
    else:
        notes = [f"The correction could not be applied automatically without AI ({reason}). Use 'Field: value' lines "
                 "(e.g. 'Vendor Name: Globex Corp') or 'verify on <url>', or edit the payload table in Step 2."]
    return raw, notes


def refine_analysis(target_url: str, sop_text: str, media_path: Optional[str], previous: Dict[str, Any],
                    parameters: List[Dict[str, Any]], corrections: List[str],
                    history: Optional[Dict[str, Any]] = None, extra_urls: Optional[List[str]] = None) -> Dict[str, Any]:
    """Re-runs WATCH with the whole correction history. Returns the same shape as analyze_inputs + 'applied_by'."""
    inv, others = discover_pages(target_url, extra_urls)
    extra_urls = [u for u, _ in others]
    prev = previous_for_prompt(previous, parameters)
    res = generate_json(
        "WATCH_REFINE",
        {"target_url": urls_for_prompt(target_url, extra_urls), "dom_inventory": inventories_for_prompt(inv, others),
         "sop_text": sop_text or "",
         "previous_analysis": prev,
         "corrections": "\n".join(f"{i}. {c}" for i, c in enumerate(corrections, 1)),
         "history_context": history_mod.prompt_text(history), "catalog_hint": catalog_hint_text()},
        untrusted=["dom_inventory", "sop_text", "corrections", "history_context"], media_path=media_path)
    notes: List[str] = []
    if res.ok:
        raw, agent_source, model = res.data, res.agent_source, res.model
        observed = str(raw.get("observed_text") or "")
        if observed:
            tv = screen_text(observed, "MEDIA_TRANSCRIPT")
            if not tv.is_safe:
                tv.channel = "MEDIA_TRANSCRIPT"
                raise GuardrailViolation(tv)
    else:
        raw, notes = _heuristic_refine(prev, parameters, corrections, target_url, res.error or "AI unavailable")
        agent_source, model = "LOCAL_HEURISTIC", "Correction parser"
        record_fallback("WATCH_REFINE", res.error or "AI unavailable",
                        {k: raw.get(k) for k in ("summary", "parameters", "verification")},
                        "Applied 'Field: value' and 'verify on <url>' statements to the previous understanding.")
    if agent_source != "ACTUAL_AI_API" and history:
        raw["history_applied"] = history_mod.apply_offline(raw, history, inv, target_url)
    analysis, problems = normalize_analysis(raw, target_url, extra_urls)
    ground_analysis(analysis, inv, target_url)
    if agent_source != "ACTUAL_AI_API" and not raw.get("framework_options"):
        # keep the earlier AI framework suggestions instead of the offline list
        analysis["framework_options"] = previous.get("framework_options") or analysis["framework_options"]
    analysis["media_interpreted"] = agent_source == "ACTUAL_AI_API" or bool(previous.get("media_interpreted"))
    analysis["media_kind"] = previous.get("media_kind")
    return {"analysis": analysis, "inventory": inv.to_dict(), "agent_source": agent_source, "model": model,
            "notes": notes, "security_problems": problems, "ai_error": None if res.ok else res.error,
            "recall": res.recall}
