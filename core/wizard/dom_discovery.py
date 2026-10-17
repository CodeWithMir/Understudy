"""Safe target-page fetching and form-field inventory.

Used to:
  * ground the WATCH agent (the AI must prefer selectors that really exist),
  * provide the offline WATCH fallback (without an AI key the video cannot be interpreted,
    so the step plan is inferred from the page's own form),
  * heal drifted selectors during VERIFY retries,
  * read the page state for VERIFY (dom_text method).

All fetches go through ``guardrails.screen_url`` on every redirect hop (SSRF protection),
use a size cap and a timeout, and never send cookies or credentials.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, asdict
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin

from core.wizard.guardrails import screen_url, screen_text

MAX_PAGE_BYTES = 3 * 1024 * 1024
FETCH_TIMEOUT_S = float(os.environ.get("UNDERSTUDY_FETCH_TIMEOUT_S", "10"))


class UnsafeFetch(Exception):
    pass


def fetch_html(url: str, max_redirects: int = 3) -> Tuple[str, str]:
    """Returns (final_url, html). Raises UnsafeFetch / requests exceptions."""
    import requests
    current = url
    for _ in range(max_redirects + 1):
        verdict = screen_url(current)
        if not verdict.is_safe:
            raise UnsafeFetch(verdict.friendly_message)
        resp = requests.get(verdict.normalized_url, timeout=FETCH_TIMEOUT_S, allow_redirects=False, stream=True,
                            headers={"User-Agent": "Understudy/6.0 (+automation discovery)"})
        if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("Location"):
            current = urljoin(verdict.normalized_url, resp.headers["Location"])
            resp.close()
            continue
        chunks, total = [], 0
        for chunk in resp.iter_content(65536):
            total += len(chunk)
            if total > MAX_PAGE_BYTES:
                resp.close()
                raise UnsafeFetch("Target page is larger than 3 MB")
            chunks.append(chunk)
        enc = resp.encoding or "utf-8"
        return verdict.normalized_url, b"".join(chunks).decode(enc, errors="replace")
    raise UnsafeFetch("Too many redirects")


@dataclass
class DomElement:
    tag: str
    type: str = ""
    id: str = ""
    name: str = ""
    testid: str = ""
    label: str = ""
    placeholder: str = ""
    aria_label: str = ""
    text: str = ""
    value: str = ""
    required: bool = False
    options: List[str] = field(default_factory=list)
    option_values: List[str] = field(default_factory=list)
    role: str = ""
    href: str = ""
    form: int = -1                      # index into PageInventory.forms (-1 = not inside a <form>)
    min: str = ""
    max: str = ""
    minlength: str = ""
    maxlength: str = ""
    pattern: str = ""
    step: str = ""
    multiple: bool = False
    editable: bool = False              # contenteditable / role=textbox widgets

    @property
    def kind(self) -> str:
        if self.tag == "a" or self.role == "link":
            return "link"
        if self.tag == "button" or self.type in ("submit", "button") or self.role in ("button", "menuitem", "tab"):
            return "button"
        if self.tag == "select" or self.role in ("combobox", "listbox"):
            return "select"
        if self.type in ("checkbox", "radio") or self.role in ("checkbox", "switch", "radio"):
            return "check"
        return "input"

    def _attr(self, attr: str, value: str) -> str:
        return f'[{attr}="{value}"]' if '"' not in value else f"[{attr}='{value}']"

    def selectors(self) -> List[str]:
        out = []
        grouped = self.type == "radio" or (self.type == "checkbox" and self.value and self.value != "on")
        if self.id:
            out.append(f"#{self.id}" if re.match(r"^[A-Za-z][\w-]*$", self.id) else self._attr("id", self.id))
        if self.testid:
            out.append(f'[data-testid="{self.testid}"]')
        if self.name:
            sel = f'{self.tag}[name="{self.name}"]'
            if grouped and self.value:          # radios share a name: only name+value identifies one of them
                sel += self._attr("value", self.value)
            out.append(sel)
        if self.aria_label:
            out.append(self._attr("aria-label", self.aria_label))
        if self.placeholder and self.kind == "input":
            out.append(self._attr("placeholder", self.placeholder))
        if self.kind == "link" and self.href and len(self.href) <= 120 and not self.href.startswith(("javascript:", "#")):
            out.append(f"a{self._attr('href', self.href)}")
        return out

    def best_selector(self) -> str:
        s = self.selectors()
        if s:
            return s[0]
        txt = (self.text or "")[:40].replace('"', "'")
        if self.kind == "button" and txt:
            return f'button:has-text("{txt}")' if self.tag == "button" else f'[role="{self.role or "button"}"]:has-text("{txt}")'
        if self.kind == "link" and txt:
            return f'a:has-text("{txt}")'
        if self.role:
            return f'[role="{self.role}"]'
        return self.tag

    def descriptor(self) -> str:
        return " ".join(x for x in (self.label, self.aria_label, self.placeholder, self.name, self.id, self.testid, self.text) if x)

    def constraints(self) -> Dict[str, Any]:
        """Payload-schema constraints the page itself declares for this control."""
        c: Dict[str, Any] = {}
        if self.required:
            c["required"] = True
        if self.kind == "select" and self.options:
            opts = [o for o in self.options if not re.match(r"(?i)^\s*(--|select|choose|please select)", o)]
            if opts:
                c["options"] = opts[:50]
        for k in ("min", "max", "minlength", "maxlength", "step"):
            v = getattr(self, k)
            if v and re.match(r"^-?[0-9][0-9.:-]{0,24}$", v):
                c[k] = v
        if self.pattern and len(self.pattern) <= 200:
            c["pattern"] = self.pattern
        if self.type in ("email", "url", "tel", "date", "datetime-local", "time", "month", "number"):
            c["format"] = self.type
        if self.multiple:
            c["multiple"] = True
        return c

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["kind"] = self.kind
        d["selector"] = self.best_selector()
        d["selectors"] = self.selectors()
        d["constraints"] = self.constraints()
        return d


_ROLE_WIDGETS = {"button", "link", "textbox", "searchbox", "combobox", "listbox", "checkbox", "switch", "radio",
                 "menuitem", "tab"}


class _FormParser(HTMLParser):
    """Collects every interactive element a person can use: native form controls, buttons, links, ARIA widgets and
    contenteditable editors - with labels, the form they belong to and the constraints the page declares."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.elements: List[DomElement] = []
        self.labels_for: Dict[str, str] = {}
        self.forms: List[Dict[str, Any]] = []
        self._form = -1
        self._label_stack: List[Dict[str, Any]] = []
        self._texted: List[DomElement] = []        # open button/link/widget elements collecting their visible text
        self._select: Optional[DomElement] = None
        self._option_open = False
        self._skip = 0
        self.text_parts: List[str] = []
        self.title = ""
        self._in_title = False
        self.scripts: List[str] = []
        self._in_script = False

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        if tag in ("script", "style", "noscript", "template"):
            self._skip += 1
            self._in_script = tag == "script"
            if self._in_script:
                self.scripts.append("")
            return
        if self._skip:
            return
        if tag == "title":
            self._in_title = True
        if tag == "form":
            self.forms.append({"index": len(self.forms), "id": a.get("id", ""), "name": a.get("name", ""),
                               "action": a.get("action", "")[:300], "method": (a.get("method") or "get").lower()[:10]})
            self._form = len(self.forms) - 1
        if tag == "label":
            self._label_stack.append({"for": a.get("for", ""), "text": "", "element": None})
        role = a.get("role", "").lower().strip()
        editable = a.get("contenteditable", "").lower() in ("", "true", "plaintext-only") and "contenteditable" in a
        native = tag in ("input", "textarea", "select", "button") or (tag == "a" and "href" in a)
        widget = role in _ROLE_WIDGETS or editable
        if not (native or widget):
            if tag == "option":
                self._option_open = True
                if self._select is not None:
                    self._select.option_values.append(a.get("value", ""))
            return
        if tag == "input" and a.get("type", "text").lower() == "hidden":
            return
        if tag == "input":
            t = a.get("type", "text").lower()
        elif tag == "button":
            t = a.get("type", "submit").lower()
        else:
            t = ""
        if "aria-hidden" in a and a.get("aria-hidden") == "true" and not native:
            return
        el = DomElement(tag=tag, type=t, id=a.get("id", ""), name=a.get("name", ""),
                        testid=a.get("data-testid", "") or a.get("data-test", "") or a.get("data-cy", "") or a.get("data-qa", ""),
                        placeholder=a.get("placeholder", "") or a.get("aria-placeholder", ""),
                        aria_label=a.get("aria-label", ""), value=a.get("value", "") if t != "password" else "",
                        required="required" in a or a.get("aria-required") == "true", role=role,
                        href=a.get("href", "") if tag == "a" else "", form=self._form,
                        min=a.get("min", ""), max=a.get("max", ""), minlength=a.get("minlength", ""),
                        maxlength=a.get("maxlength", ""), pattern=a.get("pattern", ""), step=a.get("step", ""),
                        multiple="multiple" in a, editable=editable or role in ("textbox", "searchbox"))
        if tag == "input" and t in ("submit", "button", "reset"):
            el.text = a.get("value", "")
        self.elements.append(el)
        if self._label_stack and self._label_stack[-1]["element"] is None:
            self._label_stack[-1]["element"] = el
        if tag in ("button", "a") or (widget and tag not in ("input", "textarea", "select")):
            self._texted.append(el)
        if tag == "select":
            self._select = el

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "template"):
            self._skip = max(0, self._skip - 1)
            self._in_script = False
            return
        if self._skip:
            return
        if tag == "title":
            self._in_title = False
        if tag == "form":
            self._form = -1
        if tag == "label" and self._label_stack:
            lab = self._label_stack.pop()
            text = " ".join(lab["text"].split())
            if lab["for"]:
                self.labels_for[lab["for"]] = text
            elif lab["element"] is not None and not lab["element"].label:
                lab["element"].label = text
        if self._texted and (tag in ("button", "a") or tag == self._texted[-1].tag):
            self._texted.pop()
        if tag == "select":
            self._select = None
        if tag == "option":
            self._option_open = False

    def handle_data(self, data):
        if self._skip:
            if self._in_script and self.scripts:
                self.scripts[-1] += data
            return
        if self._in_title:
            self.title += data
        if self._label_stack:
            self._label_stack[-1]["text"] += data
        if self._texted:
            el = self._texted[-1]
            el.text = " ".join((el.text + " " + data).split())[:120]
        if self._select is not None and self._option_open and data.strip():
            self._select.options.append(data.strip())
        if data.strip():
            self.text_parts.append(data.strip())


