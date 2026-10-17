"""Runtime provisioning for tool setups (8.0): when the clarification loop finds that a tool needs Java or Node.js and
the admin answers "download it", Understudy fetches an official build into ``generated/runtimes/`` - never into the
system - and the tool's manifest points at it (JAVA_HOME / PATH).

* OpenJDK: Eclipse Temurin from the Adoptium API (``api.adoptium.net``), SHA-256 from the same API.
* Node.js: the official tarball from ``nodejs.org/dist/latest-v<major>.x/``, SHA-256 from its SHASUMS256.txt.
* Apache Maven (8.2): the newest 3.x binary zip from Maven Central, SHA-512 from Maven Central (or archive.apache.org).
* Gradle (8.2): the newest 8.x / 9.x ``-bin.zip`` listed by ``services.gradle.org/versions/all``, SHA-256 from its
  ``checksumUrl``.
* Ruby + RubyGems + Bundler (8.3): on Windows the newest RubyInstaller 3.x ``.7z`` from GitHub
  (``oneclick/rubyinstaller2``), SHA-256 from the release asset's digest; on Linux / macOS conda-forge's relocatable
  ``ruby`` package, installed with micromamba (micromamba itself SHA-256 verified; conda verifies every package).
* Go (8.3): the newest stable release from ``go.dev/dl/?mode=json``, SHA-256 from the same list.
* .NET SDK (8.3): the newest 8.0 SDK from Microsoft's ``releases.json``, SHA-512 from the same file.
* Any conda-forge package (8.3, ``conda-<package>``): only when the AI runtime advisor suggested it AND the admin
  approved it - e.g. ``conda-php``. micromamba installs it into its own prefix.

The checksum must match before anything is extracted, archive members can't escape the target folder, and the
download size is capped. ``set_transport`` replaces the network for tests.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import platform
import re
import shutil
import tarfile
import tempfile
import urllib.request
import zipfile
from typing import Any, Callable, Dict, List, Optional, Tuple

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RUNTIMES_DIR = os.path.join(BASE_DIR, "generated", "runtimes")
MAX_BYTES = 450 * 1024 * 1024
SUPPORTED = {"jdk-17": ("jdk", 17), "jdk-21": ("jdk", 21), "node-20": ("node", 20), "node-22": ("node", 22),
             "maven-3": ("maven", 3), "gradle-8": ("gradle", 8), "gradle-9": ("gradle", 9),
             # 8.3: runtimes Understudy used to tell the admin to install by hand
             "ruby-3": ("ruby", 3), "go-1": ("go", 1), "dotnet-8": ("dotnet", 8),
             # 8.4: standalone CPython builds a tool can run on while Understudy itself stays on its own Python
             "python-3.13": ("python", 13), "python-3.12": ("python", 12), "python-3.11": ("python", 11),
             "python-3.10": ("python", 10)}
_EXE = {"python": "python3", "jdk": "java", "node": "node", "maven": "mvn", "gradle": "gradle", "ruby": "ruby", "go": "go",
        "dotnet": "dotnet"}
_HOME_VAR = {"python": "", "jdk": "JAVA_HOME", "maven": "MAVEN_HOME", "gradle": "GRADLE_HOME", "node": "", "ruby": "",
             "go": "GOROOT", "dotnet": "DOTNET_ROOT"}
# conda-forge packages the AI runtime advisor may propose (``conda-<name>``). Anything else needs a code change.
CONDA_ALLOWED = {"ruby", "go", "nodejs", "openjdk", "maven", "gradle", "php", "perl", "r-base", "julia", "rust",
                 "dotnet", "k6", "lua", "deno", "bun", "kotlin", "scala", "sbt", "groovy", "elixir", "erlang", "dart",
                 "swift", "zig", "make", "cmake", "git", "unzip", "p7zip", "chromium", "firefox", "geckodriver",
                 "python-chromedriver-binary"}
_CONDA_RX = re.compile(r"^conda-([a-z0-9][a-z0-9._-]{0,40})(?:@(\d+(?:\.\d+){0,2}))?$")
MICROMAMBA_BASE = "https://github.com/mamba-org/micromamba-releases/releases/latest/download/"
RUBYINSTALLER_API = "https://api.github.com/repos/oneclick/rubyinstaller2/releases?per_page=30"
GO_DL = "https://go.dev/dl/?mode=json"
PYTHON_BUILDS_API = "https://api.github.com/repos/astral-sh/python-build-standalone/releases?per_page=6"
PYTHON_BUILDS_PAGES = 8              # 9.9: older lines (3.9, 3.8) are only in older releases - look further back
_PBS_PREFIXES = ("https://github.com/astral-sh/python-build-standalone/releases/download/",
                 "https://github.com/indygreg/python-build-standalone/releases/download/")
DOTNET_META = "https://dotnetcli.blob.core.windows.net/dotnet/release-metadata/{major}.0/releases.json"


def is_supported(key: str) -> bool:
    """9.9: a release line of a runtime (``jdk-11``, ``node-18``, ``python-3.9``, ``gradle-7``, ``dotnet-6`` ... -
    ``runtime_catalog.parse``), or ``conda-<package>`` for an allowed conda-forge package."""
    from core.wizard import runtime_catalog
    if key in SUPPORTED or runtime_catalog.parse(key):
        return True
    m = _CONDA_RX.match(key or "")
    return bool(m and m.group(1) in CONDA_ALLOWED)


def kind_of(key: str) -> str:
    """jdk / node / maven / gradle / ruby / go / dotnet - or the conda package name."""
    from core.wizard import runtime_catalog
    p = runtime_catalog.parse(key)
    if p:
        return p[0]
    m = _CONDA_RX.match(key or "")
    if m:
        return {"nodejs": "node", "openjdk": "jdk"}.get(m.group(1), m.group(1))
    return (key or "").split("-")[0]
MAVEN_CENTRAL = "https://repo.maven.apache.org/maven2/org/apache/maven/apache-maven/"
_TRANSPORT: Optional[Callable[[str], bytes]] = None


class RuntimeError_(RuntimeError):
    pass


def set_transport(fn: Optional[Callable[[str], bytes]]) -> None:
    global _TRANSPORT
    _TRANSPORT = fn


def _get(url: str) -> bytes:
    """Every runtime download and list goes through here. 9.9: an official address is fetched from the admin's
    mirror when one is set (runtime_mirrors) - the checksum checks that follow are the same."""
    from core.wizard import runtime_mirrors
    return _get_raw(runtime_mirrors.mirrored(url))


def _get_raw(url: str, timeout: int = 300, limit: int = 0) -> bytes:
    limit = limit or MAX_BYTES
    if _TRANSPORT is not None:
        return _TRANSPORT(url)
    req = urllib.request.Request(url, headers={"User-Agent": "Understudy-runtime-provisioning"})
    with urllib.request.urlopen(req, timeout=timeout) as r:      # follows the GitHub release redirect
        data = r.read(limit + 1)
    if len(data) > limit:
        raise RuntimeError_(f"The download from {url} is larger than {limit // (1024 * 1024)} MB")
    return data


def _official(url: str) -> str:
    from core.wizard import runtime_mirrors                       # a mirror's own links count as the official ones
    return runtime_mirrors.official(str(url or ""))


# 9.9: peak memory of a download on Cloud Run (the archive in memory + the unpacked files on the in-memory disk)
NEEDS_MB = {"jdk": 700, "node": 250, "python": 300, "maven": 60, "gradle": 350, "dotnet": 900, "ruby": 500, "go": 600}


def memory_headroom_mb() -> Optional[int]:
    """Free memory of this container in MB (cgroup v2 / v1), only on Cloud Run - where every written file uses
    memory. None elsewhere, or when it can't be read."""
    if not os.environ.get("K_SERVICE"):
        return None
    for limit_f, used_f in (("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory.current"),
                            ("/sys/fs/cgroup/memory/memory.limit_in_bytes", "/sys/fs/cgroup/memory/memory.usage_in_bytes")):
        try:
            with open(limit_f) as fh:
                limit = fh.read().strip()
            with open(used_f) as fh:
                used = int(fh.read().strip())
            if limit == "max" or int(limit) > 1 << 50:
                return None
            return max(0, (int(limit) - used) // (1024 * 1024))
        except (OSError, ValueError):
            continue
    return None


def _os_arch() -> Tuple[str, str]:
    sysname = {"linux": "linux", "darwin": "mac", "windows": "windows"}.get(platform.system().lower(), "linux")
    m = platform.machine().lower()
    arch = "aarch64" if m in ("arm64", "aarch64") else "x64"
    return sysname, arch


def _safe_extract(data: bytes, name: str, dest: str) -> None:
    os.makedirs(dest, exist_ok=True)
    root = os.path.realpath(dest)

    def ok(member: str) -> bool:
        target = os.path.realpath(os.path.join(dest, member))
        return target == root or target.startswith(root + os.sep)
    if name.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for m in z.namelist():
                if not ok(m):
                    raise RuntimeError_(f"Refused archive member outside the target folder: {m}")
            z.extractall(dest)
    else:
        mode = "r:xz" if name.endswith(".xz") else "r:gz"
        with tarfile.open(fileobj=io.BytesIO(data), mode=mode) as t:
            for m in t.getmembers():
                if not ok(m.name) or m.issym() and not ok(os.path.join(os.path.dirname(m.name), m.linkname)):
                    raise RuntimeError_(f"Refused archive member outside the target folder: {m.name}")
            t.extractall(dest)


def _find(dest: str, rel_exe: str) -> Optional[str]:
    """The directory under dest that contains rel_exe (e.g. bin/java), searching two levels deep."""
    for root, dirs, _files in os.walk(dest):
        if os.path.isfile(os.path.join(root, rel_exe)) or os.path.isfile(os.path.join(root, rel_exe + ".exe")):
            return root
        if root.count(os.sep) - dest.count(os.sep) >= 3:
            dirs[:] = []
    return None


def installed(key: str) -> Optional[Dict[str, str]]:
    dest = os.path.join(RUNTIMES_DIR, key)
    meta = os.path.join(dest, ".understudy-runtime.json")
    if os.path.exists(meta):
        try:
            with open(meta, encoding="utf-8") as fh:
                return json.load(fh)
        except ValueError:
            return None
    return None


def key_of_path(path: str) -> Optional[str]:
    """9.9: the runtime a path belongs to (``generated/runtimes/<key>/...``), or None."""
    try:
        rel = os.path.relpath(os.path.abspath(str(path or "")), RUNTIMES_DIR)
    except ValueError:                                   # another drive on Windows
        return None
    parts = rel.replace("\\", "/").split("/")
    if not path or rel.startswith("..") or parts[0] in ("", ".") or not is_supported(parts[0]):
        return None
    return parts[0]


def resolve_path(path: str) -> str:
    """9.9: a path inside a downloaded runtime that this machine doesn't have (another Cloud Run instance downloaded
    it, or a newer patch release was downloaded again here) is mapped onto the runtime this machine has:
    ``runtimes/jdk-21/jdk-21.0.12+1/bin`` -> ``runtimes/jdk-21/jdk-21.0.13+1/bin``. Anything else is returned as is."""
    key = key_of_path(path)
    if not key or os.path.exists(path):
        return path
    have = installed(key)
    home = (have or {}).get("home") or ""
    if not home or not os.path.isdir(home):
        return path
    rel = os.path.relpath(os.path.abspath(path), RUNTIMES_DIR).replace("\\", "/").split("/")
    home_rel = os.path.relpath(os.path.abspath(home), RUNTIMES_DIR).replace("\\", "/").split("/")
    depth = len(home_rel)                               # <key>/<release folder>
    if len(rel) < depth:
        return path
    mapped = os.path.join(home, *rel[depth:]) if len(rel) > depth else home
    return mapped if os.path.exists(mapped) else path


def provision(key: str, log: Callable[[str], None] = lambda m: None) -> Dict[str, str]:
    """Downloads (or re-uses) a runtime. Returns {key, home, bin, env_name, version, source}."""
    if not is_supported(key):
        raise RuntimeError_(f"Unknown runtime '{key}'. Supported: jdk-<N>, node-<N>, python-3.<N>, maven-3|4, "
                            f"gradle-<N>, dotnet-<N>, ruby-3, go-1 or conda-<package> ({', '.join(sorted(CONDA_ALLOWED))})")
    have = installed(key)
    if have and os.path.isdir(have.get("bin", "")):
        log(f"Runtime {key} already downloaded: {have['home']}")
        return have
    if key.startswith("conda-"):
        m = _CONDA_RX.match(key)
        return _provision_conda(key, m.group(1), m.group(2) or "", log)
    from core.wizard import runtime_catalog
    kind, major = runtime_catalog.parse(key)
    room = memory_headroom_mb()
    if room is not None and room < NEEDS_MB.get(kind, 600):
        raise RuntimeError_(f"Not enough memory to download {key} here: {room} MB free, about {NEEDS_MB.get(kind, 600)} MB "
                            "needed (on Cloud Run the disk is held in memory). Give the service more memory "
                            "(gcloud run services update <service> --memory=8Gi) and try again.")
    if kind == "ruby":
        if _os_arch()[0] == "windows":
            return _provision_rubyinstaller(key, major, log)
        return _provision_conda(key, "ruby", str(major), log, exe="ruby")
    if kind in ("go", "dotnet"):
        return _provision_archive(key, kind, major, log)
    if kind == "python":
        return _provision_python(key, major, log)
    osname, arch = _os_arch()
    dest = os.path.join(RUNTIMES_DIR, key)
    tmp = tempfile.mkdtemp(prefix=f"{key}-", dir=os.path.dirname(dest) if os.path.isdir(os.path.dirname(dest)) else None)
    url = sha = name = version = ""
    try:
        if kind == "jdk":
            api = (f"https://api.adoptium.net/v3/assets/latest/{major}/hotspot?architecture={arch}&image_type=jdk"
                   f"&os={osname}&vendor=eclipse")
            log(f"Looking up Eclipse Temurin JDK {major} ({osname}/{arch}) ...")
            assets = json.loads(_get(api))
            if not assets:
                raise RuntimeError_(f"Adoptium has no JDK {major} build for {osname}/{arch}")
            pkg = assets[0]["binary"]["package"]
            url, sha, name, version = pkg["link"], pkg["checksum"], pkg["name"], assets[0].get("release_name", str(major))
        elif kind == "node":
            base = f"https://nodejs.org/dist/latest-v{major}.x/"
            log(f"Looking up Node.js {major} ({osname}/{arch}) ...")
            sums = _get(base + "SHASUMS256.txt").decode()
            plat = {"linux": "linux", "mac": "darwin", "windows": "win"}[osname]
            ext = "zip" if osname == "windows" else "tar.gz"
            m = re.search(rf"^([0-9a-f]{{64}})\s+(node-v([\d.]+)-{plat}-{arch if arch == 'x64' else 'arm64'}\.{re.escape(ext)})$",
                          sums, re.M)
            if not m:
                raise RuntimeError_(f"nodejs.org has no Node.js {major} build for {plat}/{arch}")
            sha, name, version = m.group(1), m.group(2), m.group(3)
            url = base + name
        algo = "sha256"
        if kind == "maven":
            log(f"Looking up the newest Apache Maven {major}.x on Maven Central ...")
            meta = _get(MAVEN_CENTRAL + "maven-metadata.xml").decode("utf-8", "replace")
            vers = [v for v in re.findall(r"<version>([^<]+)</version>", meta) if re.match(rf"^{major}\.\d+\.\d+$", v)]
            if not vers:
                raise RuntimeError_(f"Maven Central lists no Apache Maven {major}.x release")
            version = sorted(vers, key=lambda v: [int(x) for x in v.split(".")])[-1]
            name = f"apache-maven-{version}-bin.zip"
            url = f"{MAVEN_CENTRAL}{version}/{name}"
            sha, algo = "", "sha512"
            for cs in (url + ".sha512", f"https://archive.apache.org/dist/maven/maven-{major}/{version}/binaries/{name}.sha512"):
                try:
                    sha = _get(cs).decode().split()[0].strip()
                    if re.match(r"^[0-9a-fA-F]{128}$", sha):
                        break
                    sha = ""
                except Exception:
                    continue
            if not sha:
                raise RuntimeError_(f"No published SHA-512 checksum found for {name} - not downloading it")
        elif kind == "gradle":
            log(f"Looking up the newest Gradle {major}.x on services.gradle.org ...")
            rel = [r for r in json.loads(_get("https://services.gradle.org/versions/all"))
                   if str(r.get("version", "")).startswith(f"{major}.") and not r.get("snapshot") and not r.get("nightly")
                   and not r.get("broken") and not r.get("rcFor") and not r.get("milestoneFor")
                   and re.match(r"^\d+\.\d+(\.\d+)?$", str(r.get("version", "")))]
            if not rel:
                raise RuntimeError_(f"services.gradle.org lists no Gradle {major}.x release")
            best = sorted(rel, key=lambda r: [int(x) for x in r["version"].split(".")])[-1]
            version, url = best["version"], best["downloadUrl"]
            if not _official(url).startswith("https://services.gradle.org/distributions/"):
                raise RuntimeError_(f"Unexpected Gradle download location: {url}")
            name = url.rsplit("/", 1)[-1]
            sha = _get(best["checksumUrl"]).decode().split()[0].strip()
        log(f"Downloading {name} ...")
        data = _get(url)
        digest = hashlib.new(algo, data).hexdigest()
        if not sha or digest.lower() != sha.lower():
            raise RuntimeError_(f"Checksum mismatch for {name}: expected {sha[:16]}..., got {digest[:16]}... - not used")
        log(f"Checksum OK ({len(data) // (1024 * 1024)} MB). Extracting ...")
        _safe_extract(data, name, tmp)
        shutil.rmtree(dest, ignore_errors=True)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.move(tmp, dest)
        exe = _EXE[kind]
        home = _find(dest, os.path.join("bin", exe))
        if not home and kind in ("maven", "gradle"):          # Windows launchers are .cmd / .bat
            home = _find(dest, os.path.join("bin", exe + ".cmd")) or _find(dest, os.path.join("bin", exe + ".bat"))
        bindir = os.path.join(home, "bin") if home else None
        if not home and kind == "node":                       # the Windows zip has node.exe at its top level
            home = bindir = _find(dest, "node")
        if not home:
            raise RuntimeError_(f"No bin/{exe} found in {name}")
        if kind in ("maven", "gradle") and os.name != "nt":   # zip archives don't keep the executable bit
            for f in os.listdir(bindir):
                fp = os.path.join(bindir, f)
                if os.path.isfile(fp) and not f.endswith((".cmd", ".bat", ".conf")):
                    os.chmod(fp, 0o755)
        info = {"key": key, "home": home, "bin": bindir, "env_name": _HOME_VAR[kind], "version": str(version),
                "source": url}
        with open(os.path.join(dest, ".understudy-runtime.json"), "w", encoding="utf-8") as fh:
            json.dump(info, fh)
        log(f"{key} ready: {home}")
        return info
    finally:
        shutil.rmtree(tmp, ignore_errors=True)



# ---------------------------------------------------------------------------------------------------------------
# 8.3: Ruby, Go, .NET and conda-forge packages
# ---------------------------------------------------------------------------------------------------------------
def _write_meta(dest: str, info: Dict[str, Any]) -> Dict[str, Any]:
    with open(os.path.join(dest, ".understudy-runtime.json"), "w", encoding="utf-8") as fh:
        json.dump(info, fh)
    return info


def _verify(data: bytes, expected: str, algo: str, name: str) -> None:
    digest = hashlib.new(algo, data).hexdigest()
    if not expected or digest.lower() != expected.lower():
        raise RuntimeError_(f"Checksum mismatch for {name}: expected {expected[:16]}..., got {digest[:16]}... - not used")


def _extract_7z(data: bytes, dest: str) -> None:
    """RubyInstaller ships .7z archives: py7zr when installed, otherwise the bsdtar that comes with Windows 10+."""
    os.makedirs(dest, exist_ok=True)
    root = os.path.realpath(dest)
    try:
        import py7zr  # type: ignore
        with py7zr.SevenZipFile(io.BytesIO(data), mode="r") as z:
            for n in z.getnames():
                t = os.path.realpath(os.path.join(dest, n))
                if not (t == root or t.startswith(root + os.sep)):
                    raise RuntimeError_(f"Refused archive member outside the target folder: {n}")
            z.extractall(dest)
        return
    except ImportError:
        pass
    import subprocess
    tar = shutil.which("tar")
    if not tar:
        raise RuntimeError_("Can't unpack .7z: install py7zr (pip install py7zr) on the Understudy server")
    arc = os.path.join(dest, "_download.7z")
    with open(arc, "wb") as fh:
        fh.write(data)
    try:
        listing = subprocess.run([tar, "-tf", arc], capture_output=True, text=True, timeout=300)
        for n in listing.stdout.splitlines():
            t = os.path.realpath(os.path.join(dest, n))
            if n and not (t == root or t.startswith(root + os.sep)):
                raise RuntimeError_(f"Refused archive member outside the target folder: {n}")
        out = subprocess.run([tar, "-xf", arc, "-C", dest], capture_output=True, text=True, timeout=900)
        if out.returncode != 0:
            raise RuntimeError_(f"Could not unpack the .7z archive: {(out.stderr or out.stdout)[-300:]}")
    finally:
        try:
            os.remove(arc)
        except OSError:
            pass


def _github_digest(asset: Dict[str, Any], body: str = "") -> str:
    """SHA-256 of a GitHub release asset: the API's ``digest`` field, or a checksum line in the release notes."""
    d = str(asset.get("digest") or "")
    if d.lower().startswith("sha256:"):
        return d.split(":", 1)[1].strip()
    m = re.search(rf"([0-9a-fA-F]{{64}})\s+\*?{re.escape(asset.get('name', ''))}", body or "") or \
        re.search(rf"{re.escape(asset.get('name', ''))}\W+([0-9a-fA-F]{{64}})", body or "")
    return m.group(1) if m else ""


def _vkey(v: str) -> List[int]:
    return [int(x) for x in re.findall(r"\d+", v)[:4]]


def _provision_rubyinstaller(key: str, major: int, log: Callable[[str], None]) -> Dict[str, Any]:
    osname, arch = _os_arch()
    log(f"Looking up the newest RubyInstaller {major}.x for Windows on GitHub ...")
    rels = json.loads(_get(RUBYINSTALLER_API))
    best = None
    for rel in rels if isinstance(rels, list) else []:
        if rel.get("prerelease") or rel.get("draft"):
            continue
        m = re.match(rf"^RubyInstaller-({major}\.\d+\.\d+)-(\d+)$", str(rel.get("tag_name") or ""))
        if not m:
            continue
        suffix = "x64" if arch == "x64" else "arm"
        asset = next((a for a in rel.get("assets") or []
                      if re.match(rf"^rubyinstaller-{re.escape(m.group(1))}-{m.group(2)}-{suffix}\.7z$", str(a.get("name")))), None)
        if asset and (best is None or _vkey(m.group(1)) > _vkey(best[0])):
            best = (m.group(1), asset, rel.get("body") or "")
    if not best:
        raise RuntimeError_(f"GitHub lists no RubyInstaller {major}.x .7z for windows/{arch}")
    version, asset, body = best
    sha = _github_digest(asset, body)
    if not sha:
        raise RuntimeError_(f"No published SHA-256 found for {asset['name']} - not downloading it. Install Ruby "
                            f"yourself and register its folder instead.")
    url = str(asset.get("browser_download_url") or "")
    if not _official(url).startswith("https://github.com/oneclick/rubyinstaller2/releases/download/"):
        raise RuntimeError_(f"Unexpected RubyInstaller download location: {url}")
    log(f"Downloading {asset['name']} ...")
    data = _get(url)
    _verify(data, sha, "sha256", asset["name"])
    log(f"Checksum OK ({len(data) // (1024 * 1024)} MB). Extracting ...")
    dest = os.path.join(RUNTIMES_DIR, key)
    tmp = tempfile.mkdtemp(prefix=f"{key}-")
    try:
        _extract_7z(data, tmp)
        shutil.rmtree(dest, ignore_errors=True)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.move(tmp, dest)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    home = _find(dest, os.path.join("bin", "ruby"))
    if not home:
        raise RuntimeError_(f"No bin/ruby.exe found in {asset['name']}")
    info = {"key": key, "home": home, "bin": os.path.join(home, "bin"), "env_name": "", "version": version,
            "source": url, "provides": ["ruby", "gem", "bundle"]}
    log(f"{key} ready: {home} (RubyGems and Bundler included)")
    return _write_meta(dest, info)


def _provision_archive(key: str, kind: str, major: int, log: Callable[[str], None]) -> Dict[str, Any]:
    """Go (go.dev) and the .NET SDK (Microsoft release metadata): official archives with published checksums."""
    osname, arch = _os_arch()
    if kind == "go":
        log("Looking up the newest stable Go on go.dev ...")
        rels = [r for r in json.loads(_get(GO_DL)) if r.get("stable")]
        goos = {"linux": "linux", "mac": "darwin", "windows": "windows"}[osname]
        goarch = "arm64" if arch == "aarch64" else "amd64"
        f = None
        for rel in rels:
            f = next((x for x in rel.get("files") or [] if x.get("os") == goos and x.get("arch") == goarch
                      and x.get("kind") == "archive"), None)
            if f:
                break
        if not f:
            raise RuntimeError_(f"go.dev lists no Go archive for {goos}/{goarch}")
        name, sha, algo, version = f["filename"], f["sha256"], "sha256", str(f.get("version", "")).replace("go", "")
        url = "https://go.dev/dl/" + name
    else:
        log(f"Looking up the newest .NET {major} SDK in Microsoft's release metadata ...")
        meta = json.loads(_get(DOTNET_META.format(major=major)))
        rid = {"linux": "linux", "mac": "osx", "windows": "win"}[osname] + "-" + ("arm64" if arch == "aarch64" else "x64")
        ext = ".zip" if osname == "windows" else ".tar.gz"
        f, version = None, ""
        for rel in meta.get("releases") or []:
            sdk = rel.get("sdk") or {}
            f = next((x for x in sdk.get("files") or [] if x.get("rid") == rid and str(x.get("name", "")).endswith(ext)
                      and str(x.get("url", "")).endswith(ext)), None)
            if f:
                version = str(sdk.get("version") or "")
                break
        if not f:
            raise RuntimeError_(f"Microsoft lists no .NET {major} SDK for {rid}")
        url, sha, algo = f["url"], f.get("hash", ""), "sha512"
        name = url.rsplit("/", 1)[-1]
        if not _official(url).startswith(("https://builds.dotnet.microsoft.com/", "https://dotnetcli.azureedge.net/",
                               "https://download.visualstudio.microsoft.com/", "https://dotnetcli.blob.core.windows.net/")):
            raise RuntimeError_(f"Unexpected .NET download location: {url}")
    log(f"Downloading {name} ...")
    data = _get(url)
    _verify(data, sha, algo, name)
    log(f"Checksum OK ({len(data) // (1024 * 1024)} MB). Extracting ...")
    dest = os.path.join(RUNTIMES_DIR, key)
    tmp = tempfile.mkdtemp(prefix=f"{key}-")
    try:
        _safe_extract(data, name, tmp)
        shutil.rmtree(dest, ignore_errors=True)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.move(tmp, dest)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if kind == "go":
        home = _find(dest, os.path.join("bin", "go"))
        bindir = os.path.join(home, "bin") if home else None
    else:
        home = bindir = _find(dest, "dotnet")                  # dotnet sits at the top of its archive
    if not home:
        raise RuntimeError_(f"No {kind} executable found in {name}")
    if os.name != "nt":
        for f in os.listdir(bindir):
            fp = os.path.join(bindir, f)
            if os.path.isfile(fp):
                os.chmod(fp, os.stat(fp).st_mode | 0o755)
    info = {"key": key, "home": home, "bin": bindir, "env_name": _HOME_VAR[kind], "version": version, "source": url}
    log(f"{key} ready: {home}")
    return _write_meta(dest, info)


def _pbs_triple() -> str:
    osname, arch = _os_arch()
    if osname == "windows":
        return "aarch64-pc-windows-msvc" if arch == "aarch64" else "x86_64-pc-windows-msvc"
    if osname == "mac":
        return "aarch64-apple-darwin" if arch == "aarch64" else "x86_64-apple-darwin"
    return "aarch64-unknown-linux-gnu" if arch == "aarch64" else "x86_64-unknown-linux-gnu"


def _provision_python(key: str, minor: int, log: Callable[[str], None]) -> Dict[str, Any]:
    """8.4: an official standalone CPython 3.<minor> build (python-build-standalone, the builds uv and Rye use), the
    newest patch release, SHA-256 from the GitHub release asset (or the release's SHA256SUMS file)."""
    triple = _pbs_triple()
    log(f"Looking up the newest standalone Python 3.{minor} for {triple} on GitHub ...")
    rx = re.compile(rf"^cpython-(3\.{minor}\.\d+)\+(\d+)-{re.escape(triple)}-install_only\.tar\.gz$")
    best = None
    for page in range(1, PYTHON_BUILDS_PAGES + 1):             # newest releases first; older lines further back
        rels = json.loads(_get(PYTHON_BUILDS_API + (f"&page={page}" if page > 1 else "")))
        if not isinstance(rels, list) or not rels:
            break
        for rel in rels:
            if rel.get("prerelease") or rel.get("draft"):
                continue
            for a in rel.get("assets") or []:
                m = rx.match(str(a.get("name") or ""))
                if m and (best is None or _vkey(m.group(1)) > _vkey(best[0])):
                    best = (m.group(1), a, rel)
            if best:
                break                                          # newest release that has this minor version
        if best:
            break
    if not best:
        raise RuntimeError_(f"GitHub lists no standalone Python 3.{minor} build for {triple}")
    version, asset, rel = best
    sha = _github_digest(asset, rel.get("body") or "")
    if not sha:
        sums = next((a for a in rel.get("assets") or [] if a.get("name") == "SHA256SUMS"), None)
        if sums and _official(sums.get("browser_download_url", "")).startswith(_PBS_PREFIXES):
            try:
                text = _get(sums["browser_download_url"]).decode("utf-8", "replace")
                m = re.search(rf"^([0-9a-fA-F]{{64}})\s+\*?{re.escape(asset['name'])}\s*$", text, re.M)
                sha = m.group(1) if m else ""
            except Exception:
                sha = ""
    if not sha:
        raise RuntimeError_(f"No published SHA-256 found for {asset['name']} - not downloading it")
    url = str(asset.get("browser_download_url") or "")
    if not _official(url).startswith(_PBS_PREFIXES):
        raise RuntimeError_(f"Unexpected Python download location: {url}")
    log(f"Downloading {asset['name']} ...")
    data = _get(url)
    _verify(data, sha, "sha256", asset["name"])
    log(f"Checksum OK ({len(data) // (1024 * 1024)} MB). Extracting ...")
    dest = os.path.join(RUNTIMES_DIR, key)
    tmp = tempfile.mkdtemp(prefix=f"{key}-")
    try:
        _safe_extract(data, asset["name"], tmp)
        shutil.rmtree(dest, ignore_errors=True)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.move(tmp, dest)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if os.name == "nt":
        home = _find(dest, "python")
        bindir = home
        exe = os.path.join(home, "python.exe") if home else ""
    else:
        home = _find(dest, os.path.join("bin", "python3"))
        bindir = os.path.join(home, "bin") if home else None
        exe = os.path.join(bindir, "python3") if bindir else ""
    if not home or not os.path.isfile(exe):
        raise RuntimeError_(f"No Python executable found in {asset['name']}")
    info = {"key": key, "home": home, "bin": bindir, "env_name": "", "version": version, "source": url,
            "provides": ["python3"], "exe": exe}
    log(f"{key} ready: {exe} (Python {version})")
    return _write_meta(dest, info)


def _micromamba(log: Callable[[str], None]) -> str:
    """micromamba (one static binary) from mamba-org's release page, SHA-256 verified, cached in runtimes/_mamba."""
    osname, arch = _os_arch()
    plat = {"linux": "linux", "mac": "osx", "windows": "win"}[osname] + "-" + ("aarch64" if arch == "aarch64" and osname == "linux"
                                                                              else "arm64" if arch == "aarch64" else "64")
    root = os.path.join(RUNTIMES_DIR, "_mamba")
    exe = os.path.join(root, "micromamba.exe" if osname == "windows" else "micromamba")
    if os.path.isfile(exe):
        return exe
    log(f"Downloading micromamba ({plat}) to install the conda-forge package ...")
    data = b""
    for name in ([f"micromamba-{plat}.exe", f"micromamba-{plat}"] if osname == "windows" else [f"micromamba-{plat}"]):
        try:
            sha = _get(MICROMAMBA_BASE + name + ".sha256").decode().split()[0].strip()
            data = _get(MICROMAMBA_BASE + name)
            _verify(data, sha, "sha256", name)
            break
        except RuntimeError_:
            raise
        except Exception:
            data = b""
            continue
    if not data:
        raise RuntimeError_(f"Could not download micromamba for {plat} from {MICROMAMBA_BASE}")
    os.makedirs(root, exist_ok=True)
    with open(exe, "wb") as fh:
        fh.write(data)
    if osname != "windows":
        os.chmod(exe, 0o755)
    return exe


def _provision_conda(key: str, package: str, version: str, log: Callable[[str], None],
                     exe: Optional[str] = None) -> Dict[str, Any]:
    if package not in CONDA_ALLOWED:
        raise RuntimeError_(f"'{package}' is not on Understudy's list of conda-forge packages")
    import subprocess
    mm = _micromamba(log)
    dest = os.path.join(RUNTIMES_DIR, key)
    shutil.rmtree(dest, ignore_errors=True)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    spec = f"{package}={version}" if version else package
    argv = [mm, "create", "--yes", "--root-prefix", os.path.join(RUNTIMES_DIR, "_mamba"), "--prefix", dest,
            "--channel", "conda-forge", "--override-channels", spec]
    log("$ micromamba create ... " + spec + " (conda-forge, every package checksum verified)")
    out = subprocess.run(argv, capture_output=True, text=True, timeout=1800)
    for ln in (out.stdout + "\n" + out.stderr).splitlines()[-15:]:
        if ln.strip():
            log("  " + ln.strip()[:200])
    if out.returncode != 0:
        raise RuntimeError_(f"micromamba could not install {spec}: {(out.stderr or out.stdout).strip()[-400:]}")
    bins = [d for d in (os.path.join(dest, "bin"), os.path.join(dest, "Library", "bin"), os.path.join(dest, "Scripts"),
                        dest) if os.path.isdir(d)]
    want = exe or {"nodejs": "node", "openjdk": "java", "r-base": "R", "python-chromedriver-binary": "chromedriver"}.get(package, package)
    main = next((b for b in bins if any(os.path.isfile(os.path.join(b, want + s)) for s in ("", ".exe", ".bat", ".cmd"))), None)
    if not main:
        raise RuntimeError_(f"{spec} installed, but no '{want}' program was found in it")
    version_found = ""
    try:
        meta_dir = os.path.join(dest, "conda-meta")
        hit = [f for f in os.listdir(meta_dir) if f.startswith(package + "-") and f.endswith(".json")]
        if hit:
            version_found = json.load(open(os.path.join(meta_dir, hit[0]), encoding="utf-8")).get("version", "")
    except Exception:
        pass
    info = {"key": key, "home": dest, "bin": main, "extra_bins": [b for b in bins if b != main],
            "env_name": "JAVA_HOME" if package == "openjdk" else "", "version": version_found or version,
            "source": f"conda-forge::{spec}"}
    if package == "ruby":
        info["provides"] = ["ruby", "gem", "bundle"]
    log(f"{key} ready: {dest}")
    return _write_meta(dest, info)
