"""AI install assistant (8.2): the admin types a tool, the AI explains the stack, the admin selects it.

Nothing is pre-built for any framework - not even Playwright. When the admin types "Selenium" (or a user's request for
a tool reaches Mission Control):

1. ``suggest``: the ``TOOL_STACK_SUGGEST`` agent lists what a working stack consists of - the core package and the
   companion frameworks it is combined with (WebDriver, TestNG, JUnit, Cucumber, pytest ...), the languages it is
   available in (Java, Python, JavaScript ...), the package managers per language (Maven, pip, npm/npx ...), how one
   test is started, and a recommended version for every package. Understudy marks which package managers it can
   install with and which ones this server has.
2. ``versions``: for the language + package manager the admin picks, Understudy looks up the REAL version lists of
   every component's package in its registry (Maven Central, PyPI, npm) - the AI's recommendation is pre-selected.
3. ``select``: the admin's choice (language, package managers available, components, a version per component) is
   turned into a tool spec deterministically - package names only from the AI's suggestion, versions validated
   against the registry grammar, run command checked against Understudy's runner allow-list - and the setup plan is
   prepared from it (``tool_setup.prepare_plan``). From there: approve -> sandbox install -> AI smoke test -> admin
   review -> smoke run -> ACTIVE.

Without an AI the assistant can't run (that's the point of 8.2); the admin is told to configure Gemini.
"""
from __future__ import annotations

import copy
import json
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from core.wizard import activity_log, admin_policy as ap, tool_versions
from core.wizard.frameworks import (CATALOG, RUNNER_ALLOWLIST, FrameworkSpec, FrameworkValidationError, Package,
                                    validate_spec)
from core.wizard.guardrails import screen_text

# package manager -> Understudy ecosystem (the installer it uses) - from core/wizard/package_managers.py, where the admin
# can also switch each one off (8.2). None = Understudy has no installer for it.
from core.wizard import package_managers as pms  # noqa: E402

PM_ECOSYSTEM: Dict[str, Optional[str]] = {k: v["ecosystem"] for k, v in pms.CATALOG.items()}
PM_ECOSYSTEM.update({a: pms.CATALOG[t]["ecosystem"] for a, t in pms.ALIASES.items()})
PM_LABEL = {k: v["name"] for k, v in pms.CATALOG.items()}
PM_LABEL.update({"mvn": "Maven", "dotnet": "NuGet (dotnet)", "sbt": "sbt", "bazel": "Bazel"})


def _eco(pm: str) -> Optional[str]:
    """The ecosystem Understudy installs this package manager with - None when it has no installer for it or the admin
    switched it off in Mission Control > Package managers."""
    return PM_ECOSYSTEM.get(pm) if pms.is_enabled(pm) else None
LANG_LABEL = {"java": "Java", "python": "Python", "javascript": "JavaScript", "typescript": "TypeScript",
              "csharp": "C#", "ruby": "Ruby", "go": "Go", "kotlin": "Kotlin"}
# what Understudy runs when the AI's run profile is unusable
DEFAULT_RUN = {
    "pip": (["python", "{entry}"], "run_automation.py"),
    "npm": (["node", "{entry}"], "run_automation.js"),
    "maven": (["mvn", "-q", "test"], "src/test/java/automation/AutomationTest.java"),
    "gradle": (["gradle", "--no-daemon", "--console=plain", "test"], "src/test/java/automation/AutomationTest.java"),
    "gem": (["ruby", "{entry}"], "run_automation.rb"),
    "go": (["go", "test", "./..."], "automation_test.go"),
    "nuget": (["dotnet", "test"], "AutomationTest.cs"),
}
MAX_COMPONENTS = 12
_ID_RX = re.compile(r"[^a-z0-9-]+")


class StackError(ValueError):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def _now() -> str:
    return datetime.utcnow().isoformat()


def _audit(title: str, detail: str = "", data: Optional[Dict[str, Any]] = None, level: str = "info") -> None:
    activity_log.record("ADMIN", title, detail, data, source="ADMIN", level=level)


def _slug(s: str, n: int = 40) -> str:
    return _ID_RX.sub("-", (s or "").lower()).strip("-")[:n]


