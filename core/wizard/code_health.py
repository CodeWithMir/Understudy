"""Code health: AI self-healing for start-up warnings (DeprecationWarning / FutureWarning / PendingDeprecationWarning).

Cycle (runs in the background at start-up and whenever an admin asks; AI steps need a Gemini key):

  1. SCAN      a child Python process imports the backend (``import server``) with those warnings forced on and
               reports each one with the file/line it is attributed to. Warnings raised inside third-party packages
               are listed as information only.
  2. PROPOSE   for each warning in Understudy's own code, the CODE_HEALTH_FIX agent proposes exact find/replace edits.
  3. VERIFY    the edits are applied to a throw-away copy of the code base: every ``find`` must match exactly once,
               the file must compile, the edit may not add dangerous calls (subprocess, eval/exec, network, deletion),
               and a fresh scan of the copy must show the warning gone, no new kind of warning, and the backend still
               importing cleanly.
  4. APPLY     only verified fixes: automatically when the admin turned on auto-apply, otherwise with one click in
               Mission Control. The original file is backed up; any applied fix can be rolled back.

Audit trail (7.1) - so an admin always knows whether the AI changed code, what, why, and how to undo it:
  * every proposal and every applied fix carries a unified diff and a line-by-line change list
    ("lines 12-13 -> 12-16: before ... after ...") next to the AI's diagnosis and explanation;
  * every applied fix is recorded with who applied it (an admin, or "auto-apply" = the AI on its own), the warning that
    triggered it, the verification results and the backup location. The backup folder
    ``generated/code_health/backups/<fix id>/`` holds the original file at its relative path plus ``CHANGE.md``,
    ``change.json`` and ``fix.diff``, so the change is understandable even without Understudy running;
  * each fix shows its live status: ``in_place`` (the file is exactly as the AI left it), ``rolled_back``,
    ``modified_since`` (someone edited the file afterwards) or ``file_missing``;
  * rollback restores the backup; when the file was edited after the fix, ``force`` first saves the current file next
    to the backup (nothing is lost) and then restores the original;
  * ``scripts/code_health.py list | show <id> | revert <id> [--force]`` does the same from a terminal - for the case
    where a change keeps the backend from starting at all;
  * a fix or rollback only takes effect after the backend restarts: ``state()["restart_needed"]`` says so.

Protected files are never changed - above all the pipeline contract ``core/pipeline_contract.py``; warnings
attributed to them are reported with status ``protected``.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PROTECTED = {"core/pipeline_contract.py"}
PATCHABLE_ROOTS = ("core/", "erp/", "server.py")
_COPY_EXCLUDE = {"understudy-ui", "generated", "uploads", "venv", ".venv", "node_modules", ".git", "__pycache__",
                 "skills", "assets", "tests", ".next", ".pytest_cache"}
_DANGEROUS = re.compile(r"\bsubprocess\b|\bos\.system\b|\bos\.popen\b|\beval\(|\bexec\(|__import__\(|\bsocket\b|"
                        r"\brequests\.|\burllib\b|\bhttpx\b|shutil\.rmtree|os\.remove|os\.unlink|\bpickle\b|"
                        r"\bctypes\b|\bmarshal\b")
SCAN_TIMEOUT_S = int(os.environ.get("UNDERSTUDY_CODE_HEALTH_SCAN_TIMEOUT_S") or 240)
_LOCK = threading.RLock()
_RUNNING: Dict[str, Any] = {"busy": False, "task": None}

_SCAN_SCRIPT = r"""
import json, sys, warnings
def _show(message, category, filename, lineno, file=None, line=None):
    sys.stdout.write("UNDERSTUDY_WARNING " + json.dumps({"category": category.__name__, "message": str(message),
                     "filename": filename, "lineno": lineno}) + "\n")
warnings.showwarning = _show
for c in (DeprecationWarning, FutureWarning, PendingDeprecationWarning):
    warnings.simplefilter("always", c)
try:
    import server  # noqa: F401
    sys.stdout.write("UNDERSTUDY_IMPORT_OK\n")
except Exception as e:
    sys.stdout.write("UNDERSTUDY_IMPORT_ERROR " + json.dumps(repr(e)[:2000]) + "\n")
