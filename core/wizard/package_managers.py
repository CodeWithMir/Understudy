"""Package managers, build tools and runtimes the admin controls (8.2, Mission Control > Access & tools).

Every package manager the AI install assistant may offer has an entry here: which ecosystem Understudy installs it
with, which programs it needs on the server, and - when Understudy can fetch it itself - which checksum-verified
download provides it (``runtimes``). The admin can:

* **enable / disable** each one - a disabled package manager is shown locked in the assistant and refused when a stack
  is selected (tools that are already ACTIVE keep working);
* see whether the server **has** it (programs found on PATH or registered, with their version);
* **download & register** it (Gradle 8/9, Apache Maven 3, Node.js 20/22, Temurin JDK 17/21) into
  ``generated/runtimes`` - registered toolchains are put in front of PATH (and JAVA_HOME / MAVEN_HOME / GRADLE_HOME set)
  for every install, smoke test and run;
* **point Understudy at an existing install** (a folder that contains the program) or remove a registration.

Adding a package manager Understudy has no installer for (e.g. sbt, Bazel) still needs code: an ecosystem in
``frameworks``, a manifest + install plan in ``dependency_resolver``. Everything the AI suggests beyond this list is
shown to the admin as "not supported yet".
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Callable, Any, Dict, List, Optional

from core.wizard import activity_log, admin_policy as ap, runtimes

# id -> definition. ecosystem = the installer Understudy uses; needs = programs that must be on PATH
CATALOG: Dict[str, Dict[str, Any]] = {
    "pip": {"name": "pip", "kind": "package_manager", "languages": ["python"], "ecosystem": "pip", "needs": ["python"],
            "downloads": [], "note": "Python packages in an isolated venv per tool (the backend's Python, or one the "
                                    "admin names in the setup chat)."},
    "poetry": {"name": "Poetry", "kind": "package_manager", "languages": ["python"], "ecosystem": "pip",
               "needs": ["python"], "downloads": [], "note": "Stacks the AI lists for Poetry are installed with pip."},
    "pipenv": {"name": "Pipenv", "kind": "package_manager", "languages": ["python"], "ecosystem": "pip",
               "needs": ["python"], "downloads": [], "note": "Stacks the AI lists for Pipenv are installed with pip."},
    "npm": {"name": "npm", "kind": "package_manager", "languages": ["javascript", "typescript"], "ecosystem": "npm",
            "needs": ["node", "npm"], "downloads": ["node-22", "node-20"],
            "note": "Node packages with npm install --prefix per tool."},
    "npx": {"name": "npx", "kind": "package_manager", "languages": ["javascript", "typescript"], "ecosystem": "npm",
            "needs": ["node", "npx"], "downloads": ["node-22", "node-20"], "note": "Runs tools installed with npm."},
    "yarn": {"name": "Yarn", "kind": "package_manager", "languages": ["javascript", "typescript"], "ecosystem": "npm",
             "needs": ["node", "npm"], "downloads": ["node-22", "node-20"],
             "note": "Stacks the AI lists for Yarn are installed with npm (same registry)."},
    "pnpm": {"name": "pnpm", "kind": "package_manager", "languages": ["javascript", "typescript"], "ecosystem": "npm",
             "needs": ["node", "npm"], "downloads": ["node-22", "node-20"],
             "note": "Stacks the AI lists for pnpm are installed with npm (same registry)."},
    "maven": {"name": "Maven", "kind": "build_tool", "languages": ["java", "kotlin"], "ecosystem": "maven",
              "needs": ["java", "mvn"], "downloads": ["maven-3", "jdk-21", "jdk-17"],
              "note": "Java build tool: pom.xml, mvn test. Needs a JDK."},
    "gradle": {"name": "Gradle", "kind": "build_tool", "languages": ["java", "kotlin"], "ecosystem": "gradle",
               "needs": ["java", "gradle"], "downloads": ["gradle-8", "gradle-9", "jdk-21", "jdk-17"],
               "note": "Java build tool: build.gradle, gradle test (TestNG or JUnit Platform). Needs a JDK."},
    "gem": {"name": "RubyGems", "kind": "package_manager", "languages": ["ruby"], "ecosystem": "gem",
            "needs": ["ruby", "gem"], "downloads": ["ruby-3"],
            "note": "Ruby gems in an isolated GEM_HOME per tool. Understudy can download Ruby (with RubyGems and "
                    "Bundler) itself - ask the AI to set it up."},
    "bundler": {"name": "Bundler", "kind": "package_manager", "languages": ["ruby"], "ecosystem": "gem",
                "needs": ["ruby", "gem"], "downloads": ["ruby-3"], "note": "Comes with Ruby 3; installed with gem."},
    "go": {"name": "Go modules", "kind": "package_manager", "languages": ["go"], "ecosystem": "go", "needs": ["go"],
           "downloads": ["go-1"], "note": "Go modules through the Go module proxy. Understudy can download Go itself."},
    "nuget": {"name": "NuGet (dotnet)", "kind": "package_manager", "languages": ["csharp"], "ecosystem": "nuget",
              "needs": ["dotnet"], "downloads": ["dotnet-8"],
              "note": "NuGet packages with dotnet restore. Understudy can download the .NET 8 SDK itself."},
}
ALIASES = {"mvn": "maven", "dotnet": "nuget"}
# programs a registered toolchain provides
PROVIDES = {"jdk": ["java", "javac"], "node": ["node", "npm", "npx"], "maven": ["mvn"], "gradle": ["gradle"],
            "ruby": ["ruby", "gem", "bundle"], "go": ["go"], "dotnet": ["dotnet"]}
DOWNLOAD_LABEL = {"jdk-17": "Temurin JDK 17", "jdk-21": "Temurin JDK 21", "node-20": "Node.js 20 LTS",
                  "node-22": "Node.js 22 LTS", "maven-3": "Apache Maven 3 (newest)", "gradle-8": "Gradle 8 (newest)",
                  "gradle-9": "Gradle 9 (newest)", "ruby-3": "Ruby 3 + RubyGems + Bundler", "go-1": "Go (newest stable)",
                  "dotnet-8": ".NET 8 SDK", "python-3.13": "Python 3.13 (standalone)",
                  "python-3.12": "Python 3.12 (standalone)", "python-3.11": "Python 3.11 (standalone)",
                  "python-3.10": "Python 3.10 (standalone)"}
# a missing program -> the downloads that provide it (first = default)
PROGRAM_DOWNLOADS = {"java": ["jdk-21", "jdk-17"], "javac": ["jdk-21", "jdk-17"], "node": ["node-22", "node-20"],
                     "npm": ["node-22", "node-20"], "npx": ["node-22", "node-20"], "mvn": ["maven-3"],
                     "gradle": ["gradle-8", "gradle-9"], "ruby": ["ruby-3"], "gem": ["ruby-3"], "bundle": ["ruby-3"],
                     "go": ["go-1"], "dotnet": ["dotnet-8"]}
_VERSION_CACHE: Dict[str, Any] = {}
_JOBS: Dict[str, Dict[str, Any]] = {}
_LOCK = threading.Lock()


class PackageManagerError(ValueError):
    def __init__(self, message: str, status_code: int = 400, details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.status_code = status_code
        self.details = details or {}


def _now() -> str:
    return datetime.utcnow().isoformat()


def canonical(pm_id: str) -> str:
    p = re.sub(r"[^a-z0-9]", "", str(pm_id or "").lower())
    return ALIASES.get(p, p)


def is_enabled(pm_id: str) -> bool:
    """Supported by Understudy AND not switched off by the admin."""
    pm = canonical(pm_id)
    if pm not in CATALOG:
        return False
    st = ap.package_manager_settings().get(pm)
    return bool(st["enabled"]) if st else True


# ---------------------------------------------------------------------------------------------------------------
# What the server has
# ---------------------------------------------------------------------------------------------------------------
def _version(exe: str) -> str:
    try:
        key = f"{exe}:{os.path.getmtime(exe)}"
    except OSError:
        return ""
    if key in _VERSION_CACHE:
        return _VERSION_CACHE[key]
    from core.wizard.dependency_resolver import _scrubbed_env
    base = os.path.basename(exe).lower()
    arg = "-version" if base.startswith("java") else "version" if base in ("go", "go.exe") else "--version"
    text = ""
    try:
        out = subprocess.run([exe, arg], capture_output=True, text=True, timeout=25, env=_scrubbed_env())
        text = (out.stdout + "\n" + out.stderr)
    except (OSError, subprocess.SubprocessError):
        text = ""
    m = re.search(r"(?i)(?:gradle|maven|openjdk|java|version|v)\s*\"?v?(\d+(?:\.\d+){0,2}(?:[._-]\w+)?)", text) or \
        re.search(r"(\d+\.\d+(?:\.\d+)?)", text)
    v = m.group(1) if m else ""
    _VERSION_CACHE[key] = v
    return v


def _which(binary: str) -> Optional[str]:
    from core.wizard.dependency_resolver import toolchain_path
    import sys
    if binary == "python":
        return sys.executable
    return shutil.which(binary, path=toolchain_path())


def programs(names: List[str]) -> Dict[str, Dict[str, Any]]:
    found = {n: _which(n) for n in names}
    with ThreadPoolExecutor(max_workers=6) as ex:
        vers = dict(zip([n for n in names if found[n]], ex.map(_version, [found[n] for n in names if found[n]])))
    return {n: {"path": found[n], "version": vers.get(n, "") if found[n] else "", "found": bool(found[n])}
            for n in names}


def status() -> Dict[str, Any]:
    """Everything Mission Control's Package managers card shows."""
    settings = ap.package_manager_settings()
    names = sorted({b for e in CATALOG.values() for b in e["needs"]} | {"javac"})
    progs = programs(names)
    tcs = ap.toolchains()
    rows = []
    for pid, e in CATALOG.items():
        st = settings.get(pid)
        missing = [b for b in e["needs"] if not progs[b]["found"]]
        rows.append({"id": pid, "name": e["name"], "kind": e["kind"], "languages": e["languages"],
                     "ecosystem": e["ecosystem"], "note": e["note"], "enabled": bool(st["enabled"]) if st else True,
                     "changed_by": (st or {}).get("by"), "changed_at": (st or {}).get("at"),
                     "needs": [dict(progs[b], name=b) for b in e["needs"]], "ready": not missing, "missing": missing,
                     "downloads": [{"key": k, "label": label(k), "installed": bool(runtimes.installed(k)),
                                    "registered": k in tcs} for k in e["downloads"]]})
    toolchain_rows = []
    for key, t in tcs.items():
        toolchain_rows.append(dict(t, key=key, exists=bool(t.get("bin") and os.path.isdir(t["bin"]))))
    jdk = None
    try:
        from core.wizard import jdk as _jdk
        from core.wizard.dependency_resolver import toolchain_env, toolchain_path
        env = dict(os.environ)
        env.update(toolchain_env())
        env["PATH"] = toolchain_path(env.get("PATH", ""))
        cur = _jdk.maven_jdk(env)
        jdk = {k: cur.get(k) for k in ("home", "major", "source")} if cur else None
    except Exception:
        jdk = None
    return {"package_managers": rows, "toolchains": toolchain_rows, "java_for_builds": jdk,
            "jobs": {k: {kk: vv for kk, vv in v.items() if kk != "thread"} for k, v in _JOBS.items()},
            "downloadable": _downloadable(),
            "advice": _advice_rows()}