# ---------------------------------------------------------------------------------------------------------------
# 1. Suggestion
# ---------------------------------------------------------------------------------------------------------------
def _pm_row(pid: str, lang: str, name: str, notes: str, toolchains: Dict[str, str]) -> Dict[str, Any]:
    known = PM_ECOSYSTEM.get(pid)
    enabled = bool(known) and pms.is_enabled(pid)
    eco = known if enabled else None
    server = (toolchains.get(eco, "") if eco else
              "switched off by the admin (Mission Control > Package managers)" if known else
              "Understudy has no installer for it yet")
    return {"id": pid, "name": (name or PM_LABEL.get(pid, pid))[:40], "language": lang,
            "ecosystem": eco or ("disabled" if known else "unsupported"), "supported": eco is not None,
            "disabled_by_admin": bool(known) and not enabled,
            "available": bool(eco) and str(toolchains.get(eco, "")).startswith("available"), "server": server,
            "notes": notes[:300]}


def _clean_suggestion(data: Dict[str, Any], toolchains: Dict[str, str]) -> Tuple[Dict[str, Any], List[str]]:
    """Normalises the AI's stack: ids, known package managers, package names checked against the ecosystem grammar.
    Returns (suggestion, notes)."""
    from core.wizard.frameworks import _PKG_RE
    notes: List[str] = []
    langs: List[Dict[str, Any]] = []
    for l in (data.get("languages") or [])[:8]:
        if not isinstance(l, dict):
            continue
        lid = _slug(str(l.get("id") or l.get("name") or ""), 20)
        if not lid or any(x["id"] == lid for x in langs):
            continue
        langs.append({"id": lid, "name": str(l.get("name") or LANG_LABEL.get(lid, lid))[:40],
                      "recommended": bool(l.get("recommended")), "notes": str(l.get("notes") or "")[:300],
                      "package_managers": [_slug(str(p), 20) for p in (l.get("package_managers") or [])][:6]})
    pm_list: List[Dict[str, Any]] = []
    for p in (data.get("package_managers") or [])[:16]:
        if not isinstance(p, dict):
            continue
        pid = _slug(str(p.get("id") or ""), 20)
        lang = _slug(str(p.get("language") or ""), 20)
        if not pid or any(x["id"] == pid and x["language"] == lang for x in pm_list):
            continue
        pm_list.append(_pm_row(pid, lang, str(p.get("name") or ""), str(p.get("notes") or ""), toolchains))
    for l in langs:                                   # package managers named only on a language
        for pid in l["package_managers"]:
            if not any(x["id"] == pid and x["language"] == l["id"] for x in pm_list):
                pm_list.append(_pm_row(pid, l["id"], "", "", toolchains))
    comps: List[Dict[str, Any]] = []
    for c in (data.get("components") or [])[:MAX_COMPONENTS]:
        if not isinstance(c, dict):
            continue
        cid = _slug(str(c.get("id") or c.get("name") or ""), 40)
        if not cid or any(x["id"] == cid for x in comps):
            continue
        pkgs = []
        for pk in (c.get("packages") or [])[:16]:
            if not isinstance(pk, dict):
                continue
            pm = _slug(str(pk.get("package_manager") or ""), 20)
            eco = PM_ECOSYSTEM.get(pm)                    # grammar check even when the admin switched it off
            name = str(pk.get("name") or "").strip()
            if eco and _PKG_RE.get(eco) and not _PKG_RE[eco].match(name):
                notes.append(f"Ignored the package name '{name[:60]}' for {pm}: not a valid {eco} package name.")
                continue
            rv = str(pk.get("recommended_version") or "").strip().lstrip("=v^~")
            try:
                rv = tool_versions.validate_version(rv) if rv else ""
            except ValueError:
                rv = ""
            pkgs.append({"language": _slug(str(pk.get("language") or ""), 20), "package_manager": pm, "name": name,
                         "recommended_version": rv})
        if not pkgs:
            continue
        comps.append({"id": cid, "name": str(c.get("name") or cid)[:60], "role": re.sub(r"[^a-z_]", "", str(c.get("role") or "other").lower().replace("-", "_"))[:20] or "other",
                      "purpose": str(c.get("purpose") or "")[:300], "required": bool(c.get("required")),
                      "default_selected": bool(c.get("default_selected") or c.get("required")),
                      "conflicts_with": [_slug(str(x), 40) for x in (c.get("conflicts_with") or [])][:6],
                      "packages": pkgs})
    if comps and not any(c["required"] for c in comps):
        core = next((c for c in comps if c["role"] == "core"), comps[0])
        core["required"] = core["default_selected"] = True
    profiles = []
    for rp in (data.get("run_profiles") or [])[:20]:
        if not isinstance(rp, dict):
            continue
        cmd = [str(t) for t in (rp.get("run_command") or [])][:12]
        if not cmd or cmd[0] not in RUNNER_ALLOWLIST:
            notes.append(f"Ignored a run command that starts with '{cmd[0] if cmd else ''}' (not an allowed runner).")
            continue
        profiles.append({"language": _slug(str(rp.get("language") or ""), 20),
                         "package_manager": _slug(str(rp.get("package_manager") or ""), 20),
                         "requires": [_slug(str(x), 40) for x in (rp.get("requires") or [])][:6],
                         "run_command": cmd, "entry_file": str(rp.get("entry_file") or "")[:150],
                         "post_install": [[str(t) for t in c][:10] for c in (rp.get("post_install") or [])
                                          if isinstance(c, list)][:3]})
    out = {"tool_family": str(data.get("tool_family") or "")[:60], "summary": str(data.get("summary") or "")[:1500],
           "category": str(data.get("category") or "browser") if str(data.get("category") or "") in
           ("browser", "api", "mobile", "desktop", "performance", "other") else "browser",
           "languages": langs, "package_managers": pm_list, "components": comps, "run_profiles": profiles,
           "os_requirements": [str(x)[:200] for x in (data.get("os_requirements") or [])][:8],
           "runtime_requirements": [{"runtime": _slug(str(r.get("runtime") or ""), 10), "spec": str(r.get("spec") or "")[:40],
                                     "language": _slug(str(r.get("language") or ""), 20),
                                     "reason": str(r.get("reason") or "")[:300]}
                                    for r in (data.get("runtime_requirements") or []) if isinstance(r, dict)][:8],
           "questions": [str(x)[:300] for x in (data.get("questions") or [])][:3]}
    if data.get("security_flags"):
        notes.append("The AI flagged: " + "; ".join(str(x) for x in data["security_flags"][:3]))
    return out, notes


