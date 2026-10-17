"""STEP 4 - EXECUTE: inject the validated payload into the suite and run it against the target URL.

Strategies (the one used is always logged and shown to the user):
  * ``framework`` - the generated suite is run with the chosen framework's own runtime, using the
                    dependencies resolved in WATCH (``dependency_resolver.runtime_for``). stdout/stderr
                    are streamed line by line into the live log.
  * ``reference`` - Understudy's built-in Playwright driver interprets the SAME step plan with the SAME
                    payload. Used when the framework's toolchain is not installed on this server (or the
                    user skipped installation), or when self-healing diagnosed an ENVIRONMENT failure.

Payload injection: values are written to ``payload.json`` in a fresh per-attempt run directory; secrets
are passed only as ``UNDERSTUDY_SECRET_<KEY>`` environment variables and never written to disk.
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urljoin

from core.wizard.frameworks import FrameworkSpec

RUN_TIMEOUT_S = int(os.environ.get("UNDERSTUDY_RUN_TIMEOUT_S", "300"))
STEP_TIMEOUT_MS = int(os.environ.get("UNDERSTUDY_STEP_TIMEOUT_MS", "8000"))
_STEP_RX = re.compile(r"STEP\s+(\d+)\s+(OK|FAILED)\s*:?\s*(.*)")
_STEP_FAILED_BARE_RX = re.compile(r"\bSTEP\s+FAILED\s*:?\s*(.*)")


def write_run_dir(run_dir: str, files: Dict[str, str], params: List[Dict[str, Any]],
                  extra_files: Optional[Dict[str, str]] = None) -> str:
    os.makedirs(run_dir, exist_ok=True)
    for rel, content in {**(extra_files or {}), **files}.items():
        dest = os.path.normpath(os.path.join(run_dir, rel))
        if not dest.startswith(os.path.normpath(run_dir) + os.sep):
            raise ValueError(f"Refusing to write outside the run directory: {rel}")
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "w", encoding="utf-8") as fh:
            fh.write(content)
    payload_path = os.path.join(run_dir, "payload.json")
    with open(payload_path, "w", encoding="utf-8") as fh:
        json.dump(payload_for_disk(params), fh, indent=2)
    return payload_path


def payload_for_disk(params: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {p["key"]: p.get("value") for p in params if p.get("type") != "secret"}


def secret_env(params: List[Dict[str, Any]]) -> Dict[str, str]:
    return {"UNDERSTUDY_SECRET_" + p["key"].upper(): str(p.get("value") or "") for p in params if p.get("type") == "secret"}


def _parse_step_line(line: str, state: Dict[str, Any]) -> None:
    from core.wizard import governance_runtime as gr
    if gr.OK_RX.search(line) and not state.get("stepped"):     # 9.9: the SOP rules were checked before step 1
        state["governed"] = True
    fm = gr.FAILED_RX.search(line)
    if fm and not state.get("governance_failed"):
        state["governance_failed"] = fm.group(1)[:500]
    m = _STEP_RX.search(line)
    if not m:
        bare = _STEP_FAILED_BARE_RX.search(line)
        if bare:            # 9.9.1: an older script's 'STEP FAILED: ...' - a failure, even though the step is unknown
            state["step_failed_unnumbered"] = True
            state.setdefault("failure_reason", (bare.group(1) or "a step failed")[:500])
        return
    if int(m.group(1)) > 0:
        state["stepped"] = True
    n, status, msg = int(m.group(1)), m.group(2), m.group(3)
    if status == "OK":
        state["last_ok_step"] = max(state.get("last_ok_step", 0), n)
    else:
        state["failed_step"] = n
        state["failure_reason"] = msg[:500]
        if state.get("last_ok_step", 0) >= n:   # 9.9.1: an 'OK' printed before the step ran is not a pass
            state["last_ok_step"] = n - 1


_ROBOT_MSG_RX = re.compile(r"<msg\b[^>]*>([^<]*)</msg>")


def _is_robot(spec: FrameworkSpec) -> bool:
    cmd = [str(t).lower() for t in spec.run_command or []]
    return bool(cmd) and (cmd[0] == "robot" or (len(cmd) > 2 and cmd[1:3] == ["-m", "robot"]))


def _robot_markers(run_dir: str, state: Dict[str, Any], log: Callable[[str], None]) -> None:
    """9.9.1: print() inside a Robot Framework keyword goes to Robot's log, not the console - so a suite that printed
    its GOVERN / STEP lines that way looked as if it never ran a step (and was "repaired" as a broken runtime). Read
    them back from Robot's output.xml. Text only (a regex, no XML parser): the file is written by the suite."""
    import html as _html
    for rel in ("output.xml", os.path.join("results", "output.xml")):
        p = os.path.join(run_dir, rel)
        try:
            if not os.path.isfile(p) or os.path.getsize(p) > 20 * 1024 * 1024:
                continue
            with open(p, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        found = 0
        for m in _ROBOT_MSG_RX.finditer(text):
            for line in _html.unescape(m.group(1)).splitlines():
                if re.search(r"\b(GOVERN (OK|FAILED)|STEP\s+\d+\s+(OK|FAILED)|STEP\s+FAILED)\b", line):
                    found += 1
                    log(f"(from Robot's output.xml) {line.strip()[:1000]}")
                    _parse_step_line(line, state)
        if found:
            log("Note: the suite printed these lines with print(), which Robot Framework sends to its log - "
                "use robot.api.logger.console() so they reach the console.")
        return


def page_url_env(target_url: str, extra_urls: Optional[List[str]] = None) -> Dict[str, str]:
    """9.4: the page addresses a suite reads. TARGET_URL is where the process starts; when it visits more pages,
    TARGET_URL_<n> (1-based, TARGET_URL_1 == TARGET_URL) and TARGET_URLS (all of them, space-separated) - so the code
    never hard-codes an address. Cypress gets the same names with the CYPRESS_ prefix."""
    urls = [target_url] + [u for u in (extra_urls or []) if u and u != target_url]
    env = {"TARGET_URL": target_url, "CYPRESS_TARGET_URL": target_url, "TARGET_URLS": " ".join(urls)}
    for i, u in enumerate(urls, 1):
        env[f"TARGET_URL_{i}"] = u
        env[f"CYPRESS_TARGET_URL_{i}"] = u
    return env


def run_framework(spec: FrameworkSpec, run_dir: str, target_url: str, params: List[Dict[str, Any]],
                  log: Callable[[str], None], runtime_env: Optional[Dict[str, Any]] = None,
                  egress_env: Optional[Dict[str, str]] = None, extra_urls: Optional[List[str]] = None,
                  agent_env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Runs the generated suite with the tool's own runtime inside the sandbox engine (8.0): the local process runner
    (allow-listed environment, time limit, process-tree memory limit). The tool's setup environment (JAVA_HOME, PATH
    additions) comes from its registry entry."""
    from core.wizard import admin_policy
    from core.wizard.sandbox import SandboxUnavailable, create_runner, factory
    rt = admin_policy.tool_runtime(spec.id)
    extra: Dict[str, str] = {}
    if egress_env:                       # restricted network: all HTTP(S) goes through the per-run egress proxy
        extra.update(egress_env)
        admin_np = (rt.get("env_vars") or {}).get("NO_PROXY") or (rt.get("env_vars") or {}).get("no_proxy")
        if admin_np:                     # 8.3: the admin's NO_PROXY for this tool is added to the loopback bypass
            merged = ",".join(dict.fromkeys([h for h in (extra.get("NO_PROXY", "") + "," + admin_np).split(",") if h.strip()]))
            extra["NO_PROXY"] = extra["no_proxy"] = merged
    extra.update({"UNDERSTUDY_PAYLOAD": os.path.join(run_dir, "payload.json"),
                  "HEADLESS": "true", "STEP_TIMEOUT_MS": str(STEP_TIMEOUT_MS), "PYTHONUNBUFFERED": "1",
                  # 9.8: page labels / AI reasons with non-ASCII text must not crash a Python suite on Windows (cp1252)
                  "PYTHONIOENCODING": "utf-8"})
    extra.update(page_url_env(target_url, extra_urls))
    from core.wizard import governance_runtime as _gr
    if _gr.uses_pytest(spec):
        # 9.9.1: pytest captures stdout/stderr at the file-descriptor level and only shows it for FAILED tests. A
        # passing suite therefore printed nothing - no 'GOVERN OK', no 'STEP n OK' - and Understudy marked a good run
        # as "the script did not check the SOP rules", then mis-diagnosed it as a broken runtime. '-s' (capture=no)
        # streams every line live; PYTEST_ADDOPTS comes after the suite's own ini addopts, so it always wins.
        extra["PYTEST_ADDOPTS"] = " ".join(t for t in [os.environ.get("PYTEST_ADDOPTS", ""), "-s"] if t.strip())
    runtime_env = dict(runtime_env or {})
    strip_drivers = bool(runtime_env.pop("__strip_driver_path", None))
    # 8.2: PATH folders a runtime repair added (e.g. <JAVA_HOME>/bin, a downloaded runtime) and other internal markers
    heal_path = [p for p in str(runtime_env.pop("__path_prepend", "") or "").split(os.pathsep) if p]
    for k in [k for k in runtime_env if k.startswith("__")]:
        runtime_env.pop(k)
    if heal_path:
        log("Self-healing PATH additions: " + os.pathsep.join(heal_path))
    if runtime_env:
        extra.update({k: str(v) for k, v in runtime_env.items()})
        log("Self-healing runtime settings: " + ", ".join(f"{k}={v}" for k, v in runtime_env.items()))
    extra.update(secret_env(params))
    # 9.9: an agentic tool reaches the organisation's Gemini through this run's relay (a token, never a Google key)
    hidden = [v for k, v in (agent_env or {}).items() if k.startswith("UNDERSTUDY_SECRET_")] + \
        [v for k, v in secret_env(params).items() if v]
    if agent_env:
        extra.update(agent_env)
        log(f"Agentic run: {spec.name} asks your organisation's Gemini (model "
            f"{agent_env.get('UNDERSTUDY_AGENT_MODEL', '?')}) through Understudy - the run holds no Google key, only a "
            "token for this run's relay.")
    settings = factory.settings()
    timeout = int(settings["timeout_s"]) if settings.get("source") == "admin" else RUN_TIMEOUT_S
    try:
        runner = create_runner(spec, purpose="run", log=log, env=rt["env_vars"], path_prepend=heal_path + rt["path_prepend"],
                               workdir=run_dir, name="run")
    except SandboxUnavailable as e:
        log(f"Sandbox unavailable: {e}")
        return {"status": "FAIL", "exit_code": 126, "failure_reason": str(e), "failure_category_hint": "POLICY",
                "last_ok_step": 0}
    if strip_drivers:
        from core.wizard.runtime_repair import strip_driver_dirs
        runner.path_filter = strip_driver_dirs
        log("Self-healing: stale chromedriver/msedgedriver copies hidden from PATH so Selenium Manager fetches a matching driver.")
    state: Dict[str, Any] = {"last_ok_step": 0}
    tail: List[str] = []

    def on_line(line: str):
        line = line.rstrip()
        if hidden:
            from core.wizard.agent_relay import redact
            line = redact(line, hidden)
        if line:
            log(line[:1000])
            _parse_step_line(line, state)
            if re.search(r"(?i)(error|exception|failed|cannot|not found)", line) and not line.lstrip().startswith("#"):
                tail.append(line.strip()[:300])
    with runner:
        log(f"$ {' '.join(spec.run_command)}   (sandbox: {runner.kind}, cwd: run directory, TARGET_URL={target_url}, "
            f"limits: {timeout}s / {runner.limits.memory_mb} MB)")
        res = runner.execute_script({}, list(spec.run_command), env=extra, timeout_s=timeout, on_line=on_line,
                                    cwd=run_dir)
    code = res.exit_code
    if not state.get("stepped") and not state.get("governed") and _is_robot(spec):
        _robot_markers(run_dir, state, log)
    if code == 127 and res.output.startswith("Could not start"):
        log(f"Runner not found: {res.output}")
        return {"status": "FAIL", "exit_code": 127, "failure_reason": res.output.splitlines()[0],
                "failure_category_hint": "ENVIRONMENT", **state}
    failed = state.get("failed_step") or state.get("step_failed_unnumbered")
    result = {"status": "PASS" if res.ok and not failed else "FAIL", "exit_code": code,
              "sandbox": {"kind": runner.kind, "peak_memory_mb": res.peak_memory_mb, "timed_out": res.timed_out,
                          "memory_exceeded": res.memory_exceeded}, **state}
    if os.path.exists(os.path.join(run_dir, "governance.json")):     # 9.9: Govern is part of the script
        if state.get("governance_failed"):
            result.update(status="FAIL", failed_step=0, failure_category_hint="POLICY", governance_violation=True,
                          failure_reason="The payload breaks the SOP rules: " + state["governance_failed"])
            return result
        if result["status"] == "PASS" and not state.get("governed"):
            result.update(status="FAIL", failure_reason="The script did not check the SOP rules before step 1 (no "
                          "'GOVERN OK' line) - Understudy's Govern step is part of every script.",
                          governance_missing=True)
            return result
    if result["status"] == "FAIL" and not state.get("last_ok_step") and not failed:
        # The program died before it could run step 1 (no STEP line at all): the framework runtime itself - browser,
        # driver, interpreter or package - could not start. Self-healing should switch runtimes, not patch selectors.
        result["failure_category_hint"] = "ENVIRONMENT"
    if result["status"] == "FAIL" and not result.get("failure_reason"):
        if res.memory_exceeded:
            result["failure_reason"] = f"Killed: the run used more than {runner.limits.memory_mb} MB of memory"
            result["failure_category_hint"] = "ENVIRONMENT"
        elif res.timed_out:
            result["failure_reason"] = "Timed out"
        else:
            result["failure_reason"] = (
                (sorted(tail, key=lambda t: 0 if re.search(r"\b\w*(Error|Exception)\b\s*:", t) else 1)[0] if tail
                 else f"Process exited with code {code}") +
                ("" if state.get("last_ok_step") else " (the automation could not start)"))
    return result


# ---------------------------------------------------------------------------
# Reference driver (built-in Playwright interpreter of the step plan)
# ---------------------------------------------------------------------------
def _locate(page, step: Dict[str, Any]):
    sels = [s for s in [step.get("selector")] + list(step.get("fallback_selectors") or []) if s]
    for sel in sels:
        try:
            loc = page.locator(sel)
            if loc.count() > 0:
                return loc.first, sel
        except Exception:
            continue
    label = step.get("label")
    if label:
        for how, loc in (("label", page.get_by_label(label)), ("placeholder", page.get_by_placeholder(label)),
                         ("button", page.get_by_role("button", name=label)), ("text", page.get_by_text(label, exact=True))):
            try:
                if loc.count() > 0:
                    return loc.first, f"{how}={label}"
            except Exception:
                continue
    raise RuntimeError(f"Element not found (selectors={sels}, label={label!r})")


def run_reference(steps: List[Dict[str, Any]], target_url: str, params: List[Dict[str, Any]],
                  log: Callable[[str], None], screenshot_path: Optional[str] = None,
                  egress: Optional[Any] = None) -> Dict[str, Any]:
    from core.wizard.dom_discovery import install_ssrf_guard
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        return {"status": "FAIL", "exit_code": 127, "failure_reason": f"Playwright is not installed on this server ({e})",
                "failure_category_hint": "ENVIRONMENT", "last_ok_step": 0}
    values = {p["key"]: p.get("value") for p in params}
    state: Dict[str, Any] = {"last_ok_step": 0}
    post_click_ms = int(os.environ.get("POST_CLICK_WAIT_MS", "1500"))
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            install_ssrf_guard(page)
            if egress is not None:           # every request of the built-in driver goes through the run's egress policy
                from urllib.parse import urlsplit as _us

                def _egress_route(route):
                    u = route.request.url
                    if u.startswith(("data:", "blob:", "about:")):
                        return route.continue_()
                    ok, _ip = egress.check((_us(u).hostname or "").lower())
                    return route.continue_() if ok else route.abort()
                page.route("**/*", _egress_route)
            page.set_default_timeout(STEP_TIMEOUT_MS)
            current = 0
            try:
                for st in sorted(steps, key=lambda s: s["order"]):
                    current = st["order"]
                    a = st["action"]
                    used = ""
                    if a == "navigate":
                        v = st.get("value") or ""
                        url = v if v.startswith("http") else (urljoin(target_url, v) if v else target_url)
                        page.goto(url, wait_until="domcontentloaded")
                    elif a in ("fill", "select", "check", "click"):
                        el, used = _locate(page, st)
                        val = values.get(st.get("param_key"))
                        if a == "fill":
                            el.fill("" if val is None else str(val))
                        elif a == "select":
                            try:
                                el.select_option(value=str(val))
                            except Exception:
                                el.select_option(label=str(val))
                        elif a == "check":
                            el.check() if str(val).strip().lower() in ("1", "true", "yes", "on") else el.uncheck()
                        else:
                            el.click()
                            try:
                                page.wait_for_load_state("networkidle", timeout=post_click_ms)
                            except Exception:
                                pass
                            page.wait_for_timeout(post_click_ms)
                    elif a == "press":
                        page.keyboard.press(st.get("value") or "Enter")
                    elif a == "wait":
                        page.wait_for_timeout(max(0, min(10000, int(float(st.get("value") or 1000)))))
                    elif a == "assert_text":
                        txt = str(values.get(st.get("param_key")) if st.get("param_key") else st.get("value"))
                        page.get_by_text(txt).first.wait_for(timeout=STEP_TIMEOUT_MS)
                    line = f"STEP {current} OK: {st.get('description') or a}" + (f"  [{used}]" if used and used != st.get("selector") else "")
                    log(line)
                    state["last_ok_step"] = current
                    if used and used != st.get("selector"):
                        state.setdefault("runtime_healed", []).append({"step": current, "used": used})
            except Exception as exc:
                msg = str(exc).splitlines()[0][:400]
                log(f"STEP {current} FAILED: {msg}")
                state.update({"failed_step": current, "failure_reason": msg})
                try:
                    state["failure_html"] = page.content()[:400_000]
                    state["failure_url"] = page.url
                except Exception:
                    pass
            if screenshot_path:
                try:
                    page.screenshot(path=screenshot_path, full_page=True)
                except Exception:
                    pass
            browser.close()
    except Exception as e:
        msg = str(e).splitlines()[0][:400]
        log(f"Browser could not start: {msg}")
        return {"status": "FAIL", "exit_code": 1, "failure_reason": msg, "failure_category_hint": "ENVIRONMENT", **state}
    ok = not state.get("failed_step")
    if ok:
        log("AUTOMATION COMPLETE")
    return {"status": "PASS" if ok else "FAIL", "exit_code": 0 if ok else 1, **state}


USER_TOOL_BLOCKED = ("TOOLCHAIN_MISSING", "FAILED", "AWAITING_APPROVAL")


def choose_strategy(dep_status: Optional[str], override: Optional[str]) -> str:
    if override in ("reference", "framework"):
        return override
    return "framework" if dep_status == "READY" else "reference"


def execute(spec: FrameworkSpec, files: Dict[str, str], steps: List[Dict[str, Any]], params: List[Dict[str, Any]],
            target_url: str, run_dir: str, dep_status: Optional[str], log: Callable[[str], None],
            strategy_override: Optional[str] = None, extra_files: Optional[Dict[str, str]] = None,
            runtime_env: Optional[Dict[str, Any]] = None, dep_message: str = "",
            extra_urls: Optional[List[str]] = None, agent_env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    started = time.time()
    extra_urls = [u for u in (extra_urls or []) if u and u != target_url]
    write_run_dir(run_dir, files, params, extra_files)
    log(f"Injected {len([p for p in params if p.get('type') != 'secret'])} payload value(s) into payload.json"
        f" and {len([p for p in params if p.get('type') == 'secret'])} secret(s) as environment variables.")
    if strategy_override is None and dep_status in USER_TOOL_BLOCKED:
        # Never silently swap the user's tool for another one: report it, and let self-healing repair the runtime
        # (reinstall) or the user explicitly choose the built-in driver.
        reason = {"TOOLCHAIN_MISSING": f"{spec.name} can't run on this machine: {dep_message}",
                  "FAILED": f"{spec.name}'s dependencies failed to install: {dep_message}",
                  "AWAITING_APPROVAL": f"{spec.name}'s packages are still waiting for admin approval"}[dep_status]
        log(f"STEP 0 FAILED: {reason}")
        return {"status": "FAIL", "exit_code": 1, "last_ok_step": 0, "failure_reason": reason[:500],
                "failure_category_hint": "ENVIRONMENT", "runtime_cause": dep_status, "strategy": "framework",
                "duration_ms": round((time.time() - started) * 1000, 1), "finished_at": datetime.utcnow().isoformat()}
    strategy = choose_strategy(dep_status, strategy_override)
    from core.wizard import egress as _egress
    blocked = next((b for b in (_egress_precheck(u) for u in [target_url] + extra_urls) if b), "")
    if blocked:
        log(f"STEP 0 FAILED: {blocked}")
        return {"status": "FAIL", "exit_code": 1, "last_ok_step": 0, "failure_reason": blocked[:500],
                "failure_category_hint": "POLICY", "strategy": strategy,
                "duration_ms": round((time.time() - started) * 1000, 1), "finished_at": datetime.utcnow().isoformat()}
    proxy = None
    if os.environ.get("UNDERSTUDY_EGRESS", "on").lower() not in ("off", "0", "false"):
        proxy = _egress.EgressProxy(_egress.run_allow_list(target_url, extra_urls), log=log).start()
        log(f"Restricted network ({proxy.mode}): this run may reach {', '.join(proxy.allow) or 'nothing'}"
            + (" and any public host" if proxy.mode == "public" else "")
            + "; blocked-list hosts, cloud-metadata and private ranges are denied.")
    relay = None
    if agent_env is not None and strategy == "framework":   # 9.9: the agent's Gemini calls go through this run's relay
        from core.wizard import agent_relay
        relay = agent_relay.Relay(log, model_name=agent_env.get(agent_relay.MODEL_ENV)).start()
        agent_env = dict(agent_env, **relay.env())
    try:
        result = _run_strategy(strategy, spec, steps, params, target_url, run_dir, dep_status, strategy_override,
                               runtime_env, log, proxy, extra_urls, agent_env)
    finally:
        if proxy:
            proxy.stop()
        if relay:
            relay.stop()
    if relay:
        result["agent_ai"] = relay.summary()
        log(f"Agent used {relay.calls} Gemini call(s) through Understudy ({relay.summary()['model']})"
            + (f", {relay.errors} failed" if relay.errors else "") + ".")
    if proxy:
        result["egress"] = proxy.summary()
        if proxy.denied:
            log(f"Network policy denied {len(proxy.denied)} request(s) to: "
                + ", ".join(sorted({d['host'] for d in proxy.denied})))
    result["strategy"] = strategy
    result["duration_ms"] = round((time.time() - started) * 1000, 1)
    result["finished_at"] = datetime.utcnow().isoformat()
    return result


def _egress_precheck(target_url: str) -> str:
    """Policy re-check right before the run: the admin may have changed the lists since Step 1."""
    from core.wizard.guardrails import screen_url
    v = screen_url(target_url)
    return "" if v.is_safe else "Target blocked by policy: " + "; ".join(v.reasons)


def _run_strategy(strategy, spec, steps, params, target_url, run_dir, dep_status, strategy_override, runtime_env, log,
                  proxy, extra_urls=None, agent_env=None):
    if strategy == "framework":
        log(f"Running the generated {spec.name} suite with its own runtime.")
        result = run_framework(spec, run_dir, target_url, params, log, runtime_env, proxy.env() if proxy else None,
                               extra_urls, agent_env=agent_env)
    else:
        reason = {"TOOLCHAIN_MISSING": "its toolchain is not installed on this server",
                  "AWAITING_APPROVAL": "its packages are waiting for your approval",
                  "DRY_RUN": "dependency installation is in dry-run mode",
                  "SKIPPED": "dependency installation is turned off",
                  "FAILED": "its dependency installation failed",
                  "INSTALLING": "its dependencies are still installing",
                  "PENDING": "its dependencies are still installing"}.get(dep_status or "", "self-healing selected it")
        if strategy_override == "reference":
            reason = "you chose to verify with the built-in driver after the tool's runtime could not be repaired"
        log(f"Running the same step plan with Understudy's built-in Playwright driver because {reason}. "
            f"Your {spec.name} code is still included in the ZIP.")
        result = run_reference(steps, target_url, params, log, os.path.join(run_dir, "final_state.png"), proxy)
    return result
