"""Learned skills: Understudy remembers every error it hit and what fixed it, so it doesn't repeat the mistake.

Each skill is a folder on the machine running Understudy (``UNDERSTUDY_SKILLS_DIR``, default ``<app>/skills``):

    skills/<framework>--<target host>/SKILL.md      human-readable lessons (name/description front matter)
    skills/<framework>--<target host>/lessons.json  the same lessons, machine-readable
    skills/<framework>--runtime/...                 machine-wide runtime lessons (browser/driver/package) for a tool

Lifecycle of a lesson
    1. A run fails -> ``record_error``: an *unresolved* lesson with the error signature (numbers, paths and hex ids
       normalised away so the same error matches next time).
    2. The run later passes after self-healing -> ``record_success``: every lesson of that run becomes *learned* with
       the fix that worked: runtime settings (e.g. CHROME_BINARY), selector re-mappings, an added wait, healing notes.
    3. Next time the same tool is used (runtime lessons) or the same target is automated (target lessons),
       ``apply_learned`` applies the fixes BEFORE the first run: the runtime settings, the selector mapping (the old
       selector is kept as a fallback) and the wait, and feeds the healing notes to the code generator.

With ``UNDERSTUDY_STORE=firestore`` (Cloud Run) the lessons are also kept in Cloud Firestore (collection
``understudy_skills``), so every backend instance learns from every other one; SKILL.md is still written locally.

Skills never contain payload values or secrets: only selectors, environment settings the self-healer chose, error
signatures (redacted) and notes.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from datetime import datetime
from typing import Any, Dict, List
from urllib.parse import urlsplit

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_LOCK = threading.RLock()
MAX_LESSONS = 40
_SAFE_ENV = {"SELENIUM_BROWSER", "CHROME_BINARY", "EDGE_BINARY", "FIREFOX_BINARY", "PUPPETEER_EXECUTABLE_PATH",
             "__strip_driver_path"}


def skills_dir() -> str:
    return os.environ.get("UNDERSTUDY_SKILLS_DIR") or os.path.join(_BASE, "skills")


def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:60] or "unknown"


def host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "unknown").lower()
    except Exception:
        return "unknown"


def skill_id(framework_id: str, scope: str) -> str:
    return f"{_slug(framework_id)}--{_slug(scope)}"


def signature(category: str, text: str) -> str:
    """Normalised error signature: the same error on another run/machine maps to the same lesson."""
    t = str(text or "")
    try:
        from core.wizard.activity_log import redact
        t = redact(t)
    except Exception:
        pass
    t = re.sub(r"[A-Za-z]:\\(?:[^\\\r\n'\"<>|]*\\)*[^\\\s'\"<>|()]+", "<path>", t)   # windows paths (may contain spaces)
    t = re.sub(r"/[^\s'\"]+/[^\s'\"]+", "<path>", t)                                  # posix paths
    t = re.sub(r"0x[0-9a-f]+", "<hex>", t, flags=re.I)
    t = re.sub(r"\d+(\.\d+)*", "<n>", t)
    t = re.sub(r"\s+", " ", t).strip()
    return f"{category}: {t[:180]}"


def _paths(sid: str):
    d = os.path.join(skills_dir(), sid)
    return d, os.path.join(d, "lessons.json"), os.path.join(d, "SKILL.md")


def _shared():
    from core.wizard import store
    st = store.get_store()
    return st if getattr(st, "kind", "local") != "local" else None


def _load(sid: str) -> Dict[str, Any]:
    shared = _shared()
    if shared is not None:
        try:
            data = shared.get("skills", sid)
            if data:
                return data
        except Exception:
            pass                                  # store unreachable: fall back to the local copy
    _, jp, _ = _paths(sid)
    try:
        with open(jp, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def _render(data: Dict[str, Any]) -> str:
    lessons = data.get("lessons", [])
    learned = [l for l in lessons if l["status"] == "learned"]
    lines = [
        "---",
        f"name: {data['id']}",
        f"description: Lessons Understudy learned from real errors while automating {data['scope_label']} with "
        f"{data['framework_name']}. Read before generating, running or healing automation for it.",
        "---",
        f"# Understudy learned skill: {data['framework_name']} - {data['scope_label']}",
        "",
        f"_Written automatically by Understudy on this machine; updated after every run. {len(learned)} learned, "
        f"{len(lessons) - len(learned)} unresolved. Last update {data.get('updated_at', '')}._",
        "",
        "Understudy applies the *learned* fixes before the first run, so the same error doesn't happen again.",
        "",
    ]
    for i, l in enumerate(lessons, 1):
        lines += [f"## Lesson {i} - {l['category']} ({l['status']}, seen {l['occurrences']}x, fixed {l['successes']}x)",
                  "", f"**Error:** `{l['signature']}`", ""]
        if l.get("example"):
            lines += [f"**Example:** {l['example']}", ""]
        fx = l.get("fix") or {}
        if fx.get("runtime_env"):
            env = ", ".join(f"`{k}={v}`" for k, v in fx["runtime_env"].items() if not k.startswith("__"))
            lines.append(f"- Runtime settings that fixed it: {env or '(driver path cleanup)'}"
                         + (" - stale chromedriver hidden from PATH" if fx["runtime_env"].get("__strip_driver_path") else ""))
        for m in fx.get("selector_map") or []:
            lines.append(f"- Selector `{m['from']}` changed to `{m['to']}`" + (f" ({m['why']})" if m.get("why") else ""))
        if fx.get("add_wait"):
            lines.append("- Wait for the application to save after the final submit click")
        for n in fx.get("notes") or []:
            lines.append(f"- {n}")
        if not fx and l["status"] != "learned":
            lines.append("- Not solved yet. Understudy records it so the next run is diagnosed faster.")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _save(data: Dict[str, Any]) -> None:
    d, jp, mp = _paths(data["id"])
    os.makedirs(d, exist_ok=True)
    data["updated_at"] = _now()
    data["lessons"] = data.get("lessons", [])[-MAX_LESSONS:]
    tmp = jp + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, jp)
    with open(mp, "w", encoding="utf-8") as fh:
        fh.write(_render(data))
    shared = _shared()
    if shared is not None:
        try:
            shared.put("skills", data["id"], data)
        except Exception:
            pass


def _get_or_create(framework_id: str, framework_name: str, scope: str, scope_label: str) -> Dict[str, Any]:
    sid = skill_id(framework_id, scope)
    data = _load(sid)
    if not data:
        data = {"id": sid, "framework_id": framework_id, "framework_name": framework_name, "scope": scope,
                "scope_label": scope_label, "created_at": _now(), "lessons": []}
    return data


def _scope_for(category: str, host: str):
    # Runtime problems belong to the tool on this machine; everything else to the target application.
    return ("runtime", "this machine's runtime") if category == "ENVIRONMENT" else (host, host)


def record_error(framework_id: str, framework_name: str, target_url: str, category: str, error_text: str,
                 run_id: str) -> Dict[str, Any]:
    """A failure happened: create/refresh an unresolved lesson. Returns {skill_id, lesson_id}."""
    scope, label = _scope_for(category, host_of(target_url))
    sig = signature(category, error_text)
    with _LOCK:
        data = _get_or_create(framework_id, framework_name, scope, label)
        lesson = next((l for l in data["lessons"] if l["signature"] == sig), None)
        if lesson is None:
            lesson = {"id": "L" + hashlib.sha1(sig.encode()).hexdigest()[:8], "category": category,
                      "signature": sig, "example": signature("", error_text)[2:][:240], "status": "unresolved",
                      "occurrences": 0, "successes": 0, "fix": {}, "first_seen": _now(), "runs": []}
            data["lessons"].append(lesson)
        lesson["occurrences"] += 1
        lesson["last_seen"] = _now()
        lesson["runs"] = (lesson.get("runs", []) + [run_id])[-10:]
        _save(data)
        return {"skill_id": data["id"], "lesson_id": lesson["id"]}


def record_success(framework_id: str, framework_name: str, target_url: str, errors: List[Dict[str, Any]],
                   runtime_env: Dict[str, str], history: List[Dict[str, Any]], healing_notes: List[str]) -> List[str]:
    """The run passed after self-healing: mark the run's lessons learned, with the fix that worked."""
    changed = []
    selector_map = []
    add_wait = False
    for h in history:
        for c in h.get("changes") or []:
            if c.get("from") and c.get("to") and c["from"] != c["to"] and not str(c["to"]).startswith("wait"):
                selector_map.append({"from": c["from"], "to": c["to"], "why": str(c.get("why") or "")[:120]})
            if c.get("from") is None and str(c.get("to", "")).startswith("wait"):
                add_wait = True
    env = {k: v for k, v in (runtime_env or {}).items() if k in _SAFE_ENV}
    with _LOCK:
        for ref in errors:
            _, jp, _ = _paths(ref["skill_id"])
            data = _load(ref["skill_id"])
            lesson = next((l for l in data.get("lessons", []) if l["id"] == ref["lesson_id"]), None)
            if not lesson:
                continue
            fix: Dict[str, Any] = {}
            if lesson["category"] == "ENVIRONMENT":
                if env:
                    fix["runtime_env"] = env
            else:
                if selector_map:
                    fix["selector_map"] = selector_map[-20:]
                if add_wait or lesson["category"] == "TIMING":
                    fix["add_wait"] = True
                if healing_notes:
                    fix["notes"] = [str(n)[:240] for n in healing_notes[-5:]]
            if fix:
                lesson["fix"] = fix
                lesson["status"] = "learned"
            lesson["successes"] += 1
            lesson["learned_at"] = _now()
            _save(data)
            if data["id"] not in changed:
                changed.append(data["id"])
    return changed