def _downloadable() -> List[Dict[str, Any]]:
    """9.9: every release line the official sources offer (runtime_catalog), with end-of-life marks."""
    from core.wizard import runtime_catalog
    return [{"key": e["key"], "label": e["label"], "kind": e["kind"], "eol": e["eol"], "eol_date": e["eol_date"],
             "lts": e["lts"], "note": e["note"], "installed": bool(runtimes.installed(e["key"]))}
            for e in runtime_catalog.entries()]


def _advice_rows() -> Dict[str, Any]:
    try:
        from core.wizard import runtime_advisor
        return runtime_advisor.latest()
    except Exception:
        return {}


# ---------------------------------------------------------------------------------------------------------------
# Admin actions
# ---------------------------------------------------------------------------------------------------------------
def set_enabled(pm_id: str, enabled: bool, admin: str = "admin") -> Dict[str, Any]:
    pm = canonical(pm_id)
    if pm not in CATALOG:
        raise PackageManagerError(f"Understudy has no installer for '{pm_id}' yet.", 404)
    ap.set_package_manager(pm, enabled, admin)
    activity_log.record("ADMIN", f"Admin '{admin}' {'enabled' if enabled else 'disabled'} {CATALOG[pm]['name']}",
                        "applies to new tool setups; active tools keep working", source="ADMIN")
    return status()


