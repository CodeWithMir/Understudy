"""Loads the project's ``.env`` into the process environment at start-up.

Before 6.0.1 only the Gemini key was read from ``.env``; every other setting (``GOOGLE_CLOUD_PROJECT``,
``UNDERSTUDY_STORE``, ``UNDERSTUDY_GCS_BUCKET`` ...) only worked when exported in the shell or passed by Docker.
Now ``server.py`` calls ``load()`` first, so a ``.env`` written by ``scripts/gcp_setup.*`` (or by hand) just works.

Rules: real environment variables always win (Docker / Cloud Run / the shell), empty values are ignored, and
``UNDERSTUDY_SKIP_DOTENV=1`` turns it off (the test-suite does this). Values are never printed.
"""
from __future__ import annotations

import os
from typing import Dict, List, Optional

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def path() -> str:
    return os.environ.get("UNDERSTUDY_ENV_FILE") or os.path.join(_BASE, ".env")


def parse(file_path: Optional[str] = None) -> Dict[str, str]:
    out: Dict[str, str] = {}
    try:
        with open(file_path or path(), encoding="utf-8-sig") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                if line.startswith("export "):
                    line = line[len("export "):]
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
                    v = v[1:-1]
                elif " #" in v:                                  # inline comment after an unquoted value
                    v = v.split(" #", 1)[0].rstrip()
                if k and k.replace("_", "").isalnum():
                    out[k] = v
    except FileNotFoundError:
        pass
    return out


def load(file_path: Optional[str] = None) -> List[str]:
    """Copies .env values into os.environ (without overriding). Returns the names that were set."""
    if os.environ.get("UNDERSTUDY_SKIP_DOTENV") == "1":
        return []
    applied = []
    for k, v in parse(file_path).items():
        if v and k not in os.environ:
            os.environ[k] = v
            applied.append(k)
    return applied


def update(values: Dict[str, Optional[str]], file_path: Optional[str] = None) -> None:
    """Sets (or with None removes) keys in .env, keeping comments and the order of everything else."""
    p = file_path or path()
    try:
        with open(p, encoding="utf-8-sig") as fh:
            lines = fh.readlines()
    except FileNotFoundError:
        lines = []
    done = set()
    out = []
    for line in lines:
        key = line.strip().split("=", 1)[0].replace("export ", "").strip() if "=" in line and \
            not line.strip().startswith("#") else None
        if key in values:
            done.add(key)
            if values[key] is not None:
                out.append(f"{key}={values[key]}\n")
            continue
        out.append(line)
    for k, v in values.items():
        if k not in done and v is not None:
            if out and not out[-1].endswith("\n"):
                out[-1] += "\n"
            out.append(f"{k}={v}\n")
    with open(p, "w", encoding="utf-8") as fh:
        fh.writelines(out)
