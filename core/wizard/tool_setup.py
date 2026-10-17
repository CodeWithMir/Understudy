"""AI tool setup (7.2, 8.0): how a tool - or a new version of one - gets onto the admin's approved list.

1. A user requests a tool or version that is neither approved nor blocked (``admin_policy.request_tool`` /
   ``request_version``), or the admin starts one in Mission Control ("Add a tool with AI", ``admin_add``).
2. ``prepare_plan``: the ``TOOL_SETUP_PLAN`` agent (offline: the catalog + a deterministic reading of the admin's
   instructions) proposes the setup. Understudy adds the parts that must not come from a model: the exact install
   commands and the manifest files from ``dependency_resolver``, a diff of the change to the admin tool list, whether
   code comes from an offline template or the AI, and which runtimes are missing on the server.
3. The admin reads it and may give instructions ("download from https://pypi.corp/simple", "use /opt/wheels",
   "only 4.x") - every instruction is screened by Model Armor - and the plan is prepared again with them.
4. ``apply_plan``: the admin approves (choosing the allowed versions and the default). Understudy installs the tool in
   the isolated sandbox, checks it, and only then adds it to the approved list with that version policy (a version
   request adds the version). A failed install is shown with its log; the admin can revise or add it unverified.

Nothing is installed before the admin approves, and the AI never runs commands: its spec passes the same validation as
every tool (``frameworks.validate_spec``), and package sources it proposes are used only when the admin typed them.

8.2: nothing is pre-built. The plan is normally prepared from the stack the admin selected in the AI install assistant
(``tool_stack``); after the install the AI writes the smoke test, the setup pauses in ``smoke_review`` until the admin
approves the code (or tells the AI what to change), and only then does it run. There are no smoke-test templates.
"""
from __future__ import annotations

import copy
import difflib
import json
import os
import re
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from core.wizard import activity_log, admin_policy as ap, dependency_resolver, tool_versions
from core.wizard.frameworks import (CATALOG, RUNNER_ALLOWLIST, FrameworkSpec, FrameworkValidationError, match_catalog,
                                    validate_spec)
from core.wizard.guardrails import screen_text
from core.wizard.sandbox import smoke

MAX_INSTRUCTIONS = 20
_URL_RX = re.compile(r"https?://[^\s'\"<>,;)]+")
_DIR_RX = re.compile(r"(?<![\w:/.])((?:/[A-Za-z0-9._-]+)+/?|[A-Za-z]:\\[A-Za-z0-9 ._\\-]+)")
_VER_RX = re.compile(r"\b(\d+\.\d+(?:\.\d+)?(?:[.-]?[A-Za-z0-9]+)?)\b")
_JOBS: Dict[str, threading.Thread] = {}


class SetupError(ValueError):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def _now() -> str:
    return datetime.utcnow().isoformat()


def _audit(title: str, detail: str = "", data: Optional[Dict[str, Any]] = None, level: str = "info") -> None:
    activity_log.record("ADMIN", title, detail, data, source="ADMIN", level=level)


# ---------------------------------------------------------------------------------------------------------------
# Inputs for the plan
# ---------------------------------------------------------------------------------------------------------------
def server_toolchains() -> Dict[str, Any]:
    out = {}
    for eco in ("pip", "npm", "maven", "gradle", "gem", "go", "nuget"):
        missing = dependency_resolver.check_toolchain(eco)
        out[eco] = "available" if not missing else "missing " + ", ".join(missing)
    return out


def _known_tool(r: Dict[str, Any]) -> Optional[FrameworkSpec]:
    if r["tool_id"] in CATALOG:
        return FrameworkSpec.from_dict(CATALOG[r["tool_id"]].to_dict())
    return match_catalog(r.get("tool_name") or "")


def _registry(spec: Optional[FrameworkSpec]) -> Dict[str, Any]:
    if spec is None or not spec.packages or spec.ecosystem == "none":
        return {"source": "none", "versions": [], "latest": None, "error": "no package yet"}
    info = tool_versions.available_versions(spec, limit=25)
    return {k: info.get(k) for k in ("package", "source", "versions", "latest", "installed", "error")}


def _blocked_versions(spec: Optional[FrameworkSpec], name: str) -> List[str]:
    out = []
    keys = ap.tool_keys(spec, name)
    for e in ap.blocked_tools():
        if e.get("versions") and (e["key"] in keys or (spec is not None and spec.id.split("-")[0] == e["key"])):
            out += e["versions"]
    return out


def _instruction_text(r: Dict[str, Any]) -> str:
    return "\n".join(i["text"] for i in r.get("instructions") or [])


def parse_instructions(text: str) -> Dict[str, Any]:
    """Deterministic reading of the admin's instructions (offline path, and the safety net for the AI's sources)."""
    urls = [u.rstrip("./") + ("/" if u.endswith("/") else "") for u in _URL_RX.findall(text or "")]
    no_urls = _URL_RX.sub(" ", text or "")
    dirs = [d for d in _DIR_RX.findall(no_urls) if len(d) > 1]
    # 8.0: "use the local python3.10" / "use /opt/python3.10/bin/python3" -> the venv's interpreter
    interp = ""
    py_paths = [d for d in dirs if re.search(r"(?i)/python(3(\.\d{1,2})?)?(\.exe)?$", d)]
    if py_paths:
        interp = py_paths[-1]
        dirs = [d for d in dirs if d != interp]
    else:
        m = re.search(r"\b(python3\.\d{1,2})\b", no_urls)
        if m:
            interp = m.group(1)
    versions = [v for v in _VER_RX.findall(re.sub(r"python3\.\d{1,2}", " ", no_urls))]
    return {"index_url": urls[-1] if urls else "", "local_dir": dirs[-1] if dirs else "", "versions": versions,
            "interpreter": interp}


# ---------------------------------------------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------------------------------------------
def _entry_for(spec: FrameworkSpec, allowed: List[str], default: Optional[str], source: str) -> Dict[str, Any]:
    return {"spec": {k: v for k, v in spec.to_dict().items() if k not in ("template_available",)},
            "versions": {"allowed": allowed, "default": default}, "source": source}


def policy_diff(tool_id: str, before: Optional[Dict[str, Any]], after: Dict[str, Any]) -> str:
    """Unified diff of the admin tool list entry - the change the setup makes to Understudy's configuration."""
    def dump(e):
        if not e:
            return []
        e = {k: v for k, v in e.items() if k in ("spec", "versions", "source")}
        if "spec" in e:
            e["spec"] = {k: v for k, v in e["spec"].items() if k not in ("template_available",)}
        return json.dumps({tool_id: e}, indent=2, sort_keys=True).splitlines()
    label = "admin tool list (admin_policy: tool_whitelist)"
    return "\n".join(difflib.unified_diff(dump(before), dump(after), f"a/{label}", f"b/{label}", lineterm="", n=3))


def _clean_versions(cands: List[str], blocked_spec: Optional[FrameworkSpec], name: str,
                    registry: Dict[str, Any]) -> List[str]:
    out = []
    known = set(registry.get("versions") or [])
    for v in cands:
        try:
            v = tool_versions.validate_version(str(v).lstrip("=v"))
        except ValueError:
            continue
        if registry.get("source") == "registry" and known and v not in known:
            # "13.6" from an SOP -> the newest published 13.6.x
            line = tool_versions._sort_desc([k for k in known if k.startswith(v + ".")])
            if not line:
                continue                                       # doesn't exist in the registry
            v = line[0]
        if ap.tool_block_for(blocked_spec, name, v):
            continue
        if v not in out:
            out.append(v)
    return out[:12]


def _resolve_requested(spec: FrameworkSpec, version: str, registry: Dict[str, Any]) -> str:
    """The version a user/SOP asked for ("13.6") -> the newest published release of that line ("13.6.0"). The plan's
    registry list only holds the newest releases, so older lines are looked up in the full list and added to it."""
    known = list(registry.get("versions") or [])
    if registry.get("source") != "registry" or version in known:
        return version
    try:
        full = tool_versions.available_versions(spec, limit=None).get("versions") or []
    except Exception:
        return version
    line = tool_versions._sort_desc([v for v in full if v == version or v.startswith(version + ".")])
    if not line:
        return version
    if line[0] not in known:
        registry["versions"] = known + [line[0]]
    return line[0]


def _finish_plan(r: Dict[str, Any], spec: FrameworkSpec, ai: Dict[str, Any], registry: Dict[str, Any],
                 meta: Dict[str, Any]) -> Dict[str, Any]:
    """Adds the deterministic parts: commands, manifests, diff, code-generation mode, missing runtimes."""
    req_version = r.get("version")
    if r.get("kind") != "version":
        # 8.3: what the run command needs but the stack forgot (e.g. the WebdriverIO runner packages)
        from core.wizard import setup_doctor
        spec, comp_notes = setup_doctor.ensure_companions(spec)
        if comp_notes:
            meta = dict(meta, notes=list(meta.get("notes") or []) + comp_notes)
    if r.get("kind") == "version":
        pol = ap.tool_version_policy(r["tool_id"])
        allowed = list(pol["allowed"]) + ([req_version] if req_version not in pol["allowed"] else [])
        default = pol["default"] or req_version
    else:
        vs = ai.get("versions") or {}
        cands = list(vs.get("suggested_allowed") or [])
        if req_version:
            req_version = _resolve_requested(spec, req_version, registry)
            cands = [req_version] + cands
        allowed = _clean_versions(cands, spec, r.get("tool_name", ""), registry)
        if not allowed and registry.get("versions"):
            allowed = _clean_versions(registry["versions"][:3], spec, r.get("tool_name", ""), registry)
        default = vs.get("recommended_default")
        newest = tool_versions._sort_desc(list(allowed))[0] if allowed else None
        # the version the user / SOP asked for is the default (that's what their session will run)
        default = req_version if req_version in allowed else (default if default in allowed else newest)
    install_version = req_version if r.get("kind") == "version" else default      # what the approval installs
    install_spec = tool_versions.apply_version(spec, install_version) if install_version and spec.packages else spec
    plan_obj = dependency_resolver.plan_for(install_spec)
    before = (ap.whitelist().get(spec.id))
    after = _entry_for(spec, allowed, default, f"AI setup {r['id']}")
    manifest_notes: List[str] = []
    manifest = build_manifest(install_spec, ai, _instruction_text(r), manifest_notes, (r.get("plan") or {}).get("manifest"))
    code_mode = ("The BUILD agent writes the automation code for this stack with the AI (BUILD_GENERATE_SUITE)"
                 + ("; Understudy's vetted code template for exactly this stack is the fallback when the AI is unavailable"
                    if spec.template else "; without an AI key it can't generate code for it"))
    missing = plan_obj.toolchain_missing
    return {
        "kind": r.get("kind", "tool"), "tool_id": spec.id, "tool_name": spec.name,
        "summary": str(ai.get("summary") or "")[:1200],
        "spec": spec.to_dict(),
        "sources": dict(spec.sources or {}),
        "download_steps": [str(x)[:400] for x in (ai.get("download_steps") or [])][:12],
        "commands": [_short(" ".join(c)) for c in plan_obj.commands],
        "manifests": plan_obj.manifest_files,
        "already_satisfied": plan_obj.already_satisfied,
        "toolchain_missing": missing,
        "requirements": [str(x)[:300] for x in (ai.get("requirements") or [])][:8],
        "versions": {"allowed": allowed, "default": default, "registry": registry,
                     "notes": str((ai.get("versions") or {}).get("notes") or "")[:600],
                     "blocked": _blocked_versions(spec, r.get("tool_name", ""))},
        # a fact Understudy knows - never taken from the model (the AI's own remark is added after it)
        "code_generation": (code_mode + (f". AI note: {str(ai['code_generation'])[:300]}" if ai.get("code_generation") else "")),
        "template": spec.template,
        "policy_diff": policy_diff(spec.id, before, after),
        "risks": [str(x)[:300] for x in (ai.get("risks") or [])][:8]
                 + ([f"Not installed on the server: {', '.join(missing)} - install it before the setup can be verified"]
                    if missing else [])
                 + ([f"Packages outside Understudy's trusted list: {', '.join(spec.untrusted_packages)}"]
                    if spec.untrusted_packages else []),
        "instructions_applied": [str(x)[:400] for x in (ai.get("instructions_applied") or [])][:MAX_INSTRUCTIONS],
        "notes": meta.get("notes", []) + manifest_notes,
        "manifest": manifest,
        "agent_source": meta.get("agent_source", "LOCAL_HEURISTIC"), "model": meta.get("model", "Deterministic planner"),
        "prepared_at": _now(), "instructions_count": len(r.get("instructions") or []),
    }


def _short(cmd: str) -> str:
    """Install commands with the server's paths shortened, so the admin reads what matters."""
    import os
    import sys
    from core.wizard.dependency_resolver import ENVS_DIR
    # only the whole interpreter path: /usr/bin/python must not turn /usr/bin/python3.10 into python33.10
    return re.sub(re.escape(sys.executable) + r"(?=\s|$)", "python3", cmd.replace(ENVS_DIR + os.sep, "<sandbox>/"))


def _offline_plan(r: Dict[str, Any], draft: Optional[FrameworkSpec], registry: Dict[str, Any],
                  why: str) -> Tuple[Optional[FrameworkSpec], Dict[str, Any], List[str]]:
    notes = [why] if why else []
    if draft is None or (draft.ecosystem == "none" and not draft.packages):
        return None, {}, notes + [f"Understudy can't work out how to install '{r.get('tool_name')}' without the AI. "
                                  "Set up Gemini in Mission Control, or approve a tool Understudy knows."]
    spec = FrameworkSpec.from_dict(draft.to_dict())
    inst = parse_instructions(_instruction_text(r))
    applied = []
    for k in ("index_url", "local_dir", "interpreter"):
        if inst[k] and k in __import__("core.wizard.frameworks", fromlist=["SOURCE_SUPPORT"]).SOURCE_SUPPORT.get(spec.ecosystem, set()):
            spec.sources[k] = inst[k]
            applied.append({"index_url": f"Packages are downloaded from {inst[k]}",
                            "local_dir": f"Package directory set to {inst[k]}",
                            "interpreter": f"The tool's isolated venv uses the interpreter {inst[k]}"}[k])
    ai = {"summary": f"Install {spec.name} ({spec.ecosystem}: {', '.join(p.name for p in spec.packages)}) from "
                     + (spec.sources.get("index_url") or spec.sources.get("local_dir") or "the public registry")
                     + " into an isolated sandbox environment and add it to the approved tools.",
          "download_steps": _default_steps(spec),
          "versions": {"suggested_allowed": inst["versions"] or list((registry.get("versions") or [])[:3])},
          "instructions_applied": applied}
    return spec, ai, notes


def _default_steps(spec: FrameworkSpec) -> List[str]:
    src = spec.sources or {}
    where = (f"the admin's package index {src['index_url']}" if src.get("index_url") else
             f"the package files in {src['local_dir']} on the server" if src.get("local_dir") else
             {"pip": "PyPI (pypi.org)", "npm": "the npm registry", "maven": "Maven Central", "gradle": "Maven Central",
              "gem": "RubyGems.org",
              "go": "the Go module proxy", "nuget": "nuget.org"}.get(spec.ecosystem, "the package registry"))
    steps = [f"Download {', '.join(p.name for p in spec.packages) or spec.name} from {where}.",
             "Install them into an isolated sandbox environment on the Understudy server (not into the backend's own "
             "packages)."]
    for c in spec.post_install:
        steps.append("Then run: " + " ".join(c) + (" (downloads the browser build that matches this version)"
                                                    if "install" in c else ""))
    return steps


