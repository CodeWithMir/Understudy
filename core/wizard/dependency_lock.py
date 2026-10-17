"""One source of truth for the automation's dependencies: what is installed in the sandbox, what the generated code
declares, and what the exported ZIP ships are always the same packages at the same versions.

The bug this fixes (6.x): the code generator was told to "include every file needed to run (config files,
manifests)", so Gemini wrote its own requirements.txt / package.json. Understudy then kept that file
(``files.setdefault``) instead of the manifest built from the admin-approved, version-pinned tool spec, so

  * the ZIP shipped the AI's manifest (unpinned, or another version than the one the admin set / the user picked),
  * the sandbox installed only the spec's packages, while the generated code imported extra ones the AI had put in
    its own manifest (dotenv, @playwright/test, ...) -> "Cannot find module" / "ModuleNotFoundError" at run time.

Now:
  1. ``detect`` reads what the generated code really needs: the AI's manifest (if it still wrote one), the
     ``extra_packages`` it declared, and the imports/requires in the code. Packages on the trusted list (or already
     part of the admin-approved tool) become extras of the run; anything else is reported as untrusted and the AI
     suite is rejected (the vetted template is used instead). The primary package's version is ALWAYS the approved one.
  2. ``effective_spec`` = admin-approved spec (pinned to the chosen/admin-default version) + those extras. The
     sandbox installs exactly this (dependency_resolver.install), before every run if anything is missing.
  3. ``render_manifests`` writes the manifest from the effective spec, over whatever the AI wrote (keeping harmless
     package.json fields such as "type": "module" and "scripts").
  4. The ZIP pins every package to the version that was really installed and verified (``exact_versions``), and
     includes config/dependency_lock.json - so a run on another machine uses the same versions.
"""
from __future__ import annotations

import json
import re
import sys
from typing import Any, Dict, Iterable, List, Optional, Tuple

from core.wizard.frameworks import TRUSTED_PACKAGES, FrameworkSpec, Package, _VERSION_RE

MANIFEST_NAMES = {"pip": "requirements.txt", "npm": "package.json", "maven": "pom.xml", "gradle": "build.gradle",
                  "gem": "Gemfile",
                  "go": "go.mod", "nuget": "Automation.csproj"}
ALL_MANIFESTS = {"requirements.txt", "package.json", "pom.xml", "build.gradle", "settings.gradle", "build.gradle.kts",
                 "settings.gradle.kts", "Gemfile", "go.mod", "Automation.csproj",
                 "package-lock.json", "Pipfile", "pyproject.toml", "Gemfile.lock"}

# python import name -> PyPI package
_PY_IMPORTS = {"playwright": "playwright", "selenium": "selenium", "requests": "requests", "pytest": "pytest",
               "behave": "behave", "dotenv": "python-dotenv", "PIL": "pillow", "robot": "robotframework",
               "Browser": "robotframework-browser", "RequestsLibrary": "robotframework-requests",
               "pytest_playwright": "pytest-playwright", "yaml": "pyyaml", "bs4": "beautifulsoup4",
               "webdriver_manager": "webdriver-manager", "httpx": "httpx"}
_NODE_BUILTINS = {"assert", "buffer", "child_process", "crypto", "dns", "events", "fs", "http", "https", "net", "os",
                  "path", "process", "querystring", "readline", "stream", "string_decoder", "timers", "tls", "url",
                  "util", "zlib", "worker_threads", "perf_hooks", "module", "v8", "vm"}
_PY_IMPORT_RX = re.compile(r"^\s*(?:from\s+([A-Za-z_][\w]*)(?:\.[\w.]+)?\s+import\b|import\s+([A-Za-z_][\w]*(?:\s*,\s*[A-Za-z_][\w.]*)*))",
                           re.M)
_JS_IMPORT_RX = re.compile(r"""(?:require\(\s*['"]([^'"]+)['"]\s*\)|\bimport\s+(?:[^'";]+\s+from\s+)?['"]([^'"]+)['"]|"""
                           r"""\bimport\(\s*['"]([^'"]+)['"]\s*\))""")


def _base(name: str) -> str:
    return re.split(r"[\[<>=!~ ]", name.strip(), 1)[0].lower()


def _stdlib() -> set:
    return set(getattr(sys, "stdlib_module_names", ())) | {"__future__"}


def _js_pkg(spec: str) -> Optional[str]:
    if not spec or spec.startswith((".", "/", "node:")):
        return None
    parts = spec.split("/")
    name = "/".join(parts[:2]) if spec.startswith("@") else parts[0]
    return None if name in _NODE_BUILTINS else name.lower()