@dataclass
class PageInventory:
    url: str
    title: str = ""
    elements: List[DomElement] = field(default_factory=list)
    visible_text: str = ""
    api_calls: List[Dict[str, Any]] = field(default_factory=list)
    fetched: bool = False
    error: Optional[str] = None
    renderer: str = "http"
    forms: List[Dict[str, Any]] = field(default_factory=list)

    def fields(self) -> List[DomElement]:
        return [e for e in self.elements if e.kind not in ("button", "link")]

    def buttons(self) -> List[DomElement]:
        return [e for e in self.elements if e.kind == "button"]

    def links(self) -> List[DomElement]:
        return [e for e in self.elements if e.kind == "link"]

    def find(self, selector: str) -> Optional[DomElement]:
        return next((e for e in self.elements if selector and (selector in e.selectors() or selector == e.best_selector())),
                    None)

    def to_dict(self, compact: bool = False) -> Dict[str, Any]:
        d = {"url": self.url, "title": self.title, "fetched": self.fetched, "error": self.error, "renderer": self.renderer,
             "elements": [e.to_dict() for e in self.elements], "api_calls": self.api_calls, "forms": self.forms}
        if not compact:
            d["visible_text"] = self.visible_text[:5000]
        return d

    def selector_exists(self, selector: str) -> bool:
        return self.find(selector) is not None