def suggest(request_id: str, admin: str = "admin", note: Optional[str] = None) -> Dict[str, Any]:
    """Asks the AI for the stack of a request's tool. The request moves to ``stack_ready`` (admin picks)."""
    from core.wizard.llm import ai_available, generate_json
    from core.wizard.tool_setup import _event, server_toolchains
    r = copy.deepcopy(ap.get_request(request_id))
    if r.get("kind") == "version":
        raise StackError("A version request uses the tool's approved stack - prepare its setup plan instead.", 409)
    if r["status"] not in ("pending", "stack_ready", "plan_ready", "setup_failed"):
        raise StackError(f"This request is {r['status'].replace('_', ' ')} - the stack can't be changed now.", 409)
    if note:
        tv = screen_text(str(note)[:1000], "ADMIN_INSTRUCTION")
        if not tv.is_safe:
            raise StackError(tv.friendly_message)
        note = tv.sanitized
    if not ai_available():
        out = ap.update_request(request_id, stack={"error": "no_ai", "notes": [
            "The AI install assistant needs Gemini (Mission Control > AI, security & account). Nothing is pre-built "
            "in Understudy 8.2, so a tool can't be set up without it."]})
        ap.add_message(request_id, "system", "failure", "The AI install assistant needs Gemini - configure it in "
                       "Mission Control > AI, security & account, then ask again.")
        raise StackError("The AI install assistant needs Gemini. Configure it in Mission Control > AI, security & "
                         "account, then try again.", 503)
    ap.update_request(request_id, status="suggesting")
    toolchains = server_toolchains()
    res = generate_json("TOOL_STACK_SUGGEST", {
        "tool_name": r.get("typed_name") or r["tool_name"], "note": note or r.get("reason") or "(none)",
        "server_toolchains": json.dumps(toolchains), "allowed_runners": ", ".join(sorted(RUNNER_ALLOWLIST)),
    }, untrusted=["tool_name", "note"])
    if not res.ok or not isinstance(res.data, dict):
        ap.update_request(request_id, status="pending")
        raise StackError(f"The AI could not suggest a stack ({res.error}). Try again in a moment.", 502)
    sug, notes = _clean_suggestion(res.data, toolchains)
    if not sug["languages"] or not sug["components"]:
        ap.update_request(request_id, status="pending")
        raise StackError("The AI's answer had no usable languages or packages. Try again, or add a note (e.g. "
                         "'the Java version with TestNG').", 502)
    sug.update(agent_source=res.agent_source, model=res.model, suggested_at=_now(), notes=notes,
               toolchains=toolchains, note=note or "")
    stack = {"suggestion": sug, "selection": None, "history": ((r.get("stack") or {}).get("history") or [])[-3:]}
    out = ap.update_request(request_id, status="stack_ready", stack=stack)
    langs = ", ".join(l["name"] for l in sug["languages"])
    comps = ", ".join(c["name"] for c in sug["components"])
    ap.add_message(request_id, "ai", "stack",
                   f"{sug['summary'] or (sug['tool_family'] or r['tool_name']) + ' stack'} Languages: {langs}. "
                   f"Components: {comps}. Pick the language, the package managers your server has and the components "
                   f"to install - I'll look up the real versions for you.",
                   {"agent_source": res.agent_source, "questions": sug["questions"]})
    _audit(f"AI suggested the stack for {r['tool_name']}", f"languages: {langs}; components: {comps}",
           {"request": request_id, "agent_source": res.agent_source})
    _event(out)
    return copy.deepcopy(ap.get_request(request_id))


