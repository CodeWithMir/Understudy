"""Setup repair agent (8.5): Gemini stays on a failed tool setup until it passes - with the admin approving every fix.

Before 8.5 a failed setup got ONE AI answer (TOOL_SETUP_CLARIFY): the model saw the tail of the output, guessed, and
the next failure started from zero. Robot Framework's "expected 2 to 3 arguments, got 0" showed the limits: the model
never saw the installed library's real keyword signatures, never checked its own test, didn't know the same error had
come back five times, and kept writing the same layout.

``turn()`` runs when a setup halts (``tool_setup._halt``):

1. Evidence: the failed step and its output, the test that ran, the HISTORY of earlier failures in this setup with the
   fixes applied and what happened next, LESSONS (fixes that solved similar failures on this server), the admin's notes.
2. Investigation (no approval needed - read-only, inside the tool's sandbox): the agent asks for checks - a dry run of
   the test, the installed packages, the keyword / API signatures of the INSTALLED library version, runtime versions -
   and gets their output back. Up to ``MAX_STEPS`` model calls per turn.
3. A proposed fix is pre-checked by Understudy before the admin sees it: changes are validated like any other fix, a
   corrected test must pass Understudy's safety check and its dry run. A failed pre-check goes back to the agent, which
   corrects itself.
4. The admin approves (one click - the exact changes and test are shown), rejects with a hint ("look again"), answers
   in their own words, or stops. Approved: Understudy applies the fix and re-runs the setup. Still failing: the next
   turn starts with the full history. When the same error comes back, the admin is OFFERED a look by a stronger model
   (Mission Control setting) - the model is never switched without the admin's click.
5. When the setup passes, the fix that solved it is saved as a lesson.

In 'automatic' fix mode (Mission Control) a fix that only adds packages / install steps is applied without asking;
a fix that changes the test or the tool version always waits for the admin.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.wizard import activity_log, admin_policy as ap
from core.wizard.frameworks import FrameworkSpec

MAX_STEPS = 5                  # model calls per turn (checks + self-corrections)
MAX_CHECKS_PER_STEP = 3
CHECK_TIMEOUT_S = 90
AGENT_KEYS = ("add_packages", "post_install", "post_install_remove", "version", "download_runtimes",
              "skip_post_install", "smoke_files", "use_runtime")
AUTOMATIC_KEYS = ("add_packages", "post_install", "post_install_remove")
_DOTTED = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*){0,8}$")
_NPM_NAME = re.compile(r"^(@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]{0,100}$")

_KEYWORDS_SCRIPT = r"""
import sys
from robot.libdocpkg import LibraryDocumentation
used = {a.lower().replace('_', ' ').strip() for a in sys.argv[2:]}
for lib in [x for x in sys.argv[1].split(',') if x]:
    try:
        d = LibraryDocumentation(lib)
    except Exception as e:
        print(f'{lib}: cannot load ({type(e).__name__}: {e})')
        continue
    print(f'== {lib} {d.version} ({len(d.keywords)} keywords)')
    for k in d.keywords:
        if not used or k.name.lower() in used:
            print(f'{k.name} | ' + ', '.join(str(a) for a in k.args))
"""
_SIGNATURE_SCRIPT = r"""
import importlib, inspect, sys
path = sys.argv[1]
parts = path.split('.')
obj, rest = None, []
for i in range(len(parts), 0, -1):
    try:
        obj, rest = importlib.import_module('.'.join(parts[:i])), parts[i:]
        break
    except Exception:
        continue
if obj is None:
    print('cannot import', path)
    sys.exit(0)
for a in rest:
    obj = getattr(obj, a)
try:
    print(f'{path}{inspect.signature(obj)}')
except Exception as e:
    print(path, type(obj).__name__, '(no signature)')
