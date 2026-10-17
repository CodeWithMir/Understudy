"""Run reports for the exported ZIP (9.9.1).

Every time a user runs the downloaded automation (``bash run.sh`` or ``run.bat``), the launcher keeps a report of
that run - whatever tool the package uses - in ``results/<run id>/``:

    run.log                   the console output, with a time offset per line and secret values masked
    understudy-report.json    machine-readable summary (status, SOP check, every step, the failure, native reports)
    understudy-report.html    the same summary as a self-contained page (no external files, works offline)
    junit.xml                 pytest suites only (``--junitxml`` through ``PYTEST_ADDOPTS``)

``results/latest.html`` always opens the newest report.

The summary comes from the lines every Understudy script prints (``GOVERN OK`` / ``GOVERN FAILED: why``,
``STEP <n> OK: what`` / ``STEP <n> FAILED: why``) plus the exit code, so it works the same for Python, Node, Java,
Robot Framework or any other tool. Reports the tool writes itself (Maven Surefire, Gradle, Robot Framework, Playwright,
pytest JUnit XML) are linked from the page.

No extra dependency on the user's machine: macOS / Linux use bash + POSIX awk (``reporting/understudy_report.awk``,
tested with mawk, BWK awk and gawk); Windows uses PowerShell (``run.ps1``, which ``run.bat`` starts).
"""
from __future__ import annotations

import shlex
from typing import Dict, List

REPORT_DIR = "reporting"
AWK_FILE = f"{REPORT_DIR}/understudy_report.awk"
TEMPLATE_FILE = f"{REPORT_DIR}/report_template.html"
REPORT_VERSION = 1

# Reports a tool writes on its own, relative to the package folder (linked from the page when they exist).
NATIVE_REPORTS = [
    ("Maven Surefire / TestNG report", "target/surefire-reports/index.html"),
    ("Maven Surefire summary", "target/surefire-reports/emailable-report.html"),
    ("Gradle test report", "build/reports/tests/test/index.html"),
    ("Robot Framework report", "results/report.html"),
    ("Robot Framework log", "results/log.html"),
    ("Robot Framework report", "report.html"),
    ("Robot Framework log", "log.html"),
    ("Playwright HTML report", "playwright-report/index.html"),
    ("Mochawesome report", "mochawesome-report/mochawesome.html"),
    ("Final page screenshot (agentic run)", "final_state.png"),
]