# ---------------------------------------------------------------------------------------------------------------
# 2. Real versions for the admin's language + package manager
# ---------------------------------------------------------------------------------------------------------------
def _pm_matches(pm: str, wanted: str) -> bool:
    return pm == wanted or (PM_ECOSYSTEM.get(pm) is not None and PM_ECOSYSTEM.get(pm) == PM_ECOSYSTEM.get(wanted))


def _refuse(pm: str) -> "StackError":
    if PM_ECOSYSTEM.get(pm):
        return StackError(f"{PM_LABEL.get(pm, pm)} is switched off by your admin (Mission Control > Package managers) - "
                          "turn it on or pick another package manager.", 422)
    return StackError(f"Understudy has no installer for {PM_LABEL.get(pm, pm or 'that package manager')} yet - pick "
                      "pip, npm/npx, Maven, Gradle, RubyGems, Go or NuGet.", 422)


def _package_for(comp: Dict[str, Any], language: str, pm: str) -> Optional[Dict[str, Any]]:
    exact = [p for p in comp["packages"] if p["language"] == language and p["package_manager"] == pm]
    near = [p for p in comp["packages"] if p["language"] == language and _pm_matches(p["package_manager"], pm)]
    loose = [p for p in comp["packages"] if not p["language"] and _pm_matches(p["package_manager"], pm)]
    pool = exact or near or loose
    return pool[0] if pool else None


def package_versions(ecosystem: str, name: str, refresh: bool = False) -> Dict[str, Any]:
    tmp = FrameworkSpec(id="stack-probe", name=name, language="", ecosystem=ecosystem, packages=[Package(name)],
                        run_command=["python"], entry_file="x")
    info = tool_versions.available_versions(tmp, refresh=refresh, limit=30)
    return {k: info.get(k) for k in ("versions", "latest", "source", "error")}


