"""Restricted-network execution (closes the "sandbox gap").

The generated automation is code that Understudy runs on the server. Before v6.2 it could open a connection to any
address the server can reach. Now every run gets its own **egress proxy** (a tiny forward proxy bound to 127.0.0.1 on an
ephemeral port) and the child process is started with ``HTTP_PROXY`` / ``HTTPS_PROXY`` / ``ALL_PROXY`` pointing at it
and an empty ``NO_PROXY``. The proxy decides, per destination host:

* **blocked list** (Mission Control)            -> always denied, in every mode
* **cloud metadata / link-local**               -> always denied, never excepted
* **restricted mode** (default)                 -> allowed only for the run's target host(s) and the admin's *allowed* list
* **public mode**                               -> any public host
* a destination that is not on the allowed list must also resolve to a **public** address (no SSRF into the intranet);
  the proxy connects to the exact IP it validated, so DNS-rebinding cannot swap it afterwards.

Python (requests / urllib / httpx), Node (with the usual proxy agents), curl, Chromium (Playwright, Selenium, Puppeteer),
Cypress and Java tools honour those variables. The built-in reference driver additionally routes every request through
the same decision function. Secrets: the child environment never contains the Gemini key, admin token or cloud
credentials (``dependency_resolver._scrubbed_env``).

Limits (documented in README / ARCHITECTURE): this is an application-level control. A program that deliberately opens raw
sockets, ignores the proxy variables, or reaches loopback services directly (Chromium never proxies loopback) is not
stopped by it - for hard isolation run executions in a separate Cloud Run job / container with a VPC egress firewall.
Every denied attempt is logged in the run's live log and counted in the result (``egress``).
"""
from __future__ import annotations

import ipaddress
import select
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlsplit

from core.wizard import admin_policy

_METADATA = {"metadata.google.internal", "metadata", "169.254.169.254", "fd00:ec2::254", "100.100.100.200"}


def is_blocked_now(host: str) -> bool:
    h = (host or "").lower().rstrip(".")
    return h in _METADATA or admin_policy.is_host_blocked(h)


def _host_of(url: str) -> str:
    return (urlsplit(url if "://" in url else "http://" + url).hostname or "").lower().rstrip(".")


def run_allow_list(target_url: str, extra: Iterable[str] = ()) -> List[str]:
    """Hosts a run may talk to in restricted mode: its target + the admin's allowed list + explicit extras."""
    hosts = {_host_of(target_url)} | set(admin_policy.excepted_hosts()) | {_host_of(x) for x in extra if x}
    return sorted(h for h in hosts if h and not is_blocked_now(h))


def decide(host: str, allow: Iterable[str], mode: Optional[str] = None) -> Tuple[bool, str, Optional[str]]:
    """(allowed, reason, ip_to_connect). The IP is only set for non-allow-listed public hosts."""
    h = (host or "").lower().rstrip(".")
    mode = mode or admin_policy.network_mode()
    if not h:
        return False, "no host", None
    if h in _METADATA:
        return False, "cloud-metadata address (always blocked)", None
    if admin_policy.is_host_blocked(h):
        return False, "on the blocked list set by your admin", None
    allow = set(allow)
    listed = h in allow
    if mode == "restricted" and not listed:
        return False, "not the target host and not on the admin's allowed list (restricted network)", None
    ips: List[str] = []
    try:
        ips = [str(ipaddress.ip_address(h))]
    except ValueError:
        try:
            ips = sorted({i[4][0] for i in socket.getaddrinfo(h, None, proto=socket.IPPROTO_TCP)})
        except socket.gaierror:
            return (True, "allow-listed (unresolved here)", None) if listed else (False, "DNS lookup failed", None)
    for ip_s in ips:
        ip = ipaddress.ip_address(ip_s.split("%")[0])
        if ip.is_link_local or str(ip) in _METADATA:
            return False, f"{ip} is a link-local / metadata address", None
        if not listed and (ip.is_private or ip.is_loopback or ip.is_multicast or ip.is_unspecified or ip.is_reserved):
            return False, f"{h} resolves to the non-public address {ip}", None
    return True, "allow-listed" if listed else "public host", (ips[0] if ips and not listed else None)