def parse_manifest(eco: str, files: Dict[str, str]) -> List[Package]:
    """Packages declared by a manifest the code generator wrote (requirements.txt / package.json)."""
    out: List[Package] = []
    if eco == "pip" and "requirements.txt" in files:
        for line in files["requirements.txt"].splitlines():
            line = line.split("#", 1)[0].strip()
            if not line or line.startswith("-"):
                continue
            m = re.match(r"^([A-Za-z0-9_.\-\[\]]+)\s*(.*)$", line)
            if m:
                out.append(Package(m.group(1), m.group(2).replace(" ", "")))
    elif eco == "npm" and "package.json" in files:
        try:
            pj = json.loads(files["package.json"])
        except ValueError:
            return out
        for sect in ("dependencies", "devDependencies"):
            for n, v in (pj.get(sect) or {}).items():
                out.append(Package(str(n), str(v)))
    return out


# Robot Framework libraries -> the pip package that provides them (standard libraries need nothing)
ROBOT_STD_LIBS = {"BuiltIn", "Collections", "DateTime", "Dialogs", "OperatingSystem", "Process", "Screenshot", "String",
                  "Telnet", "XML", "Easter", "Remote"}
ROBOT_LIBRARY_PACKAGES = {
    "Browser": "robotframework-browser", "SeleniumLibrary": "robotframework-seleniumlibrary",
    "RequestsLibrary": "robotframework-requests", "JSONLibrary": "robotframework-jsonlibrary",
    "DatabaseLibrary": "robotframework-databaselibrary", "AppiumLibrary": "robotframework-appiumlibrary",
    "SSHLibrary": "robotframework-sshlibrary", "FakerLibrary": "robotframework-faker",
    "DataDriver": "robotframework-datadriver", "Pabot": "robotframework-pabot", "ExcelLibrary": "robotframework-excellibrary",
    "RPA.Browser.Selenium": "rpaframework", "RESTinstance": "RESTinstance", "REST": "RESTinstance",
}
_ROBOT_LIB_RX = re.compile(r"^Library(?: {2,}|\t+)([A-Za-z_][\w.]*)", re.M)


def robot_library_packages(files: Dict[str, str]) -> List[str]:
    """pip packages the ``Library`` lines of .robot / .resource files need (8.5.3: JSONLibrary used to be missed, so the
    run failed with "No module named 'JSONLibrary'" instead of the build saying which package is missing)."""
    out: List[str] = []
    for path, src in files.items():
        if not path.lower().endswith((".robot", ".resource")):
            continue
        for m in _ROBOT_LIB_RX.finditer(src):
            lib = m.group(1)
            if lib in ROBOT_STD_LIBS or lib.endswith(".py"):
                continue
            pkg = ROBOT_LIBRARY_PACKAGES.get(lib) or ROBOT_LIBRARY_PACKAGES.get(lib.split(".")[0])
            if pkg and pkg not in out:
                out.append(pkg)
    return out


# 9.9.1: Java packages -> the Maven / Gradle artifact that provides them, and the version used when nobody pinned one
# (instead of Maven's deprecated LATEST, which is unpredictable and shows up as "not installed"). Only well-known
# artifacts; anything else imported is left to the generator's own declaration.
JAVA_IMPORTS = (
    ("com.fasterxml.jackson.", "com.fasterxml.jackson.core:jackson-databind", "2.17.1"),
    ("io.github.bonigarcia.wdm", "io.github.bonigarcia:webdrivermanager", "5.8.0"),
    ("com.microsoft.playwright", "com.microsoft.playwright:playwright", "1.44.0"),
    ("org.testng", "org.testng:testng", "7.9.0"),
    ("org.junit.jupiter", "org.junit.jupiter:junit-jupiter", "5.10.2"),
    ("io.restassured", "io.rest-assured:rest-assured", "5.4.0"),
    ("org.openqa.selenium", "org.seleniumhq.selenium:selenium-java", "4.21.0"),
    ("com.google.gson", "com.google.code.gson:gson", "2.11.0"),
    ("org.json.", "org.json:json", "20240303"),
)
_JAVA_IMPORT_RX = re.compile(r"^\s*import\s+(?:static\s+)?([\w.]+)", re.M)


def default_version(coord: str) -> str:
    """The vetted version for a well-known Maven artifact nobody pinned ('' when unknown)."""
    c = _base(coord)
    return next((v for _p, a, v in JAVA_IMPORTS if a.lower() == c), "")


