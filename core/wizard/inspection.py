"""Automation-aware inspection (9.4): look at the AUTOMATION first, then check only what it needs - once.

Up to 9.3 every session loaded the target page twice and only compared locators, whatever the process was. Now, after
WATCH has understood the recording:

1. **Plan** - the ``INSPECTION_PLAN`` agent reads the step plan (no values), the pages the user listed and the SOP and
   decides, per page, which checks this automation needs from a fixed menu (``CHECKS``) and why - e.g. "login: step 3
   types a password", "repeated rows: the SOP fills one record per row, so the form is re-found after every submit".
   It also states its reloading intent (``always`` / ``if_suspicious`` / ``never``). Without AI, ``rule_plan`` makes
   the same decisions from the steps and the SOP.
2. **Inspect** - one headless Chromium. Every page is opened once; then the ``INSPECTION_RELOAD`` agent looks at what
   each first load showed (generated-looking ids on the steps' fields, recorded fields not found, framework ...) next
   to what the automation does, and DECIDES per page how many more fresh loads are needed to compare ids / names /
   order (0-3), and how many fresh loads the build agent must use to confirm the generated code's locators (1-3).
   Without AI, ``rule_reloads`` decides. The elements each step uses are looked up on their page; elements that are
   not in the main document are searched in iframes and shadow roots. Every check returns a finding that says whether
   it matters for THIS automation.
3. **Profile** - the result has the same shape as ``page_probe.probe`` (renderer, loads, signals, fields) plus
   ``pages``, ``checks`` and ``plan``, so the step grounding, the complexity score and the build / heal agents use it
   unchanged. Signals only count what is relevant: an iframe on the page that no step touches does not make the
   automation harder.

No page is ever submitted or clicked here: the inspection only reads.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from core.wizard import page_probe
from core.wizard.guardrails import screen_url
from core.wizard.llm import generate_json

# The menu the planner chooses from. Every check is read-only and runs inside the page's single load.
CHECKS: Dict[str, str] = {
    "targets": "Find every element the steps use on its page (by its visible label / selector)",
    "reachability": "The page opens directly from its address (no redirect to a sign-in page)",
    "stability": "Reload the page (as many times as the reload agent decides) and compare ids, names and field order",
    "frames": "Look for the steps' elements inside iframes",
    "shadow_dom": "Look for the steps' elements inside web components (shadow DOM)",
    "late_render": "Measure how long the page takes to show its elements (single-page apps)",
    "login": "A sign-in on the way: password fields, secrets",
    "file_upload": "File inputs the process uploads to",
    "new_window": "Links / buttons that open a new tab or window",
    "dialogs": "Buttons that pop up a browser confirm / alert",
    "captcha": "A CAPTCHA (made to stop automation)",
    "repeated_rows": "Tables / repeated rows the process loops over",
}
CHECK_TITLES = {
    "targets": "Elements the steps use", "reachability": "Page opens directly", "stability": "Ids / order on reload",
    "frames": "Iframes", "shadow_dom": "Shadow DOM", "late_render": "Late rendering", "login": "Sign-in",
    "file_upload": "File upload", "new_window": "New tab / window", "dialogs": "Pop-up dialogs", "captcha": "CAPTCHA",
    "repeated_rows": "Repeated rows",
}
ESSENTIAL = ("targets", "reachability", "captcha")
STABILITY_MODES = ("always", "if_suspicious", "never")
LATE_RENDER_MS = 2500

_DATA_DRIVEN = re.compile(r"(?i)\b(\d+\s+(rounds?|rows?|records?|times)|each (row|record|line)|every (row|record)|"
                          r"excel|spreadsheet|csv|for all|repeat|loop)\b")
_LOGIN = re.compile(r"(?i)pass(word|code)|sign[ -]?in|log[ -]?in|otp|username")
_UPLOAD = re.compile(r"(?i)upload|attach|choose file|browse")
_DIALOG = re.compile(r"(?i)delete|remove|cancel|discard|confirm|reset")
_NEWWIN = re.compile(r"(?i)open|view|preview|print|download|report|details")

# Runs inside the page after the probe: evidence for the checks the plan picked (all read-only).
_EXTRA_JS = r"""
(labels) => {
  const norm = (t) => (t || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const want = labels.map(norm).filter(Boolean);
  const textOf = (root) => { try { return norm(root.innerText || root.textContent || ''); } catch (e) { return ''; } };
  const inFrames = [], frameInfo = [];
  for (const f of document.querySelectorAll('iframe')) {
    let doc = null; try { doc = f.contentDocument; } catch (e) { doc = null; }
    const src = (f.getAttribute('src') || '').slice(0, 120);
    if (!doc) { frameInfo.push({ src, cross_origin: true }); continue; }
    const t = textOf(doc.body);
    const hits = want.filter((w) => t.includes(w));
    frameInfo.push({ src, cross_origin: false, hits: hits.length });
    hits.forEach((h) => inFrames.push(h));
  }
  const inShadow = [];
  const walk = (root, depth) => {
    if (depth > 6) return;
    root.querySelectorAll('*').forEach((el) => {
      if (el.shadowRoot) {
        const t = textOf(el.shadowRoot);
        want.filter((w) => t.includes(w)).forEach((w) => inShadow.push(w));
        walk(el.shadowRoot, depth + 1);
      }
    });
  };
  try { walk(document, 0); } catch (e) {}
  const newWin = [...document.querySelectorAll('a[target=_blank], [onclick*="window.open"], form[target=_blank]')]
    .map((e) => (e.innerText || e.getAttribute('aria-label') || e.getAttribute('href') || '').trim().slice(0, 50)).filter(Boolean);
  const dialogs = [...document.querySelectorAll('[onclick*="confirm("], [onclick*="alert("], [onsubmit*="confirm("]')]
    .map((e) => (e.innerText || e.value || e.getAttribute('aria-label') || '').trim().slice(0, 50));
  const rows = [...document.querySelectorAll('table')].map((t) => t.querySelectorAll('tbody tr, tr').length);
  const login = !!document.querySelector('input[type=password]') || /sign in|log in|login/i.test(document.title || '');
  return { in_frames: [...new Set(inFrames)], in_shadow: [...new Set(inShadow)], frames: frameInfo.slice(0, 10),
           new_window: newWin.slice(0, 8), dialogs: dialogs.slice(0, 8), table_rows: rows.slice(0, 10), login_form: login };
}
"""


# ---------------------------------------------------------------------------------------------------------------
# Which steps happen on which page
# ---------------------------------------------------------------------------------------------------------------
def _same(a: str, b: str) -> bool:
    return str(a or "").rstrip("/").lower() == str(b or "").rstrip("/").lower()


def page_of_steps(steps: List[Dict[str, Any]], urls: List[str]) -> Dict[int, int]:
    """{step order: page index (1-based)}. A navigate step to one of the user's addresses moves to that page; the
    other steps happen on the page the process is on."""
    out: Dict[int, int] = {}
    cur = 1
    for st in sorted(steps or [], key=lambda x: x.get("order", 0)):
        if st.get("action") == "navigate":
            v = str(st.get("value") or "")
            hit = next((i for i, u in enumerate(urls, 1) if v and _same(v, u)), None)
            if hit:
                cur = hit
            elif not v:
                cur = 1 if st.get("order", 0) <= 1 else cur
        out[int(st.get("order") or 0)] = cur
    return out


def _target_label(st: Dict[str, Any]) -> str:
    return str(st.get("label") or st.get("param_label") or "").strip()


# ---------------------------------------------------------------------------------------------------------------
# 1. Plan
# ---------------------------------------------------------------------------------------------------------------
def rule_plan(steps: List[Dict[str, Any]], params: List[Dict[str, Any]], sop: str, urls: List[str]) -> Dict[str, Any]:
    """The deterministic plan (also the baseline the AI planner starts from)."""
    where = page_of_steps(steps, urls)
    pages = []
    data_driven = bool(_DATA_DRIVEN.search(sop or ""))
    secrets = any(p.get("type") == "secret" for p in params or [])
    for i, u in enumerate(urls, 1):
        mine = [st for st in steps or [] if where.get(int(st.get("order") or 0)) == i]
        labels = " ".join(_target_label(st) + " " + str(st.get("description") or "") for st in mine)
        checks: Dict[str, str] = {
            "targets": f"{len([s for s in mine if s.get('action') in ('fill', 'select', 'check', 'click')])} step(s) "
                       "act on this page - each element must be found" if mine else
                       "no recorded step is on this page yet - check what it offers",
            "reachability": "the run opens this address directly" if i == 1 or any(
                s.get("action") == "navigate" for s in mine) else "the process reaches this page after earlier steps",
            "captcha": "a CAPTCHA would stop any automation - always checked"}
        if secrets or _LOGIN.search(labels):
            checks["login"] = "a step types a password / secret" if secrets else "a step looks like a sign-in"
        if _UPLOAD.search(labels):
            checks["file_upload"] = "a step uploads / attaches a file"
        if any(s.get("action") == "click" and _DIALOG.search(_target_label(s) + " " + str(s.get("description") or ""))
               for s in mine):
            checks["dialogs"] = "a click (delete / cancel / confirm ...) may pop up a browser dialog"
        if len(urls) > 1 or any(s.get("action") == "click" and _NEWWIN.search(_target_label(s)) for s in mine):
            checks["new_window"] = "the process moves between pages - a link may open a new tab instead"
        if data_driven:
            checks["repeated_rows"] = "the SOP repeats the process for several records / rows"
        if any(s.get("action") in ("fill", "select", "check") for s in mine):
            checks["late_render"] = "the steps fill fields - if the page builds them late, the code must wait"
        pages.append({"index": i, "url": u, "steps": [int(s.get("order") or 0) for s in mine], "checks": checks})
    stability = "always" if data_driven else "if_suspicious"
    why = ("the SOP repeats the process, so the form is re-found after every submit - its ids / order must be known "
           "to stay the same" if data_driven else
           "reload only if the first load shows generated-looking ids or names")
    return {"pages": pages, "stability": stability, "stability_why": why, "focus": [],
            "agent_source": "LOCAL_HEURISTIC", "model": "Inspection rules"}


def plan(s: Dict[str, Any], urls: List[str]) -> Dict[str, Any]:
    """The inspection plan for this session: the AI planner when available, the rules otherwise. The essential checks
    (targets, reachability, captcha) are always kept."""
    analysis = s.get("analysis") or {}
    steps = analysis.get("steps") or []
    base = rule_plan(steps, s.get("parameters") or [], s.get("sop_text") or "", urls)
    res = generate_json("INSPECTION_PLAN", {
        "process": json.dumps({"name": analysis.get("process_name") or "", "steps": [
            {"order": st.get("order"), "action": st.get("action"), "label": _target_label(st) or st.get("description"),
             "page": p["index"]} for p in base["pages"] for st in steps if int(st.get("order") or 0) in p["steps"]][:40],
            "secret_values": len([x for x in s.get("parameters") or [] if x.get("type") == "secret"]),
            "values": len(s.get("parameters") or [])}, indent=1),
        "pages": "\n".join(f"{p['index']}. {p['url']}" for p in base["pages"]),
        "checks_menu": "\n".join(f"- {k}: {v}" for k, v in CHECKS.items()),
        "baseline": json.dumps({"pages": [{"index": p["index"], "checks": p["checks"]} for p in base["pages"]],
                                "stability": base["stability"]}),
        "sop": (s.get("sop_text") or "")[:1500]}, untrusted=["process", "sop", "pages"])
    if not (res.ok and isinstance(res.data, dict)):
        return base
    d = res.data
    out = dict(base, agent_source=res.agent_source, model=res.model)
    by_index = {p["index"]: p for p in base["pages"]}
    for ap in d.get("pages") or []:
        if not isinstance(ap, dict):
            continue
        try:
            idx = int(ap.get("index") or 0)
        except (TypeError, ValueError):
            continue
        if idx not in by_index:
            continue
        chosen: Dict[str, str] = {}
        for c in ap.get("checks") or []:
            cid = str((c or {}).get("id") if isinstance(c, dict) else c or "").strip().lower()
            if cid in CHECKS and cid != "stability":
                why = str((c or {}).get("why") or "") if isinstance(c, dict) else ""
                chosen[cid] = why[:200] or by_index[idx]["checks"].get(cid) or CHECKS[cid]
        for e in ESSENTIAL:
            chosen.setdefault(e, by_index[idx]["checks"].get(e) or CHECKS[e])
        by_index[idx] = dict(by_index[idx], checks=chosen)
    out["pages"] = [by_index[i] for i in sorted(by_index)]
    if d.get("stability") in STABILITY_MODES:
        out["stability"] = d["stability"]
        out["stability_why"] = str(d.get("stability_why") or "")[:240] or base["stability_why"]
    out["focus"] = [str(x)[:200] for x in (d.get("focus") or []) if x][:4]
    return out


# ---------------------------------------------------------------------------------------------------------------
# 2. Inspect
# ---------------------------------------------------------------------------------------------------------------
def _load(page, url: str) -> Tuple[Dict[str, Any], int]:
    t0 = time.time()
    page.goto(url, wait_until="domcontentloaded", timeout=page_probe.PROBE_TIMEOUT_MS)
    try:
        page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        pass
    ready_ms = int((time.time() - t0) * 1000)
    page.wait_for_timeout(page_probe.SETTLE_MS)
    return page.evaluate(page_probe._PROBE_JS), ready_ms


def _match(st: Dict[str, Any], fields: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    lab = page_probe._norm(_target_label(st))
    sel = str(st.get("selector") or "")
    for f in fields:
        if lab and page_probe._norm(f.get("label") or f.get("text") or "") == lab:
            return f
    for f in fields:
        attrs = f.get("attrs") or {}
        for a in ("id", "name", "data-testid", "aria-label", "placeholder"):
            v = attrs.get(a)
            if v and len(v) >= 2 and re.search(r"(?<![\w-])" + re.escape(v) + r"(?![\w-])", sel):
                return f
    if lab:
        for f in fields:
            fl = page_probe._norm(f.get("label") or f.get("text") or "")
            if fl and len(fl) > 2 and (fl in lab or lab in fl):
                return f
    return None


def _finding(cid: str, why: str, result: str, finding: str = "", relevant: bool = False, **extra) -> Dict[str, Any]:
    return dict({"id": cid, "title": CHECK_TITLES.get(cid, cid), "why": why, "result": result, "finding": finding,
                 "relevant": relevant}, **extra)


MAX_RELOADS = 3
MAX_VERIFY_LOADS = 3


def _evidence(i: int, url: str, snap: Dict[str, Any], mine: List[Dict[str, Any]], ready_ms: int,
              prior_page: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """What the first load of a page showed - what the reload agent decides from."""
    generated = page_probe.generated_looking(snap)
    matched = {id(st): _match(st, snap.get("fields") or []) for st in mine}
    used = {(f or {}).get("label") for f in matched.values()} - {None, ""}
    unmatched = [st for st in mine if matched[id(st)] is None and st.get("action") in ("fill", "select", "check")]
    sig = snap.get("signals") or {}
    known = (prior_page or {}).get("signals") or {}
    return {"page": i, "url": url, "title": snap.get("title", ""), "ready_ms": ready_ms,
            "steps_on_page": [{"order": st.get("order"), "action": st.get("action"), "label": _target_label(st)}
                              for st in mine][:20],
            "step_elements_found": len(mine) - len([st for st in mine if matched[id(st)] is None]),
            "step_elements_not_found": [f"step {st.get('order')} '{_target_label(st) or st.get('selector') or ''}'"
                                        for st in unmatched][:8],
            "generated_looking_ids_on_step_fields": [g for g in generated if g in used][:8],
            "generated_looking_ids_on_page": len(generated),
            "framework": sig.get("framework") or "", "fields_on_page": len(snap.get("fields") or []),
            "known_from_earlier_inspection": {k: True for k in ("dynamic_ids", "dynamic_names", "shuffled_fields")
                                              if known.get(k)}}


def rule_reloads(ev: Dict[str, Any], the_plan: Dict[str, Any]) -> Dict[str, Any]:
    """Offline reload decision (the agent's fallback): reload once when the automation re-uses the form, or when the
    first load shows generated-looking ids on the steps' fields (or the recorded fields are gone and the page has
    generated-looking ids). Nothing to compare when an earlier inspection already learned it."""
    mode = the_plan.get("stability") or "if_suspicious"
    if ev["known_from_earlier_inspection"]:
        return {"reloads": 0, "why": "an earlier inspection already saw this page's ids / order change",
                "verify_loads": 1, "source": "rules"}
    if not ev["steps_on_page"]:
        return {"reloads": 0, "why": "no step acts on this page", "verify_loads": 1, "source": "rules"}
    if mode == "never":
        return {"reloads": 0, "why": the_plan.get("stability_why") or "the plan said no reload is needed",
                "verify_loads": 1, "source": "rules"}
    if mode == "always":
        return {"reloads": 1, "why": the_plan.get("stability_why") or "the process re-uses this form",
                "verify_loads": 1, "source": "rules"}
    gen = ev["generated_looking_ids_on_step_fields"]
    if gen:
        return {"reloads": 1, "why": f"{len(gen)} field(s) the steps use have generated-looking ids / names "
                                     f"({', '.join(gen[:3])})", "verify_loads": 1, "source": "rules"}
    if ev["step_elements_not_found"] and ev["generated_looking_ids_on_page"]:
        return {"reloads": 1, "why": "the recorded fields are not on the fresh page and the page has generated-looking "
                                     "ids - they probably change on every load", "verify_loads": 1, "source": "rules"}
    return {"reloads": 0, "why": "the first load showed no generated-looking ids on the fields the steps use",
            "verify_loads": 1, "source": "rules"}


def decide_reloads(evidence: List[Dict[str, Any]], the_plan: Dict[str, Any],
                   steps: List[Dict[str, Any]], sop: str = "") -> Dict[int, Dict[str, Any]]:
    """9.4: the RELOAD agent (``INSPECTION_RELOAD``) - after every page was opened once, it looks at what the automation
    does and what each first load showed, and decides per page how many more fresh loads are needed to compare ids /
    names / field order (0-3), and how many fresh loads the build agent should use to confirm the generated code's
    locators (1-3). Without AI, ``rule_reloads`` decides."""
    rules = {ev["page"]: rule_reloads(ev, the_plan) for ev in evidence}
    if not evidence:
        return rules
    res = generate_json("INSPECTION_RELOAD", {
        "process": json.dumps([{"order": st.get("order"), "action": st.get("action"),
                                "label": _target_label(st) or st.get("description")} for st in steps or []][:40], indent=1),
        "plan": json.dumps({"stability": the_plan.get("stability"), "stability_why": the_plan.get("stability_why"),
                            "focus": the_plan.get("focus") or []}),
        "evidence": json.dumps(evidence, indent=1, default=str),
        "baseline": json.dumps({str(k): {x: v[x] for x in ("reloads", "why", "verify_loads")} for k, v in rules.items()}),
        "limits": f"reloads 0-{MAX_RELOADS} per page, verify_loads 1-{MAX_VERIFY_LOADS} per page",
        "sop": (sop or "")[:1200]}, untrusted=["process", "evidence", "sop"])
    if not (res.ok and isinstance(res.data, dict)):
        return rules
    out = dict(rules)
    for d in res.data.get("pages") or []:
        if not isinstance(d, dict):
            continue
        try:
            i = int(d.get("page") or 0)
            n = max(0, min(MAX_RELOADS, int(d.get("reloads") or 0)))
            v = max(1, min(MAX_VERIFY_LOADS, int(d.get("verify_loads") or 1)))
        except (TypeError, ValueError):
            continue
        if i in out:
            out[i] = {"reloads": n, "why": str(d.get("why") or "")[:240] or out[i]["why"], "verify_loads": v,
                      "verify_why": str(d.get("verify_why") or "")[:200], "source": res.agent_source,
                      "model": res.model}
    return out


def inspect(urls: List[str], the_plan: Dict[str, Any], steps: List[Dict[str, Any]],
            prior: Optional[Dict[str, Any]] = None, sop: str = "") -> Dict[str, Any]:
    """Runs the plan in three phases. Never raises.

    1. every page is opened ONCE and its planned evidence is collected;
    2. the reload agent (``decide_reloads``) sees that evidence next to what the automation does and decides, per page,
       how many more fresh loads are needed to compare ids / order (0-3) - and how many loads the build agent should use
       to confirm locators;
    3. the reloads it asked for happen, then every planned check is evaluated.

    ``prior`` (a previous inspection, during self-healing) tells the agent what was already learned."""
    where = page_of_steps(steps, urls)
    pages_out: List[Dict[str, Any]] = []
    all_fields: List[Dict[str, Any]] = []
    checks_out: List[Dict[str, Any]] = []
    total_loads = 0
    sig: Dict[str, Any] = {}
    decisions: Dict[int, Dict[str, Any]] = {}
    try:
        from playwright.sync_api import sync_playwright
        from core.wizard.dom_discovery import install_ssrf_guard
    except Exception as e:
        return _unprobed(urls, the_plan, f"Playwright is not available ({e})")

    def fresh(browser, url):
        ctx = browser.new_context()
        page = ctx.new_page()
        install_ssrf_guard(page)
        return ctx, page

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                # -- phase 1: one load per page ------------------------------------------------------------------
                first: Dict[int, Dict[str, Any]] = {}
                for pplan in the_plan.get("pages") or []:
                    i, url = pplan["index"], pplan["url"]
                    checks = pplan.get("checks") or {}
                    verdict = screen_url(url)
                    mine = [st for st in steps or [] if where.get(int(st.get("order") or 0)) == i
                            and st.get("action") in ("fill", "select", "check", "click")]
                    if not verdict.is_safe:
                        pages_out.append({"index": i, "url": url, "error": verdict.friendly_message, "loads": 0,
                                          "steps": pplan.get("steps") or []})
                        checks_out.append(_finding("reachability", checks.get("reachability", ""), "failed",
                                                   verdict.friendly_message, True, page=i))
                        continue
                    ctx, page = fresh(browser, url)
                    try:
                        snap, ready_ms = _load(page, verdict.normalized_url)
                        total_loads += 1
                        extra = page.evaluate(_EXTRA_JS, [_target_label(st) for st in mine if _target_label(st)])
                    except Exception as e:
                        err = f"{type(e).__name__}: {str(e)[:200]}"
                        pages_out.append({"index": i, "url": url, "error": err, "loads": 1,
                                          "steps": pplan.get("steps") or []})
                        checks_out.append(_finding("reachability", checks.get("reachability", ""), "failed",
                                                   f"the page did not open: {err[:160]}", True, page=i))
                        continue
                    finally:
                        ctx.close()
                    pp = _prior_page(prior, i) if prior else None
                    first[i] = {"pplan": pplan, "url": url, "nurl": verdict.normalized_url, "snap": snap, "extra": extra,
                                "ready_ms": ready_ms, "mine": mine, "prior": pp,
                                "evidence": _evidence(i, url, snap, mine, ready_ms, pp)}
                # -- phase 2: the agent decides how many more loads each page needs -------------------------------
                decisions = decide_reloads([f["evidence"] for f in first.values()], the_plan, steps, sop)
                # -- phase 3: the reloads it asked for, then the checks ----------------------------------------------
                for i, f in first.items():
                    dec = decisions.get(i) or {"reloads": 0, "why": "", "verify_loads": 1}
                    snaps = [f["snap"]]
                    for _ in range(int(dec.get("reloads") or 0)):
                        ctx, page = fresh(browser, f["url"])
                        try:
                            snaps.append(_load(page, f["nurl"])[0])
                            total_loads += 1
                        except Exception:
                            break
                        finally:
                            ctx.close()
                    prof = page_probe.profile_from_snaps(snaps, f["url"])
                    if f["prior"]:
                        _carry_over(prof, f["prior"])
                    for fld in prof["fields"]:
                        fld["page"] = i
                    all_fields.extend(prof["fields"])
                    psig = prof["signals"]
                    checks = f["pplan"].get("checks") or {}
                    page_checks, page_sig = _evaluate(i, f["url"], prof, f["snap"], f["extra"], f["ready_ms"], checks,
                                                      f["mine"], dec, len(snaps), the_plan, f["nurl"])
                    checks_out.extend(page_checks)
                    for k, v in page_sig.items():
                        if isinstance(v, bool):
                            sig[k] = sig.get(k, False) or v
                        elif isinstance(v, (int, float)):
                            sig[k] = (sig.get(k) or 0) + v
                        elif v and not sig.get(k):
                            sig[k] = v
                    pages_out.append({"index": i, "url": f["url"], "final_url": f["snap"].get("url"),
                                      "title": f["snap"].get("title", ""), "loads": len(snaps),
                                      "reload_why": dec.get("why", ""), "reload_decided_by": dec.get("source", "rules"),
                                      "verify_loads": dec.get("verify_loads", 1), "verify_why": dec.get("verify_why", ""),
                                      "ready_ms": f["ready_ms"], "steps": f["pplan"].get("steps") or [],
                                      "summary": page_probe.strategy_summary(prof),
                                      "field_count": psig.get("field_count", 0), "error": None})
            finally:
                browser.close()
    except Exception as e:
        return _unprobed(urls, the_plan, f"{type(e).__name__}: {e}"[:300])
    pages_out.sort(key=lambda p: p["index"])
    _cross_page(checks_out, pages_out, all_fields, steps, sig)
    ok_pages = [p for p in pages_out if not p.get("error")]
    first_ok = ok_pages[0] if ok_pages else (pages_out[0] if pages_out else {"url": urls[0] if urls else ""})
    sig["pages"] = len(urls)
    sig["hosts"] = len({(urlsplit(u).hostname or "").lower() for u in urls})
    sig["field_count"] = sum(1 for f in all_fields if f["kind"] == "field")
    sig["action_count"] = sum(1 for f in all_fields if f["kind"] == "action")
    all_fields.sort(key=lambda f: (f.get("page", 1), 0 if f["kind"] == "field" else 1))
    src = {d.get("source") for d in decisions.values()} - {None}
    return {"renderer": "chromium" if ok_pages else "none", "url": first_ok.get("final_url") or first_ok.get("url"),
            "title": first_ok.get("title", ""), "loads": total_loads, "signals": sig,
            "fields": all_fields[:page_probe.MAX_FIELDS * max(1, min(3, len(ok_pages)))], "pages": pages_out,
            "checks": checks_out, "plan": _plan_view(the_plan),
            "reload_decision": {"source": next(iter(src - {"rules"}), "rules") if src else "rules",
                                "pages": {str(k): v for k, v in decisions.items()}},
            "verify_loads": {p["index"]: p.get("verify_loads", 1) for p in ok_pages},
            "error": None if ok_pages else (pages_out[0].get("error") if pages_out else "no page to inspect")}


def _cross_page(checks: List[Dict[str, Any]], pages: List[Dict[str, Any]], fields: List[Dict[str, Any]],
                steps: List[Dict[str, Any]], sig: Dict[str, Any]) -> None:
    """A step reached by CLICKING through to another listed page has no navigate step, so it was looked for on the page
    before. When its element is on another page the user listed, it is not missing - it is on that page."""
    if len(pages) < 2:
        return
    by_order = {int(st.get("order") or 0): st for st in steps or []}
    moved_total = 0
    for c in checks:
        if c.get("id") != "targets" or not c.get("missing_steps"):
            continue
        still = []
        for o in c["missing_steps"]:
            st = by_order.get(o)
            hit = None
            for pg in pages:
                if pg["index"] == c["page"] or pg.get("error"):
                    continue
                f = _match(st or {}, [x for x in fields if x.get("page") == pg["index"]])
                if f is not None:
                    hit = pg["index"]
                    f.setdefault("used_by_steps", []).append(o)
                    break
            if hit is None:
                still.append(o)
            else:
                moved_total += 1
                for pg in pages:
                    if pg["index"] == hit and o not in (pg.get("steps") or []):
                        pg["steps"] = sorted((pg.get("steps") or []) + [o])
                    if pg["index"] == c["page"] and o in (pg.get("steps") or []):
                        pg["steps"] = [x for x in pg["steps"] if x != o]
        if len(still) != len(c["missing_steps"]):
            n_moved = len(c["missing_steps"]) - len(still)
            c.update(missing_steps=still, missing=len(still), found=c.get("found", 0), relevant=bool(still),
                     result="warning" if still else "ok",
                     finding=c["finding"].split("; not found:")[0] + f"; {n_moved} element(s) are on a later page "
                     "(reached by a click)" + ("; still not found: " + ", ".join(f"step {o}" for o in still[:4])
                                              if still else ""))
    if moved_total:
        sig["missing_targets"] = sum(c.get("missing", 0) for c in checks if c.get("id") == "targets")


def _prior_page(prior: Dict[str, Any], index: int) -> Optional[Dict[str, Any]]:
    return next((p for p in prior.get("pages") or [] if p.get("index") == index and not p.get("error")), None) and {
        "fields": [f for f in prior.get("fields") or [] if f.get("page", 1) == index],
        "signals": prior.get("signals") or {}}


def _carry_over(prof: Dict[str, Any], old: Dict[str, Any]) -> None:
    """A re-inspection (self-healing) opens the page once; what the first inspection learned by reloading stays."""
    by_label = {page_probe._norm(f.get("label") or ""): f for f in old.get("fields") or [] if f.get("label")}
    for f in prof["fields"]:
        o = by_label.get(page_probe._norm(f.get("label") or ""))
        if o and o.get("unstable"):
            f["unstable"] = list(dict.fromkeys((f.get("unstable") or []) + o["unstable"]))
            f["unstable_values"] = dict(o.get("unstable_values") or {}, **(f.get("unstable_values") or {}))
            f["moved_between_loads"] = f.get("moved_between_loads") or o.get("moved_between_loads")
    for k in ("dynamic_ids", "dynamic_names", "shuffled_fields"):
        if (old.get("signals") or {}).get(k):
            prof["signals"][k] = True


def _evaluate(i: int, url: str, prof: Dict[str, Any], snap: Dict[str, Any], extra: Dict[str, Any], ready_ms: int,
              checks: Dict[str, str], mine: List[Dict[str, Any]], decision: Dict[str, Any], loads: int,
              the_plan: Dict[str, Any], normalized_url: str) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """The findings of one page's checks, and the signals that count for THIS automation."""
    raw = prof.get("signals") or {}
    out: List[Dict[str, Any]] = []
    sig: Dict[str, Any] = {}
    found, missing = [], []
    for st in mine:
        f = _match(st, prof["fields"])
        (found if f else missing).append((st, f))
        if f is not None:
            f.setdefault("used_by_steps", []).append(int(st.get("order") or 0))
    lab_missing = [page_probe._norm(_target_label(st)) for st, _ in missing if _target_label(st)]
    in_frames = [x for x in extra.get("in_frames") or [] if x in lab_missing]
    in_shadow = [x for x in extra.get("in_shadow") or [] if x in lab_missing]
    really_missing = [st for st, _ in missing if page_probe._norm(_target_label(st)) not in in_frames + in_shadow]
    if "targets" in checks:
        detail = f"{len(found)} of {len(mine)} element(s) the steps use were found on this page" if mine else \
            "no recorded step acts on this page"
        if really_missing:
            detail += "; not found: " + ", ".join(f"step {st.get('order')} '{_target_label(st) or st.get('selector')}'"
                                                 for st in really_missing[:4]) + \
                      " (they may only appear after an earlier step, or on another page)"
        out.append(_finding("targets", checks["targets"], "warning" if really_missing else "ok", detail,
                            bool(really_missing), page=i, found=len(found), missing=len(really_missing),
                            missing_steps=[int(st.get("order") or 0) for st in really_missing]))
        sig["missing_targets"] = len(really_missing)
        sig["targets_checked"] = len(mine)
    if "reachability" in checks:
        final = str(snap.get("url") or "")
        moved = final and urlsplit(final).path.rstrip("/") != urlsplit(normalized_url).path.rstrip("/")
        login_redirect = moved and (extra.get("login_form") or _LOGIN.search(final))
        out.append(_finding("reachability", checks["reachability"], "warning" if login_redirect else "ok",
                            (f"opening it redirected to {final[:100]} (a sign-in page) - the process must sign in "
                             "first" if login_redirect else f"opening it redirected to {final[:100]}")
                            if moved else "the page opens directly", bool(login_redirect), page=i))
        if login_redirect:
            sig["needs_login_first"] = True
    if "captcha" in checks:
        out.append(_finding("captcha", checks["captcha"], "warning" if raw.get("captcha") else "ok",
                            "the page has a CAPTCHA" if raw.get("captcha") else "no CAPTCHA", bool(raw.get("captcha")),
                            page=i))
        sig["captcha"] = bool(raw.get("captcha"))
    # stability: the reload agent decided (after the first load) how many fresh loads this page needed
    who = "the agent" if decision.get("source") not in (None, "rules") else "the rules"
    if mine or loads > 1:
        if loads > 1:
            changed = raw.get("dynamic_ids") or raw.get("dynamic_names") or raw.get("shuffled_fields")
            parts = [x for x, k in (("ids change", "dynamic_ids"), ("names change", "dynamic_names"),
                                    ("fields move", "shuffled_fields")) if raw.get(k)]
            times = "once" if loads == 2 else f"{loads - 1} times"
            out.append(_finding("stability", f"{who} decided: {decision.get('why') or 'compare ids / order'}",
                                "warning" if changed else "ok",
                                (f"reloaded {times}: " + ", ".join(parts)) if changed else
                                f"reloaded {times}: ids, names and order stayed the same", bool(changed), page=i,
                                loads=loads, decided_by=decision.get("source", "rules")))
            for k in ("dynamic_ids", "dynamic_names", "shuffled_fields"):
                sig[k] = bool(raw.get(k))
        else:
            gen = [f for f in prof["fields"] if any("generated" in u for u in f.get("unstable") or [])]
            used_gen = [f for f in gen if f.get("used_by_steps")]
            out.append(_finding("stability", f"{who} decided no reload: {decision.get('why') or 'not needed'}",
                                "skipped", "not reloaded" + (
                f"; {len(used_gen)} field(s) the steps use still have generated-looking ids - labels are used instead"
                if used_gen else ""), bool(used_gen), page=i, loads=1, decided_by=decision.get("source", "rules")))
            if used_gen:
                sig["dynamic_ids"] = True
            for k in ("dynamic_ids", "dynamic_names", "shuffled_fields"):     # carried over by a re-inspection
                if raw.get(k):
                    sig[k] = True
    if "frames" in checks or in_frames:
        cross = [f for f in extra.get("frames") or [] if f.get("cross_origin")]
        if in_frames:
            fnd = f"{len(in_frames)} element(s) the steps use are inside an iframe: " + ", ".join(in_frames[:3])
            if cross:
                fnd += f" ({len(cross)} iframe(s) from another site may hold the rest)"
        else:
            fnd = f"{raw.get('iframes', 0)} iframe(s) on the page, none holds an element the steps use" \
                if raw.get("iframes") else "no iframes"
        out.append(_finding("frames", checks.get("frames") or "an element the steps use was not in the main page",
                            "warning" if in_frames else "ok", fnd, bool(in_frames), page=i))
        if in_frames:
            sig["iframes"] = 1
            sig["cross_origin_iframes"] = len(cross)
    if "shadow_dom" in checks or in_shadow:
        out.append(_finding("shadow_dom", checks.get("shadow_dom") or "an element the steps use was not in the main page",
                            "warning" if in_shadow else "ok",
                            (f"{len(in_shadow)} element(s) the steps use are inside web components: "
                             + ", ".join(in_shadow[:3])) if in_shadow else
                            (f"{raw.get('shadow_hosts')} web component(s), none holds an element the steps use"
                             if raw.get("shadow_hosts") else "no shadow DOM"), bool(in_shadow), page=i))
        if in_shadow:
            sig["shadow_hosts"] = 1
    if "late_render" in checks:
        late = ready_ms > LATE_RENDER_MS or bool(raw.get("framework"))
        out.append(_finding("late_render", checks["late_render"], "warning" if late else "ok",
                            (f"{raw.get('framework') + ' app, ' if raw.get('framework') else ''}ready after "
                             f"{ready_ms / 1000:.1f}s - the code must wait for each element") if late else
                            f"ready after {ready_ms / 1000:.1f}s", late, page=i, ready_ms=ready_ms))
        if late:
            sig["framework"] = raw.get("framework") or "late-rendering page"
    if "login" in checks:
        has = bool(raw.get("password_inputs")) or bool(extra.get("login_form"))
        out.append(_finding("login", checks["login"], "warning" if has else "ok",
                            "the page has a sign-in form - passwords come from secret environment variables" if has
                            else "no sign-in form on this page", has, page=i))
        if has:
            sig["password_inputs"] = 1
    if "file_upload" in checks:
        n = raw.get("file_inputs") or 0
        out.append(_finding("file_upload", checks["file_upload"], "warning" if n else "ok",
                            f"{n} file input(s)" if n else "no file input on this page", bool(n), page=i))
        if n:
            sig["file_inputs"] = n
    if "new_window" in checks:
        nw = extra.get("new_window") or []
        used = [x for x in nw if any(page_probe._norm(x) == page_probe._norm(_target_label(st)) for st in mine)]
        out.append(_finding("new_window", checks["new_window"], "warning" if used else "ok",
                            ("a step clicks a link that opens a NEW tab: " + ", ".join(used[:3])) if used else
                            (f"{len(nw)} link(s) open a new tab, none is used by a step" if nw else
                             "no link opens a new tab"), bool(used), page=i))
        if used:
            sig["new_window"] = len(used)
    if "dialogs" in checks:
        dl = extra.get("dialogs") or []
        out.append(_finding("dialogs", checks["dialogs"], "warning" if dl else "ok",
                            ("buttons that pop up a browser confirm / alert: " + ", ".join(x for x in dl[:3] if x))
                            if dl else "no inline confirm / alert handlers found", bool(dl), page=i))
        if dl:
            sig["dialogs"] = len(dl)
    if "repeated_rows" in checks:
        rows = max(extra.get("table_rows") or [0])
        out.append(_finding("repeated_rows", checks["repeated_rows"], "warning", (f"a table with {rows} row(s) on the "
                            "page; " if rows else "") + "the process repeats for several records", True, page=i))
        sig["repeated_rows"] = True
    if raw.get("unlabelled_fields") and any(not (f.get("label")) and f.get("used_by_steps") for f in prof["fields"]):
        sig["unlabelled_fields"] = sum(1 for f in prof["fields"] if not f.get("label") and f.get("used_by_steps"))
    return out, sig


def recount_targets(profile: Dict[str, Any], steps: List[Dict[str, Any]], urls: List[str]) -> None:
    """After the step plan was rebuilt from the inspection (offline, on a page whose ids change), count again which of
    the steps' elements are on their page - no page load."""
    if not profile or profile.get("renderer") != "chromium":
        return
    where = page_of_steps(steps, urls)
    missing_total = 0
    for c in profile.get("checks") or []:
        if c.get("id") != "targets":
            continue
        i = c.get("page")
        mine = [st for st in steps if where.get(int(st.get("order") or 0)) == i
                and st.get("action") in ("fill", "select", "check", "click")]
        fields = [f for f in profile.get("fields") or [] if f.get("page", 1) == i]
        missing = [st for st in mine if _match(st, fields) is None]
        missing_total += len(missing)
        c.update(found=len(mine) - len(missing), missing=len(missing), relevant=bool(missing),
                 result="warning" if missing else "ok",
                 finding=f"{len(mine) - len(missing)} of {len(mine)} element(s) the steps use were found on this page"
                 + ("; not found: " + ", ".join(f"step {st.get('order')} '{_target_label(st)}'" for st in missing[:4])
                    if missing else ""))
    profile.setdefault("signals", {})["missing_targets"] = missing_total


def _plan_view(p: Dict[str, Any]) -> Dict[str, Any]:
    return {"stability": p.get("stability"), "stability_why": p.get("stability_why"), "focus": p.get("focus") or [],
            "agent_source": p.get("agent_source"), "model": p.get("model"),
            "pages": [{"index": x["index"], "url": x["url"], "steps": x.get("steps") or [],
                       "checks": [{"id": k, "title": CHECK_TITLES.get(k, k), "why": v}
                                  for k, v in (x.get("checks") or {}).items()]} for x in p.get("pages") or []]}


def _unprobed(urls: List[str], the_plan: Dict[str, Any], err: str) -> Dict[str, Any]:
    return {"renderer": "none", "url": urls[0] if urls else "", "title": "", "loads": 0, "signals": {}, "fields": [],
            "pages": [{"index": i, "url": u, "error": err, "loads": 0} for i, u in enumerate(urls, 1)],
            "checks": [], "plan": _plan_view(the_plan), "error": err}


def summary_line(profile: Dict[str, Any]) -> str:
    """One line for telemetry / the UI."""
    if not profile or profile.get("renderer") != "chromium":
        return "The pages could not be opened - the assessment uses the recording and the SOP only."
    pages = profile.get("pages") or []
    reloaded = [p for p in pages if (p.get("loads") or 0) > 1]
    issues = [c for c in profile.get("checks") or [] if c.get("relevant")]
    return (f"Inspected {len(pages)} page(s) with {profile.get('loads')} page load(s)"
            + (f" (reloaded page {', '.join(str(p['index']) for p in reloaded)} to compare ids / order - decided by "
               f"{'the AI' if (profile.get('reload_decision') or {}).get('source') not in (None, 'rules') else 'the rules'})"
               if reloaded else " - the reload decision was: none needed")
            + f"; {len(profile.get('checks') or [])} check(s), {len(issues)} matter for this automation.")