def applicable(framework_id: str, target_url: str) -> List[Dict[str, Any]]:
    out = []
    for scope in ("runtime", host_of(target_url)):
        data = _load(skill_id(framework_id, scope))
        if data:
            out.append(data)
    return out


def apply_learned(framework_id: str, target_url: str, steps: List[Dict[str, Any]], runtime_env: Dict[str, str],
                  healing_notes: List[str]) -> Dict[str, Any]:
    """Applies learned fixes before a run. Returns {runtime_env, steps, healing_notes, applied:[...]}."""
    env = dict(runtime_env or {})
    steps = [dict(s) for s in steps]
    notes = list(healing_notes or [])
    applied: List[Dict[str, Any]] = []
    for data in applicable(framework_id, target_url):
        for l in data.get("lessons", []):
            if l["status"] != "learned":
                continue
            fx = l.get("fix") or {}
            did = []
            renv = fx.get("runtime_env") or {}
            binaries = [v for k, v in renv.items() if k.endswith("_BINARY") or k == "PUPPETEER_EXECUTABLE_PATH"]
            if renv and all(os.path.isfile(b) for b in binaries):      # only if that browser is still installed
                for k, v in renv.items():
                    if env.get(k) != v:
                        env[k] = v
                        did.append(f"{k}={v}" if not k.startswith("__") else "hide stale chromedriver")
            for m in fx.get("selector_map") or []:
                for st in steps:
                    if st.get("selector") == m["from"]:
                        st["fallback_selectors"] = [m["from"]] + [x for x in st.get("fallback_selectors") or []
                                                                  if x not in (m["from"], m["to"])]
                        st["selector"] = m["to"]
                        did.append(f"{m['from']} -> {m['to']}")
            if fx.get("add_wait"):
                clicks = [i for i, st in enumerate(steps) if st.get("action") == "click"]
                if clicks and not (clicks[-1] + 1 < len(steps) and steps[clicks[-1] + 1].get("action") == "wait"):
                    steps.insert(clicks[-1] + 1, {"order": 0, "action": "wait", "value": "3000",
                                                  "description": "Wait for the application to save (learned skill)"})
                    for i, st in enumerate(steps, 1):
                        st["order"] = i
                    did.append("wait after final submit")
            for n in fx.get("notes") or []:
                if n not in notes:
                    notes.append(n)
                    did.append("healing note")
            if did:
                applied.append({"skill_id": data["id"], "lesson_id": l["id"], "category": l["category"],
                                "signature": l["signature"], "applied": did})
    return {"runtime_env": env, "steps": steps, "healing_notes": notes[-10:], "applied": applied}


