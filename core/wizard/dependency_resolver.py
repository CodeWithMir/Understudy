"""Dynamic, on-demand dependency resolution for ANY framework spec.

Flow (triggered in WATCH as soon as the user picks a framework, runs in the background):
    1. ``plan_for(spec)``  - turns the validated spec into an InstallPlan: an ecosystem manifest
                             (requirements.txt / package.json / pom.xml / Gemfile / go.mod / .csproj),
                             argv commands, the toolchain binaries needed and an isolated env dir.
    2. ``check_toolchain`` - which binaries are missing on this server (e.g. no JDK for REST Assured).
    3. ``install``         - runs the argv commands with ``shell=False``, a timeout and a scrubbed
                             environment inside ``generated/wizard/_envs/<framework>-<hash>/``.
                             Already-satisfied Python packages are detected and skipped; results are
                             cached per (framework, package set).
    4. ``runtime_for``     - the argv + env the executor uses to run the generated suite with the
                             installed dependencies (venv python, NODE_PATH/PATH for npm tools, ...).

Modes (env ``UNDERSTUDY_DEPENDENCY_INSTALL``): ``auto`` (default) | ``dry-run`` (plan only, used by
tests/CI) | ``off``. Specs that contain packages outside the trusted set stay in
``AWAITING_APPROVAL`` until the user approves them in the UI.
"""
from __future__ import annotations

import hashlib
import json
import re
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Callable, Dict, List, Optional, Tuple
from xml.sax.saxutils import escape

from core.wizard.frameworks import FrameworkSpec

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ENVS_DIR = os.path.join(BASE_DIR, "generated", "wizard", "_envs")
INSTALL_TIMEOUT_S = int(os.environ.get("UNDERSTUDY_INSTALL_TIMEOUT_S", "900"))

TOOLCHAINS = {"pip": ["python"], "npm": ["node", "npm"], "maven": ["java", "mvn"], "gradle": ["java", "gradle"],
              "gem": ["ruby", "gem"], "go": ["go"], "nuget": ["dotnet"], "none": []}


def toolchain_path(base: Optional[str] = None) -> str:
    """PATH with the toolchains the admin registered in Mission Control > Package managers (8.2: a downloaded Gradle /
    Maven / Node.js / JDK, or a folder the admin pointed at) in front."""
    try:
        from core.wizard import admin_policy
        extra = admin_policy.toolchain_bins()
    except Exception:
        extra = []
    base = os.environ.get("PATH", os.defpath) if base is None else base
    return os.pathsep.join([d for d in extra if d] + [base])


def toolchain_env() -> Dict[str, str]:
    """Home variables of registered toolchains (JAVA_HOME / MAVEN_HOME / GRADLE_HOME). The admin registered them on
    purpose, so they win over the process environment (e.g. a JAVA_HOME that still points at an old JDK)."""
    try:
        from core.wizard import admin_policy
        return dict(admin_policy.toolchain_homes())
    except Exception:
        return {}

_READY_CACHE: Dict[str, Dict[str, Any]] = {}
_LOCK = threading.Lock()


def install_mode() -> str:
    m = os.environ.get("UNDERSTUDY_DEPENDENCY_INSTALL", "auto").lower()
    return m if m in ("auto", "dry-run", "off") else "auto"


@dataclass
class InstallPlan:
    framework_id: str
    ecosystem: str
    env_dir: str
    manifest_files: Dict[str, str] = field(default_factory=dict)
    commands: List[List[str]] = field(default_factory=list)
    toolchain: List[str] = field(default_factory=list)
    toolchain_missing: List[str] = field(default_factory=list)
    already_satisfied: List[str] = field(default_factory=list)
    cache_key: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Manifests
# ---------------------------------------------------------------------------
def requirements_txt(spec: FrameworkSpec) -> str:
    src = spec.sources or {}
    head = []
    if src.get("index_url"):
        head.append(f"--index-url {src['index_url']}")
    if src.get("local_dir"):
        head.append(f"--find-links {src['local_dir']}")
        if not src.get("index_url"):
            head.append("--no-index")                     # only the admin's directory
    return "\n".join(head + [p.pip_spec() for p in spec.packages]) + "\n"


def package_json(spec: FrameworkSpec) -> str:
    deps = {p.name: (p.version or "latest") for p in spec.packages}
    return json.dumps({"name": f"understudy-{spec.id}", "version": "1.0.0", "private": True,
                       "description": f"Understudy generated {spec.name} automation",
                       "dependencies": deps}, indent=2) + "\n"


def _known_version(coord: str) -> str:
    from core.wizard.dependency_lock import default_version
    return default_version(coord)