def _register(key: str, info: Dict[str, Any], admin: str, source: str) -> None:
    kind = runtimes.kind_of(key)
    ap.set_toolchain(kind, dict(info, kind=kind, source=source), admin)   # one registration per kind (jdk, maven, ...)


def label(key: str) -> str:
    from core.wizard import runtime_catalog
    if key in DOWNLOAD_LABEL:
        return DOWNLOAD_LABEL[key]
    if runtime_catalog.parse(key):
        return runtime_catalog.label(key)
    if key.startswith("conda-"):
        return f"{key[6:]} (conda-forge)"
    return key


def _verify_in(bindir: str, names: List[str]) -> Dict[str, Dict[str, Any]]:
    """8.4: the programs a side-by-side install provides, looked up only in its own bin folder."""
    out = {}
    for n in dict.fromkeys(names):
        p = shutil.which(n, path=bindir) if bindir else None
        out[n] = {"path": p, "version": _version(p) if p else "", "found": bool(p)}
    return out


def verify_programs(names: List[str]) -> Dict[str, Dict[str, Any]]:
    """8.3: after a download, every program the runtime should provide must start and report a version."""
    _VERSION_CACHE.clear()
    return programs(list(dict.fromkeys(names)))


def download(key: str, admin: str = "admin", background: bool = True, expect: Optional[List[str]] = None,
             advice_id: Optional[str] = None, make_default: bool = True,
             on_done: Optional[Callable[[Dict[str, Any]], None]] = None, confirm_eol: bool = False) -> Dict[str, Any]:
    """Downloads (checksum verified) and registers a toolchain for the whole server. 8.3: afterwards the programs it
    should provide are started once (``expect``), and the result is attached to the job.

    8.4: ``make_default=False`` installs it next to the others (side by side) without changing what every tool gets -
    tools are pointed at it one by one in Mission Control > Environments. Python is always side by side (Understudy
    itself keeps running on its own interpreter). ``on_done(job)`` is called when the download finished."""
    if runtimes.kind_of(key) == "python":
        make_default = False
    if not runtimes.is_supported(key):
        raise PackageManagerError("Understudy can download jdk-<N>, node-<N>, python-3.<N>, maven-3|4, gradle-<N>, "
                                  "dotnet-<N>, ruby-3, go-1 or an allowed conda-forge "
                                  f"package - not '{key}'.")
    from core.wizard import runtime_catalog
    eol, why = runtime_catalog.is_eol(key)
    if eol and not confirm_eol:                            # 9.9: an old line only when the admin confirms it
        raise PackageManagerError(why + ". Download it anyway?", 409, {"eol": True, "key": key})
    with _LOCK:
        if (_JOBS.get(key) or {}).get("status") == "running":
            raise PackageManagerError(f"{label(key)} is already downloading.", 409)
        job = _JOBS[key] = {"key": key, "label": label(key), "status": "running", "log": [], "started_at": _now(),
                            "by": admin, "advice": advice_id}

    def log(m: str):
        job["log"] = (job["log"] + [str(m)[:300]])[-40:]

    def run():
        try:
            info = runtimes.provision(key, log)
            if make_default:
                _register(key, info, admin, "download")
                want = list(expect or info.get("provides") or PROVIDES.get(runtimes.kind_of(key), []))
                checked = verify_programs(want) if want else {}
            else:                                   # side by side: check the programs in ITS folder
                want = list(info.get("provides") or PROVIDES.get(runtimes.kind_of(key), []))
                checked = _verify_in(info.get("bin") or "", want)
            bad = [n for n, v in checked.items() if not v["found"]]
            for n, v in checked.items():
                log(f"check {n}: " + (f"{v['version'] or 'found'} ({v['path']})" if v["found"] else "NOT FOUND"))
            job.update(status="failed" if bad else "done", finished_at=_now(), home=info.get("home"),
                       version=info.get("version"), verified=checked,
                       error=(f"Downloaded, but {', '.join(bad)} still can't be found on the toolchain PATH" if bad else None))
            job["side_by_side"] = not make_default
            _VERSION_CACHE.clear()
            activity_log.record("ADMIN", f"{label(key)} downloaded and "
                                + ("registered" if make_default else "installed side by side"),
                                f"{info.get('home')} (version {info.get('version')}, checksum verified)"
                                + (" - end of life, confirmed by the admin" if eol else ""), source="ADMIN")
        except Exception as e:
            job.update(status="failed", finished_at=_now(), error=f"{type(e).__name__}: {e}"[:500])
            try:                                   # 8.3: the AI explains the failure and proposes the next step
                from core.wizard import runtime_advisor
                job["diagnosis"] = runtime_advisor.diagnose_failure(key, job)
            except Exception:
                pass
            activity_log.record("ADMIN", f"Download of {label(key)} failed", job["error"],
                                source="ADMIN", level="warn")
        if on_done:
            try:
                on_done(job)
            except Exception as e:                 # the callback must never break the download job
                job["after_error"] = f"{type(e).__name__}: {e}"[:300]
    if background:
        threading.Thread(target=run, daemon=True, name=f"toolchain-{key}").start()
    else:
        run()
    return status()


