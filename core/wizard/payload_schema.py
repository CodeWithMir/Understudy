"""Payload schema derived from the target page itself (any website, no hard-coded field knowledge).

Each parameter that a step types into / picks on the page is linked to the page element the step is grounded on
(core/wizard/watch.py::ground_analysis). The element's own HTML declares what the page will accept - ``required``,
``<option>`` lists, ``min``/``max``/``step``, ``minlength``/``maxlength``, ``pattern``, the input type - and that
becomes the parameter's ``constraints``. The payload table (Step 2), the API and the exported ``payload.schema.json``
use them, so a value the page would reject is caught before the run instead of failing in the browser.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any, Dict, List, Optional

_TYPE_BY_FORMAT = {"number": "number", "date": "date", "email": "email", "datetime-local": "text", "month": "text",
                   "time": "text", "url": "text", "tel": "text"}
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_NESTED_QUANT = re.compile(r"\([^)]*[+*][^)]*\)[+*{]")        # (a+)+ style patterns: never evaluated (ReDoS)


def type_for_element(el: Dict[str, Any], current: str) -> str:
    """The parameter type that matches the control (secret always stays secret)."""
    if current == "secret":
        return "secret"
    if el.get("type") == "password":
        return "secret"
    if el.get("kind") == "select" and el.get("tag") == "select":
        return "select"
    if el.get("kind") == "check":
        return "boolean"
    return _TYPE_BY_FORMAT.get(el.get("type") or "", current if current in ("text", "email", "number", "date") else "text")


def _num(v: Any) -> Optional[float]:
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def check_value(p: Dict[str, Any]) -> List[str]:
    """Problems with one parameter's value against its constraints (empty list = OK)."""
    c = p.get("constraints") or {}
    if not c or p.get("type") == "secret":
        return []
    v = p.get("value")
    s = "" if v is None else str(v)
    out: List[str] = []
    if not s.strip():
        return ["the page marks this field as required"] if c.get("required") and p.get("type") != "boolean" else []
    if c.get("options") and p.get("type") == "select":
        if s not in c["options"] and s.lower() not in [o.lower() for o in c["options"]]:
            out.append("must be one of the page's options: " + ", ".join(map(str, c["options"][:10]))
                       + (" ..." if len(c["options"]) > 10 else ""))
    if p.get("type") == "number" or c.get("format") == "number":
        n = _num(s)
        if n is None:
            out.append("must be a number")
        else:
            lo, hi = _num(c.get("min")), _num(c.get("max"))
            if lo is not None and n < lo:
                out.append(f"must be at least {c['min']}")
            if hi is not None and n > hi:
                out.append(f"must be at most {c['max']}")
    if c.get("format") == "date" and c.get("min") and c.get("max"):
        try:
            d = date.fromisoformat(s[:10])
            if d < date.fromisoformat(str(c["min"])[:10]) or d > date.fromisoformat(str(c["max"])[:10]):
                out.append(f"must be between {c['min']} and {c['max']}")
        except ValueError:
            pass
    if (p.get("type") == "email" or c.get("format") == "email") and not _EMAIL.match(s):
        out.append("must be an e-mail address")
    ml, mx = _num(c.get("minlength")), _num(c.get("maxlength"))
    if ml is not None and len(s) < ml:
        out.append(f"must be at least {int(ml)} characters")
    if mx is not None and len(s) > mx:
        out.append(f"must be at most {int(mx)} characters")
    pat = c.get("pattern")
    if pat and len(pat) <= 200 and len(s) <= 1000 and not _NESTED_QUANT.search(pat):
        try:
            if not re.fullmatch(pat, s):
                out.append("does not match the format the page expects")
        except re.error:
            pass
    return out


def problems(params: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [{"key": p.get("key"), "message": f"{p.get('label') or p.get('key')}: " + "; ".join(errs),
             "pattern": "PAGE_CONSTRAINT"} for p in params for errs in [check_value(p)] if errs]


def coerce_option(p: Dict[str, Any]) -> Optional[str]:
    """A select value that differs only in case/spacing from an option is snapped to the option. Returns a note."""
    c = p.get("constraints") or {}
    opts = c.get("options") or []
    if p.get("type") != "select" or not opts:
        return None
    v = str(p.get("value") or "")
    if v in opts:
        return None
    norm = {re.sub(r"\s+", " ", o).strip().lower(): o for o in opts}
    hit = norm.get(re.sub(r"\s+", " ", v).strip().lower())
    if hit:
        p["value"] = hit
        return f"{p.get('label') or p['key']}: '{v}' -> '{hit}' (the page's option)"
    return None


def schema_json(params: List[Dict[str, Any]]) -> Dict[str, Any]:
    """payload.schema.json for the exported ZIP (JSON-Schema draft 2020-12)."""
    props: Dict[str, Any] = {}
    required = []
    for p in params:
        c = p.get("constraints") or {}
        t = {"number": "number", "boolean": "boolean"}.get(p.get("type"), "string")
        prop: Dict[str, Any] = {"type": t, "title": p.get("label") or p["key"], "x-understudy-type": p.get("type")}
        if p.get("description"):
            prop["description"] = p["description"]
        if c.get("options"):
            prop["enum"] = c["options"]
        if p.get("type") == "date":
            prop["format"] = "date"
        if p.get("type") == "email":
            prop["format"] = "email"
        for k, js in (("min", "minimum"), ("max", "maximum")):
            if t == "number" and _num(c.get(k)) is not None:
                prop[js] = _num(c[k])
        for k, js in (("minlength", "minLength"), ("maxlength", "maxLength")):
            if t == "string" and _num(c.get(k)) is not None:
                prop[js] = int(_num(c[k]))
        if c.get("pattern") and t == "string":
            prop["pattern"] = c["pattern"]
        if p.get("type") == "secret":
            prop["description"] = (prop.get("description", "") + " Secret: set UNDERSTUDY_SECRET_"
                                   + p["key"].upper() + " in .env, never in payload.json.").strip()
        else:
            if p.get("required", True) or c.get("required"):
                required.append(p["key"])
        props[p["key"]] = prop
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object", "properties": props,
            "required": required, "additionalProperties": True}