print((inspect.getdoc(obj) or '')[:900])
"""
_IMPORT_SCRIPT = ("import importlib, sys; m = importlib.import_module(sys.argv[1]); "
                  "print(sys.argv[1], getattr(m, '__version__', ''), getattr(m, '__file__', ''))")
_EXPORTS_SCRIPT = ("const m = require(process.argv[1]); const v = (() => { try { return require(process.argv[1] + "
                   "'/package.json').version } catch (e) { return '' } })(); console.log(process.argv[1], v); "
                   "console.log(Object.keys(m).slice(0, 150).join(', '))")


def _now() -> str:
    return datetime.utcnow().isoformat()


def enabled() -> bool:
    return bool(ap.setup_agent_settings().get("enabled"))


# ---------------------------------------------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------------------------------------------
def signature(fd: Dict[str, Any]) -> str:
    """A stable fingerprint of a failure: the decisive line without numbers, paths and quotes."""
    from core.wizard import robot_syntax
    from core.wizard.tool_setup import key_error_line
    text = f"{fd.get('message', '')}\n{fd.get('output', '')}"
    m = robot_syntax.ERROR_RX.search(text)
    line = m.group(0) if m else key_error_line(fd.get("output") or "") or fd.get("message") or ""
    norm = re.sub(r"([A-Za-z]:)?[\\/][^\s'\"]+", "<path>", line)
    norm = re.sub(r"\d+", "#", norm).strip().lower()
    return f"{fd.get('stage')}:" + hashlib.sha1(norm.encode()).hexdigest()[:12]


def error_line(fd: Dict[str, Any]) -> str:
    from core.wizard import robot_syntax
    from core.wizard.tool_setup import key_error_line
    text = f"{fd.get('message', '')}\n{fd.get('output', '')}"
    m = robot_syntax.ERROR_RX.search(text)
    if m:
        line = next((ln.strip() for ln in text.splitlines() if m.group(0) in ln), m.group(0))
        return line[:300]
    return (key_error_line(fd.get("output") or "") or fd.get("message") or "")[:300]


def test_files(setup: Dict[str, Any]) -> Dict[str, str]:
    from core.wizard.sandbox import smoke
    files = (setup.get("smoke") or {}).get("files") or (setup.get("smoke_review") or {}).get("files") or {}
    return {k: v for k, v in files.items() if k.split("/")[-1] not in smoke.MANIFEST_FILES}


def _history(setup: Dict[str, Any]) -> str:
    rows = []
    for a in (setup.get("attempts") or [])[-8:]:
        f = a.get("failure") or {}
        if f:
            rows.append(f"- round {a.get('round')}: {f.get('stage')} failed: {error_line(f) or f.get('message', '')}")
    for x in setup.get("auto_fixes") or []:
        rows.append(f"- fix applied ({x.get('source')}): {json.dumps(x.get('changes'), default=str)[:300]}")
    for t in (setup.get("agent") or {}).get("turns") or []:
        fx = t.get("fix") or {}
        rows.append(f"- agent turn {t.get('n')}: error '{t.get('error_line')}' -> "
                    + (f"proposed: {fx.get('summary')} [{fx.get('describe')}] -> {t.get('outcome') or 'pending'}"
                       + (f" -> {t['result']}" if t.get("result") else "") if fx else
                       f"{t.get('action') or 'no fix'}: {t.get('question') or t.get('thought') or ''}"[:300]))
        for fb in t.get("feedback") or []:
            rows.append(f"  admin said: {fb}"[:300])
    return "\n".join(rows) or "(this is the first failure)"


def _lessons(spec: FrameworkSpec) -> str:
    names = {p.name.lower() for p in spec.packages}
    rows = []
    for l in reversed(ap.setup_lessons()):
        if l.get("ecosystem") == spec.ecosystem and (not l.get("packages") or names & set(l.get("packages") or [])):
            rows.append(f"- error '{l.get('error_line')}' was solved by: {l.get('summary')} [{l.get('describe')}]")
        if len(rows) >= 5:
            break
    return "\n".join(rows) or "(none yet)"


# ---------------------------------------------------------------------------------------------------------------
# Read-only checks inside the tool's sandbox
# ---------------------------------------------------------------------------------------------------------------
def _robot_files(files: Dict[str, str]) -> List[str]:
    return [p for p in files if p.lower().endswith(".robot")]


def _robot_libraries(files: Dict[str, str]) -> Tuple[List[str], List[str]]:
    from core.wizard import robot_syntax
    libs, used = [], set()
    for path in _robot_files(files):
        for line in files[path].splitlines():
            m = re.match(r"^Library\s{2,}|^Library\t", line)
            if m:
                name = re.split(r"\s{2,}|\t", line.strip())[1:2]
                if name and re.match(r"^[A-Za-z_][\w.]*$", name[0]) and name[0] not in libs:
                    libs.append(name[0])
            elif line[:1].isspace() and line.strip() and not line.strip().startswith(("#", "[", "...")):
                cells = [c for c in re.split(r" {2,}|\t+", line.strip()) if c]
                kw, _a = robot_syntax._call(cells)
                if kw:
                    used.add(kw)
    return libs or ["BuiltIn"], sorted(used)[:60]


def available_checks(spec: FrameworkSpec, files: Dict[str, str]) -> List[Tuple[str, str]]:
    """(id, what it shows) of the checks that make sense for this tool."""
    out = [("test_dry_run", "a syntax / dry run of the current test without opening a browser"),
           ("runtime_versions", "the versions of the language runtime the tool runs on")]
    if spec.ecosystem in ("pip", "npm", "gem"):
        out.append(("installed_packages", "the packages really installed in the tool's environment, with versions"))
    if _robot_files(files) or "robotframework" in {p.name.lower() for p in spec.packages}:
        out.append(("robot_keywords", "the real keyword names and arguments of the Robot libraries the test imports, "
                                      "from the INSTALLED versions"))
    if spec.ecosystem == "pip":
        out.append(("signature:<module.Class.method>", "the call signature and docstring of a Python API in the "
                                                       "installed version, e.g. signature:playwright.sync_api.Page.fill"))
        out.append(("import:<module>", "whether a Python module imports, and its version"))
    if spec.ecosystem == "npm":
        out.append(("exports:<package>", "the version and exported names of an installed npm package"))
    return out


def _dry_run_argv(spec: FrameworkSpec, files: Dict[str, str]) -> List[Tuple[str, List[str]]]:
    robots = _robot_files(files)
    if robots:
        return [(f"robot --dryrun {robots[0]}", ["python", "-m", "robot", "--dryrun", "--output", "NONE", "--report",
                                                  "NONE", "--log", "NONE", "--console", "verbose", robots[0]])]
    if spec.ecosystem == "pip":
        return [(f"python -m py_compile {p}", ["python", "-m", "py_compile", p]) for p in files if p.endswith(".py")][:3]
    if spec.ecosystem == "npm":
        return [(f"node --check {p}", ["node", "--check", p]) for p in files if p.endswith((".js", ".cjs", ".mjs"))][:3]
    return []


def _argv_for(check: str, spec: FrameworkSpec, files: Dict[str, str]) -> Tuple[Optional[List[Tuple[str, List[str]]]], str]:
    """([(label, argv)], problem)."""
    if check == "test_dry_run":
        runs = _dry_run_argv(spec, files)
        return (runs, "") if runs else (None, f"no dry run is available for {spec.ecosystem} tests")
    if check == "runtime_versions":
        prog = {"pip": [["python", "--version"]], "npm": [["node", "--version"], ["npm", "--version"]],
                "maven": [["java", "-version"], ["mvn", "-version"]], "gradle": [["java", "-version"], ["gradle", "--version"]],
                "gem": [["ruby", "--version"]], "go": [["go", "version"]], "nuget": [["dotnet", "--version"]]}
        return [(" ".join(a), a) for a in prog.get(spec.ecosystem, [])], ""
    if check == "installed_packages":
        argv = {"pip": ["python", "-m", "pip", "list", "--format=freeze", "--disable-pip-version-check"],
                "npm": ["npm", "ls", "--depth=0"], "gem": ["gem", "list"]}.get(spec.ecosystem)
        return ([(" ".join(argv[:3]), argv)], "") if argv else (None, "not available for this ecosystem")
    if check == "robot_keywords":
        libs, used = _robot_libraries(files)
        return [(f"libdoc {', '.join(libs)}", ["python", "-c", _KEYWORDS_SCRIPT, ",".join(libs)] + used)], ""
    if check.startswith("signature:") and spec.ecosystem == "pip":
        target = check.split(":", 1)[1].strip()
        if not _DOTTED.match(target):
            return None, f"'{target}' is not a dotted Python name"
        return [(f"signature {target}", ["python", "-c", _SIGNATURE_SCRIPT, target])], ""
    if check.startswith("import:") and spec.ecosystem == "pip":
        target = check.split(":", 1)[1].strip()
        if not _DOTTED.match(target):
            return None, f"'{target}' is not a module name"
        return [(f"import {target}", ["python", "-c", _IMPORT_SCRIPT, target])], ""
    if check.startswith("exports:") and spec.ecosystem == "npm":
        target = check.split(":", 1)[1].strip()
        if not _NPM_NAME.match(target):
            return None, f"'{target}' is not an npm package name"
        return [(f"exports {target}", ["node", "-e", _EXPORTS_SCRIPT, target])], ""
    return None, f"unknown check '{check}'"


def run_checks(checks: List[str], spec: FrameworkSpec, manifest: Dict[str, Any], files: Dict[str, str],
               request_id: str, log: Callable[[str], None] = lambda m: None) -> List[Dict[str, Any]]:
    """Runs read-only checks in a fresh sandbox of the tool's environment (the installed packages are reused).
    [{check, label, ok, exit_code, output}] - never raises."""
    from core.wizard.sandbox import create_runner
    plans = []
    results: List[Dict[str, Any]] = []
    for c in checks[:MAX_CHECKS_PER_STEP]:
        runs, problem = _argv_for(str(c).strip(), spec, files)
        if not runs:
            results.append({"check": c, "label": c, "ok": False, "exit_code": None, "output": problem})
        else:
            plans += [(c, label, argv) for label, argv in runs]
    if not plans:
        return results
    try:
        with create_runner(spec, purpose="setup", log=log, env=manifest.get("env_vars"),
                           path_prepend=manifest.get("path_prepend"), name=f"agent-{request_id}") as runner:
            for c, label, argv in plans:
                try:
                    res = runner.execute_script(dict(files), argv, timeout_s=CHECK_TIMEOUT_S)
                    out = (res.output or "").strip()
                    results.append({"check": c, "label": label, "ok": res.ok, "exit_code": res.exit_code,
                                    "output": _scrub(out)[-3500:] or "(no output)"})
                except Exception as e:
                    results.append({"check": c, "label": label, "ok": False, "exit_code": None,
                                    "output": f"{type(e).__name__}: {e}"[:400]})
    except Exception as e:
        results.append({"check": ",".join(checks), "label": "sandbox", "ok": False, "exit_code": None,
                        "output": f"The sandbox could not start: {type(e).__name__}: {e}"[:400]})
    return results


def _scrub(text: str) -> str:
    from core.wizard.tool_setup import _scrub as scrub
    return scrub(text)


# ---------------------------------------------------------------------------------------------------------------
# Pre-checking a proposed fix
# ---------------------------------------------------------------------------------------------------------------
def precheck(fix: Dict[str, Any], spec: FrameworkSpec, manifest: Dict[str, Any], request_id: str,
             current_files: Dict[str, str], log: Callable[[str], None] = lambda m: None
             ) -> Tuple[Dict[str, Any], List[str], List[Dict[str, Any]]]:
    """(validated changes, problems, dry-run results). Problems go back to the agent; no problems = shown to the admin."""
    from core.wizard.tool_setup import _validate_changes
    raw = {k: v for k, v in (fix.get("changes") or {}).items() if k in AGENT_KEYS and k != "smoke_files"}
    if fix.get("smoke_files"):
        raw["smoke_files"] = fix["smoke_files"]
    dropped = [k for k in (fix.get("changes") or {}) if k not in AGENT_KEYS]
    changes, problems = _validate_changes(raw, spec, json.dumps(raw, default=str), trusted=True)
    if dropped:
        problems.append(f"these change keys are not allowed for the agent: {', '.join(dropped)} (ask the admin instead)")
    if not changes and not problems:
        problems.append("the fix changes nothing - propose concrete changes or new test files, or ask the admin")
    tried = []
    files = changes.get("smoke_files")
    if files and not problems:
        if files == current_files:
            problems.append("this is exactly the test that already failed - change what caused the failure")
        else:
            runs = run_checks(["test_dry_run"], spec, manifest, files, request_id, log)
            tried = runs
            for r in runs:
                if r.get("exit_code") is not None and not r["ok"]:
                    problems.append(f"your test failed its dry run ({r['label']}): {r['output'][-1200:]}")
    return changes, problems, tried


def describe(changes: Dict[str, Any]) -> str:
    from core.wizard.synth_labels import describe as d
    rest = {k: v for k, v in changes.items() if k != "smoke_files"}
    parts = [d(rest)] if rest else []
    if changes.get("smoke_files"):
        parts.append("a corrected smoke test (" + ", ".join(sorted(changes["smoke_files"])) + ")")
    return "; ".join(p for p in parts if p) or "no changes"


# ---------------------------------------------------------------------------------------------------------------
# One turn
# ---------------------------------------------------------------------------------------------------------------
def escalation_model() -> Optional[str]:
    """The stronger model the admin can choose for a repeated error (None = not configured)."""
    m = ap.setup_agent_settings().get("escalation_model") or ""
    return m or None


def _model_for(repeated: bool, requested: Optional[str] = None) -> Optional[str]:
    """8.5.1: only the model the ADMIN chose for this turn - a repeated error never switches the model by itself."""
    return requested or None


def _call(vars_: Dict[str, Any], model: Optional[str]):
    from core.wizard.llm import generate_json
    untrusted = ["output", "test_files", "admin_notes", "observations"]
    res = generate_json("SETUP_AGENT_STEP", vars_, untrusted=untrusted, model=model)
    if not res.ok and model:                     # the stronger model isn't available here - use the selected one
        res = generate_json("SETUP_AGENT_STEP", vars_, untrusted=untrusted)
    return res


def turn(request_id: str, r: Dict[str, Any], setup: Dict[str, Any], fd: Dict[str, Any],
         say: Callable[[str, Dict[str, Any]], None] = lambda t, d: None,
         model: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Works the failure and returns the clarification for the admin (or None: not enabled / no AI - the caller falls
    back to the one-shot question). ``say`` posts progress to the setup conversation."""
    from core.wizard.tool_setup import _effective, _server_facts
    spec, manifest = _effective(r)
    state = setup.setdefault("agent", {"turns": []})
    turns = state.setdefault("turns", [])
    sig = signature(fd)
    line = error_line(fd)
    if turns and turns[-1].get("outcome") == "applied" and not turns[-1].get("result"):
        turns[-1]["result"] = f"the setup failed again at {fd.get('stage')}: {line}"
    repeated = any(t.get("signature") == sig for t in turns)
    model = _model_for(repeated, model)
    files = test_files(setup)
    t: Dict[str, Any] = {"n": len(turns) + 1, "at": _now(), "stage": fd.get("stage"), "signature": sig,
                         "requested_model": model,
                         "error_line": line, "repeated": repeated, "steps": [], "feedback": list(state.pop("pending_feedback", []) or [])}
    turns.append(t)
    del turns[:-12]
    observations: List[str] = []
    checks = available_checks(spec, files)
    base_vars = {"tool": json.dumps({k: v for k, v in spec.to_dict().items()
                                     if k in ("id", "name", "language", "ecosystem", "packages", "run_command",
                                              "entry_file", "post_install")}, indent=1, default=str),
                 "server_facts": json.dumps(_server_facts(spec), default=str), "stage": fd.get("stage"),
                 "command": fd.get("command") or "-", "exit_code": fd.get("exit_code"),
                 "message": fd.get("message") or "", "output": (fd.get("output") or "")[-9000:],
                 "test_files": json.dumps(files, indent=1)[:9000] if files else "(no test has run yet)",
                 "history": _history(dict(setup, agent={"turns": turns[:-1]})),
                 "repeated": "yes - " + line if repeated else "no", "lessons": _lessons(spec),
                 "admin_notes": _annotate_notes(t["feedback"]) or "(none)",
                 "checks": "\n".join(f"- {c}: {w}" for c, w in checks)}
    say(f"I'm working on it{' with ' + model + ' (you chose it)' if model else ''}"
        f"{' - this error came back after an earlier fix, so I look for a different cause' if repeated else ''}: "
        f"{line or fd.get('message', '')}", {"agent_turn": t["n"], "repeated": repeated})
    answered = False
    for step in range(MAX_STEPS):
        last = step == MAX_STEPS - 1
        res = _call(dict(base_vars, observations="\n\n".join(observations)[-12000:] or "(none yet)",
                         last_step="yes" if last else "no"), model)
        if not res.ok:
            t["steps"].append({"kind": "error", "text": f"The AI could not answer: {res.error}"[:300]})
            if not answered:                    # no AI at all: the caller asks the one-shot question instead
                turns.pop()
                return None
            break
        answered = True
        d = res.data or {}
        t["model"] = res.model
        action = d.get("action")
        thought = str(d.get("thought") or "")[:600]
        if action == "run_checks" and not last:
            want = [str(c)[:120] for c in (d.get("checks") or [])][:MAX_CHECKS_PER_STEP]
            if not want:
                observations.append("[Understudy] run_checks needs at least one check id from AVAILABLE CHECKS.")
                continue
            say(f"Checking: {', '.join(want)}" + (f" - {thought}" if thought else ""), {"agent_turn": t["n"], "checks": want})
            got = run_checks(want, spec, manifest, files, request_id)
            t["steps"].append({"kind": "checks", "thought": thought, "checks": got})
            observations += [f"[check {g['check']}] $ {g['label']} -> exit {g['exit_code']}\n{g['output']}" for g in got]
            continue
        if action == "propose_fix":
            fix = d.get("fix") if isinstance(d.get("fix"), dict) else {}
            sf = {}
            for f in fix.get("smoke_files") or []:
                if isinstance(f, dict) and f.get("path"):
                    sf[re.sub(r"^(\./)+", "", str(f["path"]).strip().replace("\\", "/")).lstrip("/")] = str(f.get("content") or "")
            fix = dict(fix, smoke_files=sf or None)
            changes, problems, dry = precheck(fix, spec, manifest, request_id, files)
            step_rec = {"kind": "proposal", "thought": thought, "summary": str(fix.get("summary") or "")[:500],
                        "precheck": dry, "problems": problems[:6]}
            t["steps"].append(step_rec)
            if problems and not last:
                say("My first fix didn't pass Understudy's check - correcting it: " + problems[0][:240],
                    {"agent_turn": t["n"]})
                observations.append("[Understudy pre-check of your proposed fix] REJECTED:\n- " + "\n- ".join(problems))
                continue
            if problems:
                t["action"] = "give_up"
                t["thought"] = "The last proposed fix did not pass Understudy's check: " + "; ".join(problems)[:400]
                break
            t["fix"] = {"summary": str(fix.get("summary") or d.get("diagnosis") or "")[:600],
                        "confidence": str(fix.get("confidence") or "")[:10], "changes": changes,
                        "describe": describe(changes), "key": "agent:" + hashlib.sha1(
                            json.dumps(changes, sort_keys=True, default=str).encode()).hexdigest()[:12]}
            t["diagnosis"] = str(d.get("diagnosis") or "")[:800]
            t["action"] = "propose_fix"
            break
        if action in ("ask_admin", "give_up") or last:
            t["action"] = action if action in ("ask_admin", "give_up") else "give_up"
            t["thought"] = thought
            t["diagnosis"] = str(d.get("diagnosis") or "")[:800]
            t["question"] = str(d.get("question") or "")[:800]
            t["options"] = [o for o in (d.get("options") or []) if isinstance(o, dict) and o.get("id")][:4]
            break
    if not t.get("action"):
        t["action"] = "give_up"
    return _clarification(t, fd, spec)