_FETCH_CALL = re.compile(
    r"fetch\(\s*[\"'`](?P<url>(?:https?://[^\"'`\s]+)?/[^\"'`]*)[\"'`]\s*,\s*\{(?P<body>.{0,800}?)\}\s*\)", re.S)
_AXIOS_CALL = re.compile(r"axios\.(?P<method>post|put|patch)\(\s*[\"'`](?P<url>(?:https?://[^\"'`\s]+)?/[^\"'`]*)[\"'`]"
                         r"\s*,\s*(?P<body>\{.{0,600}?\})", re.S | re.I)
_JQ_CALL = re.compile(r"\$\.(?P<method>post)\(\s*[\"'`](?P<url>(?:https?://[^\"'`\s]+)?/[^\"'`]*)[\"'`]\s*,\s*"
                      r"(?P<body>\{.{0,600}?\})", re.S | re.I)


def _same_origin_path(url: str, page_url: str) -> Optional[str]:
    from urllib.parse import urlsplit
    if url.startswith("/"):
        return url
    try:
        u, p = urlsplit(url), urlsplit(page_url)
    except ValueError:
        return None
    if page_url and u.hostname and p.hostname and u.hostname.lower() == p.hostname.lower():
        return (u.path or "/") + (f"?{u.query}" if u.query else "")
    return None                                   # other hosts are not offered as an API plan


