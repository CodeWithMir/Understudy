"""Java toolchain awareness (8.2): which JDK Maven really uses, which JDKs exist on this machine, and which
``--release`` the generated pom.xml may target.

Why this exists - the 8.1.1 field report ("Fatal error compiling: error: invalid target release: 17"): Maven does not
use the ``java`` that is first on PATH, it uses ``JAVA_HOME`` when that is set. On many machines (Windows especially)
``java -version`` says 17 while ``JAVA_HOME`` still points to an old JDK 8/11 - so the admin's checks passed and the
user's run failed at compile time, and the old self-healing ladder (reinstall packages) could never fix it.

8.2 therefore:

* probes the JDK the way Maven picks it (``JAVA_HOME`` first, else ``java`` on PATH) with
  ``java -XshowSettings:properties -version`` (works through the Windows ``javapath`` shims, which ``realpath`` can't),
* writes ``<maven.compiler.release>`` for the JDK that is really there (17 when it is 17 or newer, otherwise that
  JDK's own release, never below 8) - ``release_for(spec)``,
* finds other JDKs on the machine (``find_jdks``) so self-healing can point ``JAVA_HOME`` at one that is new enough,
* parses the release a compiler error asked for (``required_release``) and can patch an existing pom
  (``patch_pom_release``).

Everything is best-effort and cached; a probe that fails simply returns None (callers keep the 8.1 default, 17).
"""
from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import sys
import threading
from contextlib import contextmanager
from typing import Dict, Iterator, List, Optional, Tuple

DEFAULT_RELEASE = 17
_CACHE: Dict[Tuple[str, float], Optional[Dict[str, object]]] = {}
_LOCK = threading.Lock()
_REQUIRED_RX = [
    re.compile(r"(?i)invalid target release:\s*(?:1\.)?(\d{1,2})"),
    re.compile(r"(?i)invalid source release:\s*(?:1\.)?(\d{1,2})"),
    re.compile(r"(?i)release version (\d{1,2}) not supported"),
    re.compile(r"(?i)source option (\d{1,2}) is no longer supported"),
    re.compile(r"(?i)has been compiled by a more recent version of the Java Runtime \(class file version (\d+)\.\d+\)"),
    re.compile(r"(?i)UnsupportedClassVersionError.*class file version (\d+)\.\d+"),
]
JDK_ERROR_RX = re.compile(r"(?i)invalid (?:target|source) release|release version \d+ not supported|"
                          r"UnsupportedClassVersionError|compiled by a more recent version of the Java Runtime|"
                          r"source option \d+ is no longer supported|No compiler is provided in this environment|"
                          r"Perhaps you are running on a JRE rather than a JDK")


def _exe(home: str) -> str:
    return os.path.join(home, "bin", "java.exe" if os.name == "nt" else "java")


def probe(java: str) -> Optional[Dict[str, object]]:
    """{home, major, version, java} of a java executable, or None. Cached per executable + mtime."""
    if not java:
        return None
    try:
        key = (java, os.path.getmtime(java))
    except OSError:
        return None
    with _LOCK:
        if key in _CACHE:
            return _CACHE[key]
    info: Optional[Dict[str, object]] = None
    try:
        env = {k: v for k, v in os.environ.items() if k not in ("JAVA_TOOL_OPTIONS", "_JAVA_OPTIONS", "JDK_JAVA_OPTIONS")}
        out = subprocess.run([java, "-XshowSettings:properties", "-version"], capture_output=True, text=True,
                             timeout=20, env=env)
        text = (out.stderr or "") + "\n" + (out.stdout or "")
        home = re.search(r"^\s*java\.home\s*=\s*(.+?)\s*$", text, re.M)
        ver = re.search(r"^\s*java\.specification\.version\s*=\s*(\S+)", text, re.M) or \
            re.search(r'version "([^"]+)"', text)
        if ver:
            v = ver.group(1)
            major = int(v.split(".")[1]) if v.startswith("1.") else int(re.match(r"\d+", v).group(0))
            h = home.group(1) if home else os.path.dirname(os.path.dirname(java))
            # JDK 8 reports <jdk>/jre as java.home
            if os.path.basename(h).lower() == "jre" and os.path.isfile(_exe(os.path.dirname(h))):
                h = os.path.dirname(h)
            info = {"home": h, "major": major, "version": v, "java": java,
                    "compiler": os.path.isfile(os.path.join(h, "bin", "javac.exe" if os.name == "nt" else "javac"))}
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
        info = None
    with _LOCK:
        _CACHE[key] = info
    return info


def maven_jdk(env: Optional[Dict[str, str]] = None, path_prepend: Optional[List[str]] = None) -> Optional[Dict[str, object]]:
    """The JDK Maven will use with this environment: JAVA_HOME when set (and valid), else ``java`` on PATH."""
    env = env if env is not None else dict(os.environ)
    home = (env.get("JAVA_HOME") or "").strip().strip('"')
    if home and os.path.isfile(_exe(home)):
        info = probe(_exe(home))
        if info:
            return dict(info, source="JAVA_HOME")
    path = os.pathsep.join(list(path_prepend or []) + [env.get("PATH") or os.environ.get("PATH", "")])
    java = shutil.which("java", path=path)
    info = probe(java) if java else None
    return dict(info, source="PATH") if info else None


def release_for_major(major: Optional[int]) -> int:
    if not major:
        return DEFAULT_RELEASE
    return DEFAULT_RELEASE if major >= DEFAULT_RELEASE else max(8, int(major))


_TL = threading.local()