def _annotate_notes(notes: List[str]) -> str:
    """9.4: a path the admin typed that is a runtime Understudy knows is resolved for the agent ('that is install X,
    use_runtime=X') - the agent no longer has to guess what the path means."""
    from core.wizard.tool_setup import _ABS_PATH_RX, _URL_RX, _install_at
    out = []
    for n in notes:
        out.append(n)
        for p in _ABS_PATH_RX.findall(_URL_RX.sub(" ", n or "")):
            rec = _install_at(p.rstrip("/\\"))
            if rec:
                from core.wizard import environments
                out.append(f"[Understudy] {p} is {environments.describe_install(rec)} - works: "
                           f"{environments.runtime_works(rec['kind'], rec.get('path'))}. To use it, propose_fix with "
                           f"changes.use_runtime = '{rec['id']}'.")
    return "\n".join(out)


def _runtime_options(fd: Dict[str, Any], spec: FrameworkSpec) -> List[Dict[str, Any]]:
    """9.4: when the failure is about a runtime Understudy already has, offer it in one click (next to the agent)."""
    from core.wizard.tool_setup import _RULES, _installed_options
    text = f"{fd.get('message') or ''}\n{fd.get('output') or ''}"
    for rx, cat, sub in _RULES:
        if cat == "missing_runtime" and sub and rx.search(text):
            return _installed_options(sub)
    return []


