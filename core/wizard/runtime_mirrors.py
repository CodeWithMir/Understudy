"""Mirrors for runtime downloads (9.9) - e.g. an internal Artifactory / Nexus that proxies Adoptium or nodejs.org.

Every runtime download and version list goes through ``runtimes._get`` / ``runtime_catalog._fetch``; both call
``mirrored(url)``, which swaps an official address prefix for the mirror the admin set for that source. Nothing else
changes: the same files and checksums are expected, and the checksum is verified before anything is unpacked.

* The official prefixes are fixed here (``SOURCES``) - the admin chooses only *where* a source is fetched from.
* A mirror must be ``https://``, end with ``/`` and carry no user name or password (the address is stored in the
  admin policy and shown in Mission Control) - use a read-only anonymous repository, or network allow-listing.
* A mirror may hand back metadata that still names the official site (a plain proxy repository does) - those links
  are mirrored too - or links already rewritten to the mirror; ``official(url)`` maps them back for the location checks.
* Every change is recorded in the admin audit log; ``test(source)`` fetches one small known file through the mirror.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

# id -> (what it serves, official prefix, a small file to test the mirror with - relative to the prefix)
SOURCES: Dict[str, Tuple[str, str, str]] = {
    "adoptium-api": ("Java (Temurin): version list and download links", "https://api.adoptium.net/",
                     "v3/info/available_releases"),
    "adoptium-files": ("Java (Temurin): JDK archives (GitHub releases)", "https://github.com/adoptium/", ""),
    "nodejs": ("Node.js: archives and SHASUMS256.txt", "https://nodejs.org/dist/", "index.json"),
    "nodejs-schedule": ("Node.js: release schedule (end-of-life dates)", "https://raw.githubusercontent.com/nodejs/Release/",
                        "main/schedule.json"),
    "python-api": ("Python: standalone build list (GitHub API)",
                   "https://api.github.com/repos/astral-sh/python-build-standalone/", "releases?per_page=1"),
    "python-files": ("Python: standalone build archives",
                     "https://github.com/astral-sh/python-build-standalone/releases/download/", ""),
    "maven-central": ("Maven: Apache Maven archives and checksums (Maven Central)", "https://repo.maven.apache.org/maven2/",
                      "org/apache/maven/apache-maven/maven-metadata.xml"),
    "apache-archive": ("Maven: checksums of older releases (Apache archive)", "https://archive.apache.org/dist/", ""),
    "gradle": ("Gradle: version list, archives and checksums", "https://services.gradle.org/", "versions/current"),
    "dotnet-meta": (".NET: release metadata", "https://dotnetcli.blob.core.windows.net/",
                    "dotnet/release-metadata/releases-index.json"),
    "dotnet-files": (".NET: SDK archives", "https://builds.dotnet.microsoft.com/", ""),
    "go": ("Go: version list and archives", "https://go.dev/dl/", "?mode=json"),
}
_URL_RX = re.compile(r"^https://[A-Za-z0-9.-]+(:\d{1,5})?(/[A-Za-z0-9._~%!$&'()*+,;=:@/-]*)?/$")


class MirrorError(ValueError):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def mirrors() -> Dict[str, str]:
    """{source id: mirror prefix} set by the admin (shared through the admin policy)."""
    from core.wizard import admin_policy as ap
    v = ap._load()["settings"].get("runtime_mirrors") or {}
    return {k: str(u) for k, u in v.items() if k in SOURCES and isinstance(u, str) and u}


def mirrored(url: str) -> str:
    """The address to fetch: an official prefix swapped for its mirror (when one is set)."""
    for sid, mirror in mirrors().items():
        prefix = SOURCES[sid][1]
        if url.startswith(prefix):
            return mirror + url[len(prefix):]
    return url


def official(url: str) -> str:
    """A mirror address mapped back to the official one (for the download-location checks)."""
    for sid, mirror in mirrors().items():
        if url.startswith(mirror):
            return SOURCES[sid][1] + url[len(mirror):]
    return url


def check_url(url: str) -> str:
    u = (url or "").strip()
    if u and not u.endswith("/"):
        u += "/"
    p = urlparse(u)
    if p.scheme != "https":
        raise MirrorError("A mirror must be an https:// address.")
    if p.username or p.password or "@" in (p.netloc or ""):
        raise MirrorError("Don't put a user name or password in the mirror address - it would be stored and shown in "
                          "Mission Control. Use a read-only repository that allows anonymous reads, or allow this "
                          "server's network address on the mirror.")
    if p.query or p.fragment or not _URL_RX.match(u):
        raise MirrorError("Give the mirror's base address, e.g. https://artifactory.example.com/artifactory/nodejs/")
    return u


def set_mirror(source: str, url: Optional[str], by: str = "admin") -> Dict[str, Any]:
    """url None / '' removes the mirror (back to the official site)."""
    from core.wizard import activity_log, admin_policy as ap, runtime_catalog
    if source not in SOURCES:
        raise MirrorError(f"Unknown source '{source}'. One of: {', '.join(SOURCES)}.", 404)
    clean = check_url(url) if (url or "").strip() else ""
    with ap._LOCK:
        st = ap._load()["settings"].setdefault("runtime_mirrors", {})
        before = st.get(source) or ""
        if clean:
            st[source] = clean
        else:
            st.pop(source, None)
        ap._save()
    runtime_catalog._CACHE.clear()                          # the version lists come from the new place
    activity_log.record("ADMIN", f"Admin '{by}' " + (f"set the mirror for {SOURCES[source][0]}" if clean
                                                     else f"removed the mirror for {SOURCES[source][0]}"),
                        f"{SOURCES[source][1]} -> {clean or SOURCES[source][1]} (was {before or 'the official site'})",
                        {"source": source, "mirror": clean, "previous": before}, source="ADMIN")
    return view()


def view() -> List[Dict[str, Any]]:
    m = mirrors()
    return [{"id": sid, "label": label, "official": prefix, "mirror": m.get(sid, ""), "testable": bool(probe)}
            for sid, (label, prefix, probe) in SOURCES.items()]


def test(source: str, url: Optional[str] = None) -> Dict[str, Any]:
    """Fetches the source's small known file through the mirror (or the given address, before saving it)."""
    from core.wizard import runtimes
    if source not in SOURCES:
        raise MirrorError(f"Unknown source '{source}'.", 404)
    label, prefix, probe = SOURCES[source]
    base = check_url(url) if url else (mirrors().get(source) or prefix)
    if not probe:
        return {"ok": None, "source": source, "url": base,
                "detail": "This source has no small test file - it is checked by the next download (checksum verified)."}
    target = base + probe
    try:
        data = runtimes._get_raw(target, timeout=20, limit=8 * 1024 * 1024)
    except Exception as e:
        return {"ok": False, "source": source, "url": target, "detail": f"{type(e).__name__}: {str(e)[:200]}"}
    looks = data[:1].strip() in (b"{", b"[", b"<") if data else False
    return {"ok": bool(data) and looks, "source": source, "url": target, "bytes": len(data),
            "detail": "answered" if looks else "answered, but not with the expected JSON / XML"}
