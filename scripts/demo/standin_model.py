"""Stand-in model for recording the demo video on a machine that cannot reach Google.

NOT part of the product and never loaded by server.py. scripts/demo/serve.py plugs it in through the same test seam
the test-suite uses (core.wizard.llm.set_test_backend) ONLY when it is started with --standin. Everything it returns
is labelled with UNDERSTUDY_STANDIN_LABEL (default "stand-in model (no internet)") in the UI and telemetry, never
as a Gemini model.

It only "knows" the bundled demo recording (assets/demo_ap_recording.mp4): its answers describe what that video
really shows (Acme Industrial Supplies, 23-09-2026, $450.00 typed into Portal 1 and submitted at 00:12). For any other
recording it fails, like a model that is not reachable - which is exactly when Understudy's learned skills take over.

With a real Gemini key or Vertex AI configured, run scripts/demo/serve.py WITHOUT --standin and the demo uses the
real model instead.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any, Dict, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEMO_VIDEO = os.path.join(ROOT, "assets", "demo_ap_recording.mp4")
TARGET = "http://localhost:8000/portal1"


def _sha(path: Optional[str]) -> Optional[str]:
    if not path or not os.path.exists(path):
        return None
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# The same demo recording in other containers (e.g. a WebM copy for browsers without H.264) can be added here.
_DEMO_SHAS = {_sha(DEMO_VIDEO)} | {_sha(p) for p in os.environ.get("UNDERSTUDY_STANDIN_VIDEOS", "").split(",") if p.strip()}


def _block(user: str, name: str) -> str:
    m = re.search(r'<untrusted_input name="%s">(.*?)</untrusted_input>' % re.escape(name), user, re.S)
    if m:
        return m.group(1)
    return ""


def _json_after(user: str, label: str) -> Any:
    i = user.find(label)
    if i < 0:
        return None
    s = user[i + len(label):].lstrip()
    try:
        return json.JSONDecoder().raw_decode(s)[0]
    except Exception:
        return None


def watch_analyze(media: Optional[str]) -> Dict[str, Any]:
    if _sha(media) not in _DEMO_SHAS:
        raise RuntimeError("UNAVAILABLE: the stand-in model can only watch the bundled demo recording")
    from core.wizard.frameworks import suggest_offline
    options = [f.to_dict() for f in suggest_offline("web_ui", True)][:4]
    only = [x.strip() for x in os.environ.get("UNDERSTUDY_STANDIN_TOOLS", "").split(",") if x.strip()]
    if only:                                      # demo knob: which tools the stand-in suggests (e.g. not Cypress)
        from core.wizard.frameworks import CATALOG
        options = [dict(CATALOG[t].to_dict(), source="catalog") for t in only if t in CATALOG][:4]
    for o in options:
        o["recommended"] = o["id"] == "playwright-python"
    return {
        "process_name": "Capture a vendor invoice in the AP portal",
        "summary": ("The operator opens the Accounts Payable portal, types the vendor name, picks the invoice date and "
                    "enters the total amount, then clicks Submit Invoice. The new invoice appears at the top of the "
                    "Committed AP Ledger as TRX-1007 with status COMMITTED."),
        "app_type": "web_ui",
        "steps": [
            {"order": 1, "action": "navigate", "description": "Open the Accounts Payable portal",
             "video_time": "00:00", "evidence": "Portal 1 'Manual Invoice Intake' form is shown"},
            {"order": 2, "action": "fill", "selector": "#vendor_name", "label": "Vendor Name", "param_key": "vendor_name",
             "description": "Type the vendor name", "video_time": "00:04",
             "evidence": "'Acme Industrial Supplies' is typed into Vendor Name"},
            {"order": 3, "action": "fill", "selector": "#invoice_date", "label": "Invoice Date",
             "param_key": "invoice_date", "description": "Enter the invoice date", "video_time": "00:07",
             "evidence": "Invoice Date becomes 23-09-2026"},
            {"order": 4, "action": "fill", "selector": "#total_amount", "label": "Total Amount ($ USD)",
             "param_key": "total_amount", "description": "Enter the total amount", "video_time": "00:09",
             "evidence": "450.00 is typed into Total Amount"},
            {"order": 5, "action": "click", "selector": "#submit-btn", "label": "Submit Invoice",
             "description": "Click Submit Invoice to commit the record", "video_time": "00:12",
             "evidence": "The form clears and TRX-1007 Acme Industrial Supplies $450.00 appears in the ledger"},
        ],
        "parameters": [
            {"key": "vendor_name", "label": "Vendor Name", "value": "Acme Industrial Supplies", "type": "text",
             "source": "video", "description": "Supplier on the invoice", "sop_dependent": True},
            {"key": "invoice_date", "label": "Invoice Date", "value": "2026-09-23", "type": "date", "source": "video",
             "description": "Date printed on the invoice", "sop_dependent": True},
            {"key": "total_amount", "label": "Total Amount ($ USD)", "value": "450.00", "type": "number",
             "source": "video", "description": "Invoice total in USD", "sop_dependent": True},
        ],
        "verification": {"method": "dom_text", "url": TARGET,
                         "expect_param_keys": ["vendor_name", "total_amount"], "success_text": "COMMITTED"},
        "observed_text": "Enterprise ERP - Accounts Payable Portal. Committed AP Ledger. TRX-1007 Acme Industrial "
                         "Supplies 2026-09-23 $450.00 COMMITTED",
        "framework_options": options,
    }


def _history_applied(history: str, out: Dict[str, Any]) -> list:
    """Like a model given the PREVIOUS SESSIONS CONTEXT: report what it re-used - only what really is in that digest."""
    if not history.strip():
        return []
    sels = [st["selector"] for st in out["steps"] if st.get("selector") and st["selector"] in history]
    keys = [p["key"] for p in out["parameters"] if p["key"] in history]
    applied = []
    if sels:
        applied.append("Re-used the selectors that worked in the previous session: " + ", ".join(sels))
    if keys:
        applied.append("Kept the parameter names of the previous session: " + ", ".join(keys))
    if "COMMITTED" in history:
        applied.append("Verify the same way the previous session proved success: the new ledger row shows COMMITTED")
    return applied


def parameterize(user: str) -> Dict[str, Any]:
    try:
        params = json.loads(_block(user, "parameters"))
    except Exception:
        params = []
    if not isinstance(params, list):
        params = []
    hints = {"vendor_name": "Must be a registered vendor (SOP rule 3.3)",
             "invoice_date": "Must fall inside the authorised window 2026-01-01 .. 2026-12-31 (SOP section 5)",
             "total_amount": "Up to $5,000.00 can be committed directly; above that it must be escalated (SOP 3.1/3.2)"}
    out = []
    for p in params:
        if isinstance(p, dict) and p.get("key"):
            q = dict(p)
            q["description"] = hints.get(q["key"], q.get("description") or "")
            q["sop_dependent"] = q["key"] in hints
            out.append(q)
    return {"parameters": out}


def extract_rules(user: str) -> Dict[str, Any]:
    sop = _block(user, "sop_text").replace("\r\n", "\n")
    rules = []
    m = re.search(r"Approved vendors: ([^\n]+?)\.?\n", sop + "\n")
    if m:
        vals = [v.strip() for v in m.group(1).split(",") if v.strip()]
        rules.append({"field": "vendor_name", "rule_type": "allowed_values", "values": vals, "source_text": m.group(0).strip(),
                      "plain_english": "Vendor Name must be one of: " + ", ".join(vals)})
    m = re.search(r"Never pay the following vendors: ([^\n]+?)\.?\n", sop + "\n")
    if m:
        vals = [v.strip() for v in m.group(1).split(",") if v.strip()]
        rules.append({"field": "vendor_name", "rule_type": "denied_values", "values": vals, "source_text": m.group(0).strip(),
                      "plain_english": "Vendor Name must not be: " + ", ".join(vals)})
    m = re.search(r"(?:less than or equal to|spend limit per invoice is) \$([\d,]+(?:\.\d+)?)", sop)
    if m:
        rules.append({"field": "total_amount", "rule_type": "max", "limit": float(m.group(1).replace(",", "")),
                      "source_text": m.group(0), "plain_english": f"Total Amount may be at most ${m.group(1)}"})
    m = re.search(r"(?:from|between) (\d{4}-\d{2}-\d{2}) (?:to|and) (\d{4}-\d{2}-\d{2})", sop)
    if m:
        rules.append({"field": "invoice_date", "rule_type": "date_range", "start_date": m.group(1),
                      "end_date": m.group(2), "source_text": m.group(0),
                      "plain_english": f"Invoice Date must be between {m.group(1)} and {m.group(2)}"})
    return {"rules": rules, "security_flags": []}


def generate_suite(user: str) -> Dict[str, Any]:
    from core.wizard import templates
    from core.wizard.frameworks import CATALOG
    fw = _json_after(user, "FRAMEWORK SPEC:") or {}
    spec = CATALOG.get(fw.get("id")) if isinstance(fw, dict) else None
    if spec is None or not spec.template:
        raise RuntimeError("UNAVAILABLE: the stand-in model only writes catalog frameworks")
    steps = _json_after(user, "STEP PLAN:") or []
    keys = _json_after(user, "PARAMETER KEYS AND TYPES (values intentionally omitted):") or []
    params = [{"key": k["key"], "type": k.get("type", "text"), "label": k.get("label", ""), "value": ""} for k in keys]
    files, entry = templates.render(spec.template, steps, params, None, spec)
    locators = [{"id": f"step{st.get('order')}", "step": st.get("order"), "kind": st.get("action"), "strategy": "css",
                 "value": st["selector"], "expect_label": st.get("label") or ""}
                for st in steps if isinstance(st, dict) and str(st.get("selector") or "").startswith("#")
                and st["selector"] in "".join(files.values())]
    return {"files": [{"path": p, "content": c} for p, c in files.items()], "entry_file": entry, "locators": locators,
            "notes": "Reads every value from payload.json; waits for the ledger to show the new row."}


def standin(prompt_id: str, system: str, user: str, media: Optional[str]):
    if prompt_id == "WATCH_ANALYZE":
        out = watch_analyze(media)
        out["history_applied"] = _history_applied(_block(user, "history_context"), out)
        return out
    if prompt_id == "BUILD_PARAMETERIZE":
        return parameterize(user)
    if prompt_id == "GOVERN_EXTRACT_RULES":
        return extract_rules(user)
    if prompt_id == "BUILD_GENERATE_SUITE":
        return generate_suite(user)
    if prompt_id == "WATCH_URLS":
        if _sha(media) not in _DEMO_SHAS:
            raise RuntimeError("UNAVAILABLE: the stand-in model can only watch the bundled demo recording")
        # what the demo recording really shows: the portal page without the browser's address bar
        return {"pages": [], "notes": "The browser's address bar is not visible in this recording, so no page "
                                      "address can be read from it.", "security_flags": []}
    if prompt_id == "SESSION_TITLE":
        return {"title": "Vendor Invoice Entry - AP Portal"}
    if prompt_id == "GOVERN_COMPLIANCE_REVIEW":
        return {"warnings": [], "security_flags": []}
    raise RuntimeError(f"UNAVAILABLE: the stand-in model does not answer {prompt_id}")