def _ai_plan(r: Dict[str, Any], draft: Optional[FrameworkSpec], known: Optional[FrameworkSpec],
             registry: Dict[str, Any]) -> Tuple[Optional[FrameworkSpec], Dict[str, Any], Dict[str, Any]]:
    from core.wizard.llm import generate_json
    instr = _instruction_text(r)
    res = generate_json("TOOL_SETUP_PLAN", {
        "request_kind": "new tool" if r.get("kind", "tool") == "tool" else "new version of an approved tool",
        "tool_name": r.get("tool_name", ""), "requested_version": r.get("version") or "any",
        "user_reason": r.get("reason") or "(none)",
        "known_tool": json.dumps(known.to_dict(), indent=2) if known else "none",
        "draft_spec": json.dumps(draft.to_dict(), indent=2) if draft else "none",
        "registry_versions": json.dumps(registry, default=str),
        "blocked_versions": ", ".join(_blocked_versions(draft or known, r.get("tool_name", ""))) or "none",
        "server_toolchains": json.dumps(server_toolchains()),
        "allowed_runners": ", ".join(sorted(RUNNER_ALLOWLIST)),
        "admin_instructions": "\n".join(f"{i + 1}. {x['text']}" for i, x in enumerate(r.get("instructions") or []))
                              or "(none)",
        "previous_plan": json.dumps({k: (r.get("plan") or {}).get(k) for k in ("summary", "spec", "sources", "versions")},
                                    default=str) if r.get("plan") else "none",
    }, untrusted=["tool_name", "user_reason", "draft_spec", "admin_instructions", "previous_plan"])
    meta = {"agent_source": res.agent_source, "model": res.model, "notes": []}
    if not res.ok:
        meta["notes"].append(f"AI setup plan unavailable ({res.error}); prepared from Understudy's catalog instead.")
        return None, {}, meta
    data = res.data or {}
    if data.get("security_flags"):
        meta["notes"].append("The AI flagged: " + "; ".join(str(x) for x in data["security_flags"][:3]))
    try:
        spec = FrameworkSpec.from_dict(data.get("spec") or {})
        spec.source = "ai-setup"
        src = {k: str(v).strip() for k, v in (data.get("sources") or {}).items()
               if k in ("index_url", "local_dir", "interpreter") and v}
        for k, v in list(src.items()):
            if v not in instr:                     # only sources the admin typed - never one the model made up
                meta["notes"].append(f"Ignored the AI's {k.replace('_', ' ')} '{v}': it is not in your instructions.")
                src.pop(k)
        spec.sources = src
        if known and (spec.id == known.id or match_catalog(spec.name) and match_catalog(spec.name).id == known.id):
            spec.id, spec.template = known.id, known.template      # keep the offline template
        validate_spec(spec)
        return spec, data, meta
    except FrameworkValidationError as e:
        meta["notes"].append(f"The AI's setup failed Understudy's safety validation ({e}); prepared from the catalog "
                             f"instead.")
        return None, data, meta


def prepare_plan(request_id: str, admin: str = "admin", instruction: Optional[str] = None) -> Dict[str, Any]:
    """(Re)prepares the setup plan of a request, optionally with a new admin instruction. Returns the request."""
    r = copy.deepcopy(ap.get_request(request_id))
    if r["status"] not in ("pending", "stack_ready", "plan_ready", "setup_failed", "awaiting_admin"):
        raise SetupError(f"This request is {r['status'].replace('_', ' ')} - it can't be planned now.", 409)
    if instruction is not None:
        instruction = str(instruction).strip()
        if not instruction:
            raise SetupError("Type an instruction for the AI.")
        if len(r.get("instructions") or []) >= MAX_INSTRUCTIONS:
            raise SetupError(f"At most {MAX_INSTRUCTIONS} instructions per request - reject it and start a new one.")
        tv = screen_text(instruction[:2000], "ADMIN_INSTRUCTION")
        if not tv.is_safe:
            _audit("Model Armor blocked an admin instruction for a tool setup", tv.friendly_message,
                   {"request": request_id, "pattern": tv.pattern}, level="warn")
            raise SetupError(tv.friendly_message)
        r.setdefault("instructions", []).append({"by": admin, "at": _now(), "text": tv.sanitized})
        ap.update_request(request_id, instructions=r["instructions"])
        ap.add_message(request_id, "admin", "instruction", tv.sanitized)
    ap.update_request(request_id, status="planning", clarification=None)
    try:
        plan = _build_plan(r)
    except Exception as e:
        ap.update_request(request_id, status="pending" if not r.get("plan") else "plan_ready")
        raise SetupError(f"Could not prepare the setup: {e}", 422)
    hist = (r.get("plan_history") or []) + ([r["plan"]] if r.get("plan") else [])
    out = ap.update_request(request_id, status="plan_ready" if plan.get("spec") else "pending", plan=plan,
                            plan_history=hist[-5:])
    _event(out)
    m = plan.get("manifest") or {}
    ap.add_message(request_id, "ai", "proposal",
                   (plan.get("summary") or "Setup proposal ready.") if plan.get("spec") else
                   ("I can't prepare this setup yet: " + "; ".join(plan.get("notes") or [])),
                   {"agent_source": plan.get("agent_source"), "commands": plan.get("commands"),
                    "os_dependencies": m.get("os_dependencies"), "env_vars": m.get("env_vars"),
                    "versions": (plan.get("versions") or {}).get("allowed"), "applied": plan.get("instructions_applied")}
                   if plan.get("spec") else {"notes": plan.get("notes")})
    _audit(f"AI prepared the setup of {r['tool_name']}" + (f" {r['version']}" if r.get("version") else ""),
           plan.get("summary") or "; ".join(plan.get("notes") or []),
           {"request": request_id, "agent_source": plan.get("agent_source"), "instruction": instruction})
    return copy.deepcopy(ap.get_request(request_id))


def _build_plan(r: Dict[str, Any]) -> Dict[str, Any]:
    known = _known_tool(r)
    if r.get("kind") == "version":
        entry = ap.whitelist().get(r["tool_id"])
        if not entry:
            raise SetupError("The tool is no longer approved - the version request can't be set up.", 409)
        spec = FrameworkSpec.from_dict(entry["spec"])
        registry = _registry(spec)
        notes = []
        if registry.get("source") == "registry" and r["version"] not in (tool_versions.available_versions(
                spec, include_prereleases=True, limit=None).get("versions") or []):
            notes.append(f"Version {r['version']} was not found in the package registry - the install will fail.")
        ai = {"summary": f"Allow {spec.name} {r['version']} for every user. It is installed into its own isolated "
                         f"sandbox environment and added to the allowed versions "
                         f"({', '.join(ap.tool_version_policy(r['tool_id'])['allowed'])} today).",
              "download_steps": _default_steps(tool_versions.apply_version(spec, r["version"])),
              "instructions_applied": ["Instructions are not used for a version request - the tool's approved setup "
                                       "(package source included) stays as it is."] if r.get("instructions") else []}
        return _finish_plan(r, spec, ai, registry, {"agent_source": "LOCAL_HEURISTIC", "model": "Deterministic planner",
                                                    "notes": notes})
    from core.wizard import tool_stack
    composed = tool_stack.stack_spec(r)
    if composed is not None:
        return _stack_plan(r, composed)
    draft = FrameworkSpec.from_dict(r["spec"]) if r.get("spec") else None
    if draft is not None and draft.ecosystem == "none" and not draft.packages and known is None:
        # a typed tool no one could resolve yet: try the resolver (AI) now
        from core.wizard.frameworks import resolve_custom_framework
        resolved, _m = resolve_custom_framework(r["tool_name"])
        draft = resolved or draft
    base = known or draft
    registry = _registry(base)
    spec, ai, meta = _ai_plan(r, draft, known, registry)
    if spec is None:
        spec, ai_off, notes = _offline_plan(r, base, registry, "")
        meta = {"agent_source": "LOCAL_HEURISTIC", "model": "Deterministic planner", "notes": meta.get("notes", []) + notes}
        if spec is None:
            return {"kind": "tool", "tool_id": r["tool_id"], "tool_name": r["tool_name"], "spec": None,
                    "summary": "", "notes": meta["notes"], "agent_source": "LOCAL_HEURISTIC", "prepared_at": _now()}
        try:
            validate_spec(spec)
        except FrameworkValidationError as e:
            return {"kind": "tool", "tool_id": r["tool_id"], "tool_name": r["tool_name"], "spec": None, "summary": "",
                    "notes": meta["notes"] + [f"Your instructions produced an unsafe setup: {e}"],
                    "agent_source": "LOCAL_HEURISTIC", "prepared_at": _now()}
        ai = ai_off
    if spec.id != r["tool_id"]:
        ap.update_request(r["id"], tool_id=spec.id)
        r["tool_id"] = spec.id
    blk = ap.tool_block_for(spec, r.get("tool_name", ""))
    if blk and not blk.get("versions"):
        raise SetupError(f"{spec.name} is on the blocked tools list ({blk['name']}).", 409)
    if not ai.get("download_steps"):
        ai["download_steps"] = _default_steps(spec)
    return _finish_plan(r, spec, ai, registry, meta)


def _stack_plan(r: Dict[str, Any], spec: FrameworkSpec) -> Dict[str, Any]:
    """8.2: the plan for the stack the admin selected - its packages, versions and run command are fixed by that
    selection; the AI adds the manifest (OS dependencies, environment), the download steps and the risks,
    and may only set package sources that the admin typed."""
    sel = (r.get("stack") or {}).get("selection") or {}
    registry = _registry(spec)
    ai_spec, ai, meta = _ai_plan(r, spec, None, registry)
    meta["notes"] = [n.replace("prepared from Understudy's catalog instead", "prepared from your stack selection instead")
                     .replace("prepared from the catalog instead", "prepared from your stack selection instead")
                     for n in meta.get("notes") or []]
    if ai_spec is not None:
        spec.sources = dict(ai_spec.sources or {})
    if not ai:
        _s, ai, notes = _offline_plan(r, spec, registry, "")
        meta = {"agent_source": "LOCAL_HEURISTIC", "model": "Deterministic planner",
                "notes": (meta.get("notes") or []) + notes}
        if _s is not None:
            spec.sources = dict(_s.sources or {})
    core_v = (sel.get("versions") or {}).get((sel.get("components") or [None])[0]) if sel else None
    ai = dict(ai)
    ai["versions"] = dict(ai.get("versions") or {}, suggested_allowed=[v for v in [core_v] if v]
                          + [v for v in ((ai.get("versions") or {}).get("suggested_allowed") or []) if v != core_v][:2],
                          recommended_default=core_v or (ai.get("versions") or {}).get("recommended_default"))
    comp = ", ".join(f"{u['name']} {u['version'] or ''}".strip() for u in sel.get("used") or [])
    if comp:
        ai["summary"] = (f"Install the stack you selected - {comp} - for {spec.language} with "
                         f"{tool_stack_label(sel.get('package_manager'))}. " + str(ai.get("summary") or ""))[:1200]
    meta["notes"] = list(meta.get("notes") or []) + list(sel.get("notes") or [])
    try:
        validate_spec(spec)
    except FrameworkValidationError as e:
        raise SetupError(f"The selected stack failed safety validation: {e}", 422)
    if not ai.get("download_steps"):
        ai["download_steps"] = _default_steps(spec)
    return _finish_plan(r, spec, ai, registry, meta)


def tool_stack_label(pm: Optional[str]) -> str:
    from core.wizard.tool_stack import PM_LABEL
    return PM_LABEL.get(pm or "", pm or "its package manager")


# ---------------------------------------------------------------------------------------------------------------
# Installation manifest (8.0): what the setup installs, checks and sets - shown to the admin before anything runs
# ---------------------------------------------------------------------------------------------------------------
_ECO_OS_DEPS = {
    "pip": [], "npm": [{"name": "Node.js", "check": "binary", "value": "node", "why": "runs the tool"},
                       {"name": "npm", "check": "binary", "value": "npm", "why": "installs the packages"}],
    "maven": [{"name": "Java (JDK 17+)", "check": "binary", "value": "java", "why": "runs Maven and the tests"},
              {"name": "Maven", "check": "binary", "value": "mvn", "why": "downloads the jars and runs the tests"}],
    "gradle": [{"name": "Java (JDK 17+)", "check": "binary", "value": "java", "why": "runs Gradle and the tests"},
               {"name": "Gradle", "check": "binary", "value": "gradle", "why": "downloads the jars and runs the tests"}],
    "gem": [{"name": "Ruby", "check": "binary", "value": "ruby", "why": "runs the tool"},
            {"name": "RubyGems", "check": "binary", "value": "gem", "why": "installs the gems"}],
    "go": [{"name": "Go", "check": "binary", "value": "go", "why": "builds and runs the tool"}],
    "nuget": [{"name": ".NET SDK", "check": "binary", "value": "dotnet", "why": "restores and runs the tool"}],
}
_BIN_RX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,40}$")


def _clean_os_deps(items: Any, eco: str) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for d in (items if isinstance(items, list) else [])[:12]:
        if not isinstance(d, dict):
            continue
        check, value = str(d.get("check") or ""), str(d.get("value") or "").strip()
        if check == "binary" and _BIN_RX.match(value) or check == "env" and re.match(r"^[A-Z][A-Z0-9_]{0,63}$", value):
            out.append({"name": str(d.get("name") or value)[:80], "check": check, "value": value,
                        "why": str(d.get("why") or "")[:200]})
    for d in _ECO_OS_DEPS.get(eco, []):
        if not any(x["check"] == d["check"] and x["value"] == d["value"] for x in out):
            out.append(dict(d))
    return out


def _with_stack_deps(deps: List[Dict[str, str]], spec: FrameworkSpec) -> List[Dict[str, str]]:
    from core.wizard import setup_doctor
    out = list(deps)
    for d in setup_doctor.os_dependencies(spec):
        if not any(x["check"] == d["check"] and x["value"] == d["value"] for x in out):
            out.append(d)
    return out


def _clean_env_vars(env: Any, instr: str, notes: List[str]) -> Dict[str, str]:
    from core.wizard.sandbox import SandboxError, clean_manifest_env
    out: Dict[str, str] = {}
    for k, v in (env.items() if isinstance(env, dict) else []):
        v = str(v)
        if ("/" in v or "\\" in v) and v not in instr:
            notes.append(f"Ignored the AI's {k}={v}: paths must come from your instructions.")
            continue
        try:
            out.update(clean_manifest_env({str(k): v}))
        except SandboxError as e:
            notes.append(f"Ignored the AI's {k}: {e}")
    return out


def runner_wrapper(spec: FrameworkSpec, manifest: Dict[str, Any]) -> Dict[str, str]:
    """The wrapper Understudy uses to start the tool: sets the manifest's variables, checks the OS dependencies, then
    runs the approved command. Written by Understudy from the manifest (never free-form AI code)."""
    run = " ".join(spec.run_command).replace("{entry}", "\"${ENTRY:-%s}\"" % spec.entry_file)
    sh = ["#!/bin/sh", f"# Runner wrapper for {spec.name} - generated by Understudy from the approved setup manifest",
          "set -e"]
    for k, v in (manifest.get("env_vars") or {}).items():
        sh.append(f"export {k}='{v}'")
    for p in manifest.get("path_prepend") or []:
        sh.append(f"export PATH='{p}':\"$PATH\"")
    for d in manifest.get("os_dependencies") or []:
        if d["check"] == "binary":
            sh.append(f"command -v {d['value']} >/dev/null 2>&1 || {{ echo 'missing: {d['name']} ({d['value']})' >&2; exit 127; }}")
        else:
            sh.append(f"[ -n \"${d['value']}\" ] || {{ echo 'missing: {d['value']} is not set' >&2; exit 127; }}")
    sh.append(f"exec {run}")
    cmd = ["@echo off", f"REM Runner wrapper for {spec.name} - generated by Understudy from the approved setup manifest"]
    for k, v in (manifest.get("env_vars") or {}).items():
        cmd.append(f"set \"{k}={v}\"")
    for p in manifest.get("path_prepend") or []:
        cmd.append(f"set \"PATH={p};%PATH%\"")
    for d in manifest.get("os_dependencies") or []:
        if d["check"] == "binary":
            cmd.append(f"where {d['value']} >nul 2>nul || (echo missing: {d['name']} 1>&2 & exit /b 127)")
    cmd.append(" ".join(spec.run_command).replace("{entry}", spec.entry_file))
    return {"run_tool.sh": "\n".join(sh) + "\n", "run_tool.cmd": "\r\n".join(cmd) + "\r\n"}