# ---------------------------------------------------------------------------------------------------------------------
# The HTML page: one template for both launchers. The launcher replaces the line @@JSON@@ with the report JSON
# (every '<' escaped as <, so the data can never close the <script> element). All text is inserted with
# textContent - nothing from the log is ever interpreted as HTML.
# ---------------------------------------------------------------------------------------------------------------------
TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Automation run report</title>
<style>
:root{--bg:#f7f7f5;--card:#fff;--ink:#1d1d1b;--mute:#66665f;--line:#e3e2dc;--ok:#1f7a4d;--okbg:#e6f4ec;--bad:#b42318;--badbg:#fdecea;--warn:#8a5a00;--warnbg:#fff4dc;--code:#f1f0ec}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--card:#1e1e1c;--ink:#ecebe6;--mute:#a3a29b;--line:#34332f;--ok:#5fd39a;--okbg:#17321f;--bad:#ff8a80;--badbg:#3a1714;--warn:#f2c46d;--warnbg:#3a2c10;--code:#262624}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:1000px;margin:0 auto;padding:24px 16px 48px}h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:0 0 12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin-top:16px}
.badge{display:inline-block;font-weight:700;font-size:13px;padding:2px 10px;border-radius:999px}
.PASS,.OK,.ok{color:var(--ok);background:var(--okbg)}.FAIL,.FAILED,.failed{color:var(--bad);background:var(--badbg)}.warn,.not{color:var(--warn);background:var(--warnbg)}
.head{display:flex;flex-wrap:wrap;align-items:center;gap:12px}.sub{color:var(--mute);font-size:13px}
dl{display:grid;grid-template-columns:max-content 1fr;gap:4px 16px;margin:0}dt{color:var(--mute)}dd{margin:0;overflow-wrap:anywhere}
table{width:100%;border-collapse:collapse;font-size:14px}th,td{text-align:left;padding:6px 8px;border-top:1px solid var(--line);vertical-align:top}
th{color:var(--mute);font-weight:600;border-top:0}td.num{white-space:nowrap;font-variant-numeric:tabular-nums}
.fail-box{border-color:var(--bad)}.fail-box p{margin:4px 0}code,pre{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
pre{background:var(--code);border-radius:8px;padding:12px;font-size:12.5px;overflow:auto;max-height:520px;margin:0;white-space:pre-wrap;overflow-wrap:anywhere}
pre .hl-ok{color:var(--ok);font-weight:600}pre .hl-bad{color:var(--bad);font-weight:600}
ul{margin:0;padding-left:20px}a{color:inherit}.empty{color:var(--mute)}
label{font-size:13px;color:var(--mute)}input[type=search]{width:100%;max-width:320px;padding:6px 8px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--ink);margin-bottom:8px}
@media print{pre{max-height:none}.noprint{display:none}}
</style>
</head>
<body>
<main>
  <div class="head"><h1 id="title">Automation run report</h1><span id="status" class="badge"></span></div>
  <div class="sub" id="subtitle"></div>
  <section class="card fail-box" id="failure" hidden><h2>What failed</h2><p id="failure-step"></p><p id="failure-reason"></p></section>
  <section class="card"><h2>Run</h2><dl id="meta"></dl></section>
  <section class="card"><h2>SOP rules check</h2><p id="gov"></p></section>
  <section class="card"><h2>Steps</h2><table><thead><tr><th>#</th><th>Result</th><th>Step</th><th>At</th><th>Took</th></tr></thead><tbody id="steps"></tbody></table><p id="steps-empty" class="empty" hidden>No STEP lines were printed.</p></section>
  <section class="card" id="warnings-card" hidden><h2>Warnings</h2><ul id="warnings"></ul></section>
  <section class="card" id="native-card" hidden><h2>Reports written by the tool</h2><ul id="native"></ul></section>
  <section class="card" id="payload-card" hidden><h2>Data used (payload.json)</h2><table><tbody id="payload"></tbody></table></section>
  <section class="card"><h2>Full log</h2><div class="noprint"><label for="filter">Filter lines</label><br><input id="filter" type="search" placeholder="e.g. STEP or Error"></div><pre id="log"></pre></section>
</main>
<script type="application/json" id="report-data">
@@JSON@@
</script>
<script>
(function () {
  var r = JSON.parse(document.getElementById("report-data").textContent);
  function el(tag, text, cls) { var e = document.createElement(tag); if (text !== undefined && text !== null) e.textContent = String(text); if (cls) e.className = cls; return e; }
  function secs(s) { s = Math.max(0, Math.round(Number(s) || 0)); return s < 60 ? s + " s" : Math.floor(s / 60) + " min " + (s % 60) + " s"; }
  document.title = (r.status || "") + " - " + (r.tool || "Automation") + " run " + (r.run_id || "");
  document.getElementById("title").textContent = (r.tool || "Automation") + " run";
  var st = document.getElementById("status"); st.textContent = r.status || "?"; st.classList.add(r.status === "PASS" ? "PASS" : "FAIL");
  document.getElementById("subtitle").textContent = "Run " + (r.run_id || "") + " - " + (r.started_at || "") + " - generated by " + (r.generator || "Understudy");
  var meta = document.getElementById("meta");
  [["Command", r.command], ["Started", r.started_at], ["Finished", r.finished_at], ["Duration", secs(r.duration_s)],
   ["Exit code", r.exit_code], ["Target", r.target_url]].forEach(function (p) {
    if (p[1] === undefined || p[1] === null || p[1] === "") return; meta.appendChild(el("dt", p[0])); meta.appendChild(el("dd", p[1])); });
  var g = r.governance || {}, gov = document.getElementById("gov");
  var gtxt = { ok: "Passed", failed: "Failed - the run stopped before step 1", "not reported": "Not reported" }[g.status] || g.status;
  gov.appendChild(el("span", gtxt, "badge " + (g.status === "ok" ? "ok" : g.status === "failed" ? "failed" : "not")));
  if (g.message) gov.appendChild(el("span", " " + g.message));
  var tb = document.getElementById("steps"), steps = r.steps || [];
  if (!steps.length) document.getElementById("steps-empty").hidden = false;
  steps.forEach(function (s) { var tr = document.createElement("tr");
    tr.appendChild(el("td", s.n, "num")); var c = el("td"); c.appendChild(el("span", s.status, "badge " + s.status)); tr.appendChild(c);
    tr.appendChild(el("td", s.message)); tr.appendChild(el("td", "+" + secs(s.at_s), "num")); tr.appendChild(el("td", secs(s.took_s), "num")); tb.appendChild(tr); });
  if (r.failure) { document.getElementById("failure").hidden = false;
    document.getElementById("failure-step").textContent = r.failure.step ? "Step " + r.failure.step : (r.governance && r.governance.status === "failed" ? "The SOP rules check (before step 1)" : "The run (exit code " + r.exit_code + ")");
    document.getElementById("failure-reason").textContent = r.failure.reason || ""; }
  function list(cardId, listId, items, render) { if (!items || !items.length) return; document.getElementById(cardId).hidden = false; var ul = document.getElementById(listId); items.forEach(function (i) { ul.appendChild(render(i)); }); }
  list("warnings-card", "warnings", r.warnings, function (w) { return el("li", w); });
  list("native-card", "native", r.native_reports, function (n) { var li = el("li"), a = el("a", n.name); a.href = n.href; li.appendChild(a); li.appendChild(el("span", " - " + n.path, "sub")); return li; });
  try { var p = r.payload_text ? JSON.parse(r.payload_text) : null;
    if (p && typeof p === "object") { var keys = Object.keys(p); if (keys.length) { document.getElementById("payload-card").hidden = false; var pb = document.getElementById("payload");
      keys.forEach(function (k) { var tr = document.createElement("tr"); tr.appendChild(el("th", k)); var v = p[k]; tr.appendChild(el("td", typeof v === "object" ? JSON.stringify(v) : v)); pb.appendChild(tr); }); } } } catch (e) {}
  var pre = document.getElementById("log"), lines = r.log || [];
  function draw(q) { pre.textContent = ""; q = (q || "").toLowerCase(); var shown = 0;
    lines.forEach(function (l) { var line = "[+" + secs(l.t).replace(" ", "") + "] " + l.text; if (q && line.toLowerCase().indexOf(q) < 0) return; shown++;
      var cls = /\b(STEP\s+\d+\s+FAILED|STEP\s+FAILED|GOVERN FAILED)\b/.test(l.text) ? "hl-bad" : /\b(STEP\s+\d+\s+OK|GOVERN OK)\b/.test(l.text) ? "hl-ok" : "";
      pre.appendChild(el("span", line + "\n", cls)); });
    if (!shown) pre.appendChild(el("span", lines.length ? "No line matches." : "The run printed nothing.", "empty")); }
  draw(""); document.getElementById("filter").addEventListener("input", function (e) { draw(e.target.value); });
})();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------------------------------------------------
# macOS / Linux: POSIX awk (no gawk extensions: no strftime, no match() arrays, no gensub). Input: the raw capture,
# one "<epoch seconds>\t<line>" per console line. Writes run.log, understudy-report.json and understudy-report.html.
# ---------------------------------------------------------------------------------------------------------------------
AWK = r"""# Generated by Understudy - turns the captured console output of one run into its report.
# Usage (run.sh does this): awk -v start=EPOCH -v end=EPOCH -v code=EXIT -v outdir=DIR -v tpl=TEMPLATE ... -f this RAW
function jesc(s,    i, c, o, n) {
    o = ""; n = length(s)
    for (i = 1; i <= n; i++) {
        c = substr(s, i, 1)
        if (c == "\\") o = o "\\\\"
        else if (c == "\"") o = o "\\\""
        else if (c == "<") o = o "\\u003c"
        else if (c == "\t") o = o "\\t"
        else if (index(CTRL, c) > 0) o = o " "
        else o = o c
    }
    return "\"" o "\""
}
function mask(s, secret,    p, out) {
    out = ""
    while ((p = index(s, secret)) > 0) { out = out substr(s, 1, p - 1) "******"; s = substr(s, p + length(secret)) }
    return out s
}
function exists(path,    line, r) { r = (getline line < path); close(path); return r >= 0 }
function trimc(s) { sub(/^[ \t]*:?[ \t]*/, "", s); sub(/[ \t\r]+$/, "", s); return s }
BEGIN {
    FS = "\t"; CTRL = ""
    for (i = 1; i < 32; i++) if (i != 9) CTRL = CTRL sprintf("%c", i)
    CTRL = CTRL sprintf("%c", 127)
    ns = 0
    for (k in ENVIRON) if (k ~ /^UNDERSTUDY_SECRET_/ && length(ENVIRON[k]) >= 3) secrets[++ns] = ENVIRON[k]
    logfile = outdir "/run.log"; printf "" > logfile
    nl = 0; nsteps = 0; govern = "not reported"; gmsg = ""; gat = 0; failed = 0; unnum = 0; freason = ""; lasterr = ""
}
{
    t = $1 + 0; text = substr($0, length($1) + 2)
    sub(/\r$/, "", text)
    for (i = 1; i <= ns; i++) text = mask(text, secrets[i])
    el = t - start; if (el < 0) el = 0
    print "[+" el "s] " text > logfile
    logt[++nl] = el; logx[nl] = text
    if (match(text, /GOVERN FAILED/)) { govern = "failed"; gmsg = trimc(substr(text, RSTART + RLENGTH)); gat = el }
    else if (match(text, /GOVERN OK/) && govern != "failed") { govern = "ok"; gmsg = trimc(substr(text, RSTART + RLENGTH)); gat = el }
    if (match(text, /STEP[ \t]+[0-9]+[ \t]+(OK|FAILED)/)) {
        tok = substr(text, RSTART, RLENGTH); rest = trimc(substr(text, RSTART + RLENGTH))
        sub(/^STEP[ \t]+/, "", tok); n = tok; sub(/[ \t].*$/, "", n); stt = tok; sub(/^[0-9]+[ \t]+/, "", stt); n = n + 0
        if (!(n in sstat)) order[++nsteps] = n
        sstat[n] = stt; smsg[n] = rest; sat[n] = el
        if (stt == "FAILED") { failed = n; freason = rest }
    } else if (match(text, /STEP[ \t]+FAILED/)) {
        unnum = 1; if (freason == "") freason = trimc(substr(text, RSTART + RLENGTH))
    }
    if (text ~ /(Error|Exception|FAILED|[Ff]ailed|ERROR)/ && text !~ /^[ \t]+at /) lasterr = text
}
END {
    close(logfile)
    code = code + 0
    # steps in number order
    for (i = 2; i <= nsteps; i++) { v = order[i]; j = i - 1; while (j > 0 && order[j] > v) { order[j + 1] = order[j]; j-- } order[j + 1] = v }
    rules = exists("governance.json")
    status = (code == 0 && !failed && !unnum && govern != "failed") ? "PASS" : "FAIL"
    nw = 0
    if (rules && govern == "not reported") warn[++nw] = "governance.json is in the package but the run printed no GOVERN line - the SOP rules may not have been checked."
    if (code == 0 && (failed || unnum)) warn[++nw] = "The process exited with code 0 although a step reported FAILED."
    if (code != 0 && !failed && !unnum && govern != "failed" && nsteps > 0) warn[++nw] = "Every reported step passed, but the process exited with code " code "."
    if (nl == 0) warn[++nw] = "The run printed nothing."
    js = "{\n  \"report_version\": " version ",\n  \"generator\": " jesc(generator) ",\n  \"tool\": " jesc(tool) ",\n"
    js = js "  \"command\": " jesc(command) ",\n  \"run_id\": " jesc(run_id) ",\n  \"target_url\": " jesc(ENVIRON["TARGET_URL"]) ",\n"
    js = js "  \"started_at\": " jesc(started_at) ",\n  \"finished_at\": " jesc(finished_at) ",\n"
    js = js "  \"duration_s\": " (end - start) ",\n  \"exit_code\": " code ",\n  \"status\": " jesc(status) ",\n"
    js = js "  \"governance\": {\"status\": " jesc(govern) ", \"message\": " jesc(gmsg) ", \"rules_file_present\": " (rules ? "true" : "false") "},\n"
    js = js "  \"steps\": ["
    prev = gat
    for (i = 1; i <= nsteps; i++) {
        n = order[i]; took = sat[n] - prev; if (took < 0) took = 0; prev = sat[n]
        js = js (i > 1 ? "," : "") "\n    {\"n\": " n ", \"status\": " jesc(sstat[n]) ", \"message\": " jesc(smsg[n]) ", \"at_s\": " sat[n] ", \"took_s\": " took "}"
    }
    js = js (nsteps ? "\n  " : "") "],\n"
    if (status == "FAIL") {
        reason = freason
        if (reason == "" && govern == "failed") reason = gmsg
        if (reason == "") reason = (lasterr != "" ? lasterr : "The process exited with code " code)
        js = js "  \"failure\": {\"step\": " (failed ? failed : "null") ", \"reason\": " jesc(reason) "},\n"
    } else js = js "  \"failure\": null,\n"
    js = js "  \"warnings\": ["
    for (i = 1; i <= nw; i++) js = js (i > 1 ? ", " : "") jesc(warn[i])
    js = js "],\n  \"native_reports\": ["
    nn = 0
    nr = split(native, items, "|")
    for (i = 1; i + 1 <= nr; i += 2) {
        p = items[i + 1]
        if (exists(p)) js = js (nn++ ? "," : "") "\n    {\"name\": " jesc(items[i]) ", \"path\": " jesc(p) ", \"href\": " jesc("../../" p) "}"
    }
    if (exists(outdir "/junit.xml")) js = js (nn++ ? "," : "") "\n    {\"name\": \"JUnit XML (pytest)\", \"path\": " jesc(outdir "/junit.xml") ", \"href\": \"junit.xml\"}"
    js = js (nn ? "\n  " : "") "],\n"
    ptxt = ""; pf = ENVIRON["UNDERSTUDY_PAYLOAD"]; if (pf == "") pf = "payload.json"
    while ((getline line < pf) > 0) { for (i = 1; i <= ns; i++) line = mask(line, secrets[i]); ptxt = ptxt line "\n" }
    close(pf)
    js = js "  \"payload_text\": " jesc(ptxt) ",\n  \"log\": ["
    for (i = 1; i <= nl; i++) js = js (i > 1 ? "," : "") "\n    {\"t\": " logt[i] ", \"text\": " jesc(logx[i]) "}"
    js = js (nl ? "\n  " : "") "]\n}\n"
    jf = outdir "/understudy-report.json"; printf "%s", js > jf; close(jf)
    hf = outdir "/understudy-report.html"; printf "" > hf
    while ((getline line < tpl) > 0) { if (line == "@@JSON@@") printf "%s", js > hf; else print line > hf }
    close(tpl); close(hf)
    print status > (outdir "/.status"); close(outdir "/.status")
}
"""


def _native_arg() -> str:
    return "|".join(f"{name}|{path}" for name, path in NATIVE_REPORTS)


def bash_script(install: List[str], run: str, tool: str, generator: str) -> str:
    q = shlex.quote
    lines = [
        "#!/usr/bin/env bash",
        "# Generated by Understudy - installs the dependencies, runs the automation and writes a report of the run to",
        "# results/<run id>/ (understudy-report.html, understudy-report.json, run.log). results/latest.html = newest.",
        "set -euo pipefail",
        'cd "$(dirname "$0")"',
        'if [ -f .env ]; then set -a; . ./.env; set +a; fi',
        ': "${TARGET_URL:?Set TARGET_URL in .env (see .env.example)}"',
        'export UNDERSTUDY_PAYLOAD="${UNDERSTUDY_PAYLOAD:-payload.json}"',
        "export PYTHONUNBUFFERED=1 PYTHONIOENCODING=utf-8",
        "# --- install ---",
        *install,
        "# --- run, with a report ---",
        'RUN_ID="$(date +%Y%m%d-%H%M%S)"',
        'OUT="results/$RUN_ID"',
        'mkdir -p "$OUT"',
        'export UNDERSTUDY_RESULTS_DIR="$OUT"',
        "# pytest suites: show every line (-s) and write JUnit XML; other tools ignore this variable",
        'export PYTEST_ADDOPTS="${PYTEST_ADDOPTS:-} -s --junitxml=$OUT/junit.xml"',
        'STARTED_AT="$(date \'+%Y-%m-%dT%H:%M:%S%z\')"',
        'START="$(date +%s)"',
        f'echo "== Running: {run}"',
        "set +e",
        f'{run} 2>&1 | while IFS= read -r line || [ -n "$line" ]; do',
        "  printf '%s\\n' \"$line\"",
        "  printf '%s\\t%s\\n' \"$(date +%s)\" \"$line\" >> \"$OUT/.raw.log\"",
        "done",
        "CODE=${PIPESTATUS[0]}",
        "set -e",
        'END="$(date +%s)"',
        'touch "$OUT/.raw.log"',
        f'awk -v start="$START" -v end="$END" -v code="$CODE" -v outdir="$OUT" -v tpl={TEMPLATE_FILE} \\',
        f'    -v run_id="$RUN_ID" -v started_at="$STARTED_AT" -v finished_at="$(date \'+%Y-%m-%dT%H:%M:%S%z\')" \\',
        f'    -v tool={q(tool)} -v command={q(run)} -v generator={q(generator)} -v version={REPORT_VERSION} \\',
        f'    -v native={q(_native_arg())} -f {AWK_FILE} "$OUT/.raw.log"',
        'rm -f "$OUT/.raw.log"',
        'printf \'<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="0; url=%s/understudy-report.html">'
        '<a href="%s/understudy-report.html">Open the latest report</a>\\n\' "$RUN_ID" "$RUN_ID" > results/latest.html',
        'echo "== $(cat "$OUT/.status") - report: $OUT/understudy-report.html (exit code $CODE)"',
        'rm -f "$OUT/.status"',
        'exit "$CODE"',
    ]
    return "\n".join(lines) + "\n"


def _ps_str(s: str) -> str:
    return "'" + str(s).replace("'", "''") + "'"


PS_REPORT = r"""
function Write-Utf8([string]$Path, [string]$Text) { [IO.File]::WriteAllText($Path, $Text, (New-Object Text.UTF8Encoding $false)) }
function Mask([string]$s) { foreach ($v in $Secrets) { $s = $s.Replace($v, '******') }; return $s }
function Clean([string]$s) { return ($s -replace '^[ \t]*:?[ \t]*', '') -replace '[ \t\r]+$', '' }
$Secrets = @(Get-ChildItem env: | Where-Object { $_.Name -like 'UNDERSTUDY_SECRET_*' -and $_.Value.Length -ge 3 } | ForEach-Object { $_.Value })
$govern = 'not reported'; $gmsg = ''; $gat = 0; $steps = [ordered]@{}; $failed = $null; $unnum = $false; $freason = ''; $lastErr = ''
$logOut = New-Object System.Collections.Generic.List[string]; $logJson = New-Object System.Collections.Generic.List[object]
foreach ($l in $Captured) {
  $text = (Mask $l.text) -replace '[\x00-\x08\x0A-\x1F\x7F]', ' '; $t = [int][Math]::Floor($l.t)
  $logOut.Add("[+${t}s] $text"); $logJson.Add([ordered]@{ t = $t; text = $text })
  if ($text -match 'GOVERN FAILED(.*)$') { $govern = 'failed'; $gmsg = Clean $matches[1]; $gat = $t }
  elseif ($text -match 'GOVERN OK(.*)$' -and $govern -ne 'failed') { $govern = 'ok'; $gmsg = Clean $matches[1]; $gat = $t }
  if ($text -cmatch 'STEP[ \t]+([0-9]+)[ \t]+(OK|FAILED)(.*)$') {
    $n = [int]$matches[1]; $steps["$n"] = [ordered]@{ n = $n; status = $matches[2]; message = (Clean $matches[3]); at_s = $t; took_s = 0 }
    if ($matches[2] -eq 'FAILED') { $failed = $n; $freason = Clean $matches[3] }
  } elseif ($text -cmatch 'STEP[ \t]+FAILED(.*)$') { $unnum = $true; if (-not $freason) { $freason = Clean $matches[1] } }
  if ($text -cmatch '(Error|Exception|FAILED|[Ff]ailed|ERROR)' -and $text -notmatch '^[ \t]+at ') { $lastErr = $text }
}
$ordered = @($steps.Values | Sort-Object { $_.n }); $prev = $gat
foreach ($s in $ordered) { $s.took_s = [Math]::Max(0, $s.at_s - $prev); $prev = $s.at_s }
$rules = Test-Path 'governance.json'
$status = if ($Code -eq 0 -and $null -eq $failed -and -not $unnum -and $govern -ne 'failed') { 'PASS' } else { 'FAIL' }
$warnings = @()
if ($rules -and $govern -eq 'not reported') { $warnings += 'governance.json is in the package but the run printed no GOVERN line - the SOP rules may not have been checked.' }
if ($Code -eq 0 -and ($null -ne $failed -or $unnum)) { $warnings += 'The process exited with code 0 although a step reported FAILED.' }
if ($Code -ne 0 -and $null -eq $failed -and -not $unnum -and $govern -ne 'failed' -and $ordered.Count -gt 0) { $warnings += "Every reported step passed, but the process exited with code $Code." }
if ($Captured.Count -eq 0) { $warnings += 'The run printed nothing.' }
$failure = $null
if ($status -eq 'FAIL') {
  $reason = $freason; if (-not $reason -and $govern -eq 'failed') { $reason = $gmsg }
  if (-not $reason) { $reason = if ($lastErr) { $lastErr } else { "The process exited with code $Code" } }
  $failure = [ordered]@{ step = $failed; reason = $reason }
}
$native = @()
foreach ($p in $NativeReports) { if (Test-Path -LiteralPath $p[1] -PathType Leaf) { $native += [ordered]@{ name = $p[0]; path = $p[1]; href = '../../' + $p[1] } } }
if (Test-Path -LiteralPath "$Out\junit.xml") { $native += [ordered]@{ name = 'JUnit XML (pytest)'; path = "$Out\junit.xml"; href = 'junit.xml' } }
$payloadFile = if ($env:UNDERSTUDY_PAYLOAD) { $env:UNDERSTUDY_PAYLOAD } else { 'payload.json' }
$payloadText = if (Test-Path -LiteralPath $payloadFile) { Mask ([IO.File]::ReadAllText((Resolve-Path -LiteralPath $payloadFile))) } else { '' }
$report = [ordered]@{
  report_version = $ReportVersion; generator = $Generator; tool = $Tool; command = $RunCommand; run_id = $RunId
  target_url = $env:TARGET_URL; started_at = $Start.ToString('yyyy-MM-ddTHH:mm:sszzz'); finished_at = $End.ToString('yyyy-MM-ddTHH:mm:sszzz')
  duration_s = [int][Math]::Round(($End - $Start).TotalSeconds); exit_code = $Code; status = $status
  governance = [ordered]@{ status = $govern; message = $gmsg; rules_file_present = $rules }
  steps = $ordered; failure = $failure; warnings = $warnings; native_reports = $native; payload_text = $payloadText; log = $logJson
}
$json = ($report | ConvertTo-Json -Depth 6).Replace('<', '<')
Write-Utf8 "$Out\understudy-report.json" $json
Write-Utf8 "$Out\run.log" (($logOut -join "`n") + "`n")
$tplLines = [IO.File]::ReadAllLines((Resolve-Path -LiteralPath $TemplateFile))
$html = ($tplLines | ForEach-Object { if ($_ -eq '@@JSON@@') { $json } else { $_ } }) -join "`n"
Write-Utf8 "$Out\understudy-report.html" $html
Write-Utf8 'results\latest.html' "<!doctype html><meta charset=`"utf-8`"><meta http-equiv=`"refresh`" content=`"0; url=$RunId/understudy-report.html`"><a href=`"$RunId/understudy-report.html`">Open the latest report</a>`n"
Write-Host "== $status - report: $Out\understudy-report.html (exit code $Code)"
"""


def powershell_script(install: List[str], venv: bool, run: str, tool: str, generator: str) -> str:
    natives = ", ".join(f"@({_ps_str(n)}, {_ps_str(p)})" for n, p in NATIVE_REPORTS)
    lines = [
        "# Generated by Understudy - installs the dependencies, runs the automation and writes a report of the run to",
        "# results\\<run id>\\ (understudy-report.html, understudy-report.json, run.log). results\\latest.html = newest.",
        "# Start it with run.bat (or: powershell -NoProfile -ExecutionPolicy Bypass -File run.ps1).",
        "$ErrorActionPreference = 'Stop'",
        "Set-Location -LiteralPath $PSScriptRoot",
        "try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}",
        "if (Test-Path .env) { foreach ($l in Get-Content .env) { if ($l -match '^\\s*([A-Za-z_][A-Za-z0-9_]*)\\s*=(.*)$') {"
        " [Environment]::SetEnvironmentVariable($matches[1], $matches[2].Trim().Trim('\"').Trim(\"'\"), 'Process') } } }",
        "if (-not $env:TARGET_URL) { Write-Host 'Set TARGET_URL in .env (see .env.example)'; exit 2 }",
        "if (-not $env:UNDERSTUDY_PAYLOAD) { $env:UNDERSTUDY_PAYLOAD = 'payload.json' }",
        "$env:PYTHONUNBUFFERED = '1'; $env:PYTHONIOENCODING = 'utf-8'",
        "function Invoke-Setup([string]$Command) {",
        "  Write-Host \"== $Command\"",
        "  & cmd.exe /d /c $Command",
        "  if ($LASTEXITCODE -ne 0) { Write-Host \"Setup step failed (exit code $LASTEXITCODE): $Command\"; exit $LASTEXITCODE }",
        "}",
        "# --- install ---",
    ]
    for cmd in install:
        lines.append(f"Invoke-Setup {_ps_str(cmd)}")
        if venv and cmd.startswith("python -m venv"):
            lines.append("$env:VIRTUAL_ENV = Join-Path $PSScriptRoot '.venv'; "
                         "$env:PATH = (Join-Path $env:VIRTUAL_ENV 'Scripts') + ';' + $env:PATH")
    lines += [
        "# --- run, with a report ---",
        f"$Tool = {_ps_str(tool)}; $RunCommand = {_ps_str(run)}; $Generator = {_ps_str(generator)}; "
        f"$ReportVersion = {REPORT_VERSION}",
        f"$TemplateFile = {_ps_str(TEMPLATE_FILE.replace('/', chr(92)))}",
        f"$NativeReports = @({natives})",
        "$RunId = Get-Date -Format 'yyyyMMdd-HHmmss'; $Out = \"results\\$RunId\"",
        "New-Item -ItemType Directory -Force -Path $Out | Out-Null",
        "$env:UNDERSTUDY_RESULTS_DIR = $Out",
        "# pytest suites: show every line (-s) and write JUnit XML; other tools ignore this variable",
        "$env:PYTEST_ADDOPTS = (\"$env:PYTEST_ADDOPTS -s --junitxml=$Out\\junit.xml\").Trim()",
        "Write-Host \"== Running: $RunCommand\"",
        "$Start = Get-Date",
        "$Captured = New-Object System.Collections.Generic.List[object]",
        "$ErrorActionPreference = 'Continue'",
        "& cmd.exe /d /c \"$RunCommand 2>&1\" | ForEach-Object { $line = \"$_\"; Write-Host $line; "
        "$Captured.Add([pscustomobject]@{ t = ((Get-Date) - $Start).TotalSeconds; text = $line }) }",
        "$Code = $LASTEXITCODE; if ($null -eq $Code) { $Code = 0 }",
        "$End = Get-Date",
        "$ErrorActionPreference = 'Stop'",
    ]
    return "\n".join(lines) + "\n" + PS_REPORT.lstrip("\n") + "exit $Code\n"


BAT = ("@echo off\r\n"
       "REM Generated by Understudy - starts run.ps1 (install, run, report in results\\).\r\n"
       "powershell -NoProfile -ExecutionPolicy Bypass -File \"%~dp0run.ps1\" %*\r\n"
       "exit /b %ERRORLEVEL%\r\n")


def launcher_files(install_sh: List[str], install_ps: List[str], venv: bool, run: str, tool: str,
                   generator: str) -> Dict[str, str]:
    """run.sh, run.ps1, run.bat and the report helpers for the exported ZIP."""
    return {"run.sh": bash_script(install_sh, run, tool, generator),
            "run.ps1": powershell_script(install_ps, venv, run, tool, generator).replace("\n", "\r\n"),
            "run.bat": BAT,
            AWK_FILE: AWK,
            TEMPLATE_FILE: TEMPLATE}