class EgressProxy:
    """Per-run filtering forward proxy (HTTP + CONNECT). Use as a context manager."""

    def __init__(self, allow: Iterable[str], log=None, mode: Optional[str] = None):
        self.allow = sorted(set(allow))
        self.mode = mode or admin_policy.network_mode()
        self.log = log or (lambda m: None)
        self.denied: List[Dict[str, str]] = []
        self.allowed_count = 0
        self._srv: Optional[ThreadingHTTPServer] = None

    # -- policy -----------------------------------------------------------------------------------------------
    def check(self, host: str) -> Tuple[bool, Optional[str]]:
        ok, why, ip = decide(host, self.allow, self.mode)
        if ok:
            self.allowed_count += 1
        else:
            if not any(d["host"] == host for d in self.denied):
                self.log(f"EGRESS BLOCKED: {host} - {why}")
            self.denied.append({"host": host, "reason": why})
        return ok, ip

    # -- server -----------------------------------------------------------------------------------------------
    def start(self) -> "EgressProxy":
        proxy = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *a):
                pass

            def _deny(self, why="blocked by Understudy network policy"):
                body = f"Understudy egress policy: {why}\n".encode()
                self.send_response(403)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_CONNECT(self):
                host, _, port = self.path.rpartition(":")
                host = host.strip("[]")
                ok, ip = proxy.check(host)
                if not ok:
                    return self._deny()
                try:
                    up = socket.create_connection((ip or host, int(port or 443)), timeout=15)
                except Exception:
                    self.send_response(502)
                    self.end_headers()
                    return
                self.send_response(200, "Connection established")
                self.end_headers()
                self._pipe(up)

            def _pipe(self, up):
                conn = self.connection
                socks = [conn, up]
                try:
                    while True:
                        r, _, x = select.select(socks, [], socks, 60)
                        if x or not r:
                            break
                        for s in r:
                            data = s.recv(65536)
                            if not data:
                                return
                            (up if s is conn else conn).sendall(data)
                except Exception:
                    pass
                finally:
                    up.close()

            def _forward(self):
                parts = urlsplit(self.path)
                host = (parts.hostname or "").lower()
                ok, ip = proxy.check(host)
                if not ok:
                    return self._deny()
                port = parts.port or (443 if parts.scheme == "https" else 80)
                try:
                    up = socket.create_connection((ip or host, port), timeout=15)
                    path = (parts.path or "/") + (("?" + parts.query) if parts.query else "")
                    n = int(self.headers.get("Content-Length") or 0)
                    body = self.rfile.read(n) if n else b""
                    hdrs = "".join(f"{k}: {v}\r\n" for k, v in self.headers.items()
                                   if k.lower() not in ("proxy-connection", "connection"))
                    up.sendall(f"{self.command} {path} HTTP/1.0\r\n{hdrs}Connection: close\r\n\r\n".encode() + body)
                    while True:
                        chunk = up.recv(65536)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                    up.close()
                except Exception:
                    try:
                        self.send_response(502)
                        self.end_headers()
                    except Exception:
                        pass

            do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = do_PATCH = do_OPTIONS = _forward

        class Srv(ThreadingHTTPServer):
            daemon_threads = True

        self._srv = Srv(("127.0.0.1", 0), H)
        threading.Thread(target=self._srv.serve_forever, daemon=True, name="egress-proxy").start()
        return self

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._srv.server_address[1]}" if self._srv else ""

    def env(self) -> Dict[str, str]:
        u = self.url
        # 8.3: loopback (+ the admin's bypass hosts) never goes through the proxy - the tool's local driver lives there
        try:
            bypass = ",".join(admin_policy.proxy_bypass_hosts())
        except Exception:
            bypass = "localhost,127.0.0.1,::1"
        return {"HTTP_PROXY": u, "HTTPS_PROXY": u, "ALL_PROXY": u, "http_proxy": u, "https_proxy": u, "all_proxy": u,
                "NO_PROXY": bypass, "no_proxy": bypass, "UNDERSTUDY_EGRESS_PROXY": u,
                "UNDERSTUDY_EGRESS_ALLOW": ",".join(self.allow)}

    def stop(self) -> None:
        if self._srv:
            self._srv.shutdown()
            self._srv.server_close()
            self._srv = None

    def summary(self) -> Dict[str, Any]:
        uniq = {}
        for d in self.denied:
            uniq.setdefault(d["host"], d["reason"])
        return {"mode": self.mode, "allowed_hosts": self.allow, "requests_allowed": self.allowed_count,
                "requests_denied": len(self.denied), "denied_hosts": [{"host": h, "reason": r} for h, r in uniq.items()]}

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