def scan_imports(eco: str, files: Dict[str, str]) -> List[str]:
    """Third-party packages the generated code imports (best effort, never executes anything)."""
    found: List[str] = []
    if eco == "pip":
        local = {p.rsplit("/", 1)[-1][:-3] for p in files if p.endswith(".py")}
        std = _stdlib()
        for path, src in files.items():
            if not path.endswith(".py"):
                continue
            for m in _PY_IMPORT_RX.finditer(src):
                mods = [m.group(1)] if m.group(1) else [x.strip().split(".")[0] for x in (m.group(2) or "").split(",")]
                for mod in mods:
                    mod = mod.split(" as ")[0].strip()
                    if not mod or mod in std or mod in local:
                        continue
                    pkg = _PY_IMPORTS.get(mod, mod.lower().replace("_", "-"))
                    if pkg not in found:
                        found.append(pkg)
        for pkg in robot_library_packages(files):                 # 8.5.3: "Library    JSONLibrary" in .robot files
            if pkg not in found:
                found.append(pkg)
    elif eco == "npm":
        for path, src in files.items():
            if not path.endswith((".js", ".cjs", ".mjs", ".ts")):
                continue
            for m in _JS_IMPORT_RX.finditer(src):
                pkg = _js_pkg(m.group(1) or m.group(2) or m.group(3) or "")
                if pkg and pkg not in found:
                    found.append(pkg)
    elif eco in ("maven", "gradle"):
        for path, src in files.items():
            if not path.endswith((".java", ".kt")):
                continue
            for m in _JAVA_IMPORT_RX.finditer(src):
                imp = m.group(1) + "."
                coord = next((a for prefix, a, _v in JAVA_IMPORTS if imp.startswith(prefix)), "")
                if coord and coord not in found:
                    found.append(coord)
    return found


def _clean_version(v: str) -> str:
    v = str(v or "").strip()
    if not v or v in ("*", "latest") or not _VERSION_RE.match(v) or re.search(r"[:/]|git|file|http", v):
        return ""
    return v