@contextmanager
def release_override(release: Optional[int]) -> Iterator[None]:
    """Within this block ``release_for`` returns ``release`` (the tool setup knows the JDK of the setup environment
    before the tool is in the registry)."""
    old = getattr(_TL, "release", None)
    _TL.release = release
    try:
        yield
    finally:
        _TL.release = old


def release_for(spec) -> int:
    """The ``maven.compiler.release`` to write for ``spec`` on this server (17 unless the JDK Maven uses is older)."""
    forced = getattr(_TL, "release", None)
    if forced:
        return int(forced)
    try:
        from core.wizard import admin_policy
        rt = admin_policy.tool_runtime(spec.id)
        pinned = rt.get("java_release")
        if pinned:
            return int(pinned)
        from core.wizard import dependency_resolver
        env = dict(os.environ)
        env.update(dependency_resolver.toolchain_env())
        env["PATH"] = dependency_resolver.toolchain_path(env.get("PATH", ""))
        env.update(rt.get("env_vars") or {})
        info = maven_jdk(env, rt.get("path_prepend"))
        return release_for_major(int(info["major"]) if info else None)
    except Exception:
        return DEFAULT_RELEASE


def required_release(text: str) -> Optional[int]:
    """The Java release a compiler / runtime error asked for (class-file versions are mapped to releases)."""
    for rx in _REQUIRED_RX:
        m = rx.search(text or "")
        if m:
            n = int(m.group(1))
            return n - 44 if n >= 49 else n           # class file 61 = Java 17
    return None


def _candidate_homes() -> List[str]:
    from core.wizard import runtimes
    pats = [os.path.join(runtimes.RUNTIMES_DIR, "*"), os.path.join(runtimes.RUNTIMES_DIR, "*", "*")]
    if os.name == "nt":
        roots = [os.environ.get(k) for k in ("PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432")] + \
                [r"C:\Program Files", r"C:\Program Files (x86)"]
        for r in [x for x in roots if x]:
            for vendor in ("Java", "Eclipse Adoptium", "Eclipse Foundation", "Microsoft", "Zulu", "Amazon Corretto",
                           "BellSoft", "OpenJDK", "AdoptOpenJDK", "Semeru"):
                pats.append(os.path.join(r, vendor, "*"))
        pats.append(os.path.join(os.environ.get("USERPROFILE", ""), ".jdks", "*"))
    elif sys.platform == "darwin":
        pats += ["/Library/Java/JavaVirtualMachines/*/Contents/Home",
                 os.path.expanduser("~/Library/Java/JavaVirtualMachines/*/Contents/Home")]
    else:
        pats += ["/usr/lib/jvm/*", "/usr/java/*", "/opt/java/*", "/opt/jdk*", "/opt/*jdk*"]
    pats += [os.path.expanduser("~/.sdkman/candidates/java/*"), os.path.expanduser("~/.jdks/*")]
    homes = []
    for p in pats:
        for h in glob.glob(p):
            if os.path.isfile(_exe(h)) and os.path.realpath(h) not in {os.path.realpath(x) for x in homes}:
                homes.append(h)
    return homes


def find_jdks(min_major: int = 0) -> List[Dict[str, object]]:
    """Every JDK (with javac) found on this machine whose release is >= min_major, newest first."""
    found: Dict[str, Dict[str, object]] = {}
    extra = [os.environ.get("JAVA_HOME") or ""]
    java = shutil.which("java")
    for h in _candidate_homes() + [x for x in extra if x]:
        info = probe(_exe(h))
        if info:
            found.setdefault(os.path.realpath(str(info["home"])), info)
    if java:
        info = probe(java)
        if info:
            found.setdefault(os.path.realpath(str(info["home"])), info)
    out = [i for i in found.values() if int(i["major"]) >= min_major and i.get("compiler")]
    return sorted(out, key=lambda i: -int(i["major"]))


_GRADLE_RX = re.compile(r"options\.release\s*=\s*\d{1,2}|JavaVersion\.VERSION_(?:1_)?\d{1,2}")


def patch_gradle_release(build: str, release: int) -> str:
    """build.gradle with its compiler release set to ``release`` (8.2, Gradle tools)."""
    if release >= 9 and "options.release" in build:
        return re.sub(r"options\.release\s*=\s*\d{1,2}", f"options.release = {release}", build)
    line = (f"tasks.withType(JavaCompile).configureEach {{ options.release = {release} }}" if release >= 9 else
            f"java {{ sourceCompatibility = JavaVersion.VERSION_1_{release}; targetCompatibility = JavaVersion.VERSION_1_{release} }}")
    kept = [l for l in build.splitlines() if not _GRADLE_RX.search(l)]
    return "\n".join(kept).rstrip() + "\n\n" + line + "\n"


_POM_RX = re.compile(r"<maven\.compiler\.(release|source|target)>\s*(?:1\.)?\d{1,2}\s*</maven\.compiler\.\1>")


def patch_pom_release(pom: str, release: int) -> str:
    """Sets every maven.compiler.release/source/target in ``pom`` to ``release`` (adds release when missing)."""
    val = f"1.{release}" if release <= 8 else str(release)
    out = _POM_RX.sub(lambda m: f"<maven.compiler.{m.group(1)}>{val if m.group(1) != 'release' else release}"
                                f"</maven.compiler.{m.group(1)}>", pom)
    if release >= 9 and "<maven.compiler.release>" not in out and "<properties>" in out:
        out = out.replace("<properties>", f"<properties>\n    <maven.compiler.release>{release}</maven.compiler.release>", 1)
    if release <= 8:
        out = re.sub(r"\s*<maven\.compiler\.release>\d+</maven\.compiler\.release>", "", out)
    return out