def _header_env(body: str) -> Dict[str, str]:
    headers = {}
    for hm in re.finditer(r"[\"']([A-Za-z0-9-]{2,40})[\"']\s*:\s*[\"'][^\"']*[\"']", body):
        name = hm.group(1)
        if name.lower() not in ("content-type", "accept") and name.lower().startswith(("x-", "authorization")):
            headers[name] = "UNDERSTUDY_SECRET_" + re.sub(r"[^A-Z0-9]", "_", name.upper())
    return headers


def _parse_api_calls(scripts: List[str], page_url: str = "") -> List[Dict[str, Any]]:
    """Write calls (POST/PUT/PATCH) the page's own scripts make to its own host: fetch(), axios, jQuery."""
    calls: List[Dict[str, Any]] = []
    seen = set()
    for s in scripts:
        for m in _FETCH_CALL.finditer(s):
            body = m.group("body")
            method = re.search(r"method\s*:\s*[\"'](\w+)[\"']", body)
            if not method or method.group(1).upper() not in ("POST", "PUT", "PATCH"):
                continue
            path = _same_origin_path(m.group("url"), page_url)
            if not path:
                continue
            keys = re.findall(r"JSON\.stringify\(\s*\{([^}]*)\}", s[m.start(): m.end() + 600])
            body_keys = re.findall(r"([a-zA-Z_][a-zA-Z0-9_]*)\s*:", keys[0]) if keys else []
            calls.append({"method": method.group(1).upper(), "path": path, "header_env": _header_env(body),
                          "body_keys": body_keys, "via": "fetch"})
        for rx, via in ((_AXIOS_CALL, "axios"), (_JQ_CALL, "jquery")):
            for m in rx.finditer(s):
                path = _same_origin_path(m.group("url"), page_url)
                if not path:
                    continue
                body_keys = re.findall(r"([a-zA-Z_][a-zA-Z0-9_]*)\s*:", m.group("body"))
                calls.append({"method": m.group("method").upper(), "path": path,
                              "header_env": _header_env(s[m.end(): m.end() + 400]), "body_keys": body_keys, "via": via})
    out = []
    for c in calls:
        k = (c["method"], c["path"])
        if k not in seen:
            seen.add(k)
            out.append(c)
    return out


def parse_html(url: str, html: str) -> PageInventory:
    p = _FormParser()
    try:
        p.feed(html)
    except Exception:
        pass
    for el in p.elements:
        if el.id and el.id in p.labels_for and not el.label:
            el.label = p.labels_for[el.id]
    text = " ".join(p.text_parts)
    return PageInventory(url=url, title=" ".join(p.title.split()), elements=_dedupe(p.elements), visible_text=text,
                         api_calls=_parse_api_calls(p.scripts, url), fetched=True, forms=p.forms)


def _dedupe(elements: List[DomElement]) -> List[DomElement]:
    """Repeated navigation (header + footer menus) adds the same link many times; keep the first one."""
    out, seen = [], set()
    for e in elements:
        key = (e.kind, e.best_selector(), e.text) if e.kind == "link" else None
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        out.append(e)
    return out


_SPA_HINT = re.compile(r'<(div|main|section)[^>]+id=["\'](root|app|__next|__nuxt|svelte|main-app)["\'][^>]*>\s*</\1>|'
                       r"<app-root|ng-version=|data-reactroot|window\.__NUXT__|__NEXT_DATA__", re.I)


def _render_mode() -> str:
    m = (os.environ.get("UNDERSTUDY_DISCOVERY_RENDER") or "auto").lower()
    return m if m in ("auto", "always", "never") else "auto"


def _looks_script_built(html: str, inv: "PageInventory") -> bool:
    """A page whose form is built by JavaScript (React/Angular/Vue/Next...) shows little or nothing without a browser."""
    if not inv.fields():
        return True
    return bool(_SPA_HINT.search(html or "")) and len(inv.fields()) < 3


