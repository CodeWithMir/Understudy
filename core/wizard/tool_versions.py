"""Tool version catalogue for Mission Control.

The admin decides which versions of each whitelisted automation tool users may run. The list of candidate
versions comes live from the tool's package registry:

    pip    https://pypi.org/pypi/<name>/json
    npm    https://registry.npmjs.org/<name>
    maven  https://repo1.maven.org/maven2/<group path>/<artifact>/maven-metadata.xml
    gem    https://rubygems.org/api/v1/versions/<name>.json
    nuget  https://api.nuget.org/v3-flatcontainer/<name>/index.json
    go     https://proxy.golang.org/<module>/@v/list

Results are cached for an hour. When the registry cannot be reached (offline server, proxy) the list falls back
to what is known locally: the version installed on the backend and the catalog's own constraint, and the result
says ``source: "offline"`` so the admin knows the list may be incomplete.

Only the tool's *primary* package (the first package of its spec - e.g. ``selenium`` for Selenium, ``cypress`` for
Cypress) is version-managed; supporting packages keep the constraints from the vetted spec.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional

from core.wizard.frameworks import FrameworkSpec, _VERSION_RE

CACHE_TTL_S = 3600
MAX_VERSIONS = 40
_CACHE: Dict[str, Dict[str, Any]] = {}
_LOCK = threading.Lock()
_PRE_RX = re.compile(r"(?i)(a|b|rc|alpha|beta|dev|pre|canary|next|nightly|snapshot|insiders|experimental)")
_EXACT_RX = re.compile(r"^[0-9][A-Za-z0-9.+_-]{0,39}$")


def primary_package(spec: FrameworkSpec):
    return spec.packages[0] if spec.packages else None


def _vkey(v: str):
    parts = re.split(r"[.+_-]", v)
    return [(0, int(p)) if p.isdigit() else (1, p) for p in parts]


def _sort_desc(versions: List[str]) -> List[str]:
    try:
        from packaging.version import Version
        return sorted(set(versions), key=lambda v: Version(v), reverse=True)
    except Exception:
        return sorted(set(versions), key=_vkey, reverse=True)


def _stable(versions: List[str]) -> List[str]:
    out = []
    for v in versions:
        if not _EXACT_RX.match(v):
            continue
        tail = re.sub(r"^[0-9.]+", "", v)
        if tail and _PRE_RX.search(tail):
            continue
        out.append(v)
    return out


def _get(url: str, timeout: float = 10.0, accept: str = "application/json, application/xml;q=0.9, */*;q=0.1") -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Understudy-Mission-Control/6.0", "Accept": accept})
    with urllib.request.urlopen(req, timeout=timeout) as resp:          # nosec - fixed registry hosts only
        return resp.read(64_000_000)


def _fetch(ecosystem: str, name: str) -> Dict[str, Any]:
    base = name.split("[")[0]
    if ecosystem == "pip":
        d = json.loads(_get(f"https://pypi.org/pypi/{urllib.parse.quote(base)}/json"))
        vs = [v for v, files in (d.get("releases") or {}).items() if files and not all(f.get("yanked") for f in files)]
        return {"versions": vs, "latest": (d.get("info") or {}).get("version")}
    if ecosystem == "npm":
        # abbreviated ("corgi") metadata - a fraction of the full document for big packages like playwright
        d = json.loads(_get(f"https://registry.npmjs.org/{urllib.parse.quote(base, safe='@')}",
                            accept="application/vnd.npm.install-v1+json"))
        return {"versions": list((d.get("versions") or {}).keys()), "latest": (d.get("dist-tags") or {}).get("latest")}
    if ecosystem in ("maven", "gradle"):          # Gradle uses Maven coordinates and Maven Central
        group, artifact = base.split(":", 1)
        xml = _get(f"https://repo1.maven.org/maven2/{group.replace('.', '/')}/{artifact}/maven-metadata.xml")
        root = ET.fromstring(xml)
        vs = [e.text for e in root.iter("version") if e.text]
        latest = root.findtext("versioning/release") or root.findtext("versioning/latest")
        return {"versions": vs, "latest": latest}
    if ecosystem == "gem":
        d = json.loads(_get(f"https://rubygems.org/api/v1/versions/{urllib.parse.quote(base)}.json"))
        vs = [x["number"] for x in d if not x.get("prerelease")]
        return {"versions": vs, "latest": vs[0] if vs else None}
    if ecosystem == "nuget":
        d = json.loads(_get(f"https://api.nuget.org/v3-flatcontainer/{urllib.parse.quote(base.lower())}/index.json"))
        vs = d.get("versions") or []
        return {"versions": vs, "latest": vs[-1] if vs else None}
    if ecosystem == "go":
        vs = [v.lstrip("v") for v in _get(f"https://proxy.golang.org/{base.lower()}/@v/list").decode().split()]
        return {"versions": vs, "latest": None}
    raise ValueError(f"No registry for ecosystem '{ecosystem}'")


def installed_version(spec: FrameworkSpec) -> Optional[str]:
    """Version of the primary package already available to the backend (pip), or in the tool's isolated env (npm)."""
    pkg = primary_package(spec)
    if not pkg:
        return None
    base = pkg.name.split("[")[0]
    if spec.ecosystem == "pip":
        try:
            from importlib import metadata
            return metadata.version(base)
        except Exception:
            return None
    if spec.ecosystem == "npm":
        try:
            from core.wizard.dependency_resolver import ENVS_DIR
            for d in sorted(os.listdir(ENVS_DIR)):
                if d.startswith(spec.id + "-"):
                    pj = os.path.join(ENVS_DIR, d, "node_modules", base, "package.json")
                    if os.path.exists(pj):
                        with open(pj, encoding="utf-8") as fh:
                            return json.load(fh).get("version")
        except Exception:
            return None
    return None