def detect(spec: FrameworkSpec, files: Dict[str, str],
           declared: Optional[Iterable[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """What the suite needs beyond the approved spec. Returns {extras: [{name, version, why}], untrusted: [names],
    notes: [...]}."""
    eco = spec.ecosystem
    have = {_base(p.name) for p in spec.packages}
    trusted = {n.lower() for n in TRUSTED_PACKAGES.get(eco, set())} | have
    primary = _base(spec.packages[0].name) if spec.packages else ""
    notes: List[str] = []
    wanted: Dict[str, Dict[str, Any]] = {}
    for p in parse_manifest(eco, files):
        b = _base(p.name)
        if b == primary and p.version and p.version.lstrip("=") != (spec.packages[0].version or "").lstrip("="):
            notes.append(f"The generated manifest asked for {p.name} {p.version}; the approved version "
                         f"{spec.packages[0].version or '(vetted range)'} is used instead.")
        if b not in have:
            wanted.setdefault(b, {"name": p.name.split("[")[0] if eco == "npm" else p.name, "version": _clean_version(p.version),
                                  "why": "declared in the generated manifest"})
    for d in declared or []:
        if not isinstance(d, dict) or not d.get("name"):
            continue
        b = _base(str(d["name"]))
        if b not in have:
            wanted.setdefault(b, {"name": str(d["name"])[:120], "version": _clean_version(d.get("version", "")),
                                  "why": str(d.get("reason") or "declared by the code generator")[:200]})
    for pkg in scan_imports(eco, files):
        b = _base(pkg)
        if b not in have and b not in wanted:
            wanted[b] = {"name": pkg, "version": default_version(pkg) if eco in ("maven", "gradle") else "",
                         "why": "imported by the generated code"}
    extras, untrusted = [], []
    for b, w in wanted.items():
        (extras if b in trusted else untrusted).append(w if b in trusted else w["name"])
    return {"extras": extras, "untrusted": sorted(set(untrusted)), "notes": notes}


def effective_spec(base: FrameworkSpec, extras: Iterable[Dict[str, Any]]) -> FrameworkSpec:
    """The approved spec (primary package pinned) plus the extra packages the generated code needs."""
    s = FrameworkSpec.from_dict(base.to_dict())
    have = {_base(p.name) for p in s.packages}
    for e in extras or []:
        if _base(e["name"]) not in have:
            s.packages.append(Package(e["name"], e.get("version") or ""))
            have.add(_base(e["name"]))
    return s


def exact_versions(versions_report: Optional[List[Dict[str, Any]]]) -> Dict[str, str]:
    """{package base name: installed version} from dependency_resolver.installed_versions rows."""
    out = {}
    for r in versions_report or []:
        if r.get("installed") and r.get("source") != "not installed":
            out[_base(str(r.get("name")))] = str(r["installed"])
    return out


def pinned(spec: FrameworkSpec, exact: Optional[Dict[str, str]] = None) -> FrameworkSpec:
    """A copy whose packages are pinned to the versions that were really installed (when known)."""
    s = FrameworkSpec.from_dict(spec.to_dict())
    for p in s.packages:
        v = (exact or {}).get(_base(p.name))
        if v:
            p.version = ("==" + v) if s.ecosystem == "pip" else v
    return s


def render_manifests(spec: FrameworkSpec, existing: Optional[Dict[str, str]] = None,
                     exact: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Manifest file(s) for ``spec`` (pinned to ``exact`` where known). For package.json the generator's harmless
    top-level fields ("type", "scripts", ...) are kept; dependencies always come from the spec."""
    from core.wizard import dependency_resolver
    s = pinned(spec, exact)
    out = dependency_resolver.manifest_files(s)
    if "package.json" in out and existing and existing.get("package.json"):
        try:
            ai = json.loads(existing["package.json"])
            ours = json.loads(out["package.json"])
            for k in ("type", "scripts", "engines", "main"):
                if k in ai and isinstance(ai[k], (str, dict)):
                    ours[k] = ai[k]
            out["package.json"] = json.dumps(ours, indent=2) + "\n"
        except ValueError:
            pass
    return out


_STRAY_PROJECT = re.compile(r"(?i)(^|/)([^/]+\.(csproj|fsproj|vbproj|sln|slnx)|Directory\.Build\.(props|targets)|"
                            r"pom\.xml|build\.gradle(\.kts)?|settings\.gradle(\.kts)?)$")


def apply_to_files(spec: FrameworkSpec, files: Dict[str, str], exact: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Replaces every dependency manifest in ``files`` with the one built from ``spec`` (other ecosystems' manifests
    and lock files the generator may have written are dropped - they would install something else)."""
    # 8.3: a second project file next to Understudy's Automation.csproj made 'dotnet test' fail with MSB1011
    # ("Specify which project or solution file to use because this folder contains more than one project or solution
    # file"); the same goes for an extra pom / build file. Only Understudy's own manifest is kept.
    new = {k: v for k, v in files.items() if k not in ALL_MANIFESTS and not _STRAY_PROJECT.search(k)}
    new.update(render_manifests(spec, files, exact))
    return new


def lock_report(spec: FrameworkSpec, versions_report: Optional[List[Dict[str, Any]]], extras: List[Dict[str, Any]],
                chosen_version: Optional[str], admin_policy: Dict[str, Any]) -> Dict[str, Any]:
    exact = exact_versions(versions_report)
    return {"framework": spec.id, "ecosystem": spec.ecosystem,
            "primary": {"name": spec.packages[0].name if spec.packages else None,
                        "requested": spec.packages[0].version if spec.packages else None,
                        "chosen_version": chosen_version, "admin_allowed": admin_policy.get("allowed") or [],
                        "admin_default": admin_policy.get("default")},
            "packages": [{"name": p.name, "requested": p.version or "any",
                          "installed": exact.get(_base(p.name)),
                          "extra": any(_base(e["name"]) == _base(p.name) for e in extras)} for p in spec.packages],
            "extras": extras}


def chosen_version(spec: FrameworkSpec) -> Optional[str]:
    """The exact version the primary package is pinned to (None for a range / no pin)."""
    if not spec.packages:
        return None
    v = (spec.packages[0].version or "").strip()
    v = v[2:] if v.startswith("==") else v
    return v if re.match(r"^[0-9][A-Za-z0-9.+_-]*$", v) else None


def verify_installed(spec: FrameworkSpec, versions_report: Optional[List[Dict[str, Any]]]) -> Tuple[bool, List[str]]:
    """Do the installed versions satisfy the spec? (pip/npm, where the sandbox can check.)"""
    if spec.ecosystem not in ("pip", "npm"):
        return True, []
    rows = {_base(str(r.get("name"))): r for r in versions_report or []}
    problems = []
    for p in spec.packages:
        r = rows.get(_base(p.name))
        if not r or not r.get("installed"):
            problems.append(f"{p.name} is not installed")
            continue
        want = chosen_version(FrameworkSpec.from_dict(dict(spec.to_dict(), packages=[{"name": p.name, "version": p.version}])))
        if want and str(r["installed"]) != want:
            problems.append(f"{p.name} {r['installed']} is installed but {want} is required")
    return not problems, problems