def list_skills() -> List[Dict[str, Any]]:
    root = skills_dir()
    out = []
    try:
        names = set(n for n in os.listdir(root) if os.path.isdir(os.path.join(root, n)))
    except FileNotFoundError:
        names = set()
    shared = _shared()
    if shared is not None:
        try:
            names |= set(shared.list("skills"))
        except Exception:
            pass
    for n in sorted(names):
        data = _load(n)
        if not data:
            continue
        ls = data.get("lessons", [])
        out.append({"id": data["id"], "framework_id": data["framework_id"], "framework_name": data["framework_name"],
                    "scope": data["scope"], "scope_label": data["scope_label"], "updated_at": data.get("updated_at"),
                    "lessons": len(ls), "learned": len([l for l in ls if l["status"] == "learned"]),
                    "path": os.path.join(root, n, "SKILL.md")})
    return out


def read_skill(sid: str) -> Dict[str, Any]:
    if not re.fullmatch(r"[a-z0-9-]+--[a-z0-9-]+", sid or ""):
        raise KeyError(sid)
    data = _load(sid)
    if not data:
        raise KeyError(sid)
    _, _, mp = _paths(sid)
    try:
        with open(mp, encoding="utf-8") as fh:
            md = fh.read()
    except FileNotFoundError:                     # learned on another instance: render from the shared lessons
        md = _render(data)
    return {"skill": data, "markdown": md}


def delete_skill(sid: str) -> None:
    import shutil
    if not re.fullmatch(r"[a-z0-9-]+--[a-z0-9-]+", sid or ""):
        raise KeyError(sid)
    d, _, _ = _paths(sid)
    shared = _shared()
    in_store = False
    if shared is not None:
        try:
            in_store = shared.get("skills", sid) is not None
            shared.delete("skills", sid)
        except Exception:
            pass
    if not os.path.isdir(d):
        if in_store:
            return
        raise KeyError(sid)
    shutil.rmtree(d)


def markdown_for_export(framework_id: str, target_url: str) -> Dict[str, str]:
    out = {}
    for data in applicable(framework_id, target_url):
        out[f"skills/{data['id']}/SKILL.md"] = _render(data)
    return out
