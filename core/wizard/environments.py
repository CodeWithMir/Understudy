"""Environments (8.4): which language runtimes the Understudy server has, which one every approved tool uses, and the
versions each tool needs.

* ``inventory()``: every installed version per runtime kind - java (JDKs), python, node, ruby, go, dotnet, maven,
  gradle - found on PATH, in the usual install folders (``/usr/lib/jvm``, pyenv, nvm, sdkman, Program Files ...),
  registered in Mission Control or downloaded by Understudy. Each one is started once to read its real version. Several
  versions live side by side; the one marked ``default`` is what a tool gets when nobody picked one for it.
* ``tool_views()``: for every approved tool, the version of each runtime it uses right now, where it comes from (picked
  in Mission Control, set during its setup, or the server default), its requirements and whether they are met.
* ``select()``: the admin points one tool at another installed version (no typing of paths). Java and the PATH-based
  runtimes are applied through the tool's run settings; Python through the interpreter its isolated environment is
  built with (``dependency_resolver.plan_for`` - the next run rebuilds it). The tool's smoke test is re-run.
* Requirements: "java >=17", "python >=3.9,<=3.12", "node >=18" ... from Understudy's rules for the tool's ecosystem,
  the AI stack assistant's ``runtime_requirements`` (recorded when the tool becomes ACTIVE) and the admin's own edits
  (which win). ``check()`` runs before every automation run; an unmet requirement blocks the run with a one-click fix -
  switch to an installed version that fits, or download one - which the admin approves.
"""
from __future__ import annotations

import glob
import hashlib
import os
import re
import shutil
import sys
import threading
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.wizard import activity_log, admin_policy as ap, runtimes

KINDS: Dict[str, Dict[str, Any]] = {
    "java": {"label": "Java (JDK)", "exe": ["java"], "env": "JAVA_HOME"},
    "python": {"label": "Python", "exe": ["python3", "python"], "env": ""},
    "node": {"label": "Node.js", "exe": ["node"], "env": ""},
    "ruby": {"label": "Ruby", "exe": ["ruby"], "env": ""},
    "go": {"label": "Go", "exe": ["go"], "env": "GOROOT"},
    "dotnet": {"label": ".NET SDK", "exe": ["dotnet"], "env": "DOTNET_ROOT"},
    "maven": {"label": "Apache Maven", "exe": ["mvn"], "env": "MAVEN_HOME"},
    "gradle": {"label": "Gradle", "exe": ["gradle"], "env": "GRADLE_HOME"},
}
# runtime kind of a download key, and the newest version its line can bring (for requirement checks).
# 9.9: only the fallback - ``downloads()`` lists what the official sources offer now (runtime_catalog)
DOWNLOADS: Dict[str, List[Tuple[str, str]]] = {
    "java": [("jdk-21", "21.0.999"), ("jdk-17", "17.0.999")],
    "python": [("python-3.13", "3.13.999"), ("python-3.12", "3.12.999"), ("python-3.11", "3.11.999"),
               ("python-3.10", "3.10.999")],
    "node": [("node-22", "22.999"), ("node-20", "20.999")],
    "ruby": [("ruby-3", "3.999")], "go": [("go-1", "1.999")], "dotnet": [("dotnet-8", "8.0.999")],
    "maven": [("maven-3", "3.999")], "gradle": [("gradle-9", "9.999"), ("gradle-8", "8.999")],
}
ECO_KINDS = {"pip": ["python"], "npm": ["node"], "maven": ["java", "maven"], "gradle": ["java", "gradle"],
             "gem": ["ruby"], "go": ["go"], "nuget": ["dotnet"]}
# Understudy's own knowledge: what current releases of each ecosystem's test tools need. The AI's requirements and the
# admin's edits refine these per tool.
RULES: List[Tuple[Optional[str], Optional[str], str, str, str]] = [
    ("pip", None, "python", ">=3.9", "Current releases of Python test tools need Python 3.9 or newer."),
    ("pip", r"^robotframework-browser", "node", ">=18",
     "The Robot Framework Browser library drives Playwright through Node.js 18 or newer."),
    ("npm", None, "node", ">=18", "Current Node.js test tools (Playwright, WebdriverIO, Cypress, Puppeteer) need "
                                  "Node.js 18 or newer."),
    ("maven", None, "java", ">=11", "Selenium 4 and current Java test libraries need Java 11 or newer."),
    ("gradle", None, "java", ">=11", "Selenium 4 and current Java test libraries need Java 11 or newer."),
    ("maven", r"karate", "java", ">=17", "Karate 1.5 and newer need Java 17 or newer."),
    ("gradle", r"karate", "java", ">=17", "Karate 1.5 and newer need Java 17 or newer."),
    ("nuget", None, "dotnet", ">=8", "The project is built for .NET 8."),
    ("gem", None, "ruby", ">=3.0", "Current Ruby test gems need Ruby 3."),
    ("go", None, "go", ">=1.21", "Current Go test modules need Go 1.21 or newer."),
]
SRC_LABEL = {"backend": "Understudy's own Python", "system": "installed on the server", "downloaded":
             "downloaded by Understudy", "registered": "registered in Mission Control"}
_CACHE: Dict[str, Any] = {}
_LOCK = threading.Lock()
_VERIFY: Dict[str, Dict[str, Any]] = {}
CACHE_S = 120