def versions(request_id: str, language: str, package_manager: str, refresh: bool = False) -> Dict[str, Any]:
    r = ap.get_request(request_id)
    sug = ((r.get("stack") or {}).get("suggestion") or {})
    if not sug:
        raise StackError("Ask the AI for the stack first.", 409)
    language, package_manager = _slug(language, 20), _slug(package_manager, 20)
    eco = _eco(package_manager)
    if not eco:
        raise _refuse(package_manager)
    out: Dict[str, Any] = {"language": language, "package_manager": package_manager, "ecosystem": eco, "components": {}}
    for c in sug.get("components") or []:
        pk = _package_for(c, language, package_manager)
        if not pk:
            out["components"][c["id"]] = {"available": False}
            continue
        info = package_versions(eco, pk["name"], refresh)
        rec = pk.get("recommended_version") or ""
        vs = list(info.get("versions") or [])
        if rec and vs and rec not in vs:
            line = [v for v in vs if v.startswith(rec.rsplit(".", 1)[0] + ".")] if "." in rec else []
            rec = line[0] if line else (info.get("latest") or vs[0])
        out["components"][c["id"]] = {"available": True, "package": pk["name"], "versions": vs,
                                      "latest": info.get("latest"), "recommended": rec or info.get("latest") or "",
                                      "source": info.get("source"), "error": info.get("error")}
    return out


# ---------------------------------------------------------------------------------------------------------------
# 3. The admin's selection -> a validated spec -> the setup plan
# ---------------------------------------------------------------------------------------------------------------
def _profile(sug: Dict[str, Any], language: str, pm: str, selected: List[str]) -> Optional[Dict[str, Any]]:
    cands = [p for p in sug.get("run_profiles") or [] if p["language"] == language and _pm_matches(p["package_manager"], pm)
             and set(p.get("requires") or []) <= set(selected)]
    cands.sort(key=lambda p: (-len(p.get("requires") or []), 0 if p["package_manager"] == pm else 1))
    return cands[0] if cands else None


def compose_spec(sug: Dict[str, Any], selection: Dict[str, Any]) -> Tuple[FrameworkSpec, List[str], Dict[str, Any]]:
    """(spec, notes, normalised selection). Raises StackError on an unusable selection."""
    notes: List[str] = []
    language = _slug(str(selection.get("language") or ""), 20)
    pm = _slug(str(selection.get("package_manager") or ""), 20)
    if not any(l["id"] == language for l in sug.get("languages") or []):
        raise StackError("Pick one of the languages the AI suggested.")
    eco = _eco(pm)
    if not eco:
        raise _refuse(pm)
    comps = {c["id"]: c for c in sug.get("components") or []}
    chosen = [str(x) for x in (selection.get("components") or []) if str(x) in comps]
    for c in comps.values():                                    # the core is always installed
        if c["required"] and c["id"] not in chosen:
            chosen.insert(0, c["id"])
    for cid in chosen:
        clash = [x for x in comps[cid].get("conflicts_with") or [] if x in chosen]
        if clash:
            raise StackError(f"{comps[cid]['name']} and {comps[clash[0]]['name']} are alternatives - pick one.")
    want_versions = {str(k): str(v) for k, v in (selection.get("versions") or {}).items()}
    packages: List[Package] = []
    used: List[Dict[str, Any]] = []
    order = sorted(chosen, key=lambda cid: (0 if comps[cid]["required"] else 1, chosen.index(cid)))
    for cid in order:
        pk = _package_for(comps[cid], language, pm)
        if not pk:
            notes.append(f"{comps[cid]['name']} has no {LANG_LABEL.get(language, language)} package for {pm} - skipped.")
            continue
        v = want_versions.get(cid) or pk.get("recommended_version") or ""
        if v:
            try:
                v = tool_versions.validate_version(v.lstrip("=v"))
            except ValueError as e:
                raise StackError(f"{comps[cid]['name']}: {e}")
        if any(p.name == pk["name"] for p in packages):
            continue
        packages.append(Package(pk["name"], ("==" + v) if (v and eco == "pip") else v))
        used.append({"id": cid, "name": comps[cid]["name"], "role": comps[cid]["role"], "package": pk["name"],
                     "version": v})
    if not packages:
        raise StackError("None of the selected components has a package for this language and package manager.")
    prof = _profile(sug, language, pm, [u["id"] for u in used])
    run_cmd, entry = DEFAULT_RUN[eco]
    post: List[List[str]] = []
    if prof:
        run_cmd, entry, post = prof["run_command"], prof["entry_file"] or entry, prof.get("post_install") or []
    else:
        notes.append(f"The AI gave no run command for {language} + {pm}; Understudy uses its default ({' '.join(run_cmd)}).")
    family = sug.get("tool_family") or (used[0]["name"] if used else "tool")
    extras = [u for u in used[1:] if u["role"] in ("test_runner", "bdd")]
    sid = _slug("-".join([family, language] + [u["id"] for u in extras]), 40)
    name = f"{family} ({LANG_LABEL.get(language, language)})" + "".join(f" + {u['name']}" for u in extras)
    spec = FrameworkSpec(id=sid, name=name[:80], language=LANG_LABEL.get(language, language), ecosystem=eco,
                         packages=packages, run_command=list(run_cmd), entry_file=entry,
                         category=sug.get("category") or "browser",
                         beginner_summary=(sug.get("summary") or "")[:300], post_install=post, source="ai-stack",
                         difficulty="intermediate")
    # Understudy's offline CODE template (for BUILD, not for installing) is kept only for the exact same stack
    for f in CATALOG.values():
        if f.template and f.ecosystem == eco and {p.name for p in f.packages} == {p.name for p in packages} \
                and f.run_command == spec.run_command:
            spec.template = f.template
    try:
        validate_spec(spec)
    except FrameworkValidationError as e:
        if prof:                                          # the AI's profile was unsafe: fall back to the default
            notes.append(f"The AI's run command failed Understudy's safety check ({e}); using the default.")
            spec.run_command, spec.entry_file, spec.post_install = list(DEFAULT_RUN[eco][0]), DEFAULT_RUN[eco][1], []
            validate_spec(spec)
        else:
            raise StackError(f"The selected stack failed Understudy's safety check: {e}", 422)
    norm = {"language": language, "package_manager": pm, "ecosystem": eco, "components": [u["id"] for u in used],
            "versions": {u["id"]: u["version"] for u in used}, "used": used,
            "package_managers_available": [_slug(str(x), 20) for x in (selection.get("package_managers_available") or
                                                                      [pm])][:8]}
    return spec, notes, norm