def _clarification(t: Dict[str, Any], fd: Dict[str, Any], spec: FrameworkSpec) -> Dict[str, Any]:
    from core.wizard.tool_setup import _opt
    agent_view = {k: t.get(k) for k in ("n", "steps", "fix", "repeated", "model", "action", "error_line", "diagnosis",
                                        "requested_model")}
    strong = escalation_model()
    # 8.5.1: a repeated error (or no fix) OFFERS the stronger model - the admin decides, nothing switches by itself
    offer = strong if strong and strong != t.get("requested_model") and (t.get("repeated") or not t.get("fix")) else None
    agent_view["offer_model"] = offer
    if t.get("fix"):
        fx = t["fix"]
        options = [dict(_opt("apply_agent_fix", "Approve the agent's fix: " + fx["describe"][:90],
                             fx["summary"][:200]), changes=fx["changes"], fix_key=fx["key"], source="agent"),
                   _opt("agent_retry", "Not right - look again (add a hint below)",
                        "the agent investigates again with your hint; nothing is changed")]
        if offer:
            options.append(_opt("agent_retry_stronger", f"Let {offer} look instead",
                                f"the same failure is investigated again by {offer} (may cost more); nothing is changed"))
        options.append(_opt("stop", "Stop the setup", "the request stays open; prepare the plan again or reject it"))
        question = (f"{t.get('diagnosis') or fx['summary']}\n\nProposed fix: {fx['summary']}"
                    + (f" (confidence: {fx['confidence']})" if fx.get("confidence") else "")
                    + "\nApprove it and I'll apply it, re-run the setup and keep going if anything else fails.")
    else:
        options = [_opt(re.sub(r"[^a-z0-9_]", "", str(o["id"]).lower())[:40] or "answer", str(o.get("label"))[:120],
                        str(o.get("effect") or "")[:200]) for o in t.get("options") or []]
        options = [o for o in options if o["id"] not in ("apply_agent_fix", "agent_retry")]
        rt = _runtime_options(fd, spec)
        if rt:                                      # 9.4: e.g. "Use .NET SDK 8.0.425 (downloaded by Understudy)"
            options = rt + [o for o in options if not o["id"].startswith(("install", "download"))][:2]
        options += [_opt("agent_retry", "Look again with my hint", "type a hint below; the agent investigates again")]
        if offer:
            options.append(_opt("agent_retry_stronger", f"Let {offer} look at it",
                                f"the same failure is investigated again by {offer} (may cost more); nothing is changed"))
        options.append(_opt("retry", "I fixed it on the server - try again", "re-runs the failed step unchanged"))
        question = (t.get("question") or t.get("thought") or
                    f"I couldn't find a safe fix for: {t.get('error_line') or fd.get('message')}. What should I do?")
    if offer and t.get("repeated"):
        question += (f"\n\nThis error came back after an earlier fix. If you like, {offer} can take a look instead "
                     f"of the current model - your choice.")
    return {"diagnosis": (t.get("diagnosis") or t.get("error_line") or "")[:800], "category": "agent",
            "question": question[:1800], "options": options[:6], "agent_source": "ACTUAL_AI_API",
            "model": t.get("model"), "agent": agent_view}