class EnvironmentError_(ValueError):
    def __init__(self, message: str, status_code: int = 400, details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.status_code = status_code
        self.details = details or {}


def _now() -> str:
    return datetime.utcnow().isoformat()


# ---------------------------------------------------------------------------------------------------------------
# Versions and requirement specs
# ---------------------------------------------------------------------------------------------------------------
def vtuple(version: str, kind: str = "") -> Tuple[int, ...]:
    """'17.0.2' -> (17, 0, 2); Java's '1.8.0_292' -> (8, 0, 292); 'v20.11.1' -> (20, 11, 1)."""
    nums = [int(x) for x in re.findall(r"\d+", str(version or ""))[:4]]
    if kind == "java" and len(nums) >= 2 and nums[0] == 1:
        nums = nums[1:]
    return tuple(nums)


_CLAUSE = re.compile(r"^(>=|<=|==|!=|~=|>|<)\s*v?(\d+(?:\.\d+){0,3})(?:\.\*)?$")


def normalize_spec(text: str) -> str:
    """The admin's or the AI's wording -> a spec string: '17+' -> '>=17', '3.9-3.12' -> '>=3.9,<=3.12', '20' -> '==20',
    '>=3.9,<3.13' stays, 'any' stays. Raises ValueError for anything else."""
    t = str(text or "").strip().lower().replace("\u2013", "-").replace("\u2014", "-").replace(" ", "")
    t = re.sub(r"^(java|jdk|python|node(\.js)?|ruby|go|\.net|dotnet|maven|gradle)", "", t)
    if t in ("", "any", "*"):
        return "any"
    m = re.match(r"^v?(\d+(?:\.\d+){0,3})\+$", t) or re.match(r"^v?(\d+(?:\.\d+){0,3})(?:ornewer|orlater|orhigher)$", t)
    if m:
        return f">={m.group(1)}"
    m = re.match(r"^v?(\d+(?:\.\d+){0,3})(?:-|to|\.\.)v?(\d+(?:\.\d+){0,3})$", t)
    if m:
        return f">={m.group(1)},<={m.group(2)}"
    if re.match(r"^v?\d+(?:\.\d+){0,3}(?:\.x|\.\*)?$", t):
        return "==" + re.sub(r"(\.x|\.\*)$", "", t.lstrip("v"))
    clauses = [c for c in t.split(",") if c]
    if not clauses or len(clauses) > 4 or not all(_CLAUSE.match(c) for c in clauses):
        raise ValueError(f"'{text}' is not a version requirement - write e.g. 17+, >=3.9, 3.9-3.12 or >=18,<23.")
    return ",".join(f"{_CLAUSE.match(c).group(1)}{_CLAUSE.match(c).group(2)}" for c in clauses)


def satisfies(version: str, spec: str, kind: str = "") -> bool:
    """Each clause compares the version cut (or zero-padded) to the clause's precision: Java 17.0.2 meets '>=17' and
    '==17'; Python 3.12.7 meets '<=3.12' but not '<3.12'."""
    spec = (spec or "any").strip()
    if spec == "any":
        return True
    v = vtuple(version, kind)
    if not v:
        return False
    for clause in spec.split(","):
        m = _CLAUSE.match(clause.strip())
        if not m:
            return False
        op, b = m.group(1), tuple(int(x) for x in m.group(2).split("."))
        if op == "~=":                                   # ~=3.9 -> >=3.9 and 3.x
            if not satisfies(version, f">={m.group(2)}", kind) or (len(b) > 1 and not satisfies(
                    version, "==" + ".".join(str(x) for x in b[:-1]), kind)):
                return False
            continue
        vv = (v + (0,) * len(b))[:len(b)]
        ok = {">=": vv >= b, "<=": vv <= b, ">": vv > b, "<": vv < b, "==": vv == b, "!=": vv != b}[op]
        if not ok:
            return False
    return True


def describe(spec: str) -> str:
    if not spec or spec == "any":
        return "any version"
    parts = []
    for clause in spec.split(","):
        m = _CLAUSE.match(clause)
        if not m:
            continue
        op, v = m.group(1), m.group(2)
        parts.append({">=": f"{v} or newer", "<=": f"{v} or older", ">": f"newer than {v}", "<": f"older than {v}",
                      "==": f"{v}.x" if v.count(".") < 2 else v, "!=": f"not {v}", "~=": f"compatible with {v}"}[op])
    return " and ".join(parts)


_TEXT_RULES: List[Tuple[str, re.Pattern]] = [
    ("java", re.compile(r"(?i)\b(?:java|jdk|openjdk|temurin)\s*(?:se\s*)?(?:version\s*)?(\d{1,2})\b(?!\.\d)")),
    ("node", re.compile(r"(?i)\bnode(?:\.?js)?\s*(?:v)?(\d{1,2})(?:\.\d+)*")),
    ("ruby", re.compile(r"(?i)\bruby\s*(\d\.\d)")),
    ("go", re.compile(r"(?i)\bgo(?:lang)?\s*(1\.\d{1,2})")),
    ("dotnet", re.compile(r"(?i)\.net\s*(?:sdk\s*)?(\d{1,2})")),
    ("maven", re.compile(r"(?i)\bmaven\s*(3\.\d)")),
    ("gradle", re.compile(r"(?i)\bgradle\s*(\d{1,2})")),
]
_PY_RANGE = re.compile(r"(?i)\bpython\s*(3\.\d{1,2})\s*(?:-|\u2013|to|through)\s*(3\.\d{1,2})")
_PY_MIN = re.compile(r"(?i)\bpython\s*(3\.\d{1,2})")


def parse_requirement_text(text: str) -> Optional[Tuple[str, str]]:
    """'Java 17+ JDK' -> ('java', '>=17'); 'Python 3.9-3.12' -> ('python', '>=3.9,<=3.12'); 'Google Chrome' -> None."""
    t = str(text or "")
    m = _PY_RANGE.search(t)
    if m:
        return "python", f">={m.group(1)},<={m.group(2)}"
    m = _PY_MIN.search(t)
    if m:
        return "python", f">={m.group(1)}"
    for kind, rx in _TEXT_RULES:
        m = rx.search(t)
        if m:
            return kind, f">={m.group(1)}"
    return None


# ---------------------------------------------------------------------------------------------------------------
# What the server has
# ---------------------------------------------------------------------------------------------------------------
def _exe_names(kind: str) -> List[str]:
    names = list(KINDS[kind]["exe"])
    if os.name == "nt":
        return [n + ext for n in names for ext in (".exe", ".cmd", ".bat")]
    return names


def _home() -> str:
    return os.path.expanduser("~")


def _globs(kind: str) -> List[str]:
    h = _home()
    if os.name == "nt":
        pf = [os.environ.get(k) for k in ("PROGRAMFILES", "PROGRAMFILES(X86)")] + [r"C:\Program Files"]
        la = os.environ.get("LOCALAPPDATA", "")
        pf = [p for p in pf if p]
        return {
            "python": [os.path.join(la, "Programs", "Python", "Python3*", "python.exe"), r"C:\Python3*\python.exe"]
            + [os.path.join(p, "Python3*", "python.exe") for p in pf]
            + [os.path.join(h, ".pyenv", "pyenv-win", "versions", "*", "python.exe")],
            "node": [os.path.join(p, "nodejs", "node.exe") for p in pf]
            + [os.path.join(os.environ.get("APPDATA", ""), "nvm", "v*", "node.exe")],
            "ruby": [r"C:\Ruby*\bin\ruby.exe"],
            "go": [os.path.join(p, "Go", "bin", "go.exe") for p in pf] + [os.path.join(h, "sdk", "go*", "bin", "go.exe")],
            "dotnet": [os.path.join(p, "dotnet", "dotnet.exe") for p in pf],
            "maven": [os.path.join(p, "*maven*", "bin", "mvn.cmd") for p in pf] + [r"C:\tools\*maven*\bin\mvn.cmd"],
            "gradle": [os.path.join(p, "*gradle*", "bin", "gradle.bat") for p in pf] + [r"C:\Gradle\*\bin\gradle.bat"],
        }.get(kind, [])
    base = {
        "python": ["/usr/bin/python3*", "/usr/local/bin/python3*", "/opt/homebrew/bin/python3*",
                   "/opt/python*/bin/python3", os.path.join(h, ".pyenv", "versions", "*", "bin", "python3"),
                   "/Library/Frameworks/Python.framework/Versions/*/bin/python3"],
        "node": [os.path.join(h, ".nvm", "versions", "node", "*", "bin", "node"), "/usr/local/bin/node",
                 "/opt/homebrew/bin/node", os.path.join(h, ".volta", "tools", "image", "node", "*", "bin", "node"),
                 os.path.join(h, ".local", "share", "fnm", "node-versions", "*", "installation", "bin", "node")],
        "ruby": [os.path.join(h, ".rbenv", "versions", "*", "bin", "ruby"), os.path.join(h, ".rvm", "rubies", "*", "bin", "ruby"),
                 "/usr/local/bin/ruby", "/opt/homebrew/opt/ruby/bin/ruby"],
        "go": ["/usr/local/go/bin/go", os.path.join(h, "sdk", "go*", "bin", "go"), "/usr/lib/go-*/bin/go"],
        "dotnet": ["/usr/share/dotnet/dotnet", "/usr/lib/dotnet/dotnet", os.path.join(h, ".dotnet", "dotnet"),
                   "/usr/local/share/dotnet/dotnet"],
        "maven": [os.path.join(h, ".sdkman", "candidates", "maven", "*", "bin", "mvn"), "/opt/*maven*/bin/mvn",
                  "/usr/share/maven/bin/mvn"],
        "gradle": [os.path.join(h, ".sdkman", "candidates", "gradle", "*", "bin", "gradle"), "/opt/gradle*/bin/gradle",
                   "/opt/gradle/*/bin/gradle"],
    }
    return base.get(kind, [])


_PY_NAME = re.compile(r"^python3(\.\d{1,2})?(\.exe)?$|^python(\.exe)?$", re.I)


def _version_of(kind: str, exe: str) -> str:
    from core.wizard import package_managers as pms
    if kind == "java":                                  # the full version (21.0.8), not just the release (21)
        from core.wizard import jdk
        full = pms._version(exe)
        if re.match(r"^\d+(\.\d+)+", full or ""):
            return full
        info = jdk.probe(exe)
        return str(info["version"]) if info else full
    return pms._version(exe)


def _record(kind: str, exe: str, source: str, version: str = "", key: str = "", rid: str = "") -> Dict[str, Any]:
    exe = os.path.abspath(exe)
    bindir = os.path.dirname(exe)
    home = bindir if kind == "dotnet" or (os.name == "nt" and kind in ("python", "node")) else os.path.dirname(bindir)
    if kind == "python" and os.name != "nt" and os.path.basename(bindir) != "bin":
        home = bindir
    env: Dict[str, str] = {}
    if KINDS[kind]["env"]:
        env[KINDS[kind]["env"]] = home
    real = os.path.realpath(exe)
    return {"id": rid or f"{kind}-{hashlib.sha1(real.encode()).hexdigest()[:10]}", "kind": kind, "path": exe,
            "real": real, "home": home, "bin": bindir, "env": env, "version": version or _version_of(kind, exe),
            "sources": [source], "download_key": key or "", "default": False}


def _add(found: Dict[str, Dict[str, Any]], rec: Optional[Dict[str, Any]]) -> None:
    if not rec or not rec.get("version"):
        return
    # Understudy's own Python stays its own row when it runs in a venv (the venv has the backend's packages)
    k = rec["id"] if rec["id"].endswith("-backend") and _in_venv() else rec["real"]
    if k in found:
        for s in rec["sources"]:
            if s not in found[k]["sources"]:
                found[k]["sources"].append(s)
        if rec.get("download_key") and not found[k].get("download_key"):
            found[k]["download_key"] = rec["download_key"]
        return
    found[k] = rec


def _downloaded() -> List[Tuple[str, Dict[str, Any]]]:
    out = []
    for meta in glob.glob(os.path.join(runtimes.RUNTIMES_DIR, "*", ".understudy-runtime.json")):
        key = os.path.basename(os.path.dirname(meta))
        info = runtimes.installed(key)
        if info:
            out.append((key, info))
    return out


def _kind_of_key(key: str) -> str:
    k = runtimes.kind_of(key)
    return {"jdk": "java"}.get(k, k)


def _scan(kind: str) -> List[Dict[str, Any]]:
    from core.wizard.dependency_resolver import toolchain_path
    found: Dict[str, Dict[str, Any]] = {}
    if kind == "python":
        _add(found, _record("python", sys.executable, "backend", "%d.%d.%d" % sys.version_info[:3], rid="python-backend"))
    if kind == "java":
        from core.wizard import jdk
        for info in jdk.find_jdks():
            exe = jdk._exe(str(info["home"]))
            _add(found, _record("java", exe if os.path.isfile(exe) else str(info["java"]), "system"))
    # downloaded by Understudy (side by side or registered)
    for key, info in _downloaded():
        if _kind_of_key(key) != kind or not info.get("bin"):
            continue
        exe = info.get("exe") or next((shutil.which(n, path=info["bin"]) for n in _exe_names(kind)
                                       if shutil.which(n, path=info["bin"])), None)
        if exe:
            _add(found, _record(kind, exe, "downloaded", key=key))
    # registered in Mission Control > Package managers
    tc = ap.toolchains().get({"java": "jdk"}.get(kind, kind))
    if tc and tc.get("bin") and os.path.isdir(tc["bin"]):
        exe = next((shutil.which(n, path=tc["bin"]) for n in _exe_names(kind) if shutil.which(n, path=tc["bin"])), None)
        if exe:
            _add(found, _record(kind, exe, "registered"))
    # PATH (the toolchain PATH Understudy installs and runs with) and the usual install folders
    for path in (toolchain_path(os.environ.get("PATH", "")), os.environ.get("PATH", "")):
        for n in _exe_names(kind):
            exe = shutil.which(n, path=path)
            if exe and not (kind == "python" and os.path.realpath(exe) == os.path.realpath(sys.executable)
                            and _in_venv()):
                _add(found, _record(kind, exe, "system"))
    for pat in _globs(kind):
        for exe in sorted(glob.glob(pat)):
            if not os.path.isfile(exe) or (kind == "python" and not _PY_NAME.match(os.path.basename(exe))):
                continue
            _add(found, _record(kind, exe, "system"))
    rows = list(found.values())
    rows.sort(key=lambda r: vtuple(r["version"], kind), reverse=True)
    return rows


def _in_venv() -> bool:
    return getattr(sys, "base_prefix", sys.prefix) != sys.prefix


def _default_exe(kind: str) -> Optional[str]:
    """What a tool gets when nobody picked a version for it."""
    from core.wizard.dependency_resolver import toolchain_env, toolchain_path
    if kind == "python":
        return sys.executable
    if kind == "java":
        from core.wizard import jdk
        env = dict(os.environ)
        env.update(toolchain_env())
        env["PATH"] = toolchain_path(env.get("PATH", ""))
        info = jdk.maven_jdk(env)
        return str(info["java"]) if info else None
    for n in _exe_names(kind):
        p = shutil.which(n, path=toolchain_path(os.environ.get("PATH", "")))
        if p:
            return p
    return None


def inventory(refresh: bool = False) -> Dict[str, List[Dict[str, Any]]]:
    """{kind: [install, ...]} newest first. Cached for a couple of minutes (every install is started once)."""
    with _LOCK:
        if not refresh and _CACHE.get("inv") and time.time() - _CACHE["at"] < CACHE_S:
            return _CACHE["inv"]
    if refresh:
        from core.wizard import package_managers as pms
        pms._VERSION_CACHE.clear()
    inv: Dict[str, List[Dict[str, Any]]] = {}
    for kind in KINDS:
        try:
            rows = _scan(kind)
        except Exception as e:                                   # one broken probe never hides the rest
            activity_log.record("ADMIN", f"Could not list the {KINDS[kind]['label']} installs", f"{type(e).__name__}: {e}",
                                source="ADMIN", level="warn")
            rows = []
        d = _default_exe(kind)
        if d:
            real = os.path.realpath(d)
            for r in rows:
                if (kind == "python" and r["id"] == "python-backend") or (kind != "python" and r["real"] == real):
                    r["default"] = True
                    break
        inv[kind] = rows
    with _LOCK:
        _CACHE.update(inv=inv, at=time.time())
    return inv


def find_install(install_id: str) -> Optional[Dict[str, Any]]:
    for refresh in (False, True):
        for rows in inventory(refresh).values():
            for r in rows:
                if r["id"] == install_id:
                    return r
    return None


def _match(kind: str, exe: Optional[str]) -> Optional[Dict[str, Any]]:
    if not exe:
        return None
    real = os.path.realpath(exe)
    for r in inventory().get(kind, []):
        if r["real"] == real or r["path"] == exe:
            return r
    return None


def downloads() -> Dict[str, List[Tuple[str, str]]]:
    """{kind: [(key, newest version of the line)]} - supported lines first (newest first), then end-of-life ones."""
    from core.wizard import runtime_catalog
    out: Dict[str, List[Tuple[str, str]]] = {}
    for e in sorted(runtime_catalog.entries(), key=lambda e: e["eol"]):
        if e["kind"] in KINDS:
            out.setdefault(e["kind"], []).append((e["key"], e["nominal"]))
    return out or DOWNLOADS


def downloadable() -> List[Dict[str, Any]]:
    """9.9: every release line the official sources offer, with its support status."""
    from core.wizard import runtime_catalog
    out = []
    for e in runtime_catalog.entries():
        if e["kind"] not in KINDS:
            continue
        have = runtimes.installed(e["key"])
        out.append({"key": e["key"], "kind": e["kind"], "label": e["label"], "installed": bool(have),
                    "version": (have or {}).get("version") or "", "line": e["line"], "eol": e["eol"],
                    "eol_date": e["eol_date"], "lts": e["lts"], "note": e["note"], "source": e["source"]})
    return out


# ---------------------------------------------------------------------------------------------------------------
# Per tool
# ---------------------------------------------------------------------------------------------------------------
def _spec(tool_id: str):
    spec = ap.allowed_spec(tool_id)
    if spec is None:
        raise EnvironmentError_("This tool isn't approved.", 404)
    return spec


def derived_requirements(spec) -> Dict[str, Dict[str, Any]]:
    names = [p.name.lower() for p in spec.packages]
    out: Dict[str, Dict[str, Any]] = {}
    for eco, pkg_rx, kind, req, reason in RULES:
        if eco and eco != spec.ecosystem:
            continue
        if pkg_rx and not any(re.search(pkg_rx, n) for n in names):
            continue
        prev = out.get(kind)
        # a package-specific rule is stricter than the ecosystem's
        if prev is None or pkg_rx:
            out[kind] = {"spec": req, "source": "rule", "reason": reason}
    return out


def requirements(tool_id: str, spec=None) -> Dict[str, Dict[str, Any]]:
    """Understudy's rules, refined by what the AI recorded and the admin set (stored ones win, per kind)."""
    spec = spec or _spec(tool_id)
    out = derived_requirements(spec)
    out.update(ap.tool_requirements(tool_id))
    return out


def kinds_for(spec, reqs: Optional[Dict[str, Any]] = None) -> List[str]:
    kinds = list(ECO_KINDS.get(spec.ecosystem, []))
    for k in (reqs or {}):
        if k in KINDS and k not in kinds:
            kinds.append(k)
    return kinds


def current(tool_id: str, kind: str, spec=None, rt: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The version of ``kind`` the tool's runs use right now and where it comes from."""
    from core.wizard.dependency_resolver import toolchain_env, toolchain_path
    spec = spec or _spec(tool_id)
    rt = rt if rt is not None else ap.tool_runtime(tool_id)
    picked = (rt.get("versions") or {}).get(kind) or {}
    exe, source = None, "server default"
    if picked.get("path") and os.path.isfile(picked["path"]):
        exe, source = picked["path"], "picked in Mission Control"
    elif kind == "python":
        interp = (spec.sources or {}).get("interpreter")
        if interp:
            from core.wizard.frameworks import resolve_interpreter
            exe, source = resolve_interpreter(interp), "set during the tool's setup"
        else:
            exe = sys.executable
    elif kind == "java":
        from core.wizard import jdk
        env = dict(os.environ)
        env.update(toolchain_env())
        env["PATH"] = toolchain_path(env.get("PATH", ""))
        env.update(rt.get("env_vars") or {})
        info = jdk.maven_jdk(env, rt.get("path_prepend"))
        if info:
            exe = str(info["java"])
            if (rt.get("env_vars") or {}).get("JAVA_HOME"):
                source = "set during the tool's setup" if rt.get("java_home") else "the tool's run settings"
    else:
        path = os.pathsep.join(list(rt.get("path_prepend") or []) + [toolchain_path(os.environ.get("PATH", ""))])
        exe = next((shutil.which(n, path=path) for n in _exe_names(kind) if shutil.which(n, path=path)), None)
        if exe and rt.get("path_prepend") and os.path.dirname(exe) in rt["path_prepend"]:
            source = "the tool's run settings"
    rec = _match(kind, exe)
    version = (rec or {}).get("version") or (_version_of(kind, exe) if exe and os.path.isfile(exe) else "")
    return {"kind": kind, "path": exe or "", "version": version, "install_id": (rec or {}).get("id") or "",
            "source": source, "picked": bool(picked and source == "picked in Mission Control"), "found": bool(exe)}


def _fix_for(kind: str, spec_str: str, cur_id: str = "") -> Optional[Dict[str, Any]]:
    from core.wizard import package_managers as pms
    fits = [r for r in inventory().get(kind, []) if r["id"] != cur_id and satisfies(r["version"], spec_str, kind)]
    if fits:
        r = fits[0]                                             # newest that fits
        return {"type": "switch", "install_id": r["id"], "version": r["version"], "path": r["path"],
                "label": f"Use {KINDS[kind]['label']} {r['version']} ({r['path']})"}
    from core.wizard import runtime_catalog
    for key, nominal in downloads().get(kind, []):
        if satisfies(nominal, spec_str, kind):
            eol = runtime_catalog.is_eol(key)[0]
            return {"type": "download", "key": key, "eol": eol,
                    "label": f"Download {pms.label(key)}{' (end of life - no security fixes)' if eol else ''} "
                             "(checksum verified) and use it",
                    "installed": bool(runtimes.installed(key))}
    return None


def _sandbox_mode() -> str:
    try:
        from core.wizard.sandbox import factory
        return str(factory.settings().get("mode") or "local")
    except Exception:
        return "local"


def check(tool_id: str) -> Dict[str, Any]:
    """Before every run: does each runtime the tool uses meet its requirements? Each problem carries a one-click fix
    (switch to an installed version that fits, or download one)."""
    spec = _spec(tool_id)
    reqs = requirements(tool_id, spec)
    rt = ap.tool_runtime(tool_id)
    problems = []
    for kind, req in reqs.items():
        if kind not in KINDS or not req.get("spec") or req["spec"] == "any":
            continue
        cur = current(tool_id, kind, spec, rt)
        if cur["version"] and satisfies(cur["version"], req["spec"], kind):
            continue
        label = KINDS[kind]["label"]
        problems.append({"kind": kind, "label": label, "required": req["spec"], "required_text": describe(req["spec"]),
                         "reason": req.get("reason") or "", "source": req.get("source") or "",
                         "current": cur["version"], "current_path": cur["path"],
                         "message": (f"{spec.name} needs {label} {describe(req['spec'])}, but its runs use "
                                     f"{label} {cur['version']} ({cur['path']})." if cur["version"] else
                                     f"{spec.name} needs {label} {describe(req['spec'])}, and the server has none for it."),
                         "fix": _fix_for(kind, req["spec"], cur["install_id"])})
    return {"ok": not problems, "tool_id": tool_id, "tool_name": spec.name, "problems": problems, "checked_at": _now()}


def tool_view(tool_id: str) -> Dict[str, Any]:
    spec = _spec(tool_id)
    reqs = requirements(tool_id, spec)
    rt = ap.tool_runtime(tool_id)
    tested = ap.tool_tested_with(tool_id)
    rows = []
    for kind in kinds_for(spec, reqs):
        cur = current(tool_id, kind, spec, rt)
        req = reqs.get(kind) or {}
        ok = (not req.get("spec") or req["spec"] == "any" or bool(cur["version"] and satisfies(cur["version"], req["spec"], kind)))
        rows.append({"kind": kind, "label": KINDS[kind]["label"], "current": cur, "requirement": req or None,
                     "requirement_text": describe(req.get("spec", "")) if req else "", "ok": ok,
                     "tested_with": tested.get(kind) or "",
                     "fix": None if ok else _fix_for(kind, req["spec"], cur["install_id"])})
    return {"tool_id": spec.id, "name": spec.name, "ecosystem": spec.ecosystem, "language": spec.language,
            "runtimes": rows, "ok": all(r["ok"] for r in rows), "verify": dict(_VERIFY.get(spec.id) or {}),
            "tested_at": tested.get("at")}


def tool_views() -> List[Dict[str, Any]]:
    out = []
    for tid in sorted(ap.whitelist()):
        if ap.is_builtin(tid):
            continue                                      # 9.9: built in - not shown or managed in Mission Control
        try:
            out.append(tool_view(tid))
        except Exception as e:
            out.append({"tool_id": tid, "name": tid, "error": f"{type(e).__name__}: {e}"[:300], "runtimes": []})
    return out


def snapshot(tool_id: str) -> Dict[str, str]:
    """{kind: version} the tool uses right now - kept as 'tested with' when it becomes ACTIVE."""
    spec = _spec(tool_id)
    rt = ap.tool_runtime(tool_id)
    return {k: current(tool_id, k, spec, rt)["version"] for k in kinds_for(spec, requirements(tool_id, spec))}


# ---------------------------------------------------------------------------------------------------------------
# Changes (admin only)
# ---------------------------------------------------------------------------------------------------------------
def select(tool_id: str, kind: str, install_id: Optional[str], by: str = "admin", verify: bool = True) -> Dict[str, Any]:
    """Points the tool at an installed version (``install_id`` from ``inventory()``), or back to the server default
    (None). The tool's smoke test is re-run in the background (``verify``) so the admin sees it still works."""
    if kind not in KINDS:
        raise EnvironmentError_(f"Unknown runtime '{kind}'. One of: {', '.join(KINDS)}.")
    spec = _spec(tool_id)
    choice = None
    if install_id:
        rec = find_install(install_id)
        if not rec or rec["kind"] != kind:
            raise EnvironmentError_(f"No {KINDS[kind]['label']} install '{install_id}' on this server - refresh the list.", 404)
        if kind == "python" and rec["id"] == "python-backend":
            choice = None                                        # Understudy's own Python is the default
        else:
            choice = {"install_id": rec["id"], "version": rec["version"], "path": rec["path"], "home": rec["home"],
                      "bin": rec["bin"], "env": rec["env"], "source": ",".join(rec["sources"])}
    if kind == "python" and spec.ecosystem != "pip":
        raise EnvironmentError_(f"{spec.name} is not a Python tool - its Python version can't be changed here.")
    try:
        ap.set_tool_runtime_choice(spec.id, kind, choice, by)
    except ap.PolicyError as e:
        raise EnvironmentError_(str(e))
    what = f"{KINDS[kind]['label']} {choice['version']}" if choice else f"the server's default {KINDS[kind]['label']}"
    activity_log.record("ADMIN", f"Admin '{by}' switched {spec.name} to {what}",
                        (choice or {}).get("path", "") + (" - the next run rebuilds its Python environment"
                                                          if kind == "python" else ""), source="ADMIN")
    try:
        from core.wizard import events
        events.publish("environments.changed", {"tool_id": spec.id, "kind": kind}, admin=True)
    except Exception:
        pass
    if verify:
        _start_verify(spec.id, by)
    return tool_view(spec.id)


def _start_verify(tool_id: str, by: str) -> None:
    from core.wizard import tool_setup
    st = _VERIFY[tool_id] = {"status": "running", "started_at": _now(), "by": by}

    def run():
        try:
            res = tool_setup.rerun_smoke(tool_id, by)
            st.update(status="passed" if res.get("status") == "PASSED" else "failed",
                      detail=str(res.get("detail") or "")[:500], output=str(res.get("output") or "")[-1500:])
        except Exception as e:
            st.update(status="failed", detail=f"{type(e).__name__}: {e}"[:500])
        st["finished_at"] = _now()
    threading.Thread(target=run, daemon=True, name=f"env-verify-{tool_id}").start()


def set_requirements(tool_id: str, reqs: Dict[str, str], by: str = "admin") -> Dict[str, Any]:
    """The admin's own requirements: {kind: '17+' | '>=3.9,<3.13' | 'any' | ''} ('' = back to Understudy's rule)."""
    spec = _spec(tool_id)
    clean: Dict[str, Any] = {}
    for kind, text in (reqs or {}).items():
        if kind not in KINDS:
            raise EnvironmentError_(f"Unknown runtime '{kind}'.")
        if not str(text or "").strip():
            clean[kind] = None
            continue
        try:
            clean[kind] = {"spec": normalize_spec(text), "source": "admin"}
        except ValueError as e:
            raise EnvironmentError_(f"{KINDS[kind]['label']}: {e}")
    ap.set_tool_requirements(spec.id, clean, by)
    activity_log.record("ADMIN", f"Admin '{by}' set the runtime requirements of {spec.name}",
                        ", ".join(f"{k} {v['spec'] if v else '(rule)'}" for k, v in clean.items()), source="ADMIN")
    return tool_view(spec.id)


def download(key: str, by: str = "admin", tool_id: Optional[str] = None,
             after: Optional[Callable[[Dict[str, Any]], None]] = None, background: bool = True,
             confirm_eol: bool = False) -> Dict[str, Any]:
    """Installs a version side by side (nothing else changes). With ``tool_id`` the tool is switched to it as soon as
    the download finished and passed its checks."""
    from core.wizard import package_managers as pms
    kind = _kind_of_key(key)
    if kind not in KINDS:
        raise EnvironmentError_(f"'{key}' is not a language runtime Understudy can download.")
    if tool_id:
        _spec(tool_id)

    def done(job: Dict[str, Any]):
        if job.get("status") == "done" and tool_id:
            info = runtimes.installed(key) or {}
            inv = inventory(refresh=True)
            rec = next((r for r in inv.get(kind, []) if key == r.get("download_key")
                        or (info.get("home") and r["path"].startswith(str(info["home"])))), None)
            if rec:
                select(tool_id, kind, rec["id"], by, verify=True)
                job["switched"] = {"tool_id": tool_id, "install_id": rec["id"], "version": rec["version"]}
        else:
            inventory(refresh=True)
        if after:
            after(job)
    try:
        pms.download(key, by, background=background, make_default=False, on_done=done, confirm_eol=confirm_eol)
    except pms.PackageManagerError as e:
        raise EnvironmentError_(str(e), e.status_code, e.details)
    return {"job": pms._JOBS.get(key, {}) and {k: v for k, v in pms._JOBS[key].items() if k != "thread"}}


def apply_fix(tool_id: str, kind: str, fix: Dict[str, Any], by: str,
              after: Optional[Callable[[Dict[str, Any]], None]] = None) -> str:
    """The admin approved a fix from ``check()``. Returns a sentence for the user."""
    label = KINDS.get(kind, {}).get("label", kind)
    if fix.get("type") == "switch" and fix.get("install_id"):
        view = select(tool_id, kind, fix["install_id"], by, verify=False)
        row = next((r for r in view["runtimes"] if r["kind"] == kind), {})
        if after:
            after({"status": "done"})
        return f"switched the tool to {label} {(row.get('current') or {}).get('version') or fix.get('version') or ''}".strip()
    if fix.get("type") == "download" and fix.get("key"):
        download(fix["key"], by, tool_id=tool_id, after=after, confirm_eol=True)   # the label said so
        from core.wizard import package_managers as pms
        return f"started downloading {pms.label(fix['key'])} - the tool switches to it when the download is verified"
    raise EnvironmentError_("Nothing to apply for this runtime.")


def requirements_from_stack(suggestion: Dict[str, Any], language: str) -> Dict[str, Dict[str, Any]]:
    """What the AI stack assistant said the tool needs: its structured ``runtime_requirements`` (for the chosen
    language), else what can be read from its ``os_requirements`` sentences."""
    out: Dict[str, Dict[str, Any]] = {}
    lang_kinds = {"java": {"java", "maven", "gradle"}, "kotlin": {"java", "maven", "gradle"},
                  "python": {"python", "node"}, "javascript": {"node"}, "typescript": {"node"}, "csharp": {"dotnet"},
                  "ruby": {"ruby"}, "go": {"go"}}.get(language or "", set(KINDS))
    for r in suggestion.get("runtime_requirements") or []:
        if not isinstance(r, dict):
            continue
        kind = str(r.get("runtime") or "").lower()
        if kind not in KINDS or (r.get("language") and str(r["language"]).lower() != language):
            continue
        try:
            out[kind] = {"spec": normalize_spec(str(r.get("spec") or "")), "source": "ai",
                         "reason": str(r.get("reason") or "")[:300]}
        except ValueError:
            continue
    for text in suggestion.get("os_requirements") or []:
        got = parse_requirement_text(str(text))
        if got and got[0] not in out and got[0] in lang_kinds:
            out[got[0]] = {"spec": got[1], "source": "ai", "reason": str(text)[:300]}
    return out


def overview(refresh: bool = False) -> Dict[str, Any]:
    from core.wizard import package_managers as pms
    from core.wizard.runtime_repair import detect_browsers
    inv = inventory(refresh)
    try:
        browsers = {k: v for k, v in detect_browsers().items()}
    except Exception:
        browsers = {}
    bro = []
    for name, path in browsers.items():
        if path:
            bro.append({"name": name, "path": path, "version": pms._version(path) if os.path.isfile(path) else ""})
    return {"kinds": [{"kind": k, "label": v["label"], "installs": [dict(r, source_labels=[SRC_LABEL.get(s, s) for s in r["sources"]])
                                                                     for r in inv.get(k, [])]}
                      for k, v in KINDS.items()],
            "browsers": bro, "tools": tool_views(), "downloadable": downloadable(), "sandbox": _sandbox_mode(),
            "jobs": {k: {kk: vv for kk, vv in v.items() if kk != "thread"} for k, v in pms._JOBS.items()
                     if _kind_of_key(k) in KINDS},
            "backend_python": "%d.%d.%d" % sys.version_info[:3], "os": sys.platform, "scanned_at": _now()}


# ---------------------------------------------------------------------------------------------------------------
# 9.4: a tool setup uses a runtime Understudy already has, when the one on PATH can't do the job
# ---------------------------------------------------------------------------------------------------------------
# What each package ecosystem needs to install / build (kinds of KINDS).
SETUP_KINDS: Dict[str, List[str]] = {"nuget": ["dotnet"], "maven": ["java", "maven"], "gradle": ["java", "gradle"],
                                     "npm": ["node"], "gem": ["ruby"], "go": ["go"]}
_WORKS_CACHE: Dict[str, bool] = {}


def runtime_works(kind: str, exe: Optional[str]) -> bool:
    """Does this program really work for building? A .NET *host* without an SDK (C:\\Program Files\\dotnet with only
    runtimes - 'No .NET SDKs were found', exit 0x80008091) is on PATH but can't restore or build anything."""
    if not exe or not os.path.isfile(exe):
        return False
    try:
        key = f"{kind}:{exe}:{os.path.getmtime(exe)}"
    except OSError:
        return False
    if key in _WORKS_CACHE:
        return _WORKS_CACHE[key]
    ok = False
    here = os.path.dirname(exe)

    def companion(*names):                     # a program that must sit next to the runtime (javac, npm, gem)
        # 9.7.2: on Windows a Node.js / Ruby folder holds BOTH 'npm' (a shell script for Git Bash - Windows can't
        # start it) and 'npm.cmd'. Picking 'npm' made every Node.js - the system one and the ones Understudy
        # downloads - fail this check ("npm is missing or does not start"). Windows launchers first.
        order = sorted(names, key=lambda n: 0 if os.path.splitext(n)[1].lower() in (".cmd", ".bat", ".exe") else 1) \
            if os.name == "nt" else names
        return next((os.path.join(here, n) for n in order if os.path.isfile(os.path.join(here, n))), None)
    if kind == "java":                         # Maven / Gradle compile: a JRE (no javac) is not enough
        javac = companion("javac", "javac.exe")
        ok = bool(javac) and bool(_version_of("java", exe))
    elif kind == "node":                       # npm installs the tool's packages
        npm = companion("npm", "npm.cmd")
        from core.wizard import package_managers as pms
        ok = bool(_version_of("node", exe)) and bool(npm) and bool(pms._version(npm))
    elif kind == "ruby":                       # RubyGems installs the gems
        gem = companion("gem", "gem.cmd", "gem.bat")
        from core.wizard import package_managers as pms
        ok = bool(_version_of("ruby", exe)) and bool(gem) and bool(pms._version(gem))
    elif kind == "dotnet":
        import subprocess
        try:
            env = dict(os.environ, DOTNET_CLI_TELEMETRY_OPTOUT="1", DOTNET_NOLOGO="1")
            env["DOTNET_ROOT"] = os.path.dirname(exe)
            out = subprocess.run([exe, "--list-sdks"], capture_output=True, text=True, timeout=25, env=env)
            ok = out.returncode == 0 and bool(re.search(r"^\s*\d+\.\d+\.\d+", out.stdout or "", re.M))
        except Exception:
            ok = False
    else:
        ok = bool(_version_of(kind, exe))
    _WORKS_CACHE[key] = ok
    return ok


def runtime_settings(rec: Dict[str, Any]) -> Tuple[List[str], Dict[str, str]]:
    """PATH folders and variables that make a tool use this install."""
    env = {k: v for k, v in (rec.get("env") or {}).items() if v}
    if rec.get("kind") == "dotnet":
        env["DOTNET_ROOT"] = rec.get("home") or rec.get("bin")
        env["DOTNET_MULTILEVEL_LOOKUP"] = "0"          # never fall back to the SDK-less host in Program Files
        env.setdefault("DOTNET_CLI_TELEMETRY_OPTOUT", "1")
    return [rec["bin"]], env


def runtimes_for_setup(ecosystem: str, path_prepend: Optional[List[str]] = None,
                       env: Optional[Dict[str, str]] = None, trust: Any = ()) -> List[Dict[str, Any]]:
    """For every runtime the ecosystem needs that does NOT work with the setup's PATH (missing, or a broken / SDK-less
    one), the best install Understudy knows: one it downloaded first, then the newest. [{kind, rec, why}]"""
    from core.wizard.dependency_resolver import toolchain_path
    path = os.pathsep.join([p for p in (path_prepend or []) if p] + [toolchain_path(os.environ.get("PATH", ""))])
    out = []
    for kind in SETUP_KINDS.get(ecosystem, []):
        if kind in trust:                          # the admin chose / approved this one explicitly - never overridden
            continue
        jh = (env or {}).get("JAVA_HOME") or (os.environ.get("JAVA_HOME") if kind == "java" else "") or ""
        if kind == "java" and jh and os.path.isdir(jh):
            jexe = next((os.path.join(jh, "bin", n) for n in ("java", "java.exe")
                         if os.path.isfile(os.path.join(jh, "bin", n))), None)
            if runtime_works("java", jexe):
                continue                               # the admin / an earlier fix chose a working JDK explicitly
        exe = next((shutil.which(n, path=path) for n in _exe_names(kind) if shutil.which(n, path=path)), None)
        if runtime_works(kind, exe) and not (kind == "java" and jh and os.path.isdir(jh)):
            continue
        rows = [r for r in inventory().get(kind, []) if runtime_works(kind, r.get("path"))]
        if not rows:
            # 9.4: nothing usable on this server - the setup asks the admin to approve a download (no guessing)
            keys = [k for k, _top in downloads().get(kind, [])[:4] if runtimes.is_supported(k)]
            out.append({"kind": kind, "rec": None, "why": _why_not(kind, exe, jh), "download_keys": keys})
            continue
        rows.sort(key=lambda r: (0 if r.get("download_key") or "downloaded" in " ".join(r.get("sources") or []).lower()
                                 else 1, [-int(x) for x in re.findall(r"\d+", r.get("version") or "")[:3]]))
        rec = rows[0]
        out.append({"kind": kind, "rec": rec, "why": _why_not(kind, exe, jh)})
    return out


def _why_not(kind: str, exe: Optional[str], java_home: str = "") -> str:
    label = KINDS[kind]["label"]
    if kind == "java" and java_home and os.path.isdir(java_home):
        return f"JAVA_HOME ({java_home}) is not a working JDK (no javac)"
    if not exe:
        return f"no {label} is on the server's PATH"
    return f"the {label} on PATH ({exe}) can't build - " + {
        "dotnet": "it has no SDK", "java": "it is a JRE without the compiler (javac)",
        "node": "npm is missing or does not start", "ruby": "RubyGems (gem) is missing or does not start",
    }.get(kind, "it did not start")


def kinds_chosen_by_admin(overrides: Dict[str, Any]) -> set:
    """Runtime kinds the admin decided in this setup (a home variable / PATH folder they typed, a download they
    approved) - Understudy's automatic pick never replaces those."""
    out = set()
    homes = {v["env"]: k for k, v in KINDS.items() if v.get("env")}
    for name in (overrides.get("env_vars") or {}):
        if name in homes:
            out.add(homes[name])
    for d in overrides.get("path_prepend") or []:
        for kind in KINDS:
            if any(os.path.isfile(os.path.join(str(d), n + ext)) for n in _exe_names(kind) for ext in ("", ".exe", ".cmd")):
                out.add(kind)
    for key in overrides.get("download_runtimes") or []:
        try:
            out.add(_kind_of_key(key))
        except Exception:
            pass
    return out


def describe_install(rec: Dict[str, Any]) -> str:
    src = "downloaded by Understudy" if rec.get("download_key") else ", ".join(rec.get("sources") or [])
    return f"{KINDS.get(rec.get('kind'), {}).get('label', rec.get('kind'))} {rec.get('version')} ({src}) at {rec.get('path')}"


def installs_for_prompt(kinds: List[str]) -> List[Dict[str, Any]]:
    """What the setup agent may choose from (use_runtime = the id)."""
    rows = []
    for kind in kinds:
        for r in inventory().get(kind, []):
            rows.append({"id": r["id"], "kind": kind, "version": r.get("version"), "path": r.get("path"),
                         "downloaded_by_understudy": bool(r.get("download_key")), "default": bool(r.get("default")),
                         "works": runtime_works(kind, r.get("path"))})
    return rows
