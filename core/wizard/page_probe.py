"""Page probe (9.0, 9.4): what an automation can rely on in a web page - and what it can't.

9.4: the probe is the measuring instrument of the automation-aware INSPECTION (``core/wizard/inspection.py``): each
page is opened ONCE, and a second load (to compare ids / order) only happens when the inspection decided the
automation needs it. ``verify_locators`` checks the build agent's locators on ONE fresh load per page, plus a static
check against the ids / names / positions the inspection already saw change. ``probe(url, loads=2)`` keeps the
original two-load comparison for callers that want it.

Complex sites break naive scripts: rpachallenge.com gives its inputs new random ids and a new order on every load and
after every submit; single-page apps render late; widgets hide in iframes or shadow DOM. Before any code is written,
Understudy loads the target page TWICE in headless Chromium (fresh context each time) and compares:

* for every field: its visible label (``<label for>``, wrapping or sibling label, aria-label/labelledby, placeholder,
  nearby text), its attributes, its position - and which of them stayed the same between the loads;
* a label-anchored XPath (``//label[normalize-space()='First Name']/following::input[1]``), checked IN the page to
  resolve to exactly that element on both loads;
* page signals: dynamic ids, shuffled fields, iframes, shadow DOM, SPA framework, captcha, file inputs, unlabelled fields.

``profile_for_prompt`` turns it into the PAGE PROFILE the build / heal agents write code from, and
``verify_locators`` checks the locators an agent used against two fresh loads - a locator that matched on the first
load but not the second (an id that changes) is rejected before any test runs.

No AI is involved here: this is the evidence the AI agents work from.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from core.wizard.guardrails import screen_url

PROBE_TIMEOUT_MS = 20000
SETTLE_MS = 1200
MAX_FIELDS = 40

# Runs inside the page: one entry per visible control, plus page signals.
_PROBE_JS = r"""
() => {
  const vis = (el) => { const r = el.getBoundingClientRect(); const st = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none'; };
  const txt = (el) => (el ? (el.innerText || el.textContent || '') : '').replace(/\s+/g, ' ').trim();
  const lit = (s) => s.includes("'") ? '"' + s + '"' : "'" + s + "'";
  const isAction = (el) => ['BUTTON', 'A'].includes(el.tagName) || ['submit', 'button', 'reset'].includes(el.type) || el.getAttribute('role') === 'button';
  function labelOf(el) {
    if (isAction(el)) {                         // a button / link is named by its own text
      const own = (txt(el) || el.value || el.getAttribute('aria-label') || el.getAttribute('title') || '').slice(0, 60);
      if (own) return { text: own, node: null, how: 'own text' };
    }
    if (el.labels && el.labels.length && txt(el.labels[0])) return { text: txt(el.labels[0]), node: el.labels[0], how: 'label[for]' };
    const al = el.getAttribute('aria-label'); if (al && al.trim()) return { text: al.trim(), node: null, how: 'aria-label' };
    const lb = el.getAttribute('aria-labelledby');
    if (lb) { const n = document.getElementById(lb.split(/\s+/)[0]); if (n && txt(n)) return { text: txt(n), node: n, how: 'aria-labelledby' }; }
    // sibling / nearby label (labels not linked with for= - rpachallenge, many Angular / React forms)
    let cur = el;
    for (let depth = 0; depth < 3 && cur; depth++) {
      let sib = cur.previousElementSibling;
      while (sib) {
        if (['LABEL', 'SPAN', 'DIV', 'P', 'MAT-LABEL', 'LEGEND', 'STRONG', 'B'].includes(sib.tagName) && txt(sib) && txt(sib).length < 60
            && !sib.querySelector('input,select,textarea,button')) return { text: txt(sib), node: sib, how: 'preceding ' + sib.tagName.toLowerCase() };
        sib = sib.previousElementSibling;
      }
      cur = cur.parentElement;
    }
    const ph = el.getAttribute('placeholder'); if (ph && ph.trim()) return { text: ph.trim(), node: null, how: 'placeholder' };
    if (['BUTTON', 'A'].includes(el.tagName) || el.type === 'submit' || el.type === 'button')
      return { text: (txt(el) || el.value || '').slice(0, 60), node: null, how: 'own text' };
    return { text: '', node: null, how: '' };
  }
  function labelXPath(el, lab) {
    if (!lab.node || !lab.text) return '';
    const tag = lab.node.tagName.toLowerCase();
    const anchor = '//' + (tag.includes('-') ? '*[local-name()=' + lit(tag) + ']' : tag) + '[normalize-space()=' + lit(lab.text) + ']';
    const target = el.tagName.toLowerCase();
    const xp = anchor + '/following::' + target + '[1]';
    try {
      const a = document.evaluate(anchor, document, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
      if (a.snapshotLength !== 1) return '';
      const r = document.evaluate(xp, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
      return r === el ? xp : '';
    } catch (e) { return ''; }
  }
  const sel = 'input:not([type=hidden]),textarea,select,button,a[href],[role=button],[role=textbox],[role=combobox],[contenteditable=true]';
  const all = [...document.querySelectorAll(sel)].filter(vis);
  const fields = [];
  all.forEach((el, i) => {
    const lab = labelOf(el);
    const attrs = {};
    for (const a of el.attributes) {
      if (['id', 'name', 'type', 'placeholder', 'role', 'value', 'href', 'formcontrolname', 'aria-label'].includes(a.name)
          || a.name.startsWith('data-') || a.name.startsWith('ng-reflect') || a.name.startsWith('aria-')) attrs[a.name] = (a.value || '').slice(0, 80);
    }
    const r = el.getBoundingClientRect();
    fields.push({ index: i, tag: el.tagName.toLowerCase(), type: (el.getAttribute('type') || '').toLowerCase(),
      kind: isAction(el) ? 'action' : 'field', link: el.tagName === 'A',
      label: lab.text.slice(0, 80), label_how: lab.how, label_xpath: labelXPath(el, lab), attrs,
      text: (txt(el) || el.value || '').slice(0, 60), x: Math.round(r.x), y: Math.round(r.y) });
  });
  const frames = [...document.querySelectorAll('iframe')];
  let shadow = 0; document.querySelectorAll('*').forEach(e => { if (e.shadowRoot) shadow++; });
  const html = document.documentElement.outerHTML;
  return {
    title: document.title, url: location.href, fields: fields.slice(0, 120),
    signals: {
      iframes: frames.length,
      cross_origin_iframes: frames.filter(f => { try { return !f.contentDocument; } catch (e) { return true; } }).length,
      shadow_hosts: shadow,
      captcha: /g-recaptcha|hcaptcha|recaptcha\/api|cf-turnstile/i.test(html),
      framework: document.querySelector('[ng-version]') || /ng-reflect|_ngcontent/.test(html) ? 'Angular'
        : (document.querySelector('[data-reactroot]') || window.__NEXT_DATA__ || /__react/i.test(Object.keys(document.querySelector('body') || {}).join(' '))) ? 'React'
        : (/data-v-[0-9a-f]{6,}/.test(html) ? 'Vue' : ''),
      file_inputs: document.querySelectorAll('input[type=file]').length,
      password_inputs: document.querySelectorAll('input[type=password]').length,
      tables: document.querySelectorAll('table').length,
      forms: document.querySelectorAll('form').length,
    }
  };
}
"""

_RANDOMISH = re.compile(r"^(?=.*\d)(?=.*[a-z])[a-z0-9]{5,}$|^[a-z0-9]{6,}-[a-z0-9]{4,}|^(mat|ember|react|rc|ng)[-_]?[a-z]*[-_]?\d+",
                        re.I)
_STABLE_ATTRS = ("data-testid", "data-test", "data-test-id", "data-qa", "data-cy", "id", "name", "formcontrolname",
                 "aria-label", "ng-reflect-name")


def _norm(t: str) -> str:
    return re.sub(r"\s+", " ", (t or "").strip()).lower()


def _key(f: Dict[str, Any]) -> str:
    return f"{f['tag']}|{f.get('type', '')}|{_norm(f.get('label') or f.get('text') or '')}"


def _load_once(page, url: str) -> Dict[str, Any]:
    page.goto(url, wait_until="domcontentloaded", timeout=PROBE_TIMEOUT_MS)
    try:
        page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        pass
    page.wait_for_timeout(SETTLE_MS)
    return page.evaluate(_PROBE_JS)


def _open_loads(url: str, loads: int) -> List[Dict[str, Any]]:
    from playwright.sync_api import sync_playwright
    from core.wizard.dom_discovery import install_ssrf_guard
    out = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            for _ in range(loads):
                ctx = browser.new_context()
                page = ctx.new_page()
                install_ssrf_guard(page)
                out.append(_load_once(page, url))
                ctx.close()
        finally:
            browser.close()
    return out


def _css(attr: str, value: str) -> str:
    v = value.replace('"', '\\"')
    return f"#{value}" if attr == "id" and re.match(r"^[A-Za-z][\w-]*$", value) else f'[{attr}="{v}"]'


def probe(url: str, loads: int = 2) -> Dict[str, Any]:
    """{url, title, renderer, loads, signals, fields[{label, kind, tag, type, recommended{strategy, css|xpath, why},
    alternatives[...], unstable[...]}], error}. Never raises."""
    verdict = screen_url(url)
    if not verdict.is_safe:
        return {"url": url, "renderer": "none", "error": verdict.friendly_message, "fields": [], "signals": {}}
    try:
        snaps = _open_loads(verdict.normalized_url, max(1, loads))
    except Exception as e:
        return {"url": url, "renderer": "none", "error": f"{type(e).__name__}: {e}"[:300], "fields": [], "signals": {}}
    return profile_from_snaps(snaps, url)


def generated_looking(snap: Dict[str, Any]) -> List[str]:
    """9.4: the labels of fields whose id / name LOOKS generated on a single load (e.g. 'x7Kq2m', 'mat-input-12') - the
    cue for the inspection to reload the page and see whether they really change."""
    out = []
    for f in snap.get("fields") or []:
        for a in ("id", "name"):
            v = (f.get("attrs") or {}).get(a)
            if v and _RANDOMISH.match(v):
                out.append(f.get("label") or f.get("text") or v)
                break
    return out


def profile_from_snaps(snaps: List[Dict[str, Any]], url: str) -> Dict[str, Any]:
    """The page profile from one or more snapshots of the same page (fresh loads). With one snapshot nothing can be
    compared - attributes that LOOK generated are still marked unstable."""
    first = snaps[0]
    others = snaps[1:]
    by_key = [{_key(f): f for f in s["fields"]} for s in snaps]
    order = [[_key(f) for f in s["fields"] if f["kind"] == "field"] for s in snaps]
    fields: List[Dict[str, Any]] = []
    dyn_ids = dyn_names = 0
    for f in first["fields"]:
        k = _key(f)
        twins = [m.get(k) for m in by_key[1:]]
        seen_all = all(t is not None for t in twins)
        unstable = []
        unstable_values: Dict[str, str] = {}
        stable_attrs = {}
        for a in _STABLE_ATTRS:
            v = f["attrs"].get(a)
            if not v:
                continue
            if others and (not seen_all or any((t or {}).get("attrs", {}).get(a) != v for t in twins)):
                unstable.append(a)
                unstable_values[a] = v
                if a == "id":
                    dyn_ids += 1
                if a == "name":
                    dyn_names += 1
                continue
            if a in ("id", "name") and _RANDOMISH.match(v):
                unstable.append(a + " (looks generated)")
                unstable_values[a] = v
                continue
            stable_attrs[a] = v
        moved = bool(others) and seen_all and any((t["x"], t["y"]) != (f["x"], f["y"]) for t in twins)
        xp_ok = f.get("label_xpath") and (not others or all(t and t.get("label_xpath") == f["label_xpath"] for t in twins))
        cands = []
        for a in ("data-testid", "data-test", "data-test-id", "data-qa", "data-cy"):
            if a in stable_attrs:
                cands.append({"strategy": "css", "value": _css(a, stable_attrs[a]), "why": f"stable {a}"})
        if "id" in stable_attrs:
            cands.append({"strategy": "css", "value": _css("id", stable_attrs["id"]), "why": "id stayed the same"})
        if xp_ok:
            cands.append({"strategy": "xpath", "value": f["label_xpath"],
                          "why": f"anchored on its visible label '{f['label']}' ({f['label_how']}) - unique on every load"})
        if "name" in stable_attrs:
            cands.append({"strategy": "css", "value": _css("name", stable_attrs["name"]), "why": "name stayed the same"})
        for a in ("formcontrolname", "aria-label", "ng-reflect-name"):
            if a in stable_attrs:
                cands.append({"strategy": "css", "value": _css(a, stable_attrs[a]),
                              "why": f"stable {a}" + (" (Angular development attribute - may vanish in production "
                                                      "builds)" if a == "ng-reflect-name" else "")})
        if f["kind"] == "action" and (f.get("label") or f.get("text")):
            name = f.get("label") or f.get("text")
            cands.insert(0, {"strategy": "role", "value": f"{'link' if f.get('link') else 'button'}:{name}",
                             "why": f"the {'link' if f.get('link') else 'button'} named '{name}' (role + accessible name)"})
        if (dyn_ids or moved) and xp_ok:          # positions / ids move: the label is the anchor
            cands.sort(key=lambda c: 0 if c["strategy"] == "xpath" else 1 if c["why"].startswith("stable data") else 2)
        fields.append({"label": f.get("label") or f.get("text") or "", "kind": f["kind"], "tag": f["tag"],
                       "type": f.get("type", ""), "label_how": f.get("label_how", ""),
                       "recommended": cands[0] if cands else None, "alternatives": cands[1:3], "unstable": unstable,
                       "unstable_values": unstable_values, "moved_between_loads": moved})
    shuffled = bool(others) and any(o != order[0] for o in order[1:])
    sig = dict(first.get("signals") or {})
    sig.update(dynamic_ids=dyn_ids > 0, dynamic_names=dyn_names > 0, shuffled_fields=shuffled,
               unlabelled_fields=sum(1 for f in fields if f["kind"] == "field" and not f["label"]),
               field_count=sum(1 for f in fields if f["kind"] == "field"),
               action_count=sum(1 for f in fields if f["kind"] == "action"))
    fields.sort(key=lambda f: 0 if f["kind"] == "field" else 1)
    return {"url": first.get("url") or url, "title": first.get("title", ""), "renderer": "chromium", "loads": len(snaps),
            "signals": sig, "fields": fields[:MAX_FIELDS], "error": None}


def _loads_phrase(profile: Dict[str, Any]) -> str:
    pages = profile.get("pages") or []
    if len(pages) > 1:
        return f"{len(pages)} pages, {profile.get('loads')} page load(s) in all"
    n = profile.get("loads") or 1
    return "opened once" if n == 1 else f"opened {n} times (fresh loads)"


def strategy_summary(profile: Dict[str, Any]) -> str:
    sig = profile.get("signals") or {}
    if profile.get("renderer") == "none":
        return "The page could not be loaded in a browser - no live page facts."
    compared = (profile.get("loads") or 1) > len(profile.get("pages") or [1])
    parts = []
    if sig.get("dynamic_ids") or sig.get("dynamic_names"):
        parts.append("element ids/names CHANGE on every load")
    if sig.get("shuffled_fields"):
        parts.append("fields CHANGE POSITION between loads")
    if sig.get("iframes"):
        parts.append(f"{sig['iframes']} iframe(s)")
    if sig.get("shadow_hosts"):
        parts.append("shadow DOM")
    if sig.get("captcha"):
        parts.append("a CAPTCHA")
    if not parts:
        if compared:
            return "The page looked the same when it was reloaded - its ids and positions can be used."
        return ("The page's ids and names look hand-written (not generated), so it was not reloaded - ids can be used; "
                "visible labels stay the safest anchor.")
    return ("This page has " + ", ".join(parts) + ". Locate every element by its visible label (the label-anchored "
            "locators below) - never by id, name, position, index or nth-child - and find the elements again after "
            "every submit.")


def profile_for_prompt(profile: Optional[Dict[str, Any]], limit: int = 30) -> str:
    """The PAGE PROFILE text the build / heal agents get."""
    if not profile:
        return "(no live page profile)"
    if profile.get("renderer") == "none":
        return f"(the page could not be probed: {profile.get('error')})"
    pages = profile.get("pages") or []
    lines = [f"URL: {profile.get('url')} - title '{profile.get('title', '')}' - {_loads_phrase(profile)}.",
             "STRATEGY: " + strategy_summary(profile),
             "SIGNALS: " + json.dumps({k: v for k, v in (profile.get("signals") or {}).items() if v}, default=str)]
    if len(pages) > 1:
        lines.append("PAGES (the process visits them in this order; page N's address is in env var TARGET_URL_<N>):")
        for pg in pages:
            lines.append(f"- page {pg['index']}: {pg['url']} - "
                         + ("could not be opened directly: " + str(pg.get("error"))[:160] if pg.get("error") else
                            f"title '{pg.get('title', '')}', {pg.get('loads', 1)} load(s), steps {pg.get('steps') or '-'}"))
    checks = [c for c in profile.get("checks") or [] if c.get("finding")]
    if checks:
        lines.append("INSPECTION FINDINGS (what this automation needs to handle):")
        for c in checks[:14]:
            lines.append(f"- page {c.get('page')}: {c.get('title')}: {c.get('finding')}")
    lines.append("ELEMENTS (" + ("page | " if len(pages) > 1 else "") +
                 "label | kind | recommended locator | alternatives | unstable attributes):")
    for f in (profile.get("fields") or [])[:limit]:
        rec = f.get("recommended") or {}
        alts = "; ".join(f"{a['strategy']}={a['value']}" for a in f.get("alternatives") or [])
        lines.append(f"- {('p' + str(f['page']) + ' | ') if len(pages) > 1 and f.get('page') else ''}"
                     f"{f.get('label') or '(no label)'} | {f['kind']} {f['tag']}{('/' + f['type']) if f.get('type') else ''}"
                     f" | {rec.get('strategy', '-')}={rec.get('value', '-')} ({rec.get('why', '')}) | {alts or '-'} | "
                     f"{', '.join(f.get('unstable') or []) or '-'}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------------------------------
# Checking the locators an agent wrote
# ---------------------------------------------------------------------------------------------------------------
_LABEL_OF_JS = r"""
(el) => {
  const txt = (n) => (n ? (n.innerText || n.textContent || '') : '').replace(/\s+/g, ' ').trim();
  if (el.labels && el.labels.length && txt(el.labels[0])) return txt(el.labels[0]);
  const al = el.getAttribute('aria-label'); if (al) return al.trim();
  // a button / link is named by its own text (9.5.2: it used to take a heading next to it)
  const tag = el.tagName.toLowerCase(), type = (el.getAttribute('type') || '').toLowerCase();
  if (tag === 'button' || tag === 'a' || el.getAttribute('role') === 'button') { if (txt(el)) return txt(el); }
  if (tag === 'input' && ['submit', 'button', 'reset'].includes(type) && el.value) return el.value.trim();
  let cur = el;
  for (let d = 0; d < 3 && cur; d++) { let s = cur.previousElementSibling;
    while (s) { if (txt(s) && txt(s).length < 60 && !s.querySelector('input,select,textarea,button')) return txt(s); s = s.previousElementSibling; }
    cur = cur.parentElement; }
  return el.getAttribute('placeholder') || txt(el) || el.value || '';
}
"""


def _locate(page, loc: Dict[str, Any]):
    strategy = str(loc.get("strategy") or "css").lower()
    v = str(loc.get("value") or "")
    if strategy == "xpath" or v.startswith(("//", "(//", "xpath=")):
        return page.locator("xpath=" + v[6:] if v.startswith("xpath=") else "xpath=" + v)
    if strategy == "label":
        return page.get_by_label(v, exact=True)
    if strategy == "text":
        return page.get_by_text(v, exact=True)
    if strategy == "placeholder":
        return page.get_by_placeholder(v, exact=True)
    if strategy == "testid":
        return page.get_by_test_id(v)
    if strategy == "role":
        role, _, name = v.partition(":")
        return page.get_by_role(role.strip() or "button", name=name.strip() or None)
    return page.locator(v[4:] if v.startswith("css=") else v)


_POSITIONAL = re.compile(r"nth=\d|:nth-(child|of-type)\(|:(first|last)-child|\)\[\d+\]\s*$|\[position\(\)")


def is_positional(v: str) -> bool:
    """A locator that finds its element by POSITION: nth=, :nth-child, (//input)[3], or an XPath with indexes and no
    text / label / attribute anchor (//form/div[3]/input). A label-anchored //label[...]/following::input[1] is not."""
    v = str(v or "")
    if _POSITIONAL.search(v):
        return True
    return v.lstrip("(").startswith("/") and bool(re.search(r"\[\d+\]", v)) and \
        not re.search(r"normalize-space|text\(\)|@|contains\(", v)


def static_problems(loc: Dict[str, Any], profile: Optional[Dict[str, Any]]) -> List[str]:
    """9.4: what the inspection ALREADY knows breaks a locator - no page load needed: it uses an id / name that changed
    between loads (or looks generated), or a position while the page moves its fields."""
    if not profile or profile.get("renderer") != "chromium":
        return []
    v = str(loc.get("value") or "")
    out: List[str] = []
    sig = profile.get("signals") or {}
    for f in profile.get("fields") or []:
        for attr, val in (f.get("unstable_values") or {}).items():
            if val and len(val) >= 3 and re.search(r"(?<![\w-])" + re.escape(val) + r"(?![\w-])", v):
                out.append(f"uses {attr} '{val}' of '{f.get('label') or '?'}', which "
                           + ("changed when the page was reloaded" if "generated" not in " ".join(f.get("unstable") or [])
                              or (profile.get("loads") or 1) > 1 else "looks generated (changes between loads)"))
                break
        if out:
            break
    if sig.get("shuffled_fields") and is_positional(v):
        out.append("locates by position, but this page moves its fields on every load")
    return out


def verify_locators(url: Any, locators: List[Dict[str, Any]], loads: Any = 1,
                    profile: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Every locator must match exactly ONE visible element on a fresh load of its page, of the right kind (a field
    for fill/select/check, anything for click), with the expected label when one is given.

    9.4: ``url`` may be a list (the pages the process visits). A locator with ``page`` (1-based) is checked on that
    page; one without it passes when ONE of the pages has exactly one match. Each page is loaded ``loads`` times - an
    int, or {page: n} as the inspection's reload agent decided (``profile["verify_loads"]``); what the inspection learned
    about changing ids / positions is also checked statically via ``profile``.
    {checked, ok, loads, results[{id, value, ok, problems[]}], error}"""
    urls = [url] if isinstance(url, str) else [u for u in (url or []) if u]
    safe = []
    for u in urls:
        v = screen_url(u)
        safe.append(v.normalized_url if v.is_safe else None)
    if not any(safe) or not locators:
        return {"checked": 0, "ok": True, "loads": 0, "results": [], "error": None if locators else "no locators",
                "skipped": True}
    locs = locators[:40]
    results = [{"id": str(l.get("id") or i), "value": str(l.get("value") or "")[:200], "strategy": l.get("strategy"),
                "page": l.get("page"), "ok": True, "problems": [], "_page_hits": {}} for i, l in enumerate(locs)]
    for loc, res in zip(locs, results):
        res["problems"].extend(static_problems(loc, profile))

    def page_of(loc) -> Optional[int]:
        try:
            p = int(loc.get("page") or 0)
        except (TypeError, ValueError):
            return None
        return p if 1 <= p <= len(urls) else None

    total_loads = 0
    unreachable: Dict[int, str] = {}
    try:
        from playwright.sync_api import sync_playwright
        from core.wizard.dom_discovery import install_ssrf_guard
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                for pi, purl in enumerate(safe, 1):
                    mine = [(l, r) for l, r in zip(locs, results) if page_of(l) in (None, pi)]
                    if not purl or not mine:
                        continue
                    n_loads = max(1, int((loads.get(pi) or loads.get(str(pi)) or 1) if isinstance(loads, dict)
                                         else loads or 1))
                    for n in range(n_loads):
                        ctx = browser.new_context()
                        page = ctx.new_page()
                        install_ssrf_guard(page)
                        try:
                            page.goto(purl, wait_until="domcontentloaded", timeout=PROBE_TIMEOUT_MS)
                        except Exception as e:
                            unreachable[pi] = f"{type(e).__name__}: {str(e)[:120]}"
                            ctx.close()
                            break
                        total_loads += 1
                        try:
                            page.wait_for_load_state("networkidle", timeout=5000)
                        except Exception:
                            pass
                        page.wait_for_timeout(SETTLE_MS)
                        tag_load = f"page {pi}" + (f" load {n + 1}" if n_loads > 1 else "") if len(urls) > 1 else \
                            (f"load {n + 1}" if n_loads > 1 else "fresh load")
                        for loc, res in mine:
                            hits = res["_page_hits"].setdefault(pi, [])
                            try:
                                m = _locate(page, loc)
                                count = m.count()
                                visible = [i for i in range(min(count, 5)) if m.nth(i).is_visible()]
                            except Exception as e:
                                hits.append(f"{tag_load}: invalid locator ({type(e).__name__}: {str(e)[:120]})")
                                continue
                            if len(visible) != 1:
                                hits.append(f"{tag_load}: matched {len(visible)} visible element(s), needs exactly 1")
                                continue
                            el = m.nth(visible[0])
                            kind = str(loc.get("kind") or loc.get("action") or "").lower()
                            if kind in ("fill", "type", "select", "check"):
                                tag = el.evaluate("e => e.tagName.toLowerCase() + '|' + (e.getAttribute('contenteditable') || '')")
                                if not (tag.startswith(("input", "textarea", "select")) or tag.endswith("true")):
                                    hits.append(f"{tag_load}: matched a <{tag.split('|')[0]}>, not an input field")
                                    continue
                            want = _norm(str(loc.get("expect_label") or ""))
                            if want:
                                got = _norm(el.evaluate(_LABEL_OF_JS))
                                if want not in got and got not in want:
                                    hits.append(f"{tag_load}: matched the element labelled '{got[:40]}', expected '{want[:40]}'")
                        ctx.close()
            finally:
                browser.close()
    except Exception as e:
        return {"checked": 0, "ok": True, "loads": 0, "results": [], "skipped": True,
                "error": f"the locators could not be checked in a browser: {type(e).__name__}: {e}"[:300]}
    for loc, r in zip(locs, results):
        hits = r.pop("_page_hits")
        checked_pages = {p: h for p, h in hits.items() if p not in unreachable}
        if not checked_pages:
            r["unchecked"] = True        # its page can't be opened directly (needs earlier steps) - the run will tell
        elif not any(not h for h in checked_pages.values()):
            # no page had a clean match: report the problems of its own page (or of every page tried)
            for p, h in checked_pages.items():
                r["problems"].extend(h[:2])
        r["ok"] = not r["problems"]
    return {"checked": len(results), "ok": all(r["ok"] for r in results), "loads": total_loads,
            "loads_per_page": loads if isinstance(loads, dict) else None,
            "results": results, "error": None, "unreachable": unreachable,
            "static_checks": bool(profile and profile.get("renderer") == "chromium")}


def ground_steps(steps: List[Dict[str, Any]], profile: Optional[Dict[str, Any]]) -> int:
    """9.0: when the page's ids / positions change between loads, the WATCH selectors (usually ids) are re-anchored on
    the field's visible label - also for Understudy's built-in step driver. Returns how many steps changed."""
    if not profile or profile.get("renderer") != "chromium":
        return 0
    sig = profile.get("signals") or {}
    dynamic = sig.get("dynamic_ids") or sig.get("dynamic_names") or sig.get("shuffled_fields")
    by_label = {_norm(f["label"]): f for f in profile.get("fields") or [] if f.get("label") and f.get("recommended")}
    changed = 0
    for st in steps:
        if st.get("action") not in ("fill", "select", "check", "click"):
            continue
        cands = [st.get("label"), st.get("param_label"), st.get("description")]
        f = next((by_label[_norm(c)] for c in cands if c and _norm(c) in by_label), None)
        if f is None and st.get("label"):
            f = next((v for k, v in by_label.items() if k and (k in _norm(st["label"]) or _norm(st["label"]) in k)), None)
        if f is None:
            continue
        rec = f["recommended"]
        if rec["strategy"] not in ("xpath", "css"):
            continue
        sel = rec["value"]
        old = str(st.get("selector") or "")
        if old == sel or (not dynamic and old):
            continue
        st["selector"] = sel
        st["fallback_selectors"] = [a["value"] for a in f.get("alternatives") or [] if a["strategy"] in ("xpath", "css")][:2]
        st["grounded_by"] = "page probe: " + rec["why"]
        changed += 1
    return changed


def _sop_value(label: str, sop_text: str) -> Optional[str]:
    """'First Name: Ada' / 'First Name = Ada' / 'First Name Ada,' in the SOP -> 'Ada'."""
    if not sop_text or not label:
        return None
    lab = re.escape(label.strip())
    for rx in (rf"(?im)^\s*[-*]?\s*{lab}\s*[:=]\s*(.+?)\s*$", rf"(?i)\b{lab}\s*[:=]?\s+([^,;\n]+?)\s*(?:,|;|\n|\band\b|$)"):
        m = re.search(rx, sop_text)
        if m:
            v = m.group(1).strip().strip("\"'").rstrip(".")
            if 0 < len(v) <= 200:
                return v
    return None


def plan_from_profile(profile: Dict[str, Any], sop_text: str = "") -> Optional[Dict[str, Any]]:
    """9.0: the offline (no-AI) plan for a page whose ids / names / order change: one step per field, keyed and
    labelled by its visible label (not by a random id), located by the label-anchored locator. None when the profile
    has no usable labelled fields."""
    from core.wizard.guardrails import normalize_param_key
    if not profile or profile.get("renderer") != "chromium":
        return None
    fields = [f for f in profile.get("fields") or [] if f["kind"] == "field" and f.get("label") and f.get("recommended")
              and f["recommended"]["strategy"] in ("xpath", "css") and f.get("type") not in ("file", "image", "reset")]
    if not fields:
        return None
    steps: List[Dict[str, Any]] = [{"order": 1, "action": "navigate", "selector": "", "value": "",
                                    "description": "Open the target page"}]
    params: List[Dict[str, Any]] = []
    used = set()
    for f in fields:
        key = normalize_param_key(f["label"]) or f"field_{len(params) + 1}"
        while key in used:
            key += "_2"
        used.add(key)
        t = (f.get("type") or "").lower()
        ptype = {"email": "email", "number": "number", "date": "date", "password": "secret", "checkbox": "boolean"}.get(
            t, "select" if f.get("tag") == "select" else "text")
        val = _sop_value(f["label"], sop_text)
        params.append({"key": key, "label": f["label"][:80], "type": ptype, "required": False,
                       "description": f"Value typed into '{f['label'][:60]}'", "sop_dependent": False,
                       "verify": ptype in ("text", "number", "date", "select", "email"),
                       "value": val if val is not None else "", "source": "sop" if val is not None else "default"})
        steps.append({"order": len(steps) + 1, "action": "select" if ptype == "select" else "check" if ptype == "boolean"
                      else "fill", "selector": f["recommended"]["value"],
                      "fallback_selectors": [a["value"] for a in f.get("alternatives") or [] if a["strategy"] in ("xpath", "css")][:2],
                      "label": f["label"][:80], "param_key": key, "description": f"Enter the {f['label'][:60]}",
                      "grounded_by": "page probe: " + f["recommended"]["why"]})
    return {"steps": steps, "parameters": params}