"""


def repo_root() -> str:
    return os.environ.get("UNDERSTUDY_CODE_HEALTH_ROOT") or _BASE


def state_path() -> str:
    return os.path.join(repo_root(), "generated", "code_health", "state.json")


def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def _load() -> Dict[str, Any]:
    try:
        with open(state_path(), encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {"findings": {}, "history": [], "last_scan": None, "environment": {}}


def _save(st: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(state_path()), exist_ok=True)
    tmp = state_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(st, fh, indent=2)
    os.replace(tmp, state_path())


_PROCESS_STARTED = time.time()


def backups_dir() -> str:
    return os.path.join(repo_root(), "generated", "code_health", "backups")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def diff_text(old: str, new: str, rel: str) -> str:
    import difflib
    return "".join(difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                                        fromfile=f"a/{rel} (before)", tofile=f"b/{rel} (after the AI fix)", n=3))


def line_changes(old: str, new: str) -> List[Dict[str, Any]]:
    """What changed, line by line: [{kind, old_lines: [from, to], new_lines: [from, to], before: [...], after: [...]}]
    (1-based, inclusive; an empty range is [n, n-1])."""
    import difflib
    a, b = old.splitlines(), new.splitlines()
    out = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        out.append({"kind": {"replace": "changed", "delete": "removed", "insert": "added"}[tag],
                    "old_lines": [i1 + 1, i2], "new_lines": [j1 + 1, j2],
                    "before": a[i1:i2][:60], "after": b[j1:j2][:60]})
    return out


def _range(r: List[int]) -> str:
    return "-" if r[1] < r[0] else (str(r[0]) if r[0] == r[1] else f"{r[0]}-{r[1]}")


def describe_changes(changes: List[Dict[str, Any]]) -> List[str]:
    """'line 12 changed -> line 12', 'lines 30-31 added (after line 29)', ... - for logs, CHANGE.md and the CLI."""
    out = []
    for c in changes:
        if c["kind"] == "added":
            out.append(f"added lines {_range(c['new_lines'])} (after original line {c['old_lines'][0] - 1})")
        elif c["kind"] == "removed":
            out.append(f"removed original lines {_range(c['old_lines'])}")
        else:
            out.append(f"original lines {_range(c['old_lines'])} -> new lines {_range(c['new_lines'])}")
    return out


def _tel(title: str, detail: str = "", data: Optional[Dict[str, Any]] = None, level: str = "info") -> None:
    try:
        from core.wizard import activity_log
        activity_log.record("ADMIN", title, detail, data, source="CODE_HEALTH", level=level)
    except Exception:
        pass


def _child_env() -> Dict[str, str]:
    from core.wizard.dependency_resolver import _scrubbed_env
    env = _scrubbed_env({"PYTHONDONTWRITEBYTECODE": "1", "UNDERSTUDY_CODE_HEALTH": "off"})
    env.pop("PYTHONWARNINGS", None)
    return env


# ---------------------------------------------------------------------------
# 1. SCAN
# ---------------------------------------------------------------------------
def scan_tree(root: str) -> Dict[str, Any]:
    t0 = time.time()
    try:
        proc = subprocess.run([sys.executable, "-c", _SCAN_SCRIPT], cwd=root, env=_child_env(), capture_output=True,
                              text=True, timeout=SCAN_TIMEOUT_S, shell=False)
        out, err = proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return {"ok": False, "warnings": [], "import_ok": False, "error": f"scan timed out after {SCAN_TIMEOUT_S}s"}
    warnings_: List[Dict[str, Any]] = []
    import_ok, error = False, None
    for line in out.splitlines():
        if line.startswith("UNDERSTUDY_WARNING "):
            try:
                warnings_.append(json.loads(line[len("UNDERSTUDY_WARNING "):]))
            except ValueError:
                pass
        elif line.startswith("UNDERSTUDY_IMPORT_OK"):
            import_ok = True
        elif line.startswith("UNDERSTUDY_IMPORT_ERROR "):
            error = json.loads(line[len("UNDERSTUDY_IMPORT_ERROR "):])
    if not import_ok and not error:
        error = (err or "import failed")[-1500:]
    seen, uniq = set(), []
    for w in warnings_:
        rel = _rel(w["filename"], root)
        w["file"] = rel
        k = (rel, w["lineno"], w["category"], w["message"])
        if k not in seen:
            seen.add(k)
            uniq.append(w)
    return {"ok": import_ok, "import_ok": import_ok, "error": error, "warnings": uniq,
            "duration_s": round(time.time() - t0, 1)}


def _rel(filename: str, root: str) -> Optional[str]:
    try:
        ap_ = os.path.realpath(filename)
        r = os.path.realpath(root)
        if ap_.startswith(r + os.sep):
            rel = os.path.relpath(ap_, r).replace(os.sep, "/")
            if "site-packages" in rel or rel.startswith(("venv/", ".venv/")):
                return None
            return rel
    except Exception:
        pass
    return None


def finding_id(w: Dict[str, Any]) -> str:
    return hashlib.sha1(f"{w.get('file')}|{w['lineno']}|{w['category']}|{w['message'][:200]}".encode()).hexdigest()[:10]


def scan() -> Dict[str, Any]:
    res = scan_tree(repo_root())
    with _LOCK:
        st = _load()
        current = {}
        for w in res["warnings"]:
            fid = finding_id(w)
            old = st["findings"].get(fid, {})
            f = {**old, "id": fid, "file": w["file"], "line": w["lineno"], "category": w["category"],
                 "message": w["message"][:1000], "external_file": None if w["file"] else w["filename"],
                 "last_seen": _now()}
            f.setdefault("first_seen", _now())
            if not w["file"]:
                f["status"] = "third_party"
            elif w["file"] in PROTECTED:
                f["status"] = "protected"
            elif not w["file"].startswith(PATCHABLE_ROOTS) or not w["file"].endswith(".py"):
                f["status"] = "not_patchable"
            else:
                f.setdefault("status", "open")
                if f["status"] in ("resolved",):
                    f["status"] = "open"
            current[fid] = f
        for fid, f in st["findings"].items():
            if fid in current:
                continue
            if f.get("status") in ("applied", "open", "proposed", "failed", "no_fix"):
                f["status"] = "resolved"            # no longer raised (fixed, or the code changed)
            current[fid] = f
        st["findings"] = current
        st["last_scan"] = {"at": _now(), "import_ok": res["import_ok"], "error": res["error"],
                           "count": len(res["warnings"]), "duration_s": res.get("duration_s")}
        _save(st)
    env_scan()
    _tel(f"Code health scan: {len(res['warnings'])} start-up warning(s)", res.get("error") or "",
         {"warnings": [{k: w[k] for k in ("category", "message", "file", "lineno")} for w in res["warnings"]][:30]},
         level="warn" if res["warnings"] or res["error"] else "info")
    return state()


# ---------------------------------------------------------------------------
# Environment health (Python / Node.js end-of-life, npm EBADENGINE / deprecated, pip check) - advice only
# ---------------------------------------------------------------------------
def env_scan() -> List[Dict[str, Any]]:
    from core.wizard import env_health
    try:
        found = env_health.scan()
    except Exception as e:  # pragma: no cover
        _tel("Environment health scan failed", str(e), level="warn")
        found = []
    with _LOCK:
        st = _load()
        old = st.get("environment") or {}
        cur = {}
        for f in found:
            prev = old.get(f["id"], {})
            f["status"] = prev.get("status") if prev.get("status") in ("advised", "ignored") else "open"
            if prev.get("advice"):
                f["advice"] = prev["advice"]
            f["last_seen"] = _now()
            f["first_seen"] = prev.get("first_seen") or _now()
            cur[f["id"]] = f
        for fid, f in old.items():
            if fid not in cur and f.get("status") != "ignored":
                f["status"] = "resolved"
                cur[fid] = f
        st["environment"] = cur
        _save(st)
    if found:
        _tel(f"Environment health: {len(found)} runtime/dependency warning(s)",
             "; ".join(f["title"] for f in found)[:600], {"findings": [f["title"] for f in found]}, level="warn")
    return found


def env_advise(fid: str) -> Dict[str, Any]:
    from core.wizard import env_health
    with _LOCK:
        f = (_load().get("environment") or {}).get(fid)
    if not f:
        raise KeyError(fid)
    advice = env_health.ai_advice(f)
    with _LOCK:
        st = _load()
        if fid in st.get("environment", {}):
            st["environment"][fid]["advice"] = advice
            st["environment"][fid]["status"] = "advised"
            _save(st)
    _tel(f"Upgrade advice for: {f['title']}", advice.get("summary", "")[:400],
         {"finding": fid, "advice": advice})
    return st["environment"][fid]


def env_ignore(fid: str) -> Dict[str, Any]:
    with _LOCK:
        st = _load()
        if fid not in st.get("environment", {}):
            raise KeyError(fid)
        st["environment"][fid]["status"] = "ignored"
        _save(st)
    return state()


# ---------------------------------------------------------------------------
# 2. PROPOSE + 3. VERIFY
# ---------------------------------------------------------------------------
def _installed_packages() -> str:
    try:
        from importlib import metadata
        names = sorted({f"{d.metadata['Name']}=={d.version}" for d in metadata.distributions() if d.metadata.get("Name")},
                       key=str.lower)
        return "\n".join(names[:400])
    except Exception:
        return "(unknown)"


def _apply_edits(content: str, edits: List[Dict[str, str]]) -> str:
    out = content
    for i, e in enumerate(edits, 1):
        find, repl = e.get("find") or "", e.get("replace")
        if not find or repl is None:
            raise ValueError(f"edit {i}: empty find/replace")
        n = out.count(find)
        if n != 1:
            raise ValueError(f"edit {i}: the text to replace occurs {n} times (must be exactly once)")
        out = out.replace(find, repl)
    return out


def _copy_tree(dst: str) -> None:
    root = repo_root()
    for name in os.listdir(root):
        if name in _COPY_EXCLUDE:
            continue
        s = os.path.join(root, name)
        d = os.path.join(dst, name)
        if os.path.isdir(s):
            shutil.copytree(s, d, ignore=shutil.ignore_patterns(*_COPY_EXCLUDE, "*.pyc"))
        elif os.path.isfile(s) and os.path.getsize(s) < 5_000_000:
            shutil.copy2(s, d)


def verify_edits(rel: str, new_content: str, old_content: str, finding: Dict[str, Any]) -> Dict[str, Any]:
    v: Dict[str, Any] = {"compiled": False, "safe": False, "warning_gone": False, "import_ok": False,
                         "new_warnings": [], "passed": False, "log": []}
    try:
        compile(new_content, rel, "exec")
        v["compiled"] = True
    except SyntaxError as e:
        v["log"].append(f"Does not compile: {e}")
        return v
    added = len(_DANGEROUS.findall(new_content)) - len(_DANGEROUS.findall(old_content))
    if added > 0:
        v["log"].append("Rejected: the edit adds network/process/eval/deletion calls.")
        return v
    v["safe"] = True
    with tempfile.TemporaryDirectory(prefix="understudy-codehealth-") as tmp:
        _copy_tree(tmp)
        with open(os.path.join(tmp, rel), "w", encoding="utf-8") as fh:
            fh.write(new_content)
        before_msgs = {(w["category"], w["message"][:200]) for w in _last_warnings()}
        res = scan_tree(tmp)
    v["import_ok"] = res["import_ok"]
    if not res["import_ok"]:
        v["log"].append(f"The backend no longer imports: {res['error']}")
        return v
    still = [w for w in res["warnings"] if w.get("file") == rel and w["category"] == finding["category"]
             and w["message"][:200] == finding["message"][:200]]
    v["warning_gone"] = not still
    v["new_warnings"] = [f"{w['category']}: {w['message'][:160]} ({w.get('file') or 'third-party'}:{w['lineno']})"
                         for w in res["warnings"] if (w["category"], w["message"][:200]) not in before_msgs]
    moved = [w for w in res["warnings"] if w["category"] == finding["category"]
             and w["message"][:200] == finding["message"][:200] and w.get("file") != rel]
    if moved:
        v["log"].append("The same warning is now reported by another file "
                        f"({moved[0].get('file') or 'third-party'}); it becomes the next finding.")
    v["passed"] = v["warning_gone"] and not v["new_warnings"]
    if not v["warning_gone"]:
        v["log"].append("The warning is still raised by this file.")
    if v["new_warnings"]:
        v["log"].append("The edit introduces new warnings.")
    return v


def _last_warnings() -> List[Dict[str, Any]]:
    st = _load()
    return [{"category": f["category"], "message": f["message"]} for f in st["findings"].values()]


def propose(fid: str) -> Dict[str, Any]:
    from core.wizard.llm import ai_available, generate_json, unavailable_reason
    with _LOCK:
        st = _load()
        f = st["findings"].get(fid)
    if not f:
        raise KeyError(fid)
    if f["status"] in ("protected", "third_party", "not_patchable"):
        raise ValueError(f"This warning can't be fixed here ({f['status'].replace('_', ' ')}).")
    if not ai_available():
        raise ValueError(unavailable_reason() or "AI is not available.")
    rel = f["file"]
    path = os.path.join(repo_root(), rel)
    with open(path, encoding="utf-8") as fh:
        old = fh.read()
    res = generate_json("CODE_HEALTH_FIX",
                        {"warning": f"{f['category']}: {f['message']} (at {rel}:{f['line']})", "file_path": rel,
                         "file_content": old, "installed_packages": _installed_packages(),
                         "python_version": sys.version.split()[0]},
                        untrusted=["file_content"])
    proposal: Dict[str, Any] = {"at": _now(), "agent_source": res.agent_source, "model": res.model}
    if not res.ok:
        proposal.update(ok=False, error=res.error)
        status = "failed"
    else:
        data = res.data
        proposal.update(ok=True, diagnosis=str(data.get("diagnosis") or "")[:2000],
                        strategy=data.get("strategy"), explanation=str(data.get("explanation") or "")[:2000],
                        risk=data.get("risk") or "medium", edits=data.get("edits") or [],
                        file_sha256=hashlib.sha256(old.encode()).hexdigest())
        if data.get("strategy") == "no_action" or not proposal["edits"]:
            proposal["verification"] = {"passed": False, "log": ["The agent proposed no change."]}
            status = "no_fix"
        else:
            try:
                new = _apply_edits(old, proposal["edits"])
                proposal["diff"] = diff_text(old, new, rel)
                proposal["changes"] = line_changes(old, new)
                proposal["verification"] = verify_edits(rel, new, old, f)
            except ValueError as e:
                proposal["verification"] = {"passed": False, "log": [str(e)]}
            status = "proposed" if proposal["verification"]["passed"] else "failed"
    with _LOCK:
        st = _load()
        if fid in st["findings"]:
            st["findings"][fid]["proposal"] = proposal
            st["findings"][fid]["status"] = status
            _save(st)
    _tel(f"Code health fix for {rel}:{f['line']} - {status}", proposal.get("diagnosis") or proposal.get("error") or "",
         {"finding": fid, "proposal": {k: v for k, v in proposal.items() if k != "file_sha256"}},
         level="info" if status == "proposed" else "warn")
    return finding(fid)


# ---------------------------------------------------------------------------
# 4. APPLY / ROLLBACK
# ---------------------------------------------------------------------------
def apply(fid: str, by: str = "admin") -> Dict[str, Any]:
    with _LOCK:
        st = _load()
        f = st["findings"].get(fid)
        if not f:
            raise KeyError(fid)
        p = f.get("proposal") or {}
        if f["status"] != "proposed" or not (p.get("verification") or {}).get("passed"):
            raise ValueError("Only a verified proposal can be applied.")
        rel = f["file"]
        if rel in PROTECTED:
            raise ValueError("This file is protected.")
        path = os.path.join(repo_root(), rel)
        with open(path, encoding="utf-8") as fh:
            old = fh.read()
        if hashlib.sha256(old.encode()).hexdigest() != p.get("file_sha256"):
            f["status"] = "open"
            _save(st)
            raise ValueError("The file changed since the fix was proposed - propose it again.")
        new = _apply_edits(old, p["edits"])
        hid = "fix-" + datetime.utcnow().strftime("%Y%m%d%H%M%S") + "-" + fid[:6]
        bdir = os.path.join(backups_dir(), hid)
        os.makedirs(os.path.dirname(os.path.join(bdir, rel)), exist_ok=True)
        with open(os.path.join(bdir, rel), "w", encoding="utf-8") as fh:
            fh.write(old)
        changes = line_changes(old, new)
        rec = {"id": hid, "finding": fid, "file": rel, "at": _now(), "by": by,
               "applied_by": "AI (auto-apply - no admin click)" if by == "auto-apply" else f"admin '{by}'",
               "warning": {"category": f.get("category"), "message": (f.get("message") or "")[:600], "line": f.get("line")},
               "diagnosis": p.get("diagnosis"), "explanation": p.get("explanation"), "strategy": p.get("strategy"),
               "risk": p.get("risk"), "agent_source": p.get("agent_source"), "model": p.get("model"),
               "edits": p.get("edits"), "diff": diff_text(old, new, rel), "changes": changes,
               "summary": describe_changes(changes),
               "verification": {k: (p.get("verification") or {}).get(k) for k in
                                ("compiled", "safe", "warning_gone", "import_ok", "new_warnings", "log")},
               "backup": os.path.relpath(os.path.join(bdir, rel), repo_root()).replace(os.sep, "/"),
               "backup_dir": os.path.relpath(bdir, repo_root()).replace(os.sep, "/"),
               "original_sha256": _sha(old), "new_sha256": _sha(new), "rolled_back": False}
        _write_backup_docs(bdir, rec)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(new)
        st["history"].append(rec)
        f["status"] = "applied"
        f["applied_at"] = _now()
        f["history_id"] = hid
        _save(st)
    _tel(f"AI code change applied to {rel} by {rec['applied_by']} - {len(changes)} change(s): "
         + "; ".join(rec["summary"])[:300] + " (restart the backend to load it)",
         (p.get("diagnosis") or "") + "\n\n" + rec["diff"][:6000],
         {"history_id": hid, "file": rel, "changes": changes, "backup": rec["backup"], "why": p.get("explanation")})
    return state()


def _write_backup_docs(bdir: str, rec: Dict[str, Any]) -> None:
    """CHANGE.md / change.json / fix.diff beside the backed-up original: the change explains itself on disk."""
    try:
        with open(os.path.join(bdir, "fix.diff"), "w", encoding="utf-8") as fh:
            fh.write(rec.get("diff") or "")
        with open(os.path.join(bdir, "change.json"), "w", encoding="utf-8") as fh:
            json.dump({k: v for k, v in rec.items() if k != "diff"}, fh, indent=2)
        w = rec.get("warning") or {}
        md = [f"# AI code change {rec['id']}", "",
              f"* File: `{rec['file']}`", f"* Applied: {rec['at']} by {rec.get('applied_by') or rec.get('by')}",
              f"* Triggered by: {w.get('category')} at line {w.get('line')}: {w.get('message')}",
              f"* Model: {rec.get('model') or '-'} ({rec.get('agent_source') or '-'}) · strategy "
              f"{rec.get('strategy') or '-'} · risk {rec.get('risk') or '-'}",
              f"* Original file (backup): `{rec['backup']}`", "",
              "## Why", "", str(rec.get("diagnosis") or "-"), "", str(rec.get("explanation") or ""), "",
              "## What changed", ""] + [f"* {x}" for x in rec.get("summary") or []] + [
              "", "## Diff", "", "```diff", rec.get("diff") or "", "```", "",
              "## How to undo", "",
              f"* Mission Control > Code health > AI code changes > {rec['id']} > Revert, or",
              f"* `python scripts/code_health.py revert {rec['id']}` (add `--force` if the file was edited afterwards), or",
              f"* copy `{rec['backup']}` back over `{rec['file']}`.",
              "", "Restart the backend afterwards."]
        with open(os.path.join(bdir, "CHANGE.md"), "w", encoding="utf-8") as fh:
            fh.write("\n".join(md) + "\n")
    except Exception:
        pass


def _backup_path(h: Dict[str, Any]) -> str:
    b = h.get("backup") or ""
    return b if os.path.isabs(b) else os.path.join(repo_root(), b)


def change_status(h: Dict[str, Any]) -> str:
    """in_place | rolled_back | modified_since | file_missing - checked against the file on disk right now."""
    path = os.path.join(repo_root(), h["file"])
    try:
        with open(path, encoding="utf-8") as fh:
            cur = _sha(fh.read())
    except FileNotFoundError:
        return "file_missing"
    if h.get("rolled_back"):
        return "rolled_back"
    if cur == h.get("new_sha256"):
        return "in_place"
    if h.get("original_sha256") and cur == h["original_sha256"]:
        return "rolled_back"                   # restored by hand
    return "modified_since"


def _with_details(h: Dict[str, Any]) -> Dict[str, Any]:
    """A history record with its live status (and, for records written before 7.1, a diff rebuilt from the backup)."""
    out = dict(h)
    out["status"] = change_status(h)
    out.setdefault("applied_by", "AI (auto-apply - no admin click)" if h.get("by") == "auto-apply" else f"admin '{h.get('by')}'")
    out["backup_exists"] = os.path.exists(_backup_path(h))
    if not out.get("diff") and out["backup_exists"] and out["status"] == "in_place":
        try:
            with open(_backup_path(h), encoding="utf-8") as fh:
                old = fh.read()
            with open(os.path.join(repo_root(), h["file"]), encoding="utf-8") as fh:
                cur = fh.read()
            out["diff"] = diff_text(old, cur, h["file"])
            out["changes"] = line_changes(old, cur)
            out["summary"] = describe_changes(out["changes"])
        except Exception:
            pass
    return out


def history(include_rolled_back: bool = True) -> List[Dict[str, Any]]:
    return [_with_details(h) for h in reversed(_load()["history"])
            if include_rolled_back or not h.get("rolled_back")]


def history_detail(hid: str) -> Dict[str, Any]:
    h = next((x for x in _load()["history"] if x["id"] == hid), None)
    if not h:
        raise KeyError(hid)
    return _with_details(h)


def backup_content(hid: str) -> str:
    h = history_detail(hid)
    with open(_backup_path(h), encoding="utf-8") as fh:
        return fh.read()


def changes_summary() -> Dict[str, Any]:
    hs = history()
    by_status: Dict[str, int] = {}
    for h in hs:
        by_status[h["status"]] = by_status.get(h["status"], 0) + 1
    last = max([h.get("rolled_back_at") or h["at"] for h in hs], default=None)
    pending = [h for h in hs if (h.get("rolled_back_at") or h["at"]) and _epoch(h.get("rolled_back_at") or h["at"]) > _PROCESS_STARTED]
    return {"total": len(hs), "in_place": by_status.get("in_place", 0), "rolled_back": by_status.get("rolled_back", 0),
            "modified_since": by_status.get("modified_since", 0), "file_missing": by_status.get("file_missing", 0),
            "by_ai_alone": len([h for h in hs if h.get("by") == "auto-apply"]),
            "files_in_place": sorted({h["file"] for h in hs if h["status"] == "in_place"}),
            "last_change_at": last, "restart_needed": bool(pending),
            "restart_files": sorted({h["file"] for h in pending})}


def _epoch(iso: str) -> float:
    """UTC 'YYYY-MM-DDTHH:MM:SSZ' -> epoch seconds."""
    import calendar
    try:
        return float(calendar.timegm(datetime.strptime(iso.rstrip("Z")[:19], "%Y-%m-%dT%H:%M:%S").timetuple()))
    except Exception:
        return 0.0


class RollbackConflict(ValueError):
    """The file was edited after the AI fix: reverting needs force=True (the current file is saved first)."""


def rollback(hid: str, by: str = "admin", force: bool = False) -> Dict[str, Any]:
    """Restores the original file from the backup. If the file was edited after the fix, refuses unless force=True,
    in which case the current file is saved beside the backup first (``before-revert/``) so nothing is lost."""
    with _LOCK:
        st = _load()
        h = next((x for x in st["history"] if x["id"] == hid), None)
        if not h:
            raise KeyError(hid)
        if h.get("rolled_back"):
            raise ValueError(f"{hid} was already rolled back on {h.get('rolled_back_at')} by {h.get('rolled_back_by')}.")
        path = os.path.join(repo_root(), h["file"])
        backup = _backup_path(h)
        if not os.path.exists(backup):
            raise ValueError(f"The backup {h['backup']} no longer exists, so {h['file']} can't be restored automatically.")
        cur = ""
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                cur = fh.read()
        saved_current = None
        if _sha(cur) != h["new_sha256"]:
            if not force:
                raise RollbackConflict(
                    f"{h['file']} was edited after this AI fix was applied. Reverting would also undo those later edits. "
                    f"Use 'Revert anyway' (force) - the current file is saved first - or restore it by hand from "
                    f"{h['backup']}.")
            saved_current = os.path.join(backups_dir(), hid, "before-revert", h["file"])
            os.makedirs(os.path.dirname(saved_current), exist_ok=True)
            with open(saved_current, "w", encoding="utf-8") as fh:
                fh.write(cur)
        with open(backup, encoding="utf-8") as fh:
            orig = fh.read()
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(orig)
        h["rolled_back"] = True
        h["rolled_back_at"] = _now()
        h["rolled_back_by"] = by
        h["rolled_back_forced"] = bool(saved_current)
        if saved_current:
            h["saved_before_revert"] = os.path.relpath(saved_current, repo_root()).replace(os.sep, "/")
        if h["finding"] in st["findings"]:
            st["findings"][h["finding"]]["status"] = "open"
        _save(st)
    _tel(f"AI code change {hid} reverted by {by}: {h['file']} restored from {h['backup']} (restart the backend to load it)",
         ("The file had been edited after the fix; that version was saved to " + h["saved_before_revert"])
         if saved_current else "", {"history_id": hid, "file": h["file"], "forced": bool(saved_current)})
    return state()


def ignore(fid: str) -> Dict[str, Any]:
    with _LOCK:
        st = _load()
        if fid not in st["findings"]:
            raise KeyError(fid)
        st["findings"][fid]["status"] = "ignored"
        _save(st)
    return state()


# ---------------------------------------------------------------------------
# Background cycle + views
# ---------------------------------------------------------------------------
def run_cycle(auto_apply: Optional[bool] = None, max_rounds: int = 6) -> Dict[str, Any]:
    """scan -> propose/verify every open finding (AI) -> apply verified fixes if auto-apply is on -> rescan."""
    from core.wizard import admin_policy
    from core.wizard.llm import ai_available
    auto = admin_policy.code_health_auto_apply() if auto_apply is None else auto_apply
    scan()
    if ai_available():
        for f in list((_load().get("environment") or {}).values()):
            if f.get("status") == "open":
                try:
                    env_advise(f["id"])
                except Exception as e:
                    _tel("Environment advice failed", str(e), level="warn")
    for _ in range(max_rounds):
        if not ai_available():
            break
        todo = [f for f in _load()["findings"].values() if f.get("status") == "open"]
        if not todo:
            break
        applied_any = False
        for f in todo:
            try:
                res = propose(f["id"])
            except Exception as e:  # keep going with the other findings
                _tel(f"Code health: could not propose a fix for {f['file']}", str(e), level="warn")
                continue
            if auto and res.get("status") == "proposed":
                try:
                    apply(f["id"], by="auto-apply")
                    applied_any = True
                except Exception as e:
                    _tel("Code health: auto-apply failed", str(e), level="warn")
        if not applied_any:
            break
        scan()                     # an applied fix can reveal the next file that raises the same warning
    return state()


def start_background(task: str = "cycle", fid: Optional[str] = None, **kw) -> bool:
    with _LOCK:
        if _RUNNING["busy"]:
            return False
        _RUNNING.update(busy=True, task=task, started=_now())

    def _job():
        try:
            if task == "scan":
                scan()
            elif task == "propose" and fid:
                propose(fid)
            elif task == "advise" and fid:
                env_advise(fid)
            else:
                run_cycle(**kw)
        except Exception as e:
            _tel(f"Code health {task} failed", f"{type(e).__name__}: {e}", level="error")
        finally:
            with _LOCK:
                _RUNNING.update(busy=False, task=None)

    threading.Thread(target=_job, daemon=True, name=f"code-health-{task}").start()
    return True


def enabled() -> bool:
    return (os.environ.get("UNDERSTUDY_CODE_HEALTH") or "on").lower() not in ("0", "off", "false", "no")


def finding(fid: str) -> Dict[str, Any]:
    return _load()["findings"][fid]


def state() -> Dict[str, Any]:
    from core.wizard import admin_policy
    from core.wizard.llm import ai_available
    st = _load()
    order = {"proposed": 0, "open": 1, "failed": 2, "no_fix": 3, "applied": 4, "protected": 5, "third_party": 6,
             "not_patchable": 7, "ignored": 8, "resolved": 9}
    findings = sorted(st["findings"].values(), key=lambda f: (order.get(f.get("status"), 10), f.get("file") or ""))
    sev = {"error": 0, "warn": 1, "info": 2}
    env = sorted((st.get("environment") or {}).values(),
                 key=lambda f: (f.get("status") in ("ignored", "resolved"), sev.get(f.get("severity"), 3), f["title"]))
    return {"environment": env, "findings": findings, "history": history()[:50], "changes": changes_summary(),
            "backups_dir": os.path.relpath(backups_dir(), repo_root()).replace(os.sep, "/"),
            "last_scan": st.get("last_scan"),
            "busy": _RUNNING["busy"], "task": _RUNNING.get("task"), "ai_available": ai_available(),
            "auto_apply": admin_policy.code_health_auto_apply(), "enabled": enabled(),
            "protected_files": sorted(PROTECTED)}


_WATCHER = {"started": False}


def start_watcher(interval_s: int = 30) -> None:
    """Start-up hook: scan once, and run the AI fix cycle as soon as a Gemini key is available (at start-up, or
    later when an admin sets one in Mission Control)."""
    if not enabled() or _WATCHER["started"]:
        return
    _WATCHER["started"] = True

    def _loop():
        from core.wizard.llm import ai_available
        time.sleep(5)                                    # let the server finish starting
        ai_done = False
        start_background("cycle")
        ai_done = ai_available()
        while True:
            time.sleep(interval_s)
            if not ai_done and ai_available():
                ai_done = start_background("cycle")

    threading.Thread(target=_loop, daemon=True, name="code-health-watcher").start()