def pom_xml(spec: FrameworkSpec, java_release: Optional[int] = None) -> str:
    """8.2: the compiler release follows the JDK Maven really uses on this server (jdk.release_for) - 17 when that JDK
    is 17+, otherwise its own release - so a pom never asks for a release the JDK can't produce ("invalid target
    release: 17")."""
    from core.wizard import jdk
    release = int(java_release or jdk.release_for(spec))
    props = (f"    <maven.compiler.release>{release}</maven.compiler.release>\n" if release >= 9 else
             f"    <maven.compiler.source>1.{release}</maven.compiler.source>\n"
             f"    <maven.compiler.target>1.{release}</maven.compiler.target>\n")
    idx = (spec.sources or {}).get("index_url")
    repos = (f"  <repositories><repository><id>admin-approved</id><url>{escape(idx)}</url></repository></repositories>\n"
             if idx else "")
    deps = []
    for p in spec.packages:
        g, a = p.name.split(":", 1)
        scope = "<scope>test</scope>" if "junit" in a or "testng" in a else ""
        deps.append(f"    <dependency><groupId>{escape(g)}</groupId><artifactId>{escape(a)}</artifactId>"
                    f"<version>{escape(p.version or _known_version(p.name) or 'LATEST')}</version>{scope}</dependency>")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
         xsi:schemaLocation="http://maven.apache.org/POM/4.0.0 http://maven.apache.org/xsd/maven-4.0.0.xsd">
  <modelVersion>4.0.0</modelVersion>
  <groupId>automation</groupId>
  <artifactId>understudy-{escape(spec.id)}</artifactId>
  <version>1.0.0</version>
  <properties>
{props}    <project.build.sourceEncoding>UTF-8</project.build.sourceEncoding>
  </properties>
{repos}  <dependencies>
{chr(10).join(deps)}
  </dependencies>
  <build>
    <plugins>
      <plugin><groupId>org.apache.maven.plugins</groupId><artifactId>maven-surefire-plugin</artifactId><version>3.2.5</version></plugin>
    </plugins>
  </build>
</project>
"""


def _junit_launcher(packages) -> str:
    for p in packages:
        if p.name == "org.junit.jupiter:junit-jupiter" and p.version:
            v = p.version.lstrip("=^~")
            return ("1." + v.split(".", 1)[1]) if v.startswith("5.") and "." in v else v
    return ""


def build_gradle(spec: FrameworkSpec, java_release: Optional[int] = None) -> str:
    """8.2: build.gradle for a Gradle (Java) tool - every package as a test dependency (the automation lives under
    src/test/java), TestNG or the JUnit Platform as the test engine, the compiler release that the JDK Gradle uses can
    produce (same rule as pom.xml), and an ``understudyResolve`` task that downloads every jar at install time."""
    from core.wizard import jdk
    release = int(java_release or jdk.release_for(spec))
    idx = (spec.sources or {}).get("index_url")
    repo = f"    maven {{ url '{idx}' }}" if idx else "    mavenCentral()"
    names = [p.name for p in spec.packages]
    deps = [f"    testImplementation '{p.name}:{p.version.lstrip('=^~') or _known_version(p.name) or '+'}'"
            for p in spec.packages]
    engine = "useTestNG()" if "org.testng:testng" in names else "useJUnitPlatform()"
    launcher = _junit_launcher(spec.packages)
    if engine == "useJUnitPlatform()" and launcher:
        deps.append(f"    testRuntimeOnly 'org.junit.platform:junit-platform-launcher:{launcher}'")
    compat = (f"tasks.withType(JavaCompile).configureEach {{ options.release = {release} }}" if release >= 9 else
              f"java {{ sourceCompatibility = JavaVersion.VERSION_1_{release}; "
              f"targetCompatibility = JavaVersion.VERSION_1_{release} }}")
    return f"""// Generated by Understudy for {spec.name} - the packages and versions your admin approved.
plugins {{
    id 'java'
}}

repositories {{
{repo}
}}

dependencies {{
{chr(10).join(deps)}
}}

{compat}

test {{
    {engine}
    testLogging {{
        events 'passed', 'skipped', 'failed'
        showStandardStreams = true
        exceptionFormat = 'full'
    }}
}}