def build_manifest(spec: FrameworkSpec, ai: Dict[str, Any], instr: str, notes: List[str],
                   previous: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    from core.wizard.sandbox import describe as sandbox_describe
    prev = previous or {}
    env = _clean_env_vars(ai.get("env_vars") or prev.get("env_vars") or {}, instr, notes)
    plan = dependency_resolver.plan_for(spec)
    m = {
        "os_dependencies": _with_stack_deps(_clean_os_deps(ai.get("os_dependencies") or prev.get("os_dependencies"),
                                                           spec.ecosystem), spec),
        "package_commands": [_short(" ".join(c)) for c in plan.commands],
        "env_vars": env,
        "path_prepend": list(prev.get("path_prepend") or []),
        "interpreter": (spec.sources or {}).get("interpreter") or "",
        "runner_notes": str(ai.get("runner_notes") or "")[:600],
        "sandbox": sandbox_describe()["effective"],
        "smoke_test": ("written by the AI for exactly this stack (TOOL_SMOKE_TEST); you review the code before it runs"
                       if _ai_on() else "needs the AI (8.2: there are no smoke-test templates) - configure Gemini first"),
    }
    m["runner_wrapper"] = runner_wrapper(spec, m)
    return m


def _ai_on() -> bool:
    try:
        from core.wizard.llm import ai_available
        return bool(ai_available())
    except Exception:
        return False


# ---------------------------------------------------------------------------------------------------------------
# Apply (8.0): install in the sandbox -> smoke test -> ACTIVE; any failure halts with a question for the admin
# ---------------------------------------------------------------------------------------------------------------
MAX_ROUNDS = 8
RUNNING = ("installing", "smoke_testing")


def _event(r: Dict[str, Any], type_: str = "tool_request.updated", **extra) -> None:
    from core.wizard import events
    users = list(r.get("requested_by") or [])
    events.publish(type_, {"request": r["id"], "tool_id": r["tool_id"], "tool_name": r["tool_name"],
                           "version": r.get("version"), "status": r["status"],
                           "registry_status": ap.REGISTRY_STATUS.get(r["status"], r["status"].upper()), **extra},
                   users=users, admin=True)


def _set_status(request_id: str, status: str, **fields) -> Dict[str, Any]:
    r = ap.update_request(request_id, status=status, **fields)
    _event(r)
    return r


def apply_plan(request_id: str, admin: str = "admin", allowed: Optional[List[str]] = None,
               default: Optional[str] = None, force: bool = False, background: bool = True) -> Dict[str, Any]:
    """The admin approved the plan: install in the sandbox, smoke test, then ACTIVE (added to the approved list).
    A failure on the way halts the setup with a question for the admin (``answer`` resumes it)."""
    r = copy.deepcopy(ap.get_request(request_id))
    plan = r.get("plan") or {}
    if force:
        raise SetupError("A tool only becomes active after its smoke test passes in the sandbox (8.0). Answer the "
                         "setup's question in the chat to resume it, or reject the request.", 409)
    if r["status"] not in ("plan_ready", "setup_failed") or not plan.get("spec"):
        raise SetupError("Prepare (and read) the AI setup plan first.", 409)
    spec = FrameworkSpec.from_dict(plan["spec"])
    try:
        validate_spec(spec)
    except FrameworkValidationError as e:
        raise SetupError(f"The planned setup failed safety validation: {e}", 422)
    allowed = list(plan["versions"]["allowed"] if allowed is None else allowed)
    clean: List[str] = []
    for v in allowed:
        try:
            v = tool_versions.validate_version(v)
        except ValueError as e:
            raise SetupError(str(e))
        if ap.tool_block_for(spec, r.get("tool_name", ""), v):
            raise SetupError(f"Version {v} is on the blocked list.")
        if v not in clean:
            clean.append(v)
    if r.get("kind") == "version" and r["version"] not in clean:
        raise SetupError(f"The requested version {r['version']} must stay in the allowed versions.")
    default = default if default is not None else plan["versions"].get("default")
    if clean and default not in clean:
        raise SetupError("The default version must be one of the allowed versions.")
    if not clean:
        default = None
    install_version = r["version"] if r.get("kind") == "version" else default
    setup = {"status": "installing", "started_at": _now(), "by": admin, "logs": [], "allowed": clean,
             "default": default, "install_version": install_version, "round": 0, "overrides": {},
             "attempts": []}
    _set_status(request_id, "installing", setup=setup, clarification=None)
    ap.add_message(request_id, "admin", "approve",
                   "Approved the setup" + (f": versions {', '.join(clean)} (default {default})" if clean else "")
                   + ". Install it in the sandbox and smoke-test it.")
    _audit(f"Admin approved the AI setup of {r['tool_name']}" + (f" {r['version']}" if r.get("version") else ""),
           "installing in the sandbox, then a smoke test" + (f"; versions {', '.join(clean)} (default {default})"
                                                              if clean else "; no version restriction"),
           {"request": request_id})
    return _start(request_id, background)


def _start(request_id: str, background: bool) -> Dict[str, Any]:
    def job():
        _setup_job(request_id)
    if background:
        t = threading.Thread(target=job, daemon=True, name=f"tool-setup-{request_id}")
        _JOBS[request_id] = t
        t.start()
    else:
        job()
    return ap.get_request(request_id)


def _effective(r: Dict[str, Any]) -> Tuple[FrameworkSpec, Dict[str, Any]]:
    """The spec to install (version + package-source overrides applied) and the manifest with the admin's answers."""
    plan = r.get("plan") or {}
    setup = r.get("setup") or {}
    ov = setup.get("overrides") or {}
    spec = FrameworkSpec.from_dict(plan["spec"])
    for k in ("index_url", "local_dir", "interpreter"):
        if ov.get(k):
            spec.sources[k] = ov[k]
    validate_spec(spec)
    version = ov.get("version") or setup.get("install_version")
    if version and spec.packages:
        spec = tool_versions.apply_version(spec, version)
    from core.wizard import setup_doctor
    spec, _cn = setup_doctor.ensure_companions(spec, lookup_versions=False)      # 8.3 (requests planned before 8.3)
    spec = setup_doctor.apply_overrides(spec, ov)                                 # 8.3: add_packages / post_install
    if ov.get("skip_post_install"):
        spec.post_install = []
    manifest = copy.deepcopy(plan.get("manifest") or {})
    manifest["os_dependencies"] = _with_stack_deps(list(manifest.get("os_dependencies") or []), spec)
    manifest.setdefault("env_vars", {}).update(ov.get("env_vars") or {})
    manifest["path_prepend"] = list(dict.fromkeys(list(ov.get("path_prepend") or []) + list(manifest.get("path_prepend") or [])))
    return spec, manifest


class SetupFailure(Exception):
    def __init__(self, stage: str, message: str, command: str = "", exit_code: Optional[int] = None,
                 output: str = "", data: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.stage, self.message, self.command, self.exit_code, self.output = stage, message, command, exit_code, output
        self.data = data or {}

    def to_dict(self) -> Dict[str, Any]:
        return {"stage": self.stage, "message": self.message[:600], "command": self.command[:400],
                "exit_code": self.exit_code, "output": _scrub(self.output)[-6000:], **self.data}


def _scrub(text: str) -> str:
    """Never echo secrets from tool output into the chat / the AI prompt."""
    text = re.sub(r"(?i)((?:api[_-]?key|token|password|passwd|secret)\s*[=:]\s*)\S+", r"\1***", text or "")
    return re.sub(r"(https?://)[^/\s:@]+:[^/\s@]+@", r"\1***:***@", text)


def _install_key(runner, spec: FrameworkSpec, manifest: Dict[str, Any]) -> str:
    """Identifies an installed environment: same sandbox, packages, sources and manifest -> no need to install again
    (e.g. when the setup resumes after the admin reviewed the smoke test)."""
    import hashlib
    blob = json.dumps({"kind": runner.kind, "env": getattr(runner, "env_dir", ""), "image": getattr(runner, "image", ""),
                       "pk": [(p.name, p.version) for p in spec.packages], "src": spec.sources, "post": spec.post_install,
                       "envv": manifest.get("env_vars"), "pp": manifest.get("path_prepend")}, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def _java_for_setup(spec: FrameworkSpec, manifest: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """8.2: the JDK Maven will use in this setup (manifest JAVA_HOME / PATH first) and the release the pom targets."""
    if spec.ecosystem not in ("maven", "gradle"):
        return None
    from core.wizard import jdk
    import os
    env = dict(os.environ)
    env.update(dependency_resolver.toolchain_env())
    env["PATH"] = dependency_resolver.toolchain_path(env.get("PATH", ""))
    env.update(manifest.get("env_vars") or {})
    info = jdk.maven_jdk(env, manifest.get("path_prepend"))
    rel = jdk.release_for_major(int(info["major"]) if info else None)
    return {"home": (info or {}).get("home"), "major": (info or {}).get("major"), "source": (info or {}).get("source"),
            "release": rel}


def _setup_job(request_id: str) -> None:
    from core.wizard import jdk
    from core.wizard.sandbox import SandboxError, SandboxUnavailable, create_runner
    r = copy.deepcopy(ap.get_request(request_id))
    setup = r["setup"]
    logs: List[str] = setup.setdefault("logs", [])

    def log(m: str):
        logs.append(_scrub(m)[:500])
        if len(logs) > 400:
            del logs[:len(logs) - 400]
    attempt = {"round": setup.get("round", 0), "started_at": _now(), "steps": []}
    try:
        spec, manifest = _effective(r)
        from core.wizard import runtime_restore          # 9.9: what earlier setups downloaded (Cloud Run instances)
        runtime_restore.ensure(log)
        # 1. runtimes the admin asked Understudy to download
        for key in (setup.get("overrides") or {}).get("download_runtimes") or []:
            from core.wizard import runtimes
            log(f"Downloading runtime {key} into Understudy's runtimes folder ...")
            try:
                info = runtimes.provision(key, log)
            except Exception as e:
                raise SetupFailure("runtime_download", f"Could not download {key}: {e}", data={"runtime": key})
            if info.get("env_name"):
                manifest["env_vars"][info["env_name"]] = info["home"]
            for b in [info["bin"]] + list(info.get("extra_bins") or []):
                if b not in manifest["path_prepend"]:
                    manifest["path_prepend"].insert(0, b)
            attempt["steps"].append({"step": f"runtime {key}", "ok": True, "home": info["home"]})
            kind = runtimes.kind_of(key)
            if kind in ("maven", "gradle", "ruby", "go", "dotnet") and kind not in ap.toolchains():
                # a build tool is server-wide: register it in Mission Control > Package managers too
                ap.set_toolchain(kind, dict(info, kind=kind, source="download"), setup.get("by", "admin"))
        # 9.4: a runtime the tool needs that is missing / broken on PATH (e.g. the .NET host in Program Files with
        # no SDK) -> use the install Understudy already has (downloaded in Mission Control > Environments)
        from core.wizard import environments
        try:
            picks = environments.runtimes_for_setup(
                spec.ecosystem, manifest.get("path_prepend"), manifest.get("env_vars"),
                trust=environments.kinds_chosen_by_admin(setup.get("overrides") or {}))
        except Exception as e:                       # never stop a setup over this
            picks = []
            log(f"Could not check the installed runtimes: {type(e).__name__}: {e}")
        missing = [pk for pk in picks if not pk.get("rec")]
        if missing:
            # 9.4: stop BEFORE installing and ask the admin to approve the download this fix needs
            pk = missing[0]
            raise SetupFailure("runtime_missing",
                               f"{spec.name} needs {environments.KINDS[pk['kind']]['label']}: {pk['why']}.",
                               data={"runtime_kind": pk["kind"], "download_keys": pk.get("download_keys") or [],
                                     "why": pk["why"], "missing_runtimes": [
                                         {"kind": m["kind"], "why": m["why"], "download_keys": m.get("download_keys")}
                                         for m in missing]})
        for pk in [p for p in picks if p.get("rec")]:
            bins, envs = environments.runtime_settings(pk["rec"])
            for b in reversed(bins):
                if b not in manifest["path_prepend"]:
                    manifest["path_prepend"].insert(0, b)
            manifest["env_vars"].update(envs)        # only picked because the current one doesn't work
            msg = f"Using {environments.describe_install(pk['rec'])} - {pk['why']}."
            log(msg)
            attempt["steps"].append({"step": f"runtime {pk['kind']}", "ok": True, "home": pk["rec"].get("home"),
                                     "auto": True, "why": pk["why"]})
            setup.setdefault("auto_runtimes", []).append({"kind": pk["kind"], "id": pk["rec"]["id"],
                                                          "path": pk["rec"].get("path"), "why": pk["why"], "at": _now()})
            ap.add_message(request_id, "system", "progress", msg)
        java = _java_for_setup(spec, manifest)
        if java:
            setup["java"] = java
            log(f"Java for {'Gradle' if spec.ecosystem == 'gradle' else 'Maven'}: {('Java ' + str(java['major']) + ' at ' + str(java['home']) + ' (' + str(java['source']) + ')') if java.get('major') else 'not found'}"
                f" - pom.xml targets release {java['release']}")
        try:
            with jdk.release_override(java["release"] if java else None):
                runner = create_runner(spec, purpose="setup", log=log, env=manifest.get("env_vars"),
                                       path_prepend=manifest.get("path_prepend"), name=f"setup-{request_id}")
        except SandboxUnavailable as e:
            raise SetupFailure("sandbox", str(e))
        except SandboxError as e:
            raise SetupFailure("manifest", str(e))
        setup["sandbox"] = runner.describe()
        key = _install_key(runner, spec, manifest)
        reuse = setup.get("installed_key") == key and (setup.get("install") or {}).get("status") == "READY"
        if not reuse:
            ap.add_message(request_id, "system", "progress",
                           f"Installing {spec.name}{' ' + _ver(spec) if _ver(spec) else ''} in the {runner.kind} sandbox ...")
        _set_status(request_id, "installing", setup=setup)
        with runner:
            # 2. OS dependencies from the manifest
            for d in manifest.get("os_dependencies") or []:
                ok, detail = runner.check_dependency(d["check"], d["value"])
                attempt["steps"].append({"step": f"check {d['value']}", "ok": ok, "detail": detail})
                if not ok:
                    raise SetupFailure("os_dependencies", f"{d['name']}: {detail}", command=f"check {d['check']} {d['value']}",
                                       data={"dependency": d})
            # 3. packages (skipped when exactly this environment was installed in an earlier round of this setup)
            if reuse and runner.kind == "local":
                log("Packages are already installed in this sandbox environment (same versions) - not installing again.")
                out = None
                attempt["steps"].append({"step": "install", "ok": True, "status": "REUSED"})
            else:
                with jdk.release_override(java["release"] if java else None):
                    out = runner.install_dependencies()
                attempt["steps"].append({"step": "install", "ok": out.ok, "status": out.status})
                if out.status == "TOOLCHAIN_MISSING":
                    raise SetupFailure("os_dependencies", out.message, command="toolchain check",
                                       data={"toolchain_missing": out.toolchain_missing})
                if not out.ok:
                    f = out.failed
                    output = f.output if f else ""
                    if f and "Browser.entry" in f.command and getattr(runner, "env_dir", None):
                        from core.wizard import setup_doctor
                        output += setup_doctor.rfbrowser_log_tail(runner.env_dir)   # 8.3: npm's real error
                    raise SetupFailure("install", out.message, command=f.command if f else "", exit_code=f.exit_code if f else None,
                                       output=output)
                setup["install"] = out.to_dict()
                setup["installed_key"] = key
            # 4. mandatory smoke test: written by the AI, reviewed by the admin, then run
            if out is not None and out.status == "DRY_RUN":
                setup["smoke"] = {"status": "SKIPPED", "detail": f"install mode '{dependency_resolver.install_mode()}' - "
                                  "nothing was installed, so the smoke test can't run", "at": _now()}
            elif spec.category == "agentic":
                # 9.9: an agentic tool on Understudy's agent - Understudy's own, fixed agent smoke test (self-test
                # mode: no Gemini key, no AI-written code to review)
                from core.wizard import agent_stacks
                _set_status(request_id, "smoke_testing", setup=setup)
                ap.add_message(request_id, "system", "progress", "Packages installed. Running Understudy's agent smoke "
                               "test: the agent opens a test page, types a name, clicks Go and checks the result "
                               "(self-test mode - no Gemini key needed) ...")
                files = agent_stacks.smoke_files(spec)
                res, info = smoke.run(runner, spec, files)
                setup["smoke"] = {"status": "PASSED" if res.ok else "FAILED", "exit_code": res.exit_code,
                                  "source": "understudy-agent", "notes": ["Understudy's agent smoke test (fixed)"],
                                  "duration_ms": res.duration_ms, "reviewed_by": None,
                                  "output": _scrub(res.tail(30)), "files": {k: v for k, v in files.items()
                                                                           if k != "understudy_agent.py"},
                                  "url": info.get("smoke_url"), "runner": runner.kind, "at": _now()}
                attempt["steps"].append({"step": "agent smoke test", "ok": res.ok, "exit_code": res.exit_code})
                if not res.ok:
                    raise SetupFailure("smoke_test", f"The agent smoke test exited with code {res.exit_code}"
                                       + (" (time limit)" if res.timed_out else ""),
                                       command="python run_agent.py", exit_code=res.exit_code, output=res.output,
                                       data={"smoke_source": "understudy-agent"})
            else:
                review = setup.get("smoke_review") or {}
                if not (review.get("approved") and review.get("files")):
                    _set_status(request_id, "smoke_testing", setup=setup)
                    ap.add_message(request_id, "system", "progress", "Packages installed. The AI is writing the smoke test ...")
                    files, source, notes, ai_notes = _smoke_files(r, spec, runner.kind, (java or {}).get("release"),
                                                                  review.get("feedback") or [])
                    if files is None:
                        raise SetupFailure("smoke_test", "; ".join(notes) or "No smoke test could be written for this tool",
                                           data={"smoke_source": source})
                    setup["smoke_review"] = {"files": files, "source": source, "notes": notes, "ai_notes": ai_notes,
                                             "written_at": _now(), "approved": False,
                                             "revision": int(review.get("revision", -1)) + 1,
                                             "feedback": list(review.get("feedback") or []),
                                             "previous_failure": review.get("previous_failure")}
                    attempt.update(ok=True, finished_at=_now(), paused="smoke_review")
                    setup.setdefault("attempts", []).append(attempt)
                    setup["status"] = "smoke_review"
                    out_r = _set_status(request_id, "smoke_review", setup=setup)
                    ap.add_message(request_id, "ai", "smoke_review",
                                   f"I wrote the smoke test for {spec.name} ({len(files)} file(s)"
                                   + (f": {ai_notes}" if ai_notes else "") + "). Please review the code - approve it to run "
                                   "it in the sandbox, or tell me what to change.",
                                   {"files": sorted(files), "notes": notes})
                    _event(out_r, question="Review the AI-written smoke test")
                    return
                files = review["files"]
                if review.get("source") == "agent":            # 8.5: Understudy's own manifest next to the agent's test
                    files = dict(files, **smoke.understudy_manifests(spec, (java or {}).get("release")))
                from core.wizard import robot_syntax
                files, fixed = robot_syntax.repair_files(files)
                if fixed:                              # 8.4.1: shown, so the admin sees exactly what runs
                    review["files"] = files
                    ap.add_message(request_id, "system", "progress", "Understudy fixed the Robot Framework layout of "
                                   "the reviewed test before running it: " + "; ".join(fixed[:4]))
                _set_status(request_id, "smoke_testing", setup=setup)
                ap.add_message(request_id, "system", "progress", "Running the smoke test you approved ...")
                res, info = smoke.run(runner, spec, files)
                setup["smoke"] = {"status": "PASSED" if res.ok else "FAILED", "exit_code": res.exit_code,
                                  "source": review.get("source", "ai"), "notes": review.get("notes") or [],
                                  "duration_ms": res.duration_ms, "reviewed_by": review.get("approved_by"),
                                  "output": _scrub(res.tail(30)), "files": files, "url": info.get("smoke_url"),
                                  "runner": runner.kind, "at": _now()}
                attempt["steps"].append({"step": "smoke test", "ok": res.ok, "exit_code": res.exit_code})
                if not res.ok:
                    raise SetupFailure("smoke_test", f"The smoke test exited with code {res.exit_code}"
                                       + (" (time limit)" if res.timed_out else "")
                                       + (" (memory limit)" if res.memory_exceeded else ""),
                                       command=" ".join(spec.run_command).replace("{entry}", spec.entry_file), exit_code=res.exit_code, output=res.output,
                                       data={"smoke_source": review.get("source", "ai")})
        attempt.update(ok=True, finished_at=_now())
        setup.setdefault("attempts", []).append(attempt)
        _activate(request_id, spec, manifest, setup)
    except SetupFailure as f:
        attempt.update(ok=False, failure=f.to_dict(), finished_at=_now())
        setup.setdefault("attempts", []).append(attempt)
        setup["attempts"] = setup["attempts"][-10:]
        _halt(request_id, setup, f)
    except Exception as e:                                       # never leave a request stuck in installing
        f = SetupFailure("internal", f"{type(e).__name__}: {e}")
        attempt.update(ok=False, failure=f.to_dict(), finished_at=_now())
        setup.setdefault("attempts", []).append(attempt)
        _halt(request_id, setup, f)


def _ver(spec: FrameworkSpec) -> str:
    v = spec.packages[0].version if spec.packages else ""
    return v[2:] if v.startswith("==") else v


def _smoke_files(r: Dict[str, Any], spec: FrameworkSpec, runner_kind: str, java_release: Optional[int] = None,
                 feedback: Optional[List[str]] = None
                 ) -> Tuple[Optional[Dict[str, str]], str, List[str], str]:
    """(files, source, notes, ai_notes): the AI's smoke test for exactly this stack (8.2: no templates). Understudy
    adds its own dependency manifest (Maven: pom.xml) so the test proves what users will install."""
    notes: List[str] = []
    setup = r.get("setup") or {}
    prev = setup.get("smoke") or {}
    if not _ai_on():
        return None, "none", ["The smoke test is written by the AI (8.2: nothing is pre-built, there are no templates). "
                              "Configure Gemini in Mission Control > AI, security & account, then answer 'retry'."], ""
    from core.wizard.llm import generate_json
    sel = (r.get("stack") or {}).get("selection") or {}
    installed = ", ".join(f"{p.name} {p.version.lstrip('=')}".strip() for p in spec.packages)
    comps = "\n".join(f"- {u['name']} ({u['role']}): {u['package']} {u.get('version') or ''}" for u in sel.get("used") or []) \
        or "(the packages above)"
    previous = (json.dumps({"files": prev.get("files"), "output": prev.get("output")})[:6000]
                if prev.get("status") == "FAILED" else "none")
    problems: List[str] = []
    for attempt in range(2):          # 8.2: a test that fails Understudy's check is sent back to the AI once by itself
        res = generate_json("TOOL_SMOKE_TEST", {
            "tool": json.dumps({k: v for k, v in spec.to_dict().items()
                                if k in ("id", "name", "language", "ecosystem", "packages", "run_command", "entry_file",
                                         "category")}, indent=2),
            "entry_file": spec.entry_file, "run_command": " ".join(spec.run_command).replace("{entry}", spec.entry_file),
            "installed": installed, "components": comps,
            "java_release": str(java_release) if java_release else "n/a", "sandbox": runner_kind,
            "previous": previous,
            "admin_feedback": "\n".join(f"{i + 1}. {t}" for i, t in enumerate(feedback or [])) or "(none)",
        }, untrusted=["previous", "admin_feedback"])
        if not res.ok:
            return None, "none", notes + [f"The AI could not write the smoke test ({res.error})."], ""
        files: Dict[str, str] = {}
        for f in (res.data or {}).get("files") or []:
            if isinstance(f, dict) and f.get("path"):
                path = re.sub(r"^(\./)+", "", str(f["path"]).strip().replace("\\", "/")).lstrip("/")
                if path.split("/")[-1] in smoke.MANIFEST_FILES:
                    notes.append(f"Ignored the AI's {path}: Understudy writes the dependency manifest itself.")
                    continue
                files[path] = str(f.get("content") or "")
        files = _place_entry(files, spec)
        files.update(smoke.understudy_manifests(spec, java_release))
        from core.wizard import robot_syntax
        files, fixed = robot_syntax.repair_files(files)
        if fixed:
            notes.append("Understudy fixed the Robot Framework layout of the AI's test: " + "; ".join(fixed[:4]))
        problems = smoke.validate_files(files, spec)
        if not problems:
            if attempt:
                notes.append("The AI's first test failed Understudy's check and was rewritten automatically.")
            return files, "ai", notes, str((res.data or {}).get("notes") or "")[:600]
        from core.wizard import setup_doctor
        previous = json.dumps({"files": {k: v[:3000] for k, v in files.items()},
                               "rejected_by_understudy_because": problems,
                               "fix": (setup_doctor.rewrite_hints("; ".join(problems), spec) + " ").strip()
                                      + f" Write the test in exactly {spec.entry_file}; keep every other rule."})[:6000]
    return None, "ai", notes + ["The AI's smoke test was rejected by Understudy's safety check (twice): "
                                + "; ".join(problems[:4]) + ". Answer 'retry' to have it rewritten."], ""


def _place_entry(files: Dict[str, str], spec: FrameworkSpec) -> Dict[str, str]:
    """When the AI wrote exactly one test file under another name than the entry file (e.g. tests/smoke.spec.js for
    tests/automation.spec.js), use it as the entry file - same folder rules, same content."""
    if not spec.entry_file or spec.entry_file in files or spec.ecosystem in ("maven", "gradle"):
        return files
    ext = os.path.splitext(spec.entry_file)[1]
    helper = {"conftest.py", "__init__.py", "setup.py", "pytest.ini", "tox.ini", "environment.py", "env.rb",
              "spec_helper.rb", "support.js", "e2e.js", "index.js"}
    cands = [k for k in files if k.endswith(ext) and "config" not in k.lower() and k.split("/")[-1] not in smoke.MANIFEST_FILES
             and k.split("/")[-1] not in helper and "/support/" not in "/" + k and "/steps/" not in "/" + k
             and "step_definitions" not in k and "/pages/" not in "/" + k]
    if len(cands) > 1:            # 8.3: several files - the one that looks like the test wins (test_*.py, *.spec.js ...)
        testish = [k for k in cands if re.search(r"(?i)(^|/)(test_[^/]*|[^/]*_test\.\w+|[^/]*\.(spec|test|e2e|cy)\.\w+|"
                                                 r"[^/]*(smoke|automation)[^/]*)$", k)]
        cands = testish if len(testish) == 1 else cands
    if len(cands) == 1:
        files = dict(files)
        files[spec.entry_file] = files.pop(cands[0])
    return files


def approve_smoke(request_id: str, admin: str = "admin", background: bool = True) -> Dict[str, Any]:
    """The admin reviewed the AI's smoke test: run it in the sandbox (the install is reused)."""
    r = copy.deepcopy(ap.get_request(request_id))
    setup = r.get("setup") or {}
    review = setup.get("smoke_review") or {}
    if r["status"] != "smoke_review" or not review.get("files"):
        raise SetupError("There is no smoke test waiting for your review.", 409)
    review.update(approved=True, approved_by=admin, approved_at=_now())
    setup.update(smoke_review=review, status="installing", resumed_at=_now())
    ap.add_message(request_id, "admin", "approve", "Smoke test reviewed - run it.")
    _audit(f"Admin approved the AI smoke test for {r['tool_name']}", f"revision {review.get('revision', 0)}",
           {"request": request_id})
    _set_status(request_id, "installing", setup=setup)
    return _start(request_id, background)


MAX_SMOKE_REVISIONS = 6


def revise_smoke(request_id: str, admin: str = "admin", feedback: str = "", background: bool = True) -> Dict[str, Any]:
    """The admin tells the AI what to change in the smoke test; it is rewritten and comes back for review."""
    r = copy.deepcopy(ap.get_request(request_id))
    setup = r.get("setup") or {}
    review = setup.get("smoke_review") or {}
    if r["status"] != "smoke_review":
        raise SetupError("There is no smoke test waiting for your review.", 409)
    text = str(feedback or "").strip()
    if not text:
        raise SetupError("Tell the AI what to change in the smoke test.")
    if int(review.get("revision", 0)) >= MAX_SMOKE_REVISIONS:
        raise SetupError(f"The AI already rewrote the smoke test {MAX_SMOKE_REVISIONS} times - approve it, or reject the "
                         f"request and start again with a note for the AI.", 409)
    tv = screen_text(text[:2000], "ADMIN_INSTRUCTION")
    if not tv.is_safe:
        _audit("Model Armor blocked an admin comment on a smoke test", tv.friendly_message,
               {"request": request_id, "pattern": tv.pattern}, level="warn")
        raise SetupError(tv.friendly_message)
    review = dict(review, approved=False, feedback=list(review.get("feedback") or []) + [tv.sanitized])
    setup.update(smoke_review=review)
    ap.add_message(request_id, "admin", "smoke_feedback", tv.sanitized)
    _set_status(request_id, "smoke_review", setup=dict(setup, rewriting=True))

    def job():
        rr = copy.deepcopy(ap.get_request(request_id))
        st = rr.get("setup") or {}
        rv = st.get("smoke_review") or {}
        try:
            spec, _m = _effective(rr)
            files, source, notes, ai_notes = _smoke_files(rr, spec, (st.get("sandbox") or {}).get("kind", "local"),
                                                          (st.get("java") or {}).get("release"), rv.get("feedback"))
        except Exception as e:                                    # pragma: no cover - surfaced to the admin
            files, source, notes, ai_notes = None, "none", [f"{type(e).__name__}: {e}"], ""
        if files is None:
            st.update(rewriting=False, smoke_review=dict(rv, rewrite_error="; ".join(notes)))
            _set_status(request_id, "smoke_review", setup=st)
            ap.add_message(request_id, "ai", "smoke_review", "I could not rewrite the smoke test: " + "; ".join(notes))
            return
        st["smoke_review"] = dict(rv, files=files, source=source, notes=notes, ai_notes=ai_notes, written_at=_now(),
                                  revision=int(rv.get("revision", 0)) + 1, approved=False, rewrite_error=None)
        st["rewriting"] = False
        out_r = _set_status(request_id, "smoke_review", setup=st)
        ap.add_message(request_id, "ai", "smoke_review", "I rewrote the smoke test with your comments"
                       + (f": {ai_notes}" if ai_notes else "") + ". Please review it again.", {"files": sorted(files)})
        _event(out_r, question="Review the rewritten smoke test")
    if background:
        t = threading.Thread(target=job, daemon=True, name=f"smoke-rewrite-{request_id}")
        _JOBS[request_id] = t
        t.start()
    else:
        job()
    return ap.get_request(request_id)


def _record_environment(spec: FrameworkSpec, r: Dict[str, Any]) -> None:
    """8.4: what the new tool needs (the AI stack assistant's runtime requirements) and the runtime versions its smoke
    test passed with - shown in Mission Control > Environments and checked before every run."""
    try:
        from core.wizard import environments
        stack = r.get("stack") or {}
        lang = ((stack.get("selection") or {}).get("language") or spec.language or "").lower()
        reqs = environments.requirements_from_stack(stack.get("suggestion") or {}, lang)
        if reqs:
            ap.set_tool_requirements(spec.id, reqs, "AI stack assistant")
        ap.set_tool_tested_with(spec.id, environments.snapshot(spec.id))
    except Exception as e:                                       # never undo an activation over this
        _audit(f"Could not record the runtime versions of {spec.name}", f"{type(e).__name__}: {e}", level="warn")


def _activate(request_id: str, spec: FrameworkSpec, manifest: Dict[str, Any], setup: Dict[str, Any]) -> None:
    r = ap.get_request(request_id)
    setup.update(status="done", finished_at=_now(), result="ACTIVE")
    try:                                                           # 8.5: what solved it helps the next setup
        from core.wizard import setup_agent
        setup_agent.record_lessons(spec, setup)
    except Exception as e:
        _audit("Could not save the setup agent's lesson", f"{type(e).__name__}: {e}", level="warn")
    smoke_info = {k: v for k, v in (setup.get("smoke") or {}).items() if k != "files"}
    base_spec = FrameworkSpec.from_dict((r.get("plan") or {}).get("spec") or spec.to_dict())
    for k in ("index_url", "local_dir", "interpreter"):
        if (setup.get("overrides") or {}).get(k):
            base_spec.sources[k] = setup["overrides"][k]
    from core.wizard import setup_doctor
    base_spec, _cn = setup_doctor.ensure_companions(base_spec, lookup_versions=False)
    base_spec = setup_doctor.apply_overrides(base_spec, setup.get("overrides") or {})   # 8.3: what the fixes added
    if (setup.get("overrides") or {}).get("skip_post_install"):
        base_spec.post_install = []
    runtime = {"env_vars": dict(manifest.get("env_vars") or {}), "path_prepend": manifest.get("path_prepend") or []}
    java = setup.get("java") or {}
    if java.get("release") and (setup.get("smoke") or {}).get("status") == "PASSED":
        # 8.2: every run uses the JDK and compiler release the smoke test passed with ("invalid target release" fix)
        runtime["java_release"] = int(java["release"])
        if java.get("home"):
            runtime["java_home"] = java["home"]
            runtime["env_vars"].setdefault("JAVA_HOME", java["home"])
    if r.get("kind") == "version":
        pol = ap.tool_version_policy(r["tool_id"])
        ap.set_tool_versions(r["tool_id"], setup["allowed"] or pol["allowed"] + [r["version"]],
                             setup["default"] or pol["default"], set_by=setup["by"])
    else:
        versions = {"allowed": setup["allowed"], "default": setup["default"], "set_by": setup["by"], "set_at": _now()}
        ap.whitelist_tool(base_spec, setup["by"], source=f"AI setup {request_id}", versions=versions,
                          setup={"request": request_id, "summary": (r.get("plan") or {}).get("summary"),
                                 "sources": base_spec.sources, "agent_source": (r.get("plan") or {}).get("agent_source"),
                                 "verified": (setup.get("smoke") or {}).get("status") == "PASSED",
                                 "install": (setup.get("install") or {}).get("status"), "smoke": smoke_info,
                                 "sandbox": (setup.get("sandbox") or {}).get("kind"), "runtime": runtime,
                                 "manifest": {k: manifest.get(k) for k in ("os_dependencies", "env_vars", "path_prepend",
                                                                           "runner_wrapper")},
                                 "smoke_files": (setup.get("smoke") or {}).get("files") or {},
                                 "stack": ((r.get("stack") or {}).get("selection") or None),
                                 "instructions": [i["text"] for i in r.get("instructions") or []]})
        _record_environment(base_spec, r)
        orig = (r.get("stack") or {}).get("original_tool_id")
        if orig and orig != base_spec.id:                      # the tool the user / SOP asked for resolves to it
            ap.add_tool_alias(base_spec.id, orig)
    smoke_word = {"PASSED": "smoke test passed", "SKIPPED": "smoke test skipped (" + str(smoke_info.get("detail")) + ")"
                  }.get(smoke_info.get("status"), "")
    out = _set_status(request_id, "approved", setup=setup, decided_at=_now(), decided_by=setup["by"], clarification=None,
                      admin_note=f"ACTIVE - installed in the {(setup.get('sandbox') or {}).get('kind', 'local')} sandbox, "
                                 f"{smoke_word}")
    ap.add_message(request_id, "system", "active", f"{r['tool_name']}{' ' + r['version'] if r.get('version') else ''} is "
                   f"ACTIVE - {smoke_word}. Waiting sessions are resumed.", {"smoke": smoke_info})
    _audit(f"{r['tool_name']}{' ' + r['version'] if r.get('version') else ''} is now ACTIVE", smoke_word,
           {"request": request_id, "smoke": smoke_info})
    _event(out, "tool.activated")
    try:
        from core.wizard import session as wz
        wz.resume_after_approval(out)
    except Exception as e:                                       # resuming must never undo the activation
        _audit(f"Could not resume the sessions waiting for {r['tool_name']}", f"{type(e).__name__}: {e}", level="warn")


# ---------------------------------------------------------------------------------------------------------------
# Clarification loop
# ---------------------------------------------------------------------------------------------------------------
def _server_facts(spec: FrameworkSpec) -> Dict[str, Any]:
    import os
    import shutil
    bins = ["python3", "node", "npm", "java", "mvn", "ruby", "gem", "go", "dotnet", "google-chrome", "chromium",
            "chromedriver", "gcc"]
    from core.wizard.sandbox import describe as sandbox_describe
    sd = sandbox_describe()
    path = dependency_resolver.toolchain_path()
    eff = sd["effective"]
    from core.wizard import runtime_repair
    browsers = runtime_repair.detect_browsers()
    on_path = {b: bool(shutil.which(b, path=path)) for b in bins}
    on_path["google-chrome"] = on_path["google-chrome"] or bool(browsers.get("chrome"))      # 9.9: chrome.exe on Windows
    on_path["chromium"] = on_path["chromium"] or bool(browsers.get("chromium"))
    return {"on_path": on_path,
            "browsers_installed": {k: v for k, v in browsers.items() if v},
            "env_set": sorted(k for k in ("JAVA_HOME", "MAVEN_HOME", "HTTPS_PROXY", "HTTP_PROXY", "PIP_INDEX_URL",
                                          "NPM_CONFIG_REGISTRY", "PLAYWRIGHT_BROWSERS_PATH") if os.environ.get(k)),
            "sandbox": eff, "os": os.name,
            # 8.3: the AI used to claim "the sandbox is isolated and cannot download browsers" for the LOCAL sandbox
            "network": ("local sandbox (the only one - Understudy runs on Cloud Run, which has no Docker): processes run "
                        "on the server with the server's own network - installs AND the smoke test can download "
                        "(browser drivers included) unless a proxy / firewall blocks it"),
            # 8.3: the AI blamed "npm registry access / proxy" while npm tools installed fine on the same server
            "registries_proven_reachable": sorted({e["spec"].get("ecosystem") for e in ap.whitelist().values()
                                                   if (e.get("setup") or {}).get("verified") and (e.get("spec") or {}).get("ecosystem")}),
            "packages_installed": [f"{p.name} {p.version}".strip() for p in spec.packages],
            "post_install": [" ".join(c) for c in spec.post_install],
            "run_command": " ".join(spec.run_command), "entry_file": spec.entry_file,
            # 9.4: the installs Understudy has for what this tool needs (use_runtime = the id) - and whether each works
            "installed_runtimes": _installed_runtimes_fact(spec)}


def _installed_runtimes_fact(spec: FrameworkSpec) -> List[Dict[str, Any]]:
    try:
        from core.wizard import environments
        return environments.installs_for_prompt(environments.SETUP_KINDS.get(spec.ecosystem, []))
    except Exception:
        return []


def _conversation_text(r: Dict[str, Any], n: int = 12) -> str:
    return "\n".join(f"[{m['role']}] {m['text'][:400]}" for m in (r.get("conversation") or [])[-n:]) or "(none)"


def _failure_vars(r: Dict[str, Any], f: Dict[str, Any]) -> Dict[str, Any]:
    spec, manifest = _effective(r)
    return {"tool_name": r["tool_name"], "ecosystem": spec.ecosystem,
            "manifest": json.dumps({"spec": {k: spec.to_dict()[k] for k in ("packages", "run_command", "post_install",
                                                                            "sources")},
                                    **{k: manifest.get(k) for k in ("env_vars", "path_prepend", "os_dependencies")}},
                            indent=1, default=str),
            "sandbox": json.dumps((r.get("setup") or {}).get("sandbox") or {}, default=str),
            "stage": f.get("stage"), "command": f.get("command") or "-", "exit_code": f.get("exit_code"),
            "output": f.get("output") or f.get("message") or "", "server_facts": json.dumps(_server_facts(spec)),
            "conversation": _conversation_text(r)}


_RULES: List[Tuple[re.Pattern, str, str]] = [
    (re.compile(r"(?i)JAVA_HOME|java: (command )?not found|No Java runtime|'java' is not installed|Unable to locate a Java"),
     "missing_runtime", "java"),
    (re.compile(r"(?i)'(node|npm)' is not installed|(node|npm): (command )?not found|Could not start '(node|npm|npx)'"),
     "missing_runtime", "node"),
    (re.compile(r"(?i)'mvn' is not installed|mvn: (command )?not found|Could not start 'mvn'"), "missing_runtime", "maven"),
    (re.compile(r"(?i)'gradle' is not installed|gradle: (command )?not found|Could not start 'gradle'"), "missing_runtime",
     "gradle"),
    # 9.4: a .NET host without an SDK ('dotnet restore' exits 0x80008091 = 2147516561)
    (re.compile(r"(?i)No \.NET SDKs were found|sdk-not-found|2147516561|0x80008091|'dotnet' is not installed|"
                r"Could not start 'dotnet'|A compatible \.NET SDK was not found"), "missing_runtime", "dotnet"),
    (re.compile(r"(?i)error: command '?(gcc|cc|g\+\+|cl\.exe)'? failed|Microsoft Visual C\+\+ \d|Failed building wheel|"
                r"node-gyp|gyp ERR|make: not found|fatal error: \S+\.h: No such file"), "compiler", ""),
    (re.compile(r"(?i)CERTIFICATE_VERIFY_FAILED|self[- ]signed certificate|unable to get local issuer|UNABLE_TO_GET_ISSUER"),
     "network", "certificate"),
    (re.compile(r"(?i)ProxyError|Tunnel connection failed|407 Proxy|Connection refused|Max retries exceeded|ETIMEDOUT|"
                r"ECONNREFUSED|ECONNRESET|ENOTFOUND|EAI_AGAIN|getaddrinfo|Temporary failure in name resolution|"
                r"Could not resolve host|Network is unreachable|Download failure|ERR_SOCKET|connect EHOSTUNREACH"),
     "network", ""),
    (re.compile(r"(?i)\bE40[13]\b|\b40[13] (Forbidden|Unauthorized)\b|HTTP error 40[13]|status code 40[13]|"
                r"Return code is: 40[13]|authentication required|Unable to authenticate"), "access", ""),
    (re.compile(r"(?i)No matching distribution found|Could not find a version that satisfies|E404|ETARGET|notarget|"
                r"No matching version|404 Not Found|Could not find artifact|Could not resolve dependencies"), "version", ""),
    (re.compile(r"(?i)Permission denied|EACCES|EPERM|Access is denied"), "permission", ""),
    (re.compile(r"(?i)Executable doesn't exist|playwright install|chromedriver|SessionNotCreated|cannot find Chrome binary|"
                r"Chrome failed to start|unable to obtain driver|selenium manager|Unable to obtain|Could not find Chrome|"
                r"Failed to launch the browser|Browser was not found"), "browser", ""),
]


def _opt(id_: str, label: str, effect: str) -> Dict[str, str]:
    return {"id": id_, "label": label, "effect": effect}


def _runtime_question(r: Dict[str, Any], fd: Dict[str, Any]) -> Dict[str, Any]:
    """9.4: 'To set up X I need Y - may I download it?' with the download as a one-click, pre-validated option.
    The admin clicks it, or answers in their own words ('yes', 'use JDK 17', a path ...) and the setup continues."""
    from core.wizard import environments, package_managers as pms
    kind = fd.get("runtime_kind") or ""
    label = environments.KINDS.get(kind, {}).get("label", kind)
    keys = list(fd.get("download_keys") or [])
    others = [m for m in fd.get("missing_runtimes") or [] if m.get("kind") != kind]
    opts: List[Dict[str, Any]] = []
    for i, k in enumerate(keys[:3]):
        opts.append(dict(_opt(f"download_runtime_{i}", ("Yes - download " if i == 0 else "Download ") + pms.label(k)
                              + (" (recommended)" if i == 0 else ""),
                              "official build, checksum verified, into Understudy's runtimes folder; this tool uses it "
                              "and the setup continues"),
                         changes={"download_runtimes": [k] + [m["download_keys"][0] for m in others
                                                              if m.get("download_keys")]}, source="rules"))
    opts.append(_opt("provide_path", f"I'll give you the {label} location", "type the folder (or the program) below"))
    opts.append(_opt("stop", "Stop the setup", "nothing is downloaded"))
    what = pms.label(keys[0]) if keys else label
    also = (" It also needs " + ", ".join(environments.KINDS.get(m["kind"], {}).get("label", m["kind"]) for m in others)
            + " - I'll download those with it.") if others else ""
    q = (f"Admin, to set up {r.get('tool_name') or 'this tool'} I need {label}, and this server has no working one "
         f"({fd.get('why')}). May I download {what}? I'll use it for this tool and continue the setup by myself.{also} "
         "Click the download, or tell me in your own words what to do instead (another version, or where it is).")
    return {"diagnosis": f"{label} is needed: {fd.get('why')}.", "category": "runtime_download", "question": q,
            "options": opts[:6], "agent_source": "LOCAL_HEURISTIC", "recommended": opts[0]["id"] if keys else None}


def _installed_options(kind: str) -> List[Dict[str, Any]]:
    """9.4: one-click options for working installs of a runtime Understudy already knows (Mission Control >
    Environments), e.g. 'Use .NET SDK 8.0.425 (downloaded by Understudy)'."""
    try:
        from core.wizard import environments
        rows = [r for r in environments.inventory().get(kind, []) if environments.runtime_works(kind, r.get("path"))]
    except Exception:
        return []
    out = []
    for i, r in enumerate(rows[:2]):
        lab = f"Use {environments.KINDS[kind]['label']} {r.get('version')}" + (" (downloaded by Understudy)"
                                                                               if r.get("download_key") else "")
        out.append(dict(_opt(f"use_runtime_{i}", lab, f"puts {r.get('bin')} first on the setup's PATH"
                             + (" and sets DOTNET_ROOT" if kind == "dotnet" else "")),
                        changes={"use_runtime": r["id"]}, source="rules"))
    return out


_NOISE_RX = re.compile(r"(?i)complete log of this run|^\W*(npm )?(error|err!)\W*$|^(npm )?(error|err!)\s+(code|path|command|errno|syscall)\b"
                       r"|^(npm )?(error|err!)\s+at |^\s*at |node\.js v\d|^\W*\^\W*$|throw er;|^[{}]$")
_SPECIFIC_RX = re.compile(r"\b(E[A-Z]{3,}|E\d{3}|Forbidden|Unauthorized|ModuleNotFoundError|No matching distribution|Could not find|not found|"
                          r"denied|refused|timed? ?out|JAVA_HOME|certificate|SSL)\b")
_ERRORISH_RX = re.compile(r"(?i)error|failed|denied|not found|refused|timed? ?out|exception|missing")


def key_error_line(text: str) -> str:
    """The most telling line of a failed command's output (skips npm/pip boilerplate and stack frames)."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    useful = [ln for ln in lines if not _NOISE_RX.search(ln)]
    for pool in ([ln for ln in useful if _SPECIFIC_RX.search(ln)], [ln for ln in useful if _ERRORISH_RX.search(ln)], useful):
        if pool:
            return pool[-1]
    return lines[-1] if lines else ""


def diagnose(f: Dict[str, Any], spec: FrameworkSpec) -> Dict[str, Any]:
    """Deterministic clarification (offline path, and the safety net when the AI's answer is unusable)."""
    text = f"{f.get('message', '')}\n{f.get('output', '')}"
    dep = (f.get("dependency") or {}).get("value", "")
    missing = f.get("toolchain_missing") or []
    category, sub = "unknown", ""
    if f.get("stage") == "sandbox":
        category = "sandbox"
    elif dep in ("java", "JAVA_HOME") or "java" in missing:
        category, sub = "missing_runtime", "java"
    elif dep in ("node", "npm") or set(missing) & {"node", "npm"}:
        category, sub = "missing_runtime", "node"
    elif dep == "mvn" or "mvn" in missing:
        category, sub = "missing_runtime", "maven"
    elif dep == "gradle" or "gradle" in missing:
        category, sub = "missing_runtime", "gradle"
    elif dep in ("ruby", "gem", "bundle") or set(missing) & {"ruby", "gem"}:
        category, sub = "missing_runtime", "ruby"
    elif dep == "go" or "go" in missing:
        category, sub = "missing_runtime", "go"
    elif dep == "dotnet" or "dotnet" in missing:
        category, sub = "missing_runtime", "dotnet"
    elif f.get("stage") == "os_dependencies":
        category, sub = "missing_runtime", dep or (missing[0] if missing else "")
    else:
        for rx, cat, s in _RULES:
            if rx.search(text):
                category, sub = cat, s
                break
        if category == "unknown" and f.get("stage") == "smoke_test":
            category = "smoke_test"
    line = _scrub(key_error_line(text))[:240]
    tool = spec.name
    retry = _opt("retry", "I fixed it on the server - try again", "re-runs the failed step unchanged")
    if category == "sandbox":
        q = f"Admin, the {tool} setup can't start its sandbox: {f.get('message')}. Can you fix it on the server?"
        opts = [retry]
    elif category == "missing_runtime" and sub == "java":
        q = (f"Admin, the installation of {tool} failed because Java isn't available (JAVA_HOME is not set) on this "
             f"server. Should I download OpenJDK dynamically, or can you provide the local path?")
        opts = [_opt("download_jdk", "Download OpenJDK 17 (Eclipse Temurin)",
                     "downloads it into Understudy's runtimes folder (checksum verified) and sets JAVA_HOME"),
                _opt("provide_path", "I'll give you the JDK path", "type the JDK folder, e.g. /usr/lib/jvm/java-17"), retry]
    elif category == "missing_runtime" and sub == "node":
        q = f"Admin, {tool} needs Node.js and npm, which aren't on this server's PATH. Should I download Node.js 20, or can you provide its path?"
        opts = [_opt("download_node", "Download Node.js 20 LTS", "downloads it into Understudy's runtimes folder (checksum verified)"),
                _opt("provide_path", "I'll give you the Node.js path", "type the folder that contains node"), retry]
    elif category == "missing_runtime" and sub in ("maven", "gradle"):
        what = {"maven": "Apache Maven", "gradle": "Gradle"}[sub]
        q = (f"Admin, {tool} needs {what}, which isn't installed or not on PATH here. Should I download {what} "
             f"(checksum verified, registered for the whole server), or can you give me its location?")
        opts = [_opt(f"download_{sub}", f"Download {what}", "downloads it into Understudy's runtimes folder and registers "
                     "it in Mission Control > Package managers"),
                _opt("provide_path", f"I'll give you the {what} path", "type the folder that contains bin/"
                     + ("mvn" if sub == "maven" else "gradle")), retry]
    elif category == "missing_runtime" and sub in ("ruby", "go", "dotnet"):
        what = {"ruby": "Ruby (with RubyGems)", "go": "Go", "dotnet": "the .NET SDK"}[sub]
        have = _installed_options(sub)
        if have:
            q = (f"Admin, {tool} needs {what}, and the one on this server's PATH can't build "
                 f"{'(it has no SDK)' if sub == 'dotnet' else ''}. Understudy already has a working one - use it?")
        else:
            q = (f"Admin, {tool} needs {what}, which isn't on this server. Should I download it (official build, "
                 f"checksum verified, registered for the whole server), or can you give me its location?")
        opts = have + [_opt(f"download_{sub}", f"Download {what}", "downloads it into Understudy's runtimes folder, "
                            "registers it in Mission Control > Package managers and checks it starts"),
                       _opt("provide_path", f"I'll give you the {what} path", "type the folder (or the program) - e.g. "
                            + ("E:\\understudy\\generated\\runtimes\\dotnet-8" if sub == "dotnet" else "/opt/" + sub)), retry]
        opts = opts[:6]
    elif category == "missing_runtime":
        what = {"maven": "Maven (mvn)"}.get(sub, sub or "a required program")
        q = f"Admin, {tool} needs {what}, which isn't installed or not on PATH here. Can you give me its location?"
        opts = [_opt("provide_path", f"I'll give you the {what} path", "type the folder that contains it"), retry]
    elif category == "compiler":
        q = (f"Admin, a package of {tool} needs a C/C++ compiler to build a native extension ('{line}'). Should I pin a "
             f"version that ships pre-built binaries, use your internal package mirror, or will you install build tools?")
        opts = [_opt("pin_version", "Use another version", "type the version to install"),
                _opt("use_index_url", "Use our package mirror", "type the mirror URL (e.g. your Nexus)"), retry]
    elif category == "network":
        cert = sub == "certificate"
        q = (f"Admin, the {tool} download was blocked ({line or 'network error'}). "
             + ("The server doesn't trust the TLS certificate - set SSL_CERT_FILE / REQUESTS_CA_BUNDLE / "
                "NODE_EXTRA_CA_CERTS on the server, or give me an internal mirror. " if cert else "")
             + "Should I pull the packages from your internal repository (e.g. Nexus), or go through a proxy?")
        opts = [_opt("use_index_url", "Use our internal repository", "type the repository URL, e.g. https://nexus.corp.local/repository/npm-group/"),
                _opt("use_proxy", "Use a proxy", "type the proxy URL, e.g. http://proxy.corp.local:3128"), retry]
    elif category == "access":
        q = (f"Admin, the package repository refused the {tool} download ({line or 'HTTP 401/403'}). Does it need "
             f"credentials, or should I use another repository URL or go through a proxy?")
        opts = [_opt("use_index_url", "Use another repository", "type its URL, e.g. https://nexus.corp.local/repository/npm-group/"),
                _opt("use_proxy", "Use a proxy", "type the proxy URL, e.g. http://proxy.corp.local:3128"),
                _opt("credentials", "It needs credentials", "set them on the server (e.g. ~/.npmrc or pip.conf) - never type secrets here"),
                retry]
    elif category == "version":
        q = f"Admin, the package registry doesn't have the requested {tool} version ('{line}'). Which version should I install?"
        opts = [_opt("pin_version", "Install another version", "type the version"),
                _opt("use_index_url", "Use another package repository", "type its URL"), retry]
    elif category == "permission":
        q = f"Admin, the {tool} setup hit a permission error ('{line}'). Can you fix the folder permissions on the server?"
        opts = [retry]
    elif category == "browser":
        q = (f"Admin, {tool} installed but couldn't start its browser / driver ('{line}'). The sandbox has the server's "
             "network, so this is usually a missing Chrome / a proxy. Can you give me the path to Chrome (or chromedriver)?")
        opts = [_opt("provide_path", "I'll give you the browser / driver path", "type the path to chrome or chromedriver"),
                retry]
    elif category == "smoke_test":
        q = (f"Admin, {tool} installed, but its smoke test failed ('{line or f.get('message')}'). Should I write a new "
             f"smoke test (you review it again), or is something on the server missing?")
        opts = [_opt("retry", "Rewrite the smoke test", "the AI rewrites the test with this output; you review it before it runs"),
                _opt("rerun_smoke", "Run the same smoke test again", "keeps the reviewed test (e.g. after you fixed the server)"),
                _opt("provide_path", "Something is missing - here is the path", "type the path")]
    else:
        q = (f"Admin, the {tool} setup failed at '{f.get('stage')}' ({line or f.get('message')}). What should I change? "
             f"You can give me a path, a repository URL, a version, or tell me to retry.")
        opts = [retry]
    return {"diagnosis": f"{f.get('message')}" + (f" - {line}" if line and line not in f.get("message", "") else ""),
            "category": category, "question": q, "options": opts, "agent_source": "LOCAL_HEURISTIC"}


_SKIP_SMOKE_RX = re.compile(r"(?i)\bskip\w*\b.{0,30}\b(smoke|test|verification|validation)|\b(smoke|test)\w*\b.{0,20}\bskip|"
                            r"\bwithout (the )?(smoke )?test|\bproceed (anyway|without)|\bforce[_ ]?(activate|approve)")


def _asks_to_skip_smoke(text: str) -> bool:
    t = (text or "").replace("_", " ")
    return bool(_SKIP_SMOKE_RX.search(t)) and "browser download" not in t


def _clarify(r: Dict[str, Any], f: Dict[str, Any]) -> Dict[str, Any]:
    spec, _m = _effective(r)
    base = diagnose(f, spec)
    if not _ai_on():
        return base
    from core.wizard.llm import generate_json
    res = generate_json("TOOL_SETUP_CLARIFY", _failure_vars(r, f), untrusted=["output", "conversation", "manifest"])
    if not res.ok:
        return dict(base, notes=[f"AI unavailable ({res.error}) - Understudy's own diagnosis"])
    d = res.data or {}
    opts = []
    for o in (d.get("options") or [])[:5]:
        if isinstance(o, dict) and o.get("id") and o.get("label"):
            oid = re.sub(r"[^a-z0-9_]", "", str(o["id"]).lower())[:40]
            if oid in ("use_docker", "smoke_network"):
                continue                                  # 9.9.1: no Docker sandbox on Cloud Run
            if _asks_to_skip_smoke(oid + " " + str(o.get("label"))):
                continue                                  # a tool is never ACTIVE without a passing smoke test
            opt = _opt(oid, str(o["label"])[:120], str(o.get("effect") or "")[:200])
            # 8.3: an option can carry the exact change it makes (e.g. add @wdio/local-runner) - clicking it applies
            # that change. Before, "add the missing packages?" could never be acted on.
            if isinstance(o.get("changes"), dict) and o["changes"]:
                ch, probs = _validate_changes(o["changes"], spec, json.dumps(o["changes"]), trusted=True)
                if ch and not probs:
                    opt["changes"] = ch
            opts.append(opt)
    if not any(o["id"] == "retry" for o in opts):
        opts.append(_opt("retry", "I fixed it on the server - try again", "re-runs the failed step unchanged"))
    if not d.get("question") or len(opts) < 2:
        return base
    out = {"diagnosis": _scrub(str(d.get("diagnosis") or base["diagnosis"]))[:800],
           "category": str(d.get("category") or base["category"])[:30], "question": str(d["question"])[:800],
           "options": opts, "agent_source": res.agent_source, "model": res.model}
    af = d.get("auto_fix") if isinstance(d.get("auto_fix"), dict) else None
    if af and isinstance(af.get("changes"), dict):
        ch, probs = _validate_changes(af["changes"], spec, json.dumps(af["changes"]), trusted=True)
        if ch and not probs:
            # ask mode shows every validated change for the admin's click; automatic mode applies only the safe keys
            out["auto_fix"] = {"changes": ch, "summary": str(af.get("summary") or "")[:400],
                               "key": "ai:" + json.dumps(ch, sort_keys=True)[:200]}
    return out


AUTO_FIX_KEYS = ("add_packages", "post_install", "post_install_remove", "rewrite_smoke")


def _try_auto_fix(request_id: str, r: Dict[str, Any], setup: Dict[str, Any], fd: Dict[str, Any],
                  ai_fix: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """8.3: a failure with a concrete, safe fix is fixed by Understudy itself (deterministic rules first, then the
    AI's auto_fix), at most MAX_AUTO_FIXES times per setup, each fix only once. Returns the fix applied, or None."""
    from core.wizard import setup_doctor
    auto = setup.setdefault("auto_fixes", [])
    if len(auto) >= setup_doctor.MAX_AUTO_FIXES:
        return None
    tried = [a.get("key") for a in auto]
    try:
        spec, _m = _effective(r)
    except Exception:
        return None
    fix = ai_fix if ai_fix is not None else setup_doctor.known_fix(fd, spec, fd.get("runner") or "local", tried)
    if not fix or fix.get("key") in tried or fix.get("needs_admin"):
        return None
    changes, problems = _validate_changes(fix["changes"], spec, json.dumps(fix["changes"]), trusted=True)
    changes = {k: v for k, v in changes.items() if k in AUTO_FIX_KEYS}
    if not changes or problems:
        return None
    _merge_changes(setup, changes, fd)
    auto.append({"key": fix["key"], "changes": changes, "at": _now(), "stage": fd.get("stage"),
                 "source": "ai" if ai_fix is not None else "rules"})
    setup.update(status="installing", resumed_at=_now(), failure=fd, message=None)
    ap.add_message(request_id, "system", "failure", f"{fd.get('stage', '').replace('_', ' ')} failed: {fd.get('message')}",
                   {"output": fd.get("output", "")[-3000:], "command": fd.get("command"), "exit_code": fd.get("exit_code")})
    ap.add_message(request_id, "ai", "auto_fix", "Fixed automatically: " + (fix.get("summary") or "applied a fix") +
                   f" (automatic fix {len(auto)} of {setup_doctor.MAX_AUTO_FIXES})",
                   {"changes": changes, "agent_source": "ACTUAL_AI_API" if ai_fix is not None else "LOCAL_HEURISTIC"})
    _audit(f"Setup of {r['tool_name']} fixed itself", fix.get("summary") or "", {"request": request_id, "changes": changes})
    _set_status(request_id, "installing", setup=setup, clarification=None)
    return fix


def _merge_changes(setup: Dict[str, Any], changes: Dict[str, Any], fd: Optional[Dict[str, Any]] = None) -> None:
    """Puts validated changes into the setup's overrides (shared by answers and automatic fixes)."""
    ov = setup.setdefault("overrides", {})
    for k, v in changes.items():
        if k == "env_vars":
            ov.setdefault("env_vars", {}).update(v)
        elif k in ("path_prepend", "download_runtimes"):
            ov[k] = list(dict.fromkeys(list(v) + list(ov.get(k) or [])))
        elif k == "add_packages":
            have = {p["name"].lower() for p in ov.get("add_packages") or []}
            ov["add_packages"] = list(ov.get("add_packages") or []) + [p for p in v if p["name"].lower() not in have]
        elif k == "post_install":
            ov["post_install"] = list(ov.get("post_install") or []) + [c for c in v if c not in (ov.get("post_install") or [])]
        elif k == "post_install_remove":
            ov["post_install_remove"] = list(ov.get("post_install_remove") or []) + [c for c in v if c not in (ov.get("post_install_remove") or [])]
            ov["post_install"] = [c for c in ov.get("post_install") or [] if c not in v]
        elif k == "smoke_files":                                  # 8.5: approved together with the agent's fix
            review = setup.get("smoke_review") or {}
            setup["smoke_review"] = {"files": dict(v), "source": "agent", "notes": ["Written by the setup agent and "
                                     "approved with its fix."], "ai_notes": "", "written_at": _now(), "approved": True,
                                     "approved_by": review.get("approved_by") or "admin", "approved_at": _now(),
                                     "revision": int(review.get("revision", -1)) + 1,
                                     "feedback": list(review.get("feedback") or []),
                                     "previous_failure": ((fd or {}).get("output") or "")[-2000:]}
        elif k == "rewrite_smoke":
            review = setup.get("smoke_review") or {}
            setup["smoke_review"] = dict(review, approved=False,
                                         feedback=list(review.get("feedback") or []) + ["[Understudy] " + str(v)],
                                         previous_failure=((fd or {}).get("output") or "")[-2000:])
        else:
            ov[k] = v


def _halt(request_id: str, setup: Dict[str, Any], f: SetupFailure) -> None:
    r = copy.deepcopy(ap.get_request(request_id))
    r["setup"] = setup
    fd = f.to_dict()
    fd["runner"] = (setup.get("sandbox") or {}).get("kind")
    auto = ap.setup_fix_approval() == "automatic"
    if auto and _try_auto_fix(request_id, r, setup, fd):        # 8.3: known failure -> fixed without asking
        _setup_job(request_id)                                   # bounded: each fix once, MAX_AUTO_FIXES in total
        return
    setup["round"] = int(setup.get("round", 0)) + 1
    setup.update(status="awaiting_admin", failure=fd, message=f.message)
    ap.add_message(request_id, "system", "failure", f"{f.stage.replace('_', ' ')} failed: {f.message}",
                   {"output": fd.get("output", "")[-3000:], "command": fd.get("command"), "exit_code": fd.get("exit_code")})
    if f.stage == "runtime_missing":                         # 9.4: the fix is known - ask to approve the download
        q = _runtime_question(r, fd)
        q.update(asked_at=_now(), round=setup["round"], stage=f.stage, failure=fd)
        out = _set_status(request_id, "awaiting_admin", setup=setup, clarification=q)
        ap.add_message(request_id, "ai", "question", q["question"],
                       {"options": q["options"], "diagnosis": q.get("diagnosis"), "category": q.get("category"),
                        "agent_source": q.get("agent_source")})
        _audit(f"Setup of {r['tool_name']} needs a runtime download", q["question"], {"request": request_id}, level="warn")
        _event(out, question=q["question"])
        return
    try:
        q = _agent_question(request_id, r, setup, fd)          # 8.5: the agent stays on it
        if q and auto and _apply_agent_fix_automatically(request_id, setup, q):
            ap.update_request(request_id, setup=setup)
            _setup_job(request_id)
            return
        q = q or _clarify(r, fd)
        if auto and q.get("auto_fix") and _try_auto_fix(request_id, r, setup, fd, q["auto_fix"]):
            setup["round"] = max(0, int(setup.get("round", 1)) - 1)
            ap.update_request(request_id, setup=setup)
            _setup_job(request_id)
            return
        # 8.3 human in the loop (default): the AI's / Understudy's fix is the FIRST option - one click applies exactly
        # the change shown; nothing happens until the admin decides.
        proposed = [] if q.get("agent") else \
            [o for o in (_proposed_fix_option(r, fd, q.pop("auto_fix", None), include_auto_keys=not auto),
                         _admin_fix_option(r, fd)) if o]
        for opt in reversed(proposed):
            if not any(o["id"] == opt["id"] for o in q.get("options") or []):
                q["options"] = [opt] + list(q.get("options") or [])
        if proposed:
            q["proposed_fix"] = proposed[0]
    except Exception as e:                                       # the loop must always reach the admin
        q = {"diagnosis": f.message, "category": "unknown", "agent_source": "LOCAL_HEURISTIC",
             "question": f"Admin, the setup failed at '{f.stage}': {f.message}. What should I do?",
             "options": [_opt("retry", "Try again", "re-runs the failed step")], "notes": [f"{type(e).__name__}: {e}"]}
    q.update(asked_at=_now(), round=setup["round"], stage=f.stage, failure=fd)
    out = _set_status(request_id, "awaiting_admin", setup=setup, clarification=q)
    ap.add_message(request_id, "ai", "question", q["question"],
                   {"options": q["options"], "diagnosis": q.get("diagnosis"), "category": q.get("category"),
                    "agent_source": q.get("agent_source"), "agent": q.get("agent")})
    _audit(f"Setup of {r['tool_name']} needs the admin: {f.stage}", q["question"], {"request": request_id}, level="warn")
    _event(out, question=q["question"])


def _agent_question(request_id: str, r: Dict[str, Any], setup: Dict[str, Any], fd: Dict[str, Any],
                    model: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """8.5: the setup agent works the failure (read-only checks, a pre-checked fix) - unless Understudy's own rules
    already have an untried fix for it (fast and certain; the agent takes over if that doesn't solve it)."""
    from core.wizard import setup_agent, setup_doctor
    if not setup_agent.enabled() or not _ai_on():
        return None
    try:
        spec, _m = _effective(r)
        tried = [a.get("key") for a in setup.get("auto_fixes") or []]
        rule = setup_doctor.known_fix(fd, spec, fd.get("runner") or "local", tried)
        if rule and rule.get("key") not in tried:
            return None
        setup["status"] = "agent_working"
        _set_status(request_id, "installing", setup=setup, clarification=None)

        def say(text: str, data: Dict[str, Any]) -> None:
            ap.add_message(request_id, "ai", "agent_step", text, dict(data, agent_source="ACTUAL_AI_API"))
            _event(ap.get_request(request_id))
        r2 = dict(r, setup=setup)
        q = setup_agent.turn(request_id, r2, setup, fd, say, model=model)
        setup["status"] = "awaiting_admin"
        return q
    except Exception as e:                                       # never let the agent block the admin's question
        setup["status"] = "awaiting_admin"
        _audit("The setup agent failed - falling back to the one-shot question", f"{type(e).__name__}: {e}",
               {"request": request_id}, level="warn")
        return None


def _apply_agent_fix_automatically(request_id: str, setup: Dict[str, Any], q: Dict[str, Any]) -> bool:
    """'automatic' fix mode: a fix that only adds packages / install steps is applied without asking (bounded like
    every automatic fix); a corrected test or another tool version always waits for the admin."""
    from core.wizard import setup_agent, setup_doctor
    fx = (q.get("agent") or {}).get("fix") or {}
    ch = fx.get("changes") or {}
    auto = setup.setdefault("auto_fixes", [])
    if not ch or set(ch) - set(setup_agent.AUTOMATIC_KEYS) or len(auto) >= setup_doctor.MAX_AUTO_FIXES \
            or fx.get("key") in [a.get("key") for a in auto]:
        return False
    _merge_changes(setup, ch, setup.get("failure"))
    auto.append({"key": fx["key"], "changes": ch, "at": _now(), "stage": (setup.get("failure") or {}).get("stage"),
                 "source": "agent"})
    setup_agent.mark_applied(setup, fx["key"], "automatic")
    setup.update(status="installing", resumed_at=_now(), message=None)
    ap.add_message(request_id, "ai", "auto_fix", "Fixed automatically by the setup agent: " + fx.get("summary", ""),
                   {"changes": ch, "agent_source": "ACTUAL_AI_API"})
    _set_status(request_id, "installing", setup=setup, clarification=None)
    return True


def agent_again(request_id: str, admin: str = "admin", hint: str = "", background: bool = True,
                stronger: bool = False) -> Dict[str, Any]:
    """8.5: the admin rejected the agent's proposal (or has a hint): the agent investigates the same failure again
    with the hint - nothing is changed and the setup isn't re-run until the admin approves a fix. 8.5.1: ``stronger``
    - the admin chose the stronger model (Mission Control setting) for this look; it is never chosen automatically."""
    import threading
    r = copy.deepcopy(ap.get_request(request_id))
    if r["status"] != "awaiting_admin" or not r.get("clarification"):
        raise SetupError("The setup isn't waiting for an answer.", 409)
    from core.wizard import setup_agent
    setup = r.get("setup") or {}
    fd = (r["clarification"] or {}).get("failure") or setup.get("failure") or {}
    model = setup_agent.escalation_model() if stronger else None
    if stronger and not model:
        raise SetupError("No stronger model is set - choose one in Mission Control > Self-healing > Setup agent.", 409)
    setup_agent.add_feedback(setup, hint)
    ap.add_message(request_id, "admin", "answer", (f"[Look again with {model}] " if model else "[Look again] ")
                   + (hint or "(no hint)"), {"option": "agent_retry_stronger" if model else "agent_retry"})
    setup["status"] = "agent_working"
    out = _set_status(request_id, "installing", setup=setup, clarification=None)

    def job():
        q = _agent_question(request_id, dict(r, setup=setup), setup, fd, model) or _clarify(dict(r, setup=setup), fd)
        q.update(asked_at=_now(), round=setup.get("round"), stage=fd.get("stage"), failure=fd)
        res = _set_status(request_id, "awaiting_admin", setup=setup, clarification=q)
        ap.add_message(request_id, "ai", "question", q["question"],
                       {"options": q["options"], "diagnosis": q.get("diagnosis"), "agent_source": q.get("agent_source"),
                        "agent": q.get("agent")})
        _event(res, question=q["question"])
    if background:
        threading.Thread(target=job, daemon=True, name=f"setup-agent-{request_id}").start()
    else:
        job()
        out = copy.deepcopy(ap.get_request(request_id))
    return out


def _proposed_fix_option(r: Dict[str, Any], fd: Dict[str, Any], ai_fix: Optional[Dict[str, Any]],
                         include_auto_keys: bool = True) -> Optional[Dict[str, Any]]:
    """The fix Understudy's rules or the AI found, as a one-click option for the admin (ask mode)."""
    from core.wizard import setup_doctor
    try:
        spec, _m = _effective(r)
    except Exception:
        return None
    tried = [a.get("key") for a in (r.get("setup") or {}).get("auto_fixes") or []]
    fix = setup_doctor.known_fix(fd, spec, fd.get("runner") or "local", tried) if include_auto_keys else None
    if fix and fix.get("needs_admin"):
        fix = None                                    # offered by _admin_fix_option
    source = "rules"
    if not fix and ai_fix:
        fix, source = ai_fix, "ai"
    if not fix:
        return None
    ch, probs = _validate_changes(fix["changes"], spec, json.dumps(fix["changes"]), trusted=True)
    if not ch or probs:
        return None
    from core.wizard.synth_labels import describe
    return dict(_opt("apply_ai_fix", "Apply the AI's fix: " + describe(ch), (fix.get("summary") or "")[:200]),
                changes=ch, fix_key=fix.get("key") or ("ai:" + json.dumps(ch, sort_keys=True)[:120]), source=source)


def _admin_fix_option(r: Dict[str, Any], fd: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """A deterministic fix that needs the admin's OK (e.g. network for the smoke test), as a one-click option."""
    from core.wizard import setup_doctor
    try:
        spec, _m = _effective(r)
    except Exception:
        return None
    tried = [a.get("key") for a in (r.get("setup") or {}).get("auto_fixes") or []]
    fix = setup_doctor.known_fix(fd, spec, fd.get("runner") or "local", [])
    if not fix:
        return None
    ch, probs = _validate_changes(fix["changes"], spec, json.dumps(fix["changes"]), trusted=True)
    if not ch or probs:
        return None
    if fix.get("needs_admin") or fix.get("key") in tried:
        label = "Apply the suggested fix"
        if "add_packages" in ch:
            label = "Add " + ", ".join(p["name"] for p in ch["add_packages"]) + " and install again"
        return dict(_opt(re.sub(r"[^a-z0-9_]", "_", next(iter(ch)))[:40], label, fix.get("summary", "")[:200]), changes=ch)
    return None


def _install_at(path: str) -> Optional[Dict[str, Any]]:
    """The install from Mission Control > Environments at this folder / program, if any."""
    import os
    try:
        from core.wizard import environments
        p = os.path.normcase(os.path.abspath(path))
        for rows in environments.inventory().values():
            for r in rows:
                for cand in (r.get("path"), r.get("bin"), r.get("home")):
                    if cand and os.path.normcase(os.path.abspath(cand)) == p:
                        return r
    except Exception:
        return None
    return None


_ABS_PATH_RX = re.compile(r"(?<![\w:/.])((?:/[A-Za-z0-9._+-]+)+/?|[A-Za-z]:\\[A-Za-z0-9 ._\\+-]+)")
_PROXY_RX = re.compile(r"^https?://[A-Za-z0-9.-]+(:\d{1,5})?/?$")


def _resolve_offline(r: Dict[str, Any], option: str, text: str) -> Dict[str, Any]:
    """Deterministic reading of the admin's answer."""
    import os
    q = r.get("clarification") or {}
    cat = q.get("category", "")
    low = (text or "").lower()
    urls = [u.rstrip(".,;)") for u in _URL_RX.findall(text or "")]
    paths = [p for p in _ABS_PATH_RX.findall(_URL_RX.sub(" ", text or "")) if len(p) > 1]
    versions = _VER_RX.findall(_URL_RX.sub(" ", text or ""))
    ch: Dict[str, Any] = {}
    if option in ("stop", "give_up") or re.search(r"\b(stop|give up|cancel)\b", low):
        return {"next": "stop", "summary": "Stopping the setup as you asked.", "changes": {}}
    if cat == "runtime_download" and not paths:
        # 9.4: 'yes' / 'go ahead' approves the recommended download; 'use 17' / 'node 22' picks that version
        dl = [o for o in q.get("options") or [] if (o.get("changes") or {}).get("download_runtimes")]
        nums = re.findall(r"\b(\d{1,2})\b", low)
        pick = next((o for o in dl for n in nums if re.search(rf"\b{n}\b", o.get("label", ""))), None)
        if not pick and dl and re.search(r"\b(yes|ok|okay|sure|go ahead|approve|approved|proceed|download|do it|fine)\b", low):
            pick = next((o for o in dl if o["id"] == q.get("recommended")), dl[0])
        if pick:
            return {"next": "retry", "summary": f"Downloading as you said: {pick['label']}.",
                    "changes": dict(pick["changes"])}
    from core.wizard import runtime_catalog

    def line(prefix: str, default: str) -> str:            # 9.9: any release line the admin names ("jdk 11")
        n = re.search(r"\b(\d{1,2})\b", low)
        return f"{prefix}{n.group(1)}" if n and runtime_catalog.parse(f"{prefix}{n.group(1)}") else default
    if option == "download_jdk" or (re.search(r"\bdownload\b", low) and re.search(r"\b(jdk|java|openjdk)\b", low)):
        ch["download_runtime"] = line("jdk-", "jdk-17")
    elif option == "download_node" or (re.search(r"\bdownload\b", low) and "node" in low):
        ch["download_runtime"] = line("node-", "node-20")
    elif option == "download_gradle" or (re.search(r"\bdownload\b", low) and "gradle" in low):
        ch["download_runtime"] = line("gradle-", "gradle-8")
    elif option == "download_maven" or (re.search(r"\bdownload\b", low) and re.search(r"\b(maven|mvn)\b", low)):
        ch["download_runtime"] = "maven-3"
    elif option == "download_ruby" or (re.search(r"\bdownload\b", low) and re.search(r"\b(ruby|gem|rubygems)\b", low)):
        ch["download_runtime"] = "ruby-3"
    elif option == "download_go" or (re.search(r"\bdownload\b", low) and re.search(r"\b(go|golang)\b", low)):
        ch["download_runtime"] = "go-1"
    elif option == "download_dotnet" or (re.search(r"\bdownload\b", low) and re.search(r"\.net|dotnet", low)):
        ch["download_runtime"] = "dotnet-8"
    elif option == "skip_browser_download":
        ch["skip_post_install"] = True
    if (option in ("use_proxy",) or "proxy" in low) and urls:
        if not _PROXY_RX.match(urls[0]):
            return {"next": "ask", "summary": "", "follow_up": {"question": "Give me the proxy as http://host:port, without a "
                                                                             "user name or password (configure those on the server).",
                                                                 "options": []}}
        ch["env_vars"] = {"HTTPS_PROXY": urls[0], "HTTP_PROXY": urls[0]}
    elif urls and (option in ("use_index_url", "") or cat in ("network", "version", "compiler")):
        ch["index_url"] = urls[0] if urls[0].endswith("/") else urls[0] + "/"
    if versions and (option == "pin_version" or cat in ("version", "compiler") or re.search(r"\bversion\b", low)):
        ch["version"] = versions[0]
    if paths and not ch.get("download_runtime"):
        p = paths[0].rstrip("/\\") or paths[0]
        base = os.path.basename(p).lower()
        rec = _install_at(p)
        if rec:                                    # 9.4: a runtime Understudy knows -> exactly its settings
            ch["use_runtime"] = rec["id"]
            paths = []
    if paths and not ch.get("download_runtime"):
        p = paths[0].rstrip("/\\") or paths[0]
        base = os.path.basename(p).lower()
        if base in ("dotnet", "dotnet.exe") or (os.path.isdir(p) and any(os.path.isfile(os.path.join(p, n))
                                                                          for n in ("dotnet", "dotnet.exe"))):
            d = os.path.dirname(p) if base in ("dotnet", "dotnet.exe") else p
            ch["path_prepend"] = [d]
            ch["env_vars"] = {"DOTNET_ROOT": d, "DOTNET_MULTILEVEL_LOOKUP": "0"}
            paths = []
    if paths and not ch.get("download_runtime"):
        p = paths[0].rstrip("/\\") or paths[0]
        base = os.path.basename(p).lower()
        if re.match(r"^python(3(\.\d{1,2})?)?(\.exe)?$", base) or option == "use_interpreter":
            ch["interpreter"] = p
        elif base in ("java", "java.exe") and os.path.basename(os.path.dirname(p)) == "bin":
            ch["env_vars"] = {"JAVA_HOME": os.path.dirname(os.path.dirname(p))}
            ch["path_prepend"] = [os.path.dirname(p)]
        elif cat == "missing_runtime" and (q.get("failure") or {}).get("dependency", {}).get("value") in ("java", "JAVA_HOME") \
                or re.search(r"(?i)jdk|jvm|java", p):
            ch["env_vars"] = {"JAVA_HOME": p}
            ch["path_prepend"] = [os.path.join(p, "bin")]
        elif base in ("chrome", "google-chrome", "chromium", "chromium-browser", "chrome.exe"):
            ch["env_vars"] = {"CHROME_BIN": p}
        elif base in ("chromedriver", "chromedriver.exe", "node", "node.exe", "mvn", "mvn.cmd", "gradle", "gradle.bat"):
            ch["path_prepend"] = [os.path.dirname(p)]
        else:
            ch["path_prepend"] = [os.path.join(p, "bin") if os.path.isdir(os.path.join(p, "bin")) else p]
    if option == "credentials" and not ch:
        return {"next": "ask", "summary": "", "follow_up": {
            "question": "Please put the repository credentials on the Understudy server - ~/.npmrc for npm, pip.conf "
                        "for pip, ~/.m2/settings.xml for Maven - never in this chat. Reply 'retry' when they're in place.",
            "options": [_opt("retry", "Credentials are set - try again", "re-runs the install")]}}
    if not ch and not (option in ("retry", "rerun_smoke") or
                       re.search(r"\b(retry|try again|done|fixed|installed|go ahead|yes|rewrite)\b", low)):
        return {"next": "ask", "summary": "", "follow_up": {
            "question": "I couldn't find a path, URL, version or one of the options in your answer. Could you give me "
                        "one of those - or reply 'retry' after fixing it on the server?",
            "options": q.get("options") or []}}
    what = ", ".join(f"{k}={v}" for k, v in ch.items()) if ch else "no changes"
    return {"next": "retry", "summary": f"Retrying the setup ({what}).", "changes": ch}


def _validate_changes(ch: Dict[str, Any], spec: FrameworkSpec, answer_text: str,
                      trusted: bool = False) -> Tuple[Dict[str, Any], List[str]]:
    """Only safe, real changes survive: paths must exist, URLs must come from the admin's answer, etc.
    trusted=True: the change comes from Understudy's own rules or an option payload already validated - only the
    package-level keys (add_packages, post_install, rewrite_smoke, download_runtime) are accepted then
    without the admin having typed them."""
    from core.wizard import setup_doctor
    import os
    from core.wizard import runtimes
    from core.wizard.frameworks import _INDEX_RE, validate_interpreter
    from core.wizard.sandbox import SandboxError, clean_manifest_env
    out: Dict[str, Any] = {}
    problems: List[str] = []
    text = answer_text or ""
    for k, v in (ch or {}).items():
        if k == "index_url":
            v = str(v)
            if v.rstrip("/") not in text and v not in text:
                problems.append("The repository URL must be the one you typed.")
            elif not _INDEX_RE.match(v):
                problems.append("The repository must be a plain http(s) URL without a user name or password.")
            elif "index_url" not in {"pip": {"index_url"}, "npm": {"index_url"}, "maven": {"index_url"},
                                     "gradle": {"index_url"}, "gem": {"index_url"}}.get(spec.ecosystem, set()):
                problems.append(f"A package repository can't be set for {spec.ecosystem} tools.")
            else:
                out["index_url"] = v
        elif k == "interpreter":
            if spec.ecosystem != "pip":
                problems.append("An interpreter only applies to Python tools.")
            else:
                errs = validate_interpreter(str(v))
                problems += errs
                if not errs:
                    out["interpreter"] = str(v)
        elif k == "env_vars":
            try:
                env = clean_manifest_env({str(a): str(b) for a, b in dict(v).items()})
            except (SandboxError, TypeError, ValueError) as e:
                problems.append(str(e))
                continue
            for name, val in env.items():
                if name.endswith("_HOME") and not os.path.isdir(val):
                    problems.append(f"{val} does not exist on the Understudy server.")
                elif name in ("CHROME_BIN",) and not os.path.isfile(val):
                    problems.append(f"{val} does not exist on the Understudy server.")
                elif ("/" in val or "\\" in val) and val not in text and not val.startswith(runtimes.RUNTIMES_DIR):
                    problems.append(f"The path for {name} must be the one you typed.")
                else:
                    out.setdefault("env_vars", {})[name] = val
        elif k == "path_prepend":
            for p in (v if isinstance(v, list) else [v])[:4]:
                p = str(p).strip().strip('"')
                if os.path.isfile(p):              # 9.4: the program itself was given (e.g. ...\dotnet-8\dotnet.exe)
                    p = os.path.dirname(p)
                if not os.path.isdir(p):
                    problems.append(f"{p} is not a folder on the Understudy server.")
                else:
                    out.setdefault("path_prepend", []).append(p)
                    if any(os.path.isfile(os.path.join(p, n)) for n in ("dotnet", "dotnet.exe")):
                        out.setdefault("env_vars", {}).setdefault("DOTNET_ROOT", p)
                        out["env_vars"].setdefault("DOTNET_MULTILEVEL_LOOKUP", "0")
        elif k == "use_runtime":                   # 9.4: an install from Mission Control > Environments, by id
            from core.wizard import environments
            rec = environments.find_install(str(v))
            if not rec:
                problems.append(f"There is no installed runtime '{v}' (see Mission Control > Environments).")
            elif not environments.runtime_works(rec["kind"], rec.get("path")):
                problems.append(f"{environments.describe_install(rec)} does not start.")
            else:
                bins, envs = environments.runtime_settings(rec)
                for b in bins:
                    if b not in out.setdefault("path_prepend", []):
                        out["path_prepend"].insert(0, b)
                out.setdefault("env_vars", {}).update(envs)
                out["runtime_used"] = environments.describe_install(rec)
        elif k == "version":
            try:
                out["version"] = tool_versions.validate_version(str(v).lstrip("=v"))
            except ValueError as e:
                problems.append(str(e))
        elif k in ("download_runtime", "download_runtimes"):
            for key in (v if isinstance(v, list) else [v])[:3]:
                if runtimes.is_supported(str(key)):
                    out.setdefault("download_runtimes", []).append(str(key))
                else:
                    problems.append(f"Understudy can download jdk-<N>, node-<N>, python-3.<N>, maven-3|4, gradle-<N>, dotnet-<N>, ruby-3, go-1 - not '{key}'.")
        elif k == "add_packages":                                  # 8.3
            pk = setup_doctor.clean_add_packages(v, spec, problems)
            if pk:
                out["add_packages"] = pk
        elif k == "post_install":                                  # 8.3
            pi = setup_doctor.clean_post_install(v, problems)
            if pi:
                out["post_install"] = pi
        elif k == "post_install_remove":                           # 8.3: only Understudy's own rules remove a step
            if trusted:
                pr = setup_doctor.clean_post_install(v, problems)
                if pr:
                    out["post_install_remove"] = pr
        elif k == "rewrite_smoke":                                 # 8.3: instructions for the AI's next smoke test
            t = str(v if not isinstance(v, bool) else "Rewrite the smoke test so it passes with this output.").strip()
            if t:
                out["rewrite_smoke"] = t[:800]
        elif k == "smoke_files":                                   # 8.5: the setup agent's corrected test
            if not trusted or not isinstance(v, dict) or not v:
                problems.append("Only the setup agent can supply a complete test.")
                continue
            from core.wizard import robot_syntax
            from core.wizard.sandbox import smoke as _smoke
            files = {str(p): str(c) for p, c in list(v.items())[:8]
                     if str(p).split("/")[-1] not in _smoke.MANIFEST_FILES}
            files, _fixed = robot_syntax.repair_files(files)
            probs = _smoke.validate_files(dict(files, **_smoke.understudy_manifests(spec, None)), spec)
            if probs:
                problems += [f"The corrected test was rejected: {x}" for x in probs[:4]]
            else:
                out["smoke_files"] = files
        elif k == "skip_post_install":
            out["skip_post_install"] = bool(v)
    return out, problems


def answer(request_id: str, admin: str = "admin", text: str = "", option: str = "",
           background: bool = True) -> Dict[str, Any]:
    """The admin answers the setup's question (chat). The AI applies it to the manifest and the setup resumes."""
    r = copy.deepcopy(ap.get_request(request_id))
    if r["status"] != "awaiting_admin" or not r.get("clarification"):
        raise SetupError("The setup isn't waiting for an answer.", 409)
    setup = r.get("setup") or {}
    if int(setup.get("round", 0)) >= MAX_ROUNDS:
        raise SetupError(f"The setup already stopped {MAX_ROUNDS} times. Prepare the plan again (with new "
                         f"instructions) or reject the request.", 409)
    option = re.sub(r"[^a-z0-9_]", "", (option or "").lower())[:40]
    text = str(text or "").strip()
    if not text and not option:
        raise SetupError("Type an answer or pick one of the options.")
    if option in ("agent_retry", "agent_retry_stronger"):        # 8.5: the agent looks again with the admin's hint
        if text:
            tv = screen_text(text[:2000], "ADMIN_INSTRUCTION")
            if not tv.is_safe:
                raise SetupError(tv.friendly_message)
            text = tv.sanitized
        return agent_again(request_id, admin, text, background, stronger=option == "agent_retry_stronger")
    if text:
        tv = screen_text(text[:2000], "ADMIN_INSTRUCTION")
        if not tv.is_safe:
            _audit("Model Armor blocked an admin answer in a tool setup", tv.friendly_message,
                   {"request": request_id, "pattern": tv.pattern}, level="warn")
            raise SetupError(tv.friendly_message)
        text = tv.sanitized
    label = next((o["label"] for o in r["clarification"].get("options") or [] if o["id"] == option), "")
    if _asks_to_skip_smoke(option + " " + label + " " + text):
        ap.add_message(request_id, "admin", "answer", (f"[{label}] " if label else "") + (text or ""), {"option": option or None})
        q = dict(r["clarification"], asked_at=_now(), agent_source="LOCAL_HEURISTIC", question=(
            "The smoke test can't be skipped - a tool only becomes ACTIVE after its smoke test passes (otherwise users "
            "would get a tool that may not run). I can rewrite the test from scratch (you review it first), run the same "
            "test again after you fixed the server, or you can reject the request."),
            options=[_opt("retry", "Rewrite the smoke test", "the AI writes a new test; you review it before it runs"),
                     _opt("rerun_smoke", "Run the same smoke test again", "keeps the current test")])
        out = ap.update_request(request_id, clarification=q)
        ap.add_message(request_id, "ai", "question", q["question"], {"options": q["options"], "agent_source": "LOCAL_HEURISTIC"})
        _event(out, question=q["question"])
        return out
    ap.add_message(request_id, "admin", "answer", (f"[{label}] " if label else "") + (text or ""),
                   {"option": option or None})
    r = copy.deepcopy(ap.get_request(request_id))
    res: Dict[str, Any] = {}
    agent = "LOCAL_HEURISTIC"
    if _ai_on():
        from core.wizard.llm import generate_json
        fvars = _failure_vars(r, r["clarification"].get("failure") or {})
        fvars.update(question=r["clarification"]["question"], answer=(f"option: {option}\n" if option else "") + text)
        out = generate_json("TOOL_SETUP_RESOLVE", fvars, untrusted=["output", "conversation", "manifest", "answer"])
        if out.ok and (out.data or {}).get("next") in ("retry", "ask", "stop"):
            res, agent = dict(out.data), out.agent_source
    offline = _resolve_offline(r, option, text)
    if option == "stop":
        res = offline                                              # the admin's click wins over the AI's reading
    payload = next((o.get("changes") for o in r["clarification"].get("options") or []
                    if o.get("id") == option and isinstance(o.get("changes"), dict)), None)
    clicked = next((o for o in r["clarification"].get("options") or [] if o.get("id") == option), None) or {}
    if clicked.get("fix_key"):
        setup.setdefault("auto_fixes", []).append({"key": clicked["fix_key"], "changes": clicked.get("changes"),
                                                   "at": _now(), "stage": (r["clarification"] or {}).get("stage"),
                                                   "source": clicked.get("source", "rules"), "approved_by": admin})
        if clicked.get("source") == "agent":                       # 8.5: the agent remembers what was applied
            from core.wizard import setup_agent
            setup_agent.mark_applied(setup, clicked["fix_key"], admin)
    elif text and (r["clarification"] or {}).get("agent"):        # 8.5: the admin's own answer is history too
        setup.setdefault("agent", {"turns": []}).setdefault("pending_feedback", []).append("admin answered: " + text[:500])
    if payload:                                                    # 8.3: the clicked option carries its exact change
        res = {"next": "retry", "summary": "Applying: " + (label or option) + ".",
               "changes": dict(payload, **{k: v for k, v in ((res or {}).get("changes") or {}).items() if k not in payload})}
    if not res:
        res = offline
    elif res.get("next") == "retry" and not res.get("changes") and offline.get("changes"):
        res["changes"] = offline["changes"]                       # the option the admin clicked always counts
    oc = offline.get("changes") or {}
    if oc.get("use_runtime") or "DOTNET_ROOT" in (oc.get("env_vars") or {}) or \
            ((r.get("clarification") or {}).get("category") == "runtime_download" and oc.get("download_runtimes")):
        # 9.4: the admin typed the path of a runtime: Understudy reads it exactly (the AI's reading may miss DOTNET_ROOT)
        res = {"next": "retry", "summary": res.get("summary") or offline.get("summary"),
               "changes": dict({k: v for k, v in (res.get("changes") or {}).items() if k not in ("path_prepend", "env_vars")},
                               **oc)}
    spec, _m = _effective(r)
    changes, problems = _validate_changes({k: v for k, v in (res.get("changes") or {}).items()
                                           if not (payload and k in payload)}, spec, text)
    if payload:
        pc, pp = _validate_changes(payload, spec, json.dumps(payload), trusted=True)
        changes.update(pc)
        problems += pp
    if res.get("next") == "stop":
        out = _set_status(request_id, "setup_failed", clarification=None,
                          setup=dict(setup, status="stopped", message="Stopped by the admin"))
        ap.add_message(request_id, "ai", "summary", res.get("summary") or "Stopped. Prepare the plan again or reject it.")
        return out
    if problems or res.get("next") == "ask":
        fu = res.get("follow_up") or {}
        question = " ".join(problems) + (" " if problems else "") + (fu.get("question") or "Could you clarify?")
        q = dict(r["clarification"], question=question.strip(), asked_at=_now(), agent_source=agent,
                 options=[o for o in (fu.get("options") or r["clarification"].get("options") or [])
                          if isinstance(o, dict) and o.get("id")][:5] or r["clarification"].get("options"))
        out = ap.update_request(request_id, clarification=q)
        ap.add_message(request_id, "ai", "question", q["question"], {"options": q["options"], "agent_source": agent})
        _event(out, question=q["question"])
        return out
    _merge_changes(setup, changes, (r["clarification"] or {}).get("failure"))
    if changes.get("smoke_files"):                                 # the admin saw the agent's test before approving
        setup["smoke_review"].update(approved_by=admin)
    if changes.get("version") and r.get("kind") != "version":
        if changes["version"] not in setup.get("allowed", []) and setup.get("allowed"):
            setup["allowed"] = [changes["version"]] + [v for v in setup["allowed"] if v != changes["version"]]
        setup["default"] = changes["version"] if setup.get("allowed") else setup.get("default")
        setup["install_version"] = changes["version"]
    # 8.2: a smoke test that failed because of the TEST is rewritten by the AI (and reviewed again); after a fix to the
    # server (changes, or "run the same test again") the reviewed test is kept.
    q = r["clarification"] or {}
    review = setup.get("smoke_review") or {}
    if q.get("stage") == "smoke_test" and review.get("files") and not changes.get("rewrite_smoke") and not (
            set(changes) - {"rewrite_smoke"}) and option != "rerun_smoke" and (
            option == "retry" or q.get("category") in ("smoke_test", "unknown") or re.search(r"(?i)\brewrite\b", text)):
        setup["smoke_review"] = dict(review, approved=False,
                                     previous_failure=(q.get("failure") or {}).get("output", "")[-2000:])
    setup.update(status="installing", resumed_at=_now())
    ap.add_message(request_id, "ai", "summary", res.get("summary") or "Retrying the setup.",
                   {"changes": changes, "agent_source": agent})
    _audit(f"Admin answered the {r['tool_name']} setup - resuming", res.get("summary") or "",
           {"request": request_id, "changes": changes})
    _set_status(request_id, "installing", setup=setup, clarification=None)
    return _start(request_id, background)


def rerun_smoke(tool_id: str, admin: str = "admin") -> Dict[str, Any]:
    """Re-runs the smoke test of an ACTIVE tool and records the result. 8.2: the exact test the admin reviewed when the
    tool was set up is re-run; only a tool set up before 8.2 (no stored test) gets a new AI-written one."""
    from core.wizard.sandbox import create_runner
    entry = ap.whitelist().get(tool_id)
    if not entry:
        raise SetupError("This tool isn't active.", 404)
    spec = ap.allowed_spec(tool_id)
    rt = (entry.get("setup") or {}).get("runtime") or {}
    logs: List[str] = []
    result: Dict[str, Any]
    try:
        with create_runner(spec, purpose="setup", log=logs.append, env=rt.get("env_vars"),
                           path_prepend=rt.get("path_prepend"), name=f"smoke-{tool_id}") as runner:
            out = runner.install_dependencies()
            if not out.ok:
                result = {"status": "FAILED", "detail": out.message, "output": _scrub(out.failed.tail(30)) if out.failed else ""}
            else:
                stored = (entry.get("setup") or {}).get("smoke_files") or {}
                if stored:
                    files, source, notes = dict(stored), "reviewed", []
                    files.update(smoke.understudy_manifests(spec, rt.get("java_release")))
                else:
                    files, source, notes, _n = _smoke_files({"setup": {}, "stack": {"selection": (entry.get("setup") or {}).get("stack")}},
                                                            spec, runner.kind, rt.get("java_release"))
                if files is None:
                    result = {"status": "FAILED", "detail": "; ".join(notes)}
                else:
                    res, info = smoke.run(runner, spec, files)
                    result = {"status": "PASSED" if res.ok else "FAILED", "exit_code": res.exit_code, "source": source,
                              "output": _scrub(res.tail(30)), "duration_ms": res.duration_ms, "runner": runner.kind}
    except Exception as e:
        result = {"status": "FAILED", "detail": f"{type(e).__name__}: {e}"}
    result["at"] = _now()
    result["by"] = admin
    ap.record_smoke(tool_id, result)
    _audit(f"Smoke test of {spec.name}: {result['status']}", result.get("detail") or "", {"tool": tool_id})
    from core.wizard import events
    events.publish("tool.smoke", {"tool_id": tool_id, "status": result["status"]})
    return result


def reject(request_id: str, admin: str = "admin", note: str = "") -> Dict[str, Any]:
    r = ap.get_request(request_id)
    if r["status"] not in ap.ACTIVE_REQUEST_STATES or r["status"] in RUNNING:
        raise SetupError(f"This request is {r['status'].replace('_', ' ')} - it can't be rejected now.", 409)
    if note:
        tv = screen_text(note[:500], "ADMIN_INSTRUCTION")
        note = tv.sanitized if tv.is_safe else ""
    out = ap.update_request(request_id, status="rejected", decided_at=_now(), decided_by=admin, admin_note=note,
                            clarification=None)
    ap.add_message(request_id, "admin", "reject", "Rejected" + (f": {note}" if note else "."))
    _event(out)
    try:
        from core.wizard import session as wz
        wz.release_after_rejection(out)
    except Exception:
        pass
    _audit(f"Admin rejected the request for {r['tool_name']}" + (f" {r['version']}" if r.get("version") else ""), note,
           {"request": request_id})
    return out


# ---------------------------------------------------------------------------------------------------------------
# Admin starts it: "Add a tool with AI"
# ---------------------------------------------------------------------------------------------------------------
def admin_add(name: str, admin: str = "admin", version: Optional[str] = None, instruction: Optional[str] = None,
              prepare: bool = True, stack: bool = False) -> Dict[str, Any]:
    """Creates the admin's request. ``stack=True`` (Mission Control's AI install assistant, 8.2) asks the AI for the
    stack first; ``prepare=True`` without it prepares the setup plan directly (API / older clients)."""
    tv = screen_text((name or "")[:200], "TOOL_NAME")
    if not tv.is_safe:
        raise SetupError(tv.friendly_message)
    name = tv.sanitized.strip()
    if not name:
        raise SetupError("Type the name of the tool (e.g. Playwright, Cypress, Selenium, Karate).")
    spec = match_catalog(name)
    if stack and not version:
        # 8.2: the assistant composes the stack from what the admin typed ("Selenium" - not "Selenium (Python)")
        from core.wizard.session import _placeholder_spec
        spec = _placeholder_spec(name)
    if spec is None:
        from core.wizard.session import _placeholder_spec
        spec = _placeholder_spec(name)
    if version and ap.is_tool_allowed(spec.id):
        req = ap.request_version(spec.id, version, "", reason="Added by the admin", requested_by=admin)
    else:
        req = ap.request_tool(spec, "", reason="Added by the admin in Mission Control", source="admin",
                              requested_by=admin, typed_name=name)
        if version and req.get("id"):
            ap.update_request(req["id"], version=version)
    if not req.get("id"):
        raise SetupError(f"{spec.name}{' ' + version if version else ''} is already approved.", 409)
    _audit(f"Admin asked the AI to set up {name}" + (f" {version}" if version else ""), "", {"request": req["id"]})
    if stack and req.get("kind", "tool") == "tool":
        from core.wizard import tool_stack
        try:
            return tool_stack.suggest(req["id"], admin, instruction)
        except tool_stack.StackError as e:
            raise SetupError(str(e), e.status_code)
    return prepare_plan(req["id"], admin, instruction) if prepare else req