def select(request_id: str, selection: Dict[str, Any], admin: str = "admin") -> Dict[str, Any]:
    """The admin's choice -> spec -> setup plan (plan_ready)."""
    from core.wizard import tool_setup
    r = copy.deepcopy(ap.get_request(request_id))
    if r["status"] not in ("stack_ready", "plan_ready", "setup_failed"):
        raise StackError(f"This request is {r['status'].replace('_', ' ')} - pick the stack after the AI suggested it.", 409)
    sug = (r.get("stack") or {}).get("suggestion")
    if not sug:
        raise StackError("Ask the AI for the stack first.", 409)
    spec, notes, norm = compose_spec(sug, selection or {})
    blk = ap.tool_block_for(spec, r.get("tool_name", ""))
    if blk and not blk.get("versions"):
        raise StackError(f"{spec.name} is on the blocked tools list ({blk['name']}).", 409)
    core_v = norm["versions"].get(norm["components"][0]) if norm["components"] else None
    orig = (r.get("stack") or {}).get("original_tool_id") or r["tool_id"]
    stack = dict(r.get("stack") or {}, selection=dict(norm, notes=notes, by=admin, at=_now()), original_tool_id=orig)
    ap.update_request(request_id, stack=stack, spec=spec.to_dict(), tool_name=spec.name,
                      version=core_v or r.get("version"), status="pending", plan=None, setup=None, clarification=None)
    used = ", ".join(f"{u['name']} {u['version'] or '(latest)'}" for u in norm["used"])
    ap.add_message(request_id, "admin", "stack_choice",
                   f"Selected {LANG_LABEL.get(norm['language'], norm['language'])} with {PM_LABEL.get(norm['package_manager'], norm['package_manager'])}: {used}.",
                   {"selection": norm})
    _audit(f"Admin selected the stack for {spec.name}", used, {"request": request_id})
    if spec.id != r["tool_id"]:
        ap.update_request(request_id, tool_id=spec.id)
    return tool_setup.prepare_plan(request_id, admin)


def stack_spec(r: Dict[str, Any]) -> Optional[FrameworkSpec]:
    """The spec the admin composed for this request, if any."""
    sel = (r.get("stack") or {}).get("selection")
    if sel and r.get("spec"):
        try:
            return validate_spec(FrameworkSpec.from_dict(r["spec"]))
        except FrameworkValidationError:
            return None
    return None