// Understudy: downloads every jar the tests need (run once at install time)
tasks.register('understudyResolve') {{
    doLast {{
        configurations.testRuntimeClasspath.resolve()
        configurations.testCompileClasspath.resolve()
    }}
}}
"""


def gemfile(spec: FrameworkSpec) -> str:
    lines = [f'source "{(spec.sources or {}).get("index_url") or "https://rubygems.org"}"']
    lines += [f'gem "{p.name}"' + (f', "{p.version}"' if p.version else "") for p in spec.packages]
    return "\n".join(lines) + "\n"


def go_mod(spec: FrameworkSpec) -> str:
    req = "\n".join(f"\t{p.name} {p.version or 'latest'}" for p in spec.packages)
    return f"module understudy/automation\n\ngo 1.21\n\nrequire (\n{req}\n)\n"


def csproj(spec: FrameworkSpec) -> str:
    refs = "\n".join(f'    <PackageReference Include="{escape(p.name)}" Version="{escape(p.version or "*")}" />'
                     for p in spec.packages)
    return f"""<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup><TargetFramework>net8.0</TargetFramework><IsPackable>false</IsPackable></PropertyGroup>
  <ItemGroup>
{refs}
  </ItemGroup>
</Project>
"""


def manifest_files(spec: FrameworkSpec) -> Dict[str, str]:
    eco = spec.ecosystem
    if eco == "pip":
        return {"requirements.txt": requirements_txt(spec)}
    if eco == "npm":
        out = {"package.json": package_json(spec)}
        if (spec.sources or {}).get("index_url"):
            out[".npmrc"] = f"registry={spec.sources['index_url']}\n"
        return out
    if eco == "maven":
        return {"pom.xml": pom_xml(spec)}
    if eco == "gradle":
        return {"build.gradle": build_gradle(spec), "settings.gradle": "rootProject.name = 'understudy-automation'\n"}
    if eco == "gem":
        return {"Gemfile": gemfile(spec)}
    if eco == "go":
        return {"go.mod": go_mod(spec)}
    if eco == "nuget":
        return {"Automation.csproj": csproj(spec)}
    return {}


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------
def _pip_satisfied(spec: FrameworkSpec) -> List[str]:
    from importlib import metadata
    ok = []
    for p in spec.packages:
        base = p.name.split("[")[0]
        try:
            installed = metadata.version(base)
        except metadata.PackageNotFoundError:
            continue
        if _version_ok(installed, p.version):
            ok.append(p.name)
    return ok


def _version_ok(installed: str, constraint: str) -> bool:
    """Does the installed version satisfy the constraint?

    8.3 fix: when ``packaging`` could not be imported (it isn't always in Understudy's venv) or the constraint didn't
    parse, this returned True - so "playwright==1.30.0" counted as satisfied by the backend's 1.63.0, the run silently
    used 1.63.0 ("requested ==1.30.0; shared backend environment") and, after a failed install, the next attempt skipped
    pip entirely ("No module named playwright"). Now a built-in comparison decides, and anything unparseable is NOT
    satisfied (the isolated install then runs)."""
    if not constraint:
        return True
    c = constraint if constraint[0] in "<>=!~" else f"=={constraint}"
    try:
        from packaging.specifiers import InvalidSpecifier, SpecifierSet
        from packaging.version import InvalidVersion, Version
        try:
            return SpecifierSet(c).contains(Version(installed), prereleases=True)
        except (InvalidSpecifier, InvalidVersion):
            return False
    except ImportError:
        return _simple_version_ok(installed, c)


def _vtuple(v: str) -> Tuple[int, ...]:
    nums = re.findall(r"\d+", v.split("+")[0])
    return tuple(int(x) for x in nums[:4]) + (0,) * (4 - min(4, len(nums)))


def _simple_version_ok(installed: str, constraint: str) -> bool:
    """A small PEP 440 subset for when ``packaging`` is missing: ==, !=, >=, <=, >, <, ~= and comma-separated ANDs."""
    try:
        have = _vtuple(installed)
        for part in [x.strip() for x in constraint.split(",") if x.strip()]:
            m = re.match(r"^(===|==|!=|>=|<=|~=|>|<)\s*([0-9][0-9A-Za-z.*+_-]*)$", part)
            if not m:
                return False
            op, want = m.groups()
            if want.endswith(".*"):
                prefix = _vtuple(want[:-2])[:len(re.findall(r"\d+", want[:-2]))]
                hit = have[:len(prefix)] == prefix
                if (op in ("==", "===") and not hit) or (op == "!=" and hit):
                    return False
                continue
            w = _vtuple(want)
            ok = {"==": have == w, "===": installed == want, "!=": have != w, ">=": have >= w, "<=": have <= w,
                  ">": have > w, "<": have < w}.get(op)
            if op == "~=":
                n = len(re.findall(r"\d+", want))
                ok = have >= w and have[:max(1, n - 1)] == w[:max(1, n - 1)]
            if not ok:
                return False
        return True
    except Exception:
        return False


def check_toolchain(ecosystem: str, tool_id: Optional[str] = None) -> List[str]:
    """Programs of the ecosystem's toolchain that can't be found. 9.9: with ``tool_id`` the tool's own run settings
    (PATH additions such as a JDK its setup downloaded) count too - that is what its runs get."""
    missing = []
    path = toolchain_path()
    if tool_id:
        try:
            from core.wizard import admin_policy
            path = os.pathsep.join([p for p in admin_policy.tool_runtime(tool_id)["path_prepend"] if p] + [path])
        except Exception:
            pass
    for binary in TOOLCHAINS.get(ecosystem, []):
        if binary == "python":
            continue
        if not shutil.which(binary, path=path):
            missing.append(binary)
    return missing


def _browsers_present() -> bool:
    root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or os.path.expanduser("~/.cache/ms-playwright")
    try:
        return any(n.startswith("chromium") for n in os.listdir(root))
    except Exception:
        return False


def chosen_python(spec: FrameworkSpec) -> str:
    """8.4: the Python the admin picked for this tool in Mission Control > Environments ('' = none picked). The tool's
    own ``sources.interpreter`` (set during its setup) wins over it."""
    if spec.ecosystem != "pip" or (spec.sources or {}).get("interpreter"):
        return ""
    try:
        from core.wizard import admin_policy
        ch = (admin_policy.tool_runtime(spec.id).get("versions") or {}).get("python") or {}
    except Exception:
        return ""
    p = str(ch.get("path") or "")
    return p if p and os.path.isfile(p) else ""


def tool_interpreter(spec: FrameworkSpec) -> str:
    """The interpreter the tool's venv is created with: sources.interpreter, the admin's pick, or '' (the backend's)."""
    return (spec.sources or {}).get("interpreter") or chosen_python(spec)


def plan_for(spec: FrameworkSpec) -> InstallPlan:
    picked = chosen_python(spec)
    key_src = json.dumps({"id": spec.id, "eco": spec.ecosystem, "pk": [(p.name, p.version) for p in spec.packages],
                          "post": spec.post_install, **({"src": spec.sources} if spec.sources else {}),
                          **({"py": picked} if picked else {})}, sort_keys=True)
    cache_key = hashlib.sha256(key_src.encode()).hexdigest()[:12]
    env_dir = os.path.join(ENVS_DIR, f"{spec.id}-{cache_key}")
    plan = InstallPlan(framework_id=spec.id, ecosystem=spec.ecosystem, env_dir=env_dir,
                       manifest_files=manifest_files(spec), toolchain=TOOLCHAINS.get(spec.ecosystem, []),
                       toolchain_missing=check_toolchain(spec.ecosystem), cache_key=cache_key)
    eco = spec.ecosystem
    if eco == "pip":
        # an admin-chosen interpreter never shares the backend's packages
        plan.already_satisfied = [] if tool_interpreter(spec) else _pip_satisfied(spec)
        if len(plan.already_satisfied) < len(spec.packages):
            venv_py = _venv_python(env_dir)
            plan.commands.append(_venv_create(spec, env_dir))
            plan.commands.append([venv_py, "-m", "pip", "install", "--disable-pip-version-check", "-r",
                                  os.path.join(env_dir, "requirements.txt")])
    elif eco == "npm":
        plan.commands.append(["npm", "install", "--no-audit", "--no-fund", "--prefix", env_dir])
    elif eco == "maven":
        plan.commands.append(["mvn", "-q", "-f", os.path.join(env_dir, "pom.xml"), "dependency:resolve"])
    elif eco == "gradle":
        plan.commands.append(["gradle", "-q", "--no-daemon", "-p", env_dir, "understudyResolve"])
    elif eco == "gem":
        for p in spec.packages:          # one command per gem so each keeps its own pinned version
            plan.commands.append(["gem", "install", "--no-document", "--install-dir", os.path.join(env_dir, "gems"), p.name]
                                 + (["-v", p.version] if p.version else [])
                                 + (["--clear-sources", "--source", spec.sources["index_url"]]
                                    if (spec.sources or {}).get("index_url") else []))
    elif eco == "go":
        plan.commands.append(["go", "mod", "download"])
    elif eco == "nuget":
        plan.commands.append(["dotnet", "restore", os.path.join(env_dir, "Automation.csproj")])
    uses_backend_packages = eco == "pip" and len(plan.already_satisfied) == len(spec.packages)
    for cmd in spec.post_install:
        # Browser builds are tied to the exact Playwright version. Skipping the download is only safe when the tool
        # runs on the backend's own (already provisioned) Playwright; a separately installed Playwright - e.g. the npm
        # package, or an admin-pinned version - must fetch its matching build ("Executable doesn't exist" otherwise).
        if "playwright" in cmd and "install" in cmd and uses_backend_packages and _browsers_present():
            plan.already_satisfied.append("playwright browsers")
            continue
        argv = _resolve_argv(cmd, spec, env_dir)
        if eco == "pip" and cmd and cmd[0] in ("python", "python3") and \
                any("venv" in c for c in (plan.commands[0] if plan.commands else [])):
            # the isolated venv is created by an earlier command of this plan: run post-install steps (e.g. the
            # browser download of the pinned Playwright version) with ITS python, not the backend's
            argv[0] = _venv_python(env_dir)
        plan.commands.append(argv)
    return plan


def _venv_create(spec: FrameworkSpec, env_dir: str) -> List[str]:
    """``python -m venv`` for the tool. With the admin's own interpreter (8.0, sources.interpreter) the venv is fully
    isolated; with the backend's interpreter it also sees the backend's packages (e.g. its provisioned Playwright)."""
    interp = tool_interpreter(spec)
    if interp:
        from core.wizard.frameworks import resolve_interpreter
        return [resolve_interpreter(interp), "-m", "venv", env_dir]
    return [sys.executable, "-m", "venv", "--system-site-packages", env_dir]


def _venv_python(env_dir: str) -> str:
    return os.path.join(env_dir, "Scripts" if os.name == "nt" else "bin", "python.exe" if os.name == "nt" else "python")


def _resolve_argv(cmd: List[str], spec: FrameworkSpec, env_dir: str, entry_path: str = "") -> List[str]:
    out = []
    for i, tok in enumerate(cmd):
        tok = tok.replace("{entry}", entry_path or spec.entry_file)
        if i == 0 and tok in ("python", "python3") and spec.ecosystem == "pip":
            vpy = _venv_python(env_dir)
            tok = vpy if os.path.exists(vpy) else sys.executable
        elif i == 0 and tok in ("python", "python3"):
            tok = sys.executable
        elif i == 0 and spec.ecosystem == "pip" and tok not in ("python", "python3"):
            # console-script (e.g. robot, pytest, behave) - prefer the venv's, then the current interpreter's
            for d in (os.path.dirname(_venv_python(env_dir)), os.path.dirname(sys.executable)):
                cand = shutil.which(tok, path=d)
                if cand:
                    tok = cand
                    break
        out.append(tok)
    return out


# Environment variables that must never reach third-party installers or the generated automation.
_SECRET_ENV = re.compile(r"(?i)(GEMINI|GOOGLE_API|OPENAI|ANTHROPIC|UNDERSTUDY_ADMIN_TOKEN|API[_-]?KEY|SECRET|PASSWORD|PASSWD|"
                         r"TOKEN|CREDENTIAL|PRIVATE[_-]?KEY|AWS_|AZURE_|GCP_|GOOGLE_APPLICATION|SESSION[_-]?KEY)")
# ...except these, which are configuration rather than secrets.
_ENV_ALLOW = {"PIP_INDEX_URL", "NPM_CONFIG_REGISTRY", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "NODE_EXTRA_CA_CERTS"}


def _scrubbed_env(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """The child-process environment: everything the OS and toolchains need, minus secrets.

    Earlier versions used a short allow-list, which silently broke real tools on Windows: Selenium Manager locates
    Chrome through %PROGRAMFILES% / %PROGRAMFILES(X86)% (without them it reports a non-existent default path -> "The
    browser path is not a valid file"), .cmd launchers need PATHEXT/ComSpec, and npm/Maven read WINDIR/ProgramData.
    Now the full environment is passed through and only secret-looking variables (API keys, tokens, passwords, cloud
    credentials, the Gemini key and the admin token) are removed.
    """
    env = {k: v for k, v in os.environ.items() if k.upper() in _ENV_ALLOW or not _SECRET_ENV.search(k)}
    env["PATH"] = toolchain_path(env.get("PATH", os.defpath))
    env.update(toolchain_env())
    env.update(extra or {})
    return env


def resolve_executable(tok: str, env: Optional[Dict[str, str]] = None) -> str:
    """Absolute path for a command name, honouring PATHEXT on Windows (npm -> npm.cmd, mvn -> mvn.cmd).

    subprocess with shell=False cannot start 'npm' on Windows because the real file is npm.cmd; that was the
    '[WinError 2] The system cannot find the file specified' reported for Cypress/Puppeteer/Playwright-JS.
    """
    if not tok or os.path.isabs(tok) or os.sep in tok or (os.altsep and os.altsep in tok):
        return tok
    path = (env or os.environ).get("PATH") if env is not None else None
    found = shutil.which(tok, path=path) if path is not None else shutil.which(tok)
    return found or tok


TOOLCHAIN_HELP = {
    "node": "Node.js 18+ (https://nodejs.org) - it also provides npm and npx",
    "npm": "Node.js 18+ (https://nodejs.org) - it also provides npm and npx",
    "java": "a Java 17+ JDK (e.g. https://adoptium.net) and set JAVA_HOME",
    "mvn": "Apache Maven 3.9+ (https://maven.apache.org/download.cgi) and add its bin folder to PATH - or download it "
           "in Mission Control > Package managers",
    "gradle": "Gradle 8+ (https://gradle.org/install/) and add its bin folder to PATH - or download it in Mission "
              "Control > Package managers",
    "ruby": "Ruby 3.x (https://www.ruby-lang.org/en/downloads/)",
    "gem": "Ruby 3.x (https://www.ruby-lang.org/en/downloads/)",
    "go": "Go 1.21+ (https://go.dev/dl/)",
    "dotnet": "the .NET 8 SDK (https://dotnet.microsoft.com/download)",
}


def toolchain_help(missing: List[str]) -> str:
    seen, parts = set(), []
    for b in missing:
        h = TOOLCHAIN_HELP.get(b, b)
        if h not in seen:
            seen.add(h)
            parts.append(h)
    return "; ".join(parts)


_VERSION_PROBE = r"""
import json, sys
from importlib import metadata
out = {}
for name in sys.argv[1:]:
    try:
        d = metadata.distribution(name)
        out[name] = {"version": d.version, "location": str(d.locate_file(""))}
    except metadata.PackageNotFoundError:
        out[name] = None
print(json.dumps(out))
"""


def installed_versions(spec: FrameworkSpec, env_dir: Optional[str] = None) -> List[Dict[str, Any]]:
    """The versions that will actually be used at run time, for the install log and the UI:
    [{name, requested, installed, source}] - source: isolated environment | shared backend environment | declared."""
    env_dir = env_dir or plan_for(spec).env_dir
    rows: List[Dict[str, Any]] = []
    names = [p.name.split("[")[0] for p in spec.packages]
    requested = {p.name.split("[")[0]: (p.version or "any") for p in spec.packages}
    if spec.ecosystem == "pip" and names:
        vpy = _venv_python(env_dir)
        py = vpy if os.path.exists(vpy) else sys.executable
        found: Dict[str, Any] = {}
        try:
            proc = subprocess.run([py, "-c", _VERSION_PROBE, *names], capture_output=True, text=True, timeout=60,
                                  env=_scrubbed_env(), shell=False)
            found = json.loads((proc.stdout or "{}").strip().splitlines()[-1] or "{}")
        except Exception:
            found = {}
        for n in names:
            info = found.get(n)
            src = "not installed"
            if info:
                loc = os.path.realpath(info.get("location") or "")
                src = ("isolated environment" if loc.startswith(os.path.realpath(env_dir))
                       else "shared backend environment")
            rows.append({"name": n, "requested": requested[n], "installed": info["version"] if info else None,
                         "source": src})
    elif spec.ecosystem == "npm":
        for n in names:
            pj = os.path.join(env_dir, "node_modules", *n.split("/"), "package.json")
            ver = None
            try:
                with open(pj, encoding="utf-8") as fh:
                    ver = json.load(fh).get("version")
            except Exception:
                pass
            rows.append({"name": n, "requested": requested[n], "installed": ver,
                         "source": "isolated environment" if ver else "not installed"})
    else:
        for n in names:
            v = requested[n]
            used = v.lstrip("=^~") if v != "any" else _known_version(n)      # 9.9.1: what the manifest really pins
            rows.append({"name": n, "requested": v, "installed": used or None,
                         "source": "declared in the manifest (resolved by " + spec.ecosystem + ")" if used else
                                   "declared without a version - " + spec.ecosystem + " picks the newest at run time"})
    return rows


def _report_versions(spec: FrameworkSpec, env_dir: str, status: Dict[str, Any], log: Callable[[str], None]) -> None:
    try:
        rows = installed_versions(spec, env_dir)
    except Exception as e:  # never fail an install because of the report
        log(f"(could not read installed versions: {e})")
        return
    status["versions"] = rows
    summary = ", ".join(f"{r['name']} {r['installed'] or r['requested']}" for r in rows)
    if summary and status.get("message"):
        status["message"] = status["message"].rstrip(".") + f" - using {summary}."
    for r in rows:
        inst = r["installed"] or "?"
        req = r["requested"]
        log(f"Version in use: {r['name']} {inst}  (requested {req}; {r['source']})")


def install(spec: FrameworkSpec, log: Optional[Callable[[str], None]] = None, approved: bool = False,
            force: bool = False) -> Dict[str, Any]:
    """Resolves and (depending on mode) installs dependencies. Returns a status dict.

    force=True (used by self-healing when a package is missing or broken) wipes the isolated environment and the
    cache entry and installs again from scratch."""
    log = log or (lambda m: None)
    plan = plan_for(spec)
    if force:
        with _LOCK:
            _READY_CACHE.pop(plan.cache_key, None)
        shutil.rmtree(plan.env_dir, ignore_errors=True)
        plan = plan_for(spec)
        if spec.ecosystem == "pip" and not any("pip" in c for cmd in plan.commands for c in cmd):
            venv_py = _venv_python(plan.env_dir)
            plan.commands[:0] = [_venv_create(spec, plan.env_dir),
                                 [venv_py, "-m", "pip", "install", "--disable-pip-version-check", "--force-reinstall",
                                  "-r", os.path.join(plan.env_dir, "requirements.txt")]]
    status: Dict[str, Any] = {"framework_id": spec.id, "mode": install_mode(), "plan": plan.to_dict(),
                              "status": "PENDING", "message": "", "started_at": time.time(), "logs": []}

    def _log(msg: str):
        status["logs"].append(msg)
        log(msg)

    if spec.requires_approval and not approved:
        status["status"] = "AWAITING_APPROVAL"
        status["message"] = ("These packages are not in Understudy's trusted list, so they won't be installed until you "
                             "approve them: " + ", ".join(spec.untrusted_packages))
        _log(status["message"])
        return status
    if plan.toolchain_missing:
        status["status"] = "TOOLCHAIN_MISSING"
        status["toolchain_missing"] = plan.toolchain_missing
        status["message"] = (f"{spec.name} needs {', '.join(plan.toolchain_missing)}, which is not installed on the machine "
                             f"running the Understudy backend (or not on its PATH). Install {toolchain_help(plan.toolchain_missing)}, "
                             f"then restart the backend and pick the tool again. Until then Understudy can still verify "
                             f"your process with its built-in Playwright driver, and the exported ZIP includes setup "
                             f"scripts for your own machine.")
        _log(status["message"])
        return status
    with _LOCK:
        cached = _READY_CACHE.get(plan.cache_key)
    if cached:
        status.update({"status": "READY", "message": f"{spec.name} dependencies already installed (cached).",
                       "cached": True})
        _log(status["message"])
        _report_versions(spec, plan.env_dir, status, _log)
        return status
    os.makedirs(plan.env_dir, exist_ok=True)
    for fname, content in plan.manifest_files.items():
        with open(os.path.join(plan.env_dir, fname), "w", encoding="utf-8") as fh:
            fh.write(content)
    mode = install_mode()
    if mode in ("off", "dry-run"):
        status["status"] = "DRY_RUN" if mode == "dry-run" else "SKIPPED"
        status["message"] = (f"Dependency install mode is '{mode}': planned {len(plan.commands)} command(s) for {spec.name} "
                             f"without running them.")
        for c in plan.commands:
            _log("PLAN: " + " ".join(c))
        return status
    if not plan.commands:
        status["status"] = "READY"
        status["message"] = f"All {spec.name} dependencies are already available on this server."
        _log(status["message"])
        _report_versions(spec, plan.env_dir, status, _log)
        with _LOCK:
            _READY_CACHE[plan.cache_key] = status
        return status
    child_env = _scrubbed_env()
    for cmd in plan.commands:
        cmd = [resolve_executable(cmd[0], child_env)] + list(cmd[1:])
        _log("$ " + " ".join(cmd))
        cwd = plan.env_dir
        try:
            proc = subprocess.run(cmd, cwd=cwd, env=child_env, capture_output=True, text=True,
                                  timeout=INSTALL_TIMEOUT_S, shell=False)
        except FileNotFoundError as e:
            missing = os.path.basename(cmd[0])
            status.update({"status": "TOOLCHAIN_MISSING", "toolchain_missing": [missing],
                           "message": f"Could not start '{missing}' ({e}). Install {toolchain_help([missing])} on the "
                                      f"machine running the Understudy backend and make sure it is on PATH."})
            _log(status["message"])
            return status
        except subprocess.TimeoutExpired:
            status.update({"status": "FAILED", "message": f"Timed out after {INSTALL_TIMEOUT_S}s: {' '.join(cmd)}"})
            _log(status["message"])
            return status
        for line in (proc.stdout + "\n" + proc.stderr).strip().splitlines()[-25:]:
            _log("  " + line[:300])
        if proc.returncode != 0:
            out_lines = [ln.strip() for ln in (proc.stderr + "\n" + proc.stdout).splitlines() if ln.strip()]
            err_line = next((ln for ln in reversed(out_lines) if re.search(r"(?i)error|failed|denied|not found|refused|"
                                                                          r"timed? ?out|ENOTFOUND|ECONN", ln)),
                            out_lines[-1] if out_lines else "")
            status.update({"status": "FAILED", "message": f"'{' '.join(os.path.basename(c) if i == 0 else c for i, c in enumerate(cmd[:4]))} ...' "
                                                          f"exited with code {proc.returncode}" + (f": {err_line[:240]}" if err_line else "")})
            _log(status["message"])
            return status
    status.update({"status": "READY", "message": f"{spec.name} dependencies installed.",
                   "duration_s": round(time.time() - status["started_at"], 1)})
    _log(status["message"])
    _report_versions(spec, plan.env_dir, status, _log)
    with _LOCK:
        _READY_CACHE[plan.cache_key] = status
    return status


def runtime_for(spec: FrameworkSpec, suite_dir: str) -> Tuple[List[str], Dict[str, str]]:
    """argv + environment to run the generated suite with the resolved dependencies."""
    plan = plan_for(spec)
    argv = _resolve_argv(spec.run_command, spec, plan.env_dir, spec.entry_file)
    extra: Dict[str, str] = {}
    if spec.ecosystem == "npm":
        nm = os.path.join(plan.env_dir, "node_modules")
        extra["NODE_PATH"] = nm
        extra["PATH"] = os.path.join(nm, ".bin") + os.pathsep + os.environ.get("PATH", "")
    if spec.ecosystem == "gem":
        extra["GEM_HOME"] = os.path.join(plan.env_dir, "gems")
        extra["GEM_PATH"] = extra["GEM_HOME"]
    env = _scrubbed_env(extra)
    argv[0] = resolve_executable(argv[0], env)
    return argv, env


def launcher_steps(spec: FrameworkSpec) -> Dict[str, Any]:
    """The exported launchers' install commands (bash and Windows) and the run command."""
    eco = spec.ecosystem
    sh: List[str] = []
    ps: List[str] = []
    if eco == "pip":
        sh += ["python3 -m venv .venv", ". .venv/bin/activate", "pip install -r requirements.txt"]
        ps += ["python -m venv .venv", "pip install -r requirements.txt"]
    else:
        cmd = {"npm": "npm install --no-audit --no-fund", "maven": "mvn -q dependency:resolve",
               "gradle": "gradle -q --no-daemon understudyResolve", "gem": "bundle install",
               "go": "go mod download", "nuget": "dotnet restore"}.get(eco)
        if cmd:
            sh.append(cmd)
            ps.append(cmd)
    for c in spec.post_install:
        sh.append(" ".join(c))
        ps.append(" ".join(c))
    run = " ".join(t.replace("{entry}", spec.entry_file) for t in spec.run_command)
    return {"sh": sh, "ps": ps, "run": run, "venv": eco == "pip"}


def setup_scripts(spec: FrameworkSpec) -> Dict[str, str]:
    """Portable launchers written into the exported ZIP: run.sh (macOS / Linux), run.ps1 + run.bat (Windows). Each
    installs the dependencies, runs the automation and (9.9.1) writes a report of the run to results/<run id>/
    (core/wizard/run_report.py)."""
    from core.wizard import WIZARD_VERSION, run_report
    st = launcher_steps(spec)
    return run_report.launcher_files(st["sh"], st["ps"], st["venv"], st["run"], spec.name,
                                     f"Understudy {WIZARD_VERSION}")


def run_tool_command(spec: FrameworkSpec, cmd: List[str], log: Optional[Callable[[str], None]] = None,
                     timeout: int = 900) -> Dict[str, Any]:
    """Runs a provisioning command (e.g. 'playwright install chromium') inside the tool's isolated environment.
    Used by self-healing runtime repairs. Never passes secrets; shell=False."""
    log = log or (lambda m: None)
    plan = plan_for(spec)
    os.makedirs(plan.env_dir, exist_ok=True)
    argv = _resolve_argv(cmd, spec, plan.env_dir)
    extra: Dict[str, str] = {}
    if spec.ecosystem == "npm":
        nm = os.path.join(plan.env_dir, "node_modules")
        extra["PATH"] = os.path.join(nm, ".bin") + os.pathsep + toolchain_path()
    env = _scrubbed_env(extra)
    argv[0] = resolve_executable(argv[0], env)
    log("$ " + " ".join(argv))
    try:
        proc = subprocess.run(argv, cwd=plan.env_dir, env=env, capture_output=True, text=True, timeout=timeout, shell=False)
    except FileNotFoundError as e:
        missing = os.path.basename(argv[0])
        msg = f"Could not start '{missing}' ({e}). Install {toolchain_help([missing])}."
        log(msg)
        return {"ok": False, "message": msg}
    except subprocess.TimeoutExpired:
        msg = f"Timed out after {timeout}s: {' '.join(argv)}"
        log(msg)
        return {"ok": False, "message": msg}
    for line in (proc.stdout + "\n" + proc.stderr).strip().splitlines()[-20:]:
        log("  " + line[:300])
    return {"ok": proc.returncode == 0, "message": f"exit code {proc.returncode}"}