def mark_applied(setup: Dict[str, Any], key: str, admin: str) -> None:
    for t in reversed((setup.get("agent") or {}).get("turns") or []):
        if (t.get("fix") or {}).get("key") == key:
            t.update(outcome="applied", approved_by=admin, approved_at=_now())
            return


def add_feedback(setup: Dict[str, Any], text: str) -> None:
    st = setup.setdefault("agent", {"turns": []})
    if st.get("turns"):
        last = st["turns"][-1]
        if last.get("fix") and not last.get("outcome"):
            last["outcome"] = "rejected by the admin"
    st.setdefault("pending_feedback", []).append(text[:600] or "The admin rejected the proposal without a hint.")


def record_lessons(spec: FrameworkSpec, setup: Dict[str, Any]) -> None:
    """The setup passed: the agent fixes that were applied become lessons for future setups."""
    for t in (setup.get("agent") or {}).get("turns") or []:
        fx = t.get("fix") or {}
        if t.get("outcome") == "applied" and fx and not t.get("result"):     # the fix that made it pass
            t["result"] = "the setup passed"
            ap.add_setup_lesson({"ecosystem": spec.ecosystem, "packages": sorted(p.name.lower() for p in spec.packages),
                                 "signature": t.get("signature"), "error_line": t.get("error_line"),
                                 "summary": fx.get("summary"), "describe": fx.get("describe"), "tool": spec.id})
            activity_log.record("ADMIN", f"Setup agent lesson saved for {spec.name}", fx.get("summary") or "",
                                source="ADMIN")