def available_versions(spec: FrameworkSpec, refresh: bool = False, include_prereleases: bool = False,
                       limit: Optional[int] = MAX_VERSIONS) -> Dict[str, Any]:
    pkg = primary_package(spec)
    out: Dict[str, Any] = {"tool_id": spec.id, "ecosystem": spec.ecosystem, "package": pkg.name if pkg else None,
                           "catalog_constraint": pkg.version if pkg else "", "versions": [], "latest": None,
                           "installed": installed_version(spec), "source": "none", "error": None,
                           "fetched_at": None}
    if not pkg or spec.ecosystem == "none":
        out["error"] = "This tool has no package to version."
        return out
    key = f"{spec.ecosystem}:{pkg.name}"
    with _LOCK:
        hit = _CACHE.get(key)
    if hit and not refresh and time.time() - hit["t"] < CACHE_TTL_S:
        data, err, src, fetched = hit["data"], None, "registry", hit["t"]
    else:
        try:
            data = _fetch(spec.ecosystem, pkg.name)
            with _LOCK:
                _CACHE[key] = {"t": time.time(), "data": data}
            err, src, fetched = None, "registry", time.time()
        except Exception as e:           # offline / proxy / unknown package
            data, err, src, fetched = {"versions": [], "latest": None}, f"{type(e).__name__}: {e}"[:300], "offline", None
    versions = [str(v) for v in data.get("versions") or []]
    if not include_prereleases:
        versions = _stable(versions)
    known = [v for v in (out["installed"], data.get("latest")) if v and _EXACT_RX.match(str(v))]
    versions = _sort_desc(versions + known)
    versions = versions[:limit] if limit else versions
    out.update(versions=versions, latest=data.get("latest") or (versions[0] if versions else None), source=src,
               error=err, fetched_at=fetched)
    return out


def validate_version(version: str) -> str:
    v = str(version or "").strip()
    if not v or not _EXACT_RX.match(v) or not _VERSION_RE.match(v):
        raise ValueError(f"'{version}' is not a valid exact version (e.g. 4.25.0).")
    return v


def apply_version(spec: FrameworkSpec, version: Optional[str]) -> FrameworkSpec:
    """Returns a copy of the spec whose primary package is pinned to ``version`` (exact)."""
    s = FrameworkSpec.from_dict(spec.to_dict())
    if version and s.packages:
        s.packages[0].version = ("==" + version) if s.ecosystem == "pip" else version
    return s