def register_path(kind: str, path: str, admin: str = "admin") -> Dict[str, Any]:
    """The admin points Understudy at an existing install: a JDK, Maven, Gradle, Node.js, Ruby, Go or .NET folder (its
    home, its bin folder, or the program itself)."""
    kind = str(kind or "").lower()
    if kind not in PROVIDES:
        raise PackageManagerError(f"Register one of {', '.join(PROVIDES)} - not '{kind}'.")
    p = os.path.expanduser(str(path or "").strip().strip('"'))
    if not p or not os.path.isabs(p):
        raise PackageManagerError("Give the full path of the folder (e.g. /opt/gradle-8.10 or C:\\Tools\\gradle-8.10).")
    exe = PROVIDES[kind][0]
    # The folder itself, its parent when the admin gave the bin folder, and the home when the admin gave the program
    # itself (home/bin/<exe>). Never walk further up: an empty folder must not resolve to a system install at "/".
    cands = [p]
    if os.path.basename(p.rstrip("/\\")).lower() == "bin":
        cands.append(os.path.dirname(p.rstrip("/\\")))
    if os.path.isfile(p):
        cands += [os.path.dirname(p), os.path.dirname(os.path.dirname(p))]
    home = None
    if kind == "dotnet":                     # the .NET SDK has dotnet(.exe) at the top of its folder, not in bin/
        for c in cands:
            if c and any(os.path.isfile(os.path.join(c, n)) for n in ("dotnet", "dotnet.exe")):
                info = {"home": c, "bin": c, "version": _version(os.path.join(c, "dotnet")) or "", "env_name": "DOTNET_ROOT",
                        "provides": ["dotnet"]}
                ap.set_toolchain(kind, dict(info, kind=kind, source="path"), admin)
                _VERSION_CACHE.clear()
                return status()
    for c in cands:
        for name in (exe, exe + ".exe", exe + ".cmd", exe + ".bat"):
            if c and os.path.isfile(os.path.join(c, "bin", name)):
                home = c
                break
        if home:
            break
    if not home:
        raise PackageManagerError(f"No bin/{exe} found in {p} on the Understudy server.")
    info = {"home": home, "bin": os.path.join(home, "bin"), "version": _version(os.path.join(home, "bin", exe)) or "",
            "env_name": {"jdk": "JAVA_HOME", "maven": "MAVEN_HOME", "gradle": "GRADLE_HOME", "go": "GOROOT",
                         "dotnet": "DOTNET_ROOT"}.get(kind, ""), "provides": PROVIDES[kind]}
    ap.set_toolchain(kind, dict(info, kind=kind, source="path"), admin)
    _VERSION_CACHE.clear()
    activity_log.record("ADMIN", f"Admin '{admin}' registered {kind} at {home}", f"version {info['version'] or '?'}",
                        source="ADMIN")
    return status()


def unregister(kind: str, admin: str = "admin") -> Dict[str, Any]:
    if kind not in ap.toolchains():
        raise PackageManagerError(f"No {kind} is registered.", 404)
    ap.set_toolchain(kind, None, admin)
    _VERSION_CACHE.clear()
    activity_log.record("ADMIN", f"Admin '{admin}' removed the registered {kind}", "", source="ADMIN")
    return status()