def discover(url: str, render_if_empty: bool = True) -> PageInventory:
    """Fetch + parse; renders the page in headless Chromium when the form is built by JavaScript (single-page apps),
    when the static HTML has no fields, or always with UNDERSTUDY_DISCOVERY_RENDER=always. The richer of the two
    inventories wins."""
    html = ""
    try:
        final_url, html = fetch_html(url)
        inv = parse_html(final_url, html)
    except Exception as e:
        inv = PageInventory(url=url, error=f"{type(e).__name__}: {e}")
    blocked = bool(inv.error and inv.error.startswith("UnsafeFetch"))
    mode = _render_mode()
    want_render = render_if_empty and not blocked and mode != "never" and (
        mode == "always" or _looks_script_built(html, inv))
    if want_render:
        rendered = render_with_browser(url)
        if rendered is not None and not rendered.error and (
                len(rendered.fields()) > len(inv.fields()) or
                (len(rendered.fields()) == len(inv.fields()) and len(rendered.elements) > len(inv.elements))):
            if not rendered.api_calls:
                rendered.api_calls = inv.api_calls
            inv = rendered
    # Screen page text before it is ever placed in an AI context window (indirect injection).
    if inv.visible_text:
        tv = screen_text(inv.visible_text[:20000], "PAGE_CONTENT", use_semantic=False)
        if not tv.is_safe:
            inv.visible_text = "[page text withheld by Model Armor: " + str(tv.pattern) + "]"
    return inv


def render_with_browser(url: str, timeout_ms: int = 15000) -> Optional[PageInventory]:
    verdict = screen_url(url)
    if not verdict.is_safe:
        return None
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return None
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            install_ssrf_guard(page)
            page.goto(verdict.normalized_url, wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(800)
            html = page.content()
            browser.close()
        inv = parse_html(verdict.normalized_url, html)
        inv.renderer = "chromium"
        return inv
    except Exception as e:
        return PageInventory(url=url, error=f"render failed: {e}", renderer="chromium")


_HOST_CACHE: Dict[str, bool] = {}


def install_ssrf_guard(page) -> None:
    """Aborts in-page requests to blocked hosts (metadata, private ranges not allow-listed)."""
    from urllib.parse import urlsplit

    def _handler(route):
        try:
            host = urlsplit(route.request.url).hostname or ""
            if route.request.url.startswith(("data:", "blob:")):
                return route.continue_()
            from core.wizard import egress
            if egress.is_blocked_now(host):          # never cached: an admin change applies immediately
                return route.abort()
            if host not in _HOST_CACHE:
                _HOST_CACHE[host] = screen_url(route.request.url).is_safe
            return route.continue_() if _HOST_CACHE[host] else route.abort()
        except Exception:
            return route.abort()

    page.route("**/*", _handler)


def _rank(inv: PageInventory) -> List[DomElement]:
    """Most useful first: controls inside forms, other inputs, buttons, then links (navigation menus last)."""
    def score(e: DomElement) -> int:
        base = {"input": 0, "select": 0, "check": 0, "button": 1, "link": 3}.get(e.kind, 2)
        return base - (1 if e.form >= 0 else 0) + (1 if e.kind == "button" and e.form < 0 else 0)
    return sorted(inv.elements, key=score)


def inventory_for_prompt(inv: PageInventory, limit: int = 120, max_chars: int = 15000, max_links: int = 30) -> str:
    """The page inventory the AI grounds its steps on: element kind, ready-made selectors, label/text, form and the
    constraints the page declares - compact, ranked, and bounded in size."""
    items = []
    links = 0
    for e in _rank(inv):
        if e.kind == "link":
            links += 1
            if links > max_links:
                continue
        d = {k: v for k, v in e.to_dict().items()
             if v not in ("", [], {}, False, None, -1) and k not in ("option_values", "min", "max", "minlength", "maxlength",
                                                                    "pattern", "step", "multiple", "required")}
        d.pop("selector", None)
        if "options" in d:
            d["options"] = d["options"][:25]
        items.append(d)
        if len(items) >= limit:
            break
    forms = [f for f in inv.forms][:10]
    while True:
        out = json.dumps({"url": inv.url, "title": inv.title, "renderer": inv.renderer, "forms": forms,
                          "elements": items, "api_calls": inv.api_calls[:5],
                          "note": None if len(items) == len(inv.elements) else
                          f"{len(inv.elements) - len(items)} lower-priority element(s) (mostly navigation links) omitted"},
                         separators=(",", ":"))
        if len(out) <= max_chars or len(items) <= 10:
            return out
        items = items[: int(len(items) * 0.8)]
