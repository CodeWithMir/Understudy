"""8.3: one-line, human wording for a set of setup changes (shown on the admin's "Apply the AI's fix" button)."""
from __future__ import annotations

from typing import Any, Dict


def describe(ch: Dict[str, Any]) -> str:
    parts = []
    if ch.get("add_packages"):
        parts.append("add " + ", ".join(p["name"] + (f" {p['version']}" if p.get("version") else "") for p in ch["add_packages"]))
    if ch.get("post_install_remove"):
        parts.append("drop " + "; ".join(" ".join(c) for c in ch["post_install_remove"]))
    if ch.get("post_install"):
        parts.append("then run " + "; ".join(" ".join(c) for c in ch["post_install"]))
    if ch.get("rewrite_smoke"):
        parts.append("rewrite the smoke test")
    if ch.get("version"):
        parts.append(f"use version {ch['version']}")
    if ch.get("download_runtimes"):
        parts.append("download " + ", ".join(ch["download_runtimes"]))
    if ch.get("runtime_used"):
        parts.append("use " + str(ch["runtime_used"]).split(" at ")[0])
    elif ch.get("env_vars") or ch.get("path_prepend"):
        if ch.get("env_vars"):
            parts.append("set " + ", ".join(ch["env_vars"]))
        if ch.get("path_prepend"):
            parts.append("add " + ", ".join(ch["path_prepend"])[:60] + " to PATH")
    if ch.get("index_url"):
        parts.append("use repository " + ch["index_url"])
    if ch.get("skip_post_install"):
        parts.append("skip the tool's browser download")
    out = " · ".join(parts) or "apply the suggested change"
    return out[:110] + ("..." if len(out) > 110 else "")
