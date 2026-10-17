"use client";

/* Mission Control 8.0 - the HITL AI tool synthesizer and the zero-trust tool registry.
 *
 *   SetupLifecycle   REQUESTED -> PROPOSED -> INSTALLING -> SMOKE_TESTING -> ACTIVE (NEEDS_ADMIN when the AI asks)
 *   ManifestView     Phase A: OS dependencies, package-manager commands, environment variables, the runner wrapper
 *   SetupChat        Phase B/C: the conversation with the AI; when a step fails the setup halts, the AI shows the
 *                    captured output and asks a question (options + free text); the answer resumes the setup
 *   SmokeView        the mandatory smoke test (dummy page, click, assert) - exit 0 makes the tool ACTIVE
 *   RegistryPanel    every tool with its lifecycle status; "Re-run smoke test" for active tools
 *   SandboxPanel     the local process sandbox (Cloud Run has no Docker) and its limits
 *   useAdminInbox    live inbox (WebSocket /api/admin/ws, polling fallback) + toasts
 */
import React, { useCallback, useEffect, useRef, useState } from "react";
import {
  AlertTriangle, Bell, Bot, Box, CheckCircle2, CircleDot, Cpu, FlaskConical, Loader2, MessageSquare, Play, RefreshCw,
  Send, Server, ShieldCheck, Terminal, User, X, XCircle,
} from "lucide-react";
import {
  AdminApiError, adminApi, getAdminToken, type AdminPolicy, type RegistryEntry, type RegistryStatus, type SandboxInfo,
  type SandboxSettings, type SetupManifest, type SetupMessage, type SmokeResult, type ToolRequest,
  type AgentTurn,
} from "../lib/admin";
import { useLiveEvents, type LiveEventMsg } from "../lib/live";
import { Badge, Card, cx } from "./wizard/ui";

type Run = (fn: () => Promise<AdminPolicy>) => Promise<boolean>;
const input = "w-full text-xs bg-slate-950 border border-slate-700 rounded-lg px-3 py-2 text-white placeholder:text-slate-400 focus:outline-none focus:border-sky-500";

export const REGISTRY_TONE: Record<RegistryStatus, "green" | "amber" | "sky" | "violet" | "red" | "slate"> = {
  ACTIVE: "green", REQUESTED: "amber", CHOOSE_STACK: "sky", PROPOSED: "sky", INSTALLING: "violet", SMOKE_REVIEW: "amber",
  SMOKE_TESTING: "violet", NEEDS_ADMIN: "amber", FAILED: "red", REJECTED: "slate",
};
const REQ_TO_REGISTRY: Record<string, RegistryStatus> = {
  pending: "REQUESTED", suggesting: "REQUESTED", stack_ready: "CHOOSE_STACK", planning: "REQUESTED", plan_ready: "PROPOSED",
  installing: "INSTALLING", smoke_review: "SMOKE_REVIEW", smoke_testing: "SMOKE_TESTING", awaiting_admin: "NEEDS_ADMIN",
  setup_failed: "FAILED", approved: "ACTIVE", rejected: "REJECTED",
};

function Pre({ text, max = "max-h-48" }: { text: string; max?: string }) {
  return <pre className={cx("overflow-auto whitespace-pre-wrap break-all rounded bg-slate-950 p-2 font-mono text-[11px] leading-relaxed text-slate-200", max)}>{text}</pre>;
}

/** REQUESTED -> CHOOSE_STACK -> PROPOSED -> INSTALLING -> SMOKE_REVIEW -> SMOKE_TESTING -> ACTIVE (8.2) */
export function SetupLifecycle({ status, haltStage }: { status: string; haltStage?: string }) {
  const reg = REQ_TO_REGISTRY[status] || "REQUESTED";
  const steps: { id: RegistryStatus; label: string }[] = [
    { id: "REQUESTED", label: "Requested" }, { id: "CHOOSE_STACK", label: "Choose stack" }, { id: "PROPOSED", label: "AI plan" },
    { id: "INSTALLING", label: "Install" }, { id: "SMOKE_REVIEW", label: "Review test" }, { id: "SMOKE_TESTING", label: "Smoke test" },
    { id: "ACTIVE", label: "Active" },
  ];
  const order = steps.map((s) => s.id);
  // NEEDS_ADMIN / FAILED halt at the install or smoke-test stage
  const at = reg === "NEEDS_ADMIN" || reg === "FAILED" ? (haltStage === "smoke_test" ? 5 : 3) : Math.max(0, order.indexOf(reg));
  return (
    <ol className="flex flex-wrap items-center gap-1 text-[10px]" aria-label="Setup progress" data-testid="setup-lifecycle">
      {steps.map((s, i) => {
        const done = reg === "ACTIVE" || i < at;
        const cur = i === at && reg !== "ACTIVE";
        const halted = cur && (reg === "NEEDS_ADMIN" || reg === "FAILED");
        return (
          <li key={s.id} className="flex items-center gap-1">
            <span className={cx("inline-flex items-center gap-1 rounded-full border px-2 py-0.5",
              done ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-300"
                : halted ? (reg === "FAILED" ? "border-rose-500/50 bg-rose-500/10 text-rose-200" : "border-amber-500/50 bg-amber-500/10 text-amber-200")
                  : cur ? "border-violet-500/50 bg-violet-500/10 text-violet-200" : "border-slate-700 text-slate-400")}
              aria-current={cur ? "step" : undefined}>
              {done ? <CheckCircle2 className="h-3 w-3" /> : halted ? <AlertTriangle className="h-3 w-3" />
                : cur && (reg === "INSTALLING" || reg === "SMOKE_TESTING") ? <Loader2 className="h-3 w-3 animate-spin" /> : <CircleDot className="h-3 w-3" />}
              {halted ? (reg === "FAILED" ? "Failed" : "Needs you") : cur && (reg === "CHOOSE_STACK" || reg === "SMOKE_REVIEW") ? `${s.label} - your turn` : s.label}
            </span>
            {i < steps.length - 1 && <span className="text-slate-600" aria-hidden>›</span>}
          </li>
        );
      })}
    </ol>
  );
}

/** Phase A: the manifest the AI proposes. */
export function ManifestView({ m }: { m: SetupManifest }) {
  const [file, setFile] = useState<string>("run_tool.sh");
  const wrappers = m.runner_wrapper || {};
  const env = Object.entries(m.env_vars || {});
  return (
    <div className="space-y-2" data-testid="setup-manifest">
      <div className="grid gap-2 sm:grid-cols-2">
        <div className="rounded-lg border border-slate-800 bg-slate-950/60 p-2">
          <div className="mb-1 text-[10px] font-bold uppercase tracking-wide text-slate-400">OS dependencies</div>
          {m.os_dependencies?.length ? (
            <ul className="space-y-0.5 text-[11px] text-slate-300">
              {m.os_dependencies.map((d) => (
                <li key={d.check + d.value}><code className="text-sky-200">{d.check === "env" ? `$${d.value}` : d.value}</code>
                  {d.name && d.name !== d.value ? ` (${d.name})` : ""}{d.why ? <span className="text-slate-400"> - {d.why}</span> : null}</li>
              ))}
            </ul>
          ) : <p className="text-[11px] text-slate-400">None beyond the language runtime.</p>}
        </div>
        <div className="rounded-lg border border-slate-800 bg-slate-950/60 p-2">
          <div className="mb-1 text-[10px] font-bold uppercase tracking-wide text-slate-400">Environment variables</div>
          {env.length ? (
            <ul className="space-y-0.5 font-mono text-[11px] text-slate-300">{env.map(([k, v]) => <li key={k}>{k}=<span className="text-emerald-200">{v}</span></li>)}</ul>
          ) : <p className="text-[11px] text-slate-400">None.</p>}
          {m.interpreter && <p className="mt-1 text-[11px] text-slate-300">Interpreter: <code className="text-violet-200">{m.interpreter}</code></p>}
          {(m.path_prepend?.length || 0) > 0 && <p className="mt-1 text-[11px] text-slate-300">PATH first: <code className="text-violet-200">{m.path_prepend!.join(", ")}</code></p>}
        </div>
      </div>
      <div className="rounded-lg border border-slate-800 bg-slate-950/60 p-2">
        <div className="mb-1 flex flex-wrap items-center gap-2 text-[10px] font-bold uppercase tracking-wide text-slate-400">
          Package-manager commands
          <Badge tone={m.sandbox === "unavailable" ? "red" : "violet"}><Cpu className="h-3 w-3" />{m.sandbox} sandbox</Badge>
        </div>
        {m.package_commands?.length ? <Pre text={m.package_commands.map((c) => "$ " + c).join("\n")} /> : <p className="text-[11px] text-emerald-300">Nothing to install.</p>}
        {m.runner_notes && <p className="mt-1 text-[11px] text-slate-400">{m.runner_notes}</p>}
        <p className="mt-1 text-[11px] text-slate-400">Smoke test: {m.smoke_test}</p>
      </div>
      {Object.keys(wrappers).length > 0 && (
        <div className="rounded-lg border border-slate-800 bg-slate-950/60 p-2">
          <div className="mb-1 flex items-center gap-2 text-[10px] font-bold uppercase tracking-wide text-slate-400">
            <Terminal className="h-3 w-3" />Runner wrapper
            {Object.keys(wrappers).map((f) => (
              <button key={f} type="button" onClick={() => setFile(f)}
                className={cx("rounded px-1.5 py-0.5 font-mono normal-case", file === f ? "bg-slate-800 text-white" : "text-slate-400 hover:text-slate-200")}>{f}</button>
            ))}
          </div>
          <Pre text={wrappers[file] || Object.values(wrappers)[0]} />
        </div>
      )}
    </div>
  );
}

export function SmokeView({ smoke }: { smoke: SmokeResult }) {
  const pass = smoke.status === "PASSED" || smoke.status === "SEEDED";
  return (
    <div className={cx("rounded-lg border p-2 text-[11px]", pass ? "border-emerald-500/40 bg-emerald-500/5 text-emerald-100"
      : smoke.status === "SKIPPED" ? "border-slate-700 text-slate-300" : "border-rose-500/40 bg-rose-500/5 text-rose-100")} data-testid="smoke-result">
      <div className="flex flex-wrap items-center gap-2 font-semibold">
        <FlaskConical className="h-3.5 w-3.5" />Smoke test {smoke.status}
        {smoke.exit_code !== undefined && <span className="font-mono font-normal text-slate-400">exit {smoke.exit_code}</span>}
        {smoke.duration_ms !== undefined && <span className="font-normal text-slate-400">{(smoke.duration_ms / 1000).toFixed(1)} s</span>}
        {smoke.runner && <span className="font-normal text-slate-400">{smoke.runner} sandbox</span>}
        {smoke.source && <span className="font-normal text-slate-400">· {smoke.source}</span>}
      </div>
      {smoke.detail && <p className="mt-0.5 text-slate-300">{smoke.detail}</p>}
      {smoke.output && <details className="mt-1"><summary className="cursor-pointer text-slate-400">Output</summary><Pre text={smoke.output} /></details>}
    </div>
  );
}

function roleIcon(role: SetupMessage["role"]) {
  if (role === "ai") return <Bot className="h-3.5 w-3.5 text-violet-300" />;
  if (role === "admin") return <ShieldCheck className="h-3.5 w-3.5 text-sky-300" />;
  if (role === "user") return <User className="h-3.5 w-3.5 text-amber-300" />;
  return <Server className="h-3.5 w-3.5 text-slate-400" />;
}

/** Phase B/C: the conversation between the admin and the AI synthesizer, with the open question. */
export function SetupChat({ r, run, onError }: { r: ToolRequest; run: Run; onError: (m: string | null) => void }) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const end = useRef<HTMLDivElement>(null);
  const conv = r.conversation || [];
  const q = r.status === "awaiting_admin" ? r.clarification : null;
  const n = conv.length;
  useEffect(() => { end.current?.scrollIntoView({ block: "nearest" }); }, [n]);
  const send = async (option: string, label: string) => {
    if (!option && !text.trim()) return;
    setBusy(option || "text"); onError(null);
    try {
      const ok = await run(() => adminApi.answerRequest(r.id, text.trim(), option));
      if (ok) setText("");
      else if (!ok && label) onError(`Couldn't send "${label}".`);
    } finally { setBusy(null); }
  };
  if (!n && !q) return null;
  return (
    <div className="space-y-2 rounded-lg border border-slate-800 bg-slate-950/50 p-2" data-testid="setup-chat">
      <div className="flex items-center gap-1.5 text-[10px] font-bold uppercase tracking-wide text-slate-400">
        <MessageSquare className="h-3 w-3" />Setup conversation{r.setup?.round ? ` · round ${r.setup.round}` : ""}
      </div>
      <div className="max-h-72 space-y-1.5 overflow-y-auto pr-1" role="log" aria-live="polite">
        {conv.filter((m, i) => !(q && i === conv.length - 1 && m.kind === "question")).map((m) => (
          <div key={m.id} className={cx("flex gap-2 rounded-md px-2 py-1.5 text-[11px]",
            m.role === "admin" ? "bg-sky-500/5" : m.kind === "failure" ? "bg-rose-500/5" : m.kind === "question" ? "bg-amber-500/5"
              : m.kind === "auto_fix" ? "border border-emerald-500/30 bg-emerald-500/5" : m.kind === "agent_step" ? "bg-violet-500/5" : "")}>
            <span className="mt-0.5 shrink-0">{roleIcon(m.role)}</span>
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2 text-[10px] text-slate-400">
                <b className="uppercase">{m.role === "ai" ? "AI" : m.role}</b><span>{m.kind === "auto_fix" ? "fixed automatically" : m.kind === "agent_step" ? "setup agent" : m.kind}</span>
                <span className="ml-auto font-mono">{(m.at || "").slice(11, 19)}</span>
              </div>
              <p className={cx("whitespace-pre-wrap break-words", m.kind === "failure" ? "text-rose-200" : m.kind === "question" ? "text-amber-100"
                : m.kind === "auto_fix" ? "text-emerald-100" : "text-slate-200")}>{m.text}</p>
              {!!m.data?.changes && Object.keys(m.data.changes as object).length > 0 && (m.kind === "auto_fix" || m.kind === "summary") && (
                <p className="font-mono text-[10px] text-emerald-300/80">{describeChanges(m.data.changes as Record<string, unknown>)}</p>)}
              {m.data?.diagnosis && m.kind === "question" && <p className="text-[10px] text-slate-400">Diagnosis: {m.data.diagnosis}</p>}
              {m.data?.command && <p className="font-mono text-[10px] text-slate-400">$ {m.data.command}{m.data.exit_code != null ? `  → exit ${m.data.exit_code}` : ""}</p>}
              {m.data?.output && <details><summary className="cursor-pointer text-[10px] text-slate-400">Captured stdout / stderr</summary><Pre text={String(m.data.output)} /></details>}
            </div>
          </div>
        ))}
        <div ref={end} />
      </div>
      {q && (
        <div className="space-y-2 rounded-lg border border-amber-500/40 bg-amber-500/5 p-2" data-testid="clarification">
          <div className="flex items-start gap-2 text-xs text-amber-100"><Bot className="mt-0.5 h-4 w-4 shrink-0 text-violet-300" />
            <div><b>{q.agent?.fix ? "The setup agent found a fix - approve it and it will apply it, re-run the setup and keep going"
              : q.agent ? "The setup agent needs you" : "The setup is paused - the AI needs your answer."}</b>
              <p className="mt-0.5 whitespace-pre-wrap">{q.question}</p></div></div>
          {q.agent && <AgentPanel turn={q.agent} failedFiles={r.setup?.smoke?.files} model={q.model} />}
          {q.options?.length > 0 && (
            <div className="flex flex-wrap gap-1.5">
              {q.options.map((o) => (
                <button key={o.id} type="button" disabled={busy !== null}
                  title={[o.effect, o.changes ? "Does: " + describeChanges(o.changes) : ""].filter(Boolean).join(" - ")}
                  onClick={() => { void send(o.id, o.label); }}
                  className={cx("inline-flex items-center gap-1 rounded-lg border px-2.5 py-1 text-[11px] hover:bg-violet-500/10 disabled:opacity-50",
                    o.changes ? "border-emerald-500/60 text-emerald-100" : "border-violet-500/60 text-violet-100")}>
                  {busy === o.id ? <Loader2 className="h-3 w-3 animate-spin" /> : <Play className="h-3 w-3" />}{o.label}</button>
              ))}
            </div>
          )}
          <div className="flex gap-2">
            <input className={input} value={text} onChange={(e) => setText(e.target.value)} maxLength={2000}
              onKeyDown={(e) => { if (e.key === "Enter" && text.trim()) void send("", ""); }}
              aria-label={`Answer the AI about ${r.tool_name}`}
              placeholder={q.agent ? "A hint for the agent (then 'look again'), or your own answer, e.g. 'the field id is #username'"
                : "Or answer in your own words, e.g. 'JAVA_HOME is /opt/jdk-17' or 'use https://nexus.mycorp.com/repository/npm/'"} />
            <button type="button" disabled={!text.trim() || busy !== null} onClick={() => { void send("", ""); }}
              className="inline-flex shrink-0 items-center gap-1.5 rounded-lg bg-violet-500 px-3 py-1.5 text-xs font-bold text-white hover:bg-violet-400 disabled:opacity-50">
              {busy === "text" ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Send className="h-3.5 w-3.5" />}Answer</button>
          </div>
          <p className="text-[10px] text-slate-400">Your answer passes Model Armor. Paths, URLs and variables are checked before the AI&apos;s change is applied; the tool is never activated without a passing smoke test.</p>
        </div>
      )}
    </div>
  );
}

/** 8.5: one turn of the setup repair agent - what it checked in the sandbox, how it corrected itself, and the exact
 * fix (with the full corrected test) the admin approves. */
function AgentPanel({ turn, failedFiles, model }: { turn: AgentTurn; failedFiles?: Record<string, string>; model?: string | null }) {
  const files = turn.fix?.changes?.smoke_files || {};
  const checks = turn.steps.filter((s) => s.kind === "checks").flatMap((s) => s.checks || []);
  const corrected = turn.steps.filter((s) => s.kind === "proposal" && (s.problems?.length || 0) > 0);
  const dry = turn.steps.filter((s) => s.kind === "proposal" && !(s.problems?.length)).flatMap((s) => s.precheck || []);
  return (
    <div className="space-y-1.5 rounded-lg border border-violet-500/40 bg-violet-500/5 p-2 text-[11px] text-slate-200" data-testid="agent-panel">
      <div className="flex flex-wrap items-center gap-1.5 font-semibold text-violet-100">
        <Bot className="h-3.5 w-3.5 text-violet-300" />Setup agent · turn {turn.n}
        {model && <Badge tone="violet">{model}</Badge>}
        {turn.repeated && <Badge tone="amber" title="The same error came back after an earlier fix - the agent was told to find a different cause">same error again</Badge>}
        {turn.requested_model && <Badge tone="sky">you chose {turn.requested_model}</Badge>}
        {turn.fix?.confidence && <Badge tone={turn.fix.confidence === "high" ? "green" : "slate"}>confidence: {turn.fix.confidence}</Badge>}
      </div>
      {turn.error_line && <p className="font-mono text-[10px] text-rose-200">{turn.error_line}</p>}
      {turn.offer_model && (
        <p className="text-[10px] text-sky-200">You can let <b>{turn.offer_model}</b> look at this instead (button below) - the model is never
          switched without your click.</p>
      )}
      {checks.length > 0 && (
        <details>
          <summary className="cursor-pointer text-violet-200">What I checked in the sandbox ({checks.length})</summary>
          <ul className="mt-1 space-y-1">{checks.map((c, i) => (
            <li key={i}><span className={cx("font-mono", c.ok ? "text-emerald-300" : "text-amber-300")}>$ {c.label}</span>
              {c.exit_code != null && <span className="text-slate-400"> → exit {c.exit_code}</span>}
              <Pre text={c.output} max="max-h-32" /></li>))}</ul>
        </details>
      )}
      {corrected.length > 0 && (
        <p className="text-amber-200">I corrected my first proposal {corrected.length === 1 ? "once" : `${corrected.length} times`} after Understudy&apos;s check rejected it: {corrected[0].problems?.[0]}</p>
      )}
      {turn.fix && (
        <div className="space-y-1 rounded border border-emerald-500/40 bg-emerald-500/5 p-1.5">
          <p className="text-emerald-100"><b>Fix:</b> {turn.fix.summary}</p>
          <p className="font-mono text-[10px] text-emerald-300/80">Changes: {turn.fix.describe}</p>
          {dry.length > 0 && <p className="text-[10px] text-emerald-300">Pre-checked: {dry.map((d) => `${d.label} ${d.ok ? "passed" : "failed"}`).join(" · ")}</p>}
          {Object.entries(files).map(([path, content]) => (
            <details key={path} open={Object.keys(files).length === 1}>
              <summary className="cursor-pointer font-mono text-[10px] text-slate-300">{path} - the complete test that will run</summary>
              <Pre text={content} max="max-h-64" />
              {failedFiles?.[path] && failedFiles[path] !== content && (
                <details><summary className="cursor-pointer text-[10px] text-slate-400">The test that failed</summary><Pre text={failedFiles[path]} max="max-h-40" /></details>
              )}
            </details>
          ))}
        </div>
      )}
    </div>
  );
}

/** The zero-trust registry: every tool and its lifecycle status. */
export function RegistryPanel({ policy, run, onOpenRequest }: { policy: AdminPolicy | null; run: Run; onOpenRequest?: (id: string) => void }) {
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const tools: RegistryEntry[] = policy?.tools.registry || [];
  const smoke = async (id: string) => {
    setBusy(id); setErr(null);
    try { await run(async () => { try { return await adminApi.rerunSmoke(id); } catch (e) { setErr((e as AdminApiError).message); throw e; } }); }
    finally { setBusy(null); }
  };
  return (
    <Card title="Tool registry (zero trust)" icon={<ShieldCheck className="h-4 w-4 text-emerald-400" />}
      right={<Badge tone="green">{tools.filter((t) => t.status === "ACTIVE").length} active</Badge>}>
      <p className="mb-2 text-[11px] text-slate-400">Only ACTIVE tools can run. A tool becomes ACTIVE only after the AI installs it in the sandbox and its smoke test passes.</p>
      {err && <p className="mb-2 text-[11px] text-rose-300" role="alert">{err}</p>}
      <ul className="space-y-1.5" data-testid="tool-registry">
        {tools.map((t) => (
          <li key={t.tool_id + (t.request || "")} className="flex flex-wrap items-center gap-2 rounded-lg border border-slate-800 bg-slate-950/60 px-2.5 py-1.5 text-xs">
            <Badge tone={REGISTRY_TONE[t.status] || "slate"}>{(t.status === "INSTALLING" || t.status === "SMOKE_TESTING") && <Loader2 className="h-3 w-3 animate-spin" />}{t.status}</Badge>
            <b className="text-slate-100">{t.name}</b>
            {t.status === "ACTIVE" && t.versions?.allowed?.length ? <span className="font-mono text-[10px] text-slate-400">{t.versions.allowed.join(", ")}</span> : null}
            {t.baseline && <Badge tone="slate" title="Pre-seeded by UNDERSTUDY_DEFAULT_TOOLS=playwright">pre-seeded</Badge>}
            {t.smoke?.status && t.status === "ACTIVE" && (
              <span className={cx("inline-flex items-center gap-1 text-[10px]", t.smoke.status === "PASSED" || t.smoke.status === "SEEDED" ? "text-emerald-300" : t.smoke.status === "FAILED" ? "text-rose-300" : "text-slate-400")}
                title={t.smoke.at ? `at ${t.smoke.at}` : undefined}>
                {t.smoke.status === "FAILED" ? <XCircle className="h-3 w-3" /> : <FlaskConical className="h-3 w-3" />}smoke {t.smoke.status.toLowerCase()}</span>
            )}
            {t.status !== "ACTIVE" && t.requested_by?.length ? <span className="text-[10px] text-slate-400">by {t.requested_by.join(", ")}{t.sessions ? ` · ${t.sessions} session(s) waiting` : ""}</span> : null}
            <span className="ml-auto flex gap-1">
              {t.status === "ACTIVE" && (
                <button type="button" disabled={busy !== null} onClick={() => { void smoke(t.tool_id); }} data-testid="rerun-smoke"
                  className="inline-flex items-center gap-1 rounded border border-slate-700 px-2 py-0.5 text-[10px] text-slate-300 hover:border-emerald-500 hover:text-emerald-200 disabled:opacity-50">
                  {busy === t.tool_id ? <Loader2 className="h-3 w-3 animate-spin" /> : <RefreshCw className="h-3 w-3" />}Re-run smoke test</button>
              )}
              {t.status !== "ACTIVE" && t.request && onOpenRequest && (
                <button type="button" onClick={() => onOpenRequest(t.request!)}
                  className="rounded border border-slate-700 px-2 py-0.5 text-[10px] text-slate-300 hover:text-white">Open</button>
              )}
            </span>
          </li>
        ))}
        {tools.length === 0 && <li className="text-[11px] text-slate-400">No tools yet - nothing is pre-built. Add one with the AI install assistant (Access &amp; tools).</li>}
      </ul>
    </Card>
  );
}

/** Where tool installs, smoke tests and automation runs happen: the local process sandbox (9.9.1: Understudy runs on
 *  Google Cloud Run, which has no Docker daemon, so there is no Docker sandbox). The admin sets its limits. */
export function SandboxPanel() {
  const [info, setInfo] = useState<SandboxInfo | null>(null);
  const [draft, setDraft] = useState<SandboxSettings | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const load = useCallback(async () => {
    try { const d = await adminApi.sandbox(); setInfo(d); setDraft(d.settings); } catch (e) { setMsg({ ok: false, text: (e as Error).message }); }
  }, []);
  useEffect(() => { const t = setTimeout(() => { void load(); }, 0); return () => clearTimeout(t); }, [load]);
  const save = async () => {
    if (!draft) return;
    setBusy(true); setMsg(null);
    try {
      const d = await adminApi.saveSandbox({ mode: "local", memory_mb: draft.memory_mb, timeout_s: draft.timeout_s,
        install_timeout_s: draft.install_timeout_s });
      setInfo(d); setDraft(d.settings);
      setMsg({ ok: true, text: "Saved - installs, smoke tests and runs use these limits." });
    } catch (e) { setMsg({ ok: false, text: (e as Error).message }); } finally { setBusy(false); }
  };
  const num = (k: "memory_mb" | "timeout_s" | "install_timeout_s", label: string) => {
    const b = info?.bounds?.[k];
    return (
      <label className="space-y-0.5 text-[11px] text-slate-400">
        <span>{label}</span>
        <input type="number" step={1} min={b?.[0]} max={b?.[1]} value={draft ? Number(draft[k]) : ""} className={input}
          onChange={(e) => draft && setDraft({ ...draft, [k]: parseInt(e.target.value, 10) })} />
      </label>
    );
  };
  return (
    <Card title="Sandbox" icon={<Box className="h-4 w-4 text-sky-400" />}
      right={info ? <Badge tone="violet">local</Badge> : null}>
      <div className="space-y-3" data-testid="sandbox-panel">
        <p className="text-[11px] text-slate-400">Tool installs, smoke tests and automation runs happen here: an isolated folder per
          tool (npm --prefix / python -m venv), a restricted environment, OS time-outs and a memory watchdog, inside the Cloud Run
          instance&apos;s own sandbox. Automation runs reach the network only through Understudy&apos;s run proxy.</p>
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
          {num("memory_mb", "Memory (MB)")}{num("timeout_s", "Run time-out (s)")}{num("install_timeout_s", "Install time-out (s)")}
        </div>
        <div className="flex items-center gap-2">
          <button type="button" disabled={busy || !draft} onClick={() => { void save(); }}
            className="rounded-lg bg-sky-500 px-3 py-1.5 text-xs font-bold text-slate-950 hover:bg-sky-400 disabled:opacity-50">{busy ? "Saving..." : "Save sandbox"}</button>
          {info?.settings.updated_by && <span className="text-[10px] text-slate-400">last changed by {info.settings.updated_by}</span>}
        </div>
        {msg && <p className={cx("text-[11px]", msg.ok ? "text-emerald-300" : "text-rose-300")} role="status">{msg.text}</p>}
      </div>
    </Card>
  );
}

export interface Toast { id: string; text: string; tone: "amber" | "green" | "red" | "sky" }

/** 8.3: what a fix / an option actually changes, in one line. */
export function describeChanges(ch: Record<string, unknown>): string {
  const parts: string[] = [];
  const pk = ch.add_packages as { name: string; version?: string }[] | undefined;
  if (pk?.length) parts.push("add " + pk.map((p) => p.name + (p.version ? "@" + p.version : "")).join(", "));
  const pi = ch.post_install as string[][] | undefined;
  if (pi?.length) parts.push("then run " + pi.map((c) => c.join(" ")).join("; "));
  if (ch.rewrite_smoke) parts.push("rewrite the smoke test");
  const dl = (ch.download_runtimes || (ch.download_runtime ? [ch.download_runtime] : [])) as string[];
  if (dl.length) parts.push("download " + dl.join(", "));
  for (const k of Object.keys(ch)) {
    if (!["add_packages", "post_install", "rewrite_smoke", "download_runtimes", "download_runtime"].includes(k)) parts.push(k);
  }
  return parts.join(" · ");
}

function toastFor(e: LiveEventMsg): Toast | null {
  const d = (e.data || {}) as Record<string, string>;
  const name = `${d.tool_name || d.tool_id || "A tool"}${d.version ? " " + d.version : ""}`;
  if (e.type === "tool_request.created") return { id: e.id || String(e.at), tone: "amber", text: `New enablement request: ${name}` };
  if (e.type === "tool_request.updated") {
    if (d.status === "awaiting_admin") return { id: e.id || String(e.at), tone: "amber", text: `${name}: the AI needs your answer` };
    if (d.status === "approved") return { id: e.id || String(e.at), tone: "green", text: `${name} is ACTIVE - smoke test passed` };
    if (d.status === "plan_ready") return { id: e.id || String(e.at), tone: "sky", text: `${name}: setup plan ready for review` };
    if (d.status === "stack_ready") return { id: e.id || String(e.at), tone: "sky", text: `${name}: the AI suggested the stack - choose it` };
    if (d.status === "smoke_review") return { id: e.id || String(e.at), tone: "amber", text: `${name}: review the AI-written smoke test` };
    if (d.status === "setup_failed") return { id: e.id || String(e.at), tone: "red", text: `${name}: setup stopped` };
  }
  if (e.type === "escalation.created") return { id: e.id || String(e.at), tone: "amber", text: `${d.owner || "A user"} needs your help with a run (${d.escalation}) - see Escalations` };
  if (e.type === "tool.smoke") return { id: e.id || String(e.at), tone: d.status === "PASSED" ? "green" : "red", text: `Smoke test ${d.tool_id}: ${d.status}` };
  return null;
}

/** Live Mission Control inbox: WebSocket events (polling fallback), the open-request count, toasts. */
export function useAdminInbox(refreshPolicy: () => void) {
  const [needs, setNeeds] = useState(0);
  const [escalations, setEscalations] = useState(0);
  const [toasts, setToasts] = useState<Toast[]>([]);
  const after = useRef(0);
  const seen = useRef(new Set<string>());
  const push = useCallback((t: Toast | null) => {
    if (!t || seen.current.has(t.id)) return;
    seen.current.add(t.id);
    setToasts((x) => [...x.slice(-3), t]);
    setTimeout(() => setToasts((x) => x.filter((y) => y.id !== t.id)), 8000);
  }, []);
  const poll = useCallback(() => {
    adminApi.inbox(after.current).then((d) => {
      setNeeds(d.needs_admin);
      setEscalations(d.escalations_open || 0);
      if (after.current) d.events.forEach((e) => push(toastFor(e)));
      if (d.events.length) refreshPolicy();
      after.current = d.now;
    }).catch(() => null);
  }, [push, refreshPolicy]);
  const mode = useLiveEvents({
    path: "/api/admin/ws", token: getAdminToken(), enabled: true, poll, pollMs: 6000,
    onEvent: (e) => { push(toastFor(e)); refreshPolicy(); poll(); },
  });
  const dismiss = (id: string) => setToasts((x) => x.filter((y) => y.id !== id));
  return { needs, escalations, toasts, dismiss, mode };
}

export function InboxBell({ count, mode, onClick }: { count: number; mode: string; onClick: () => void }) {
  return (
    <button type="button" onClick={onClick} data-testid="inbox-bell"
      title={`${count} request(s) need you · ${mode === "live" ? "live updates" : "polling"}`}
      aria-label={`Inbox: ${count} request(s) need your attention`}
      className="relative inline-flex items-center rounded-lg border border-slate-700 bg-slate-800 p-1.5 text-slate-300 hover:text-white">
      <Bell className="h-4 w-4" />
      {count > 0 && <span className="absolute -right-1.5 -top-1.5 min-w-[18px] rounded-full bg-amber-500 px-1 text-center text-[10px] font-bold leading-[18px] text-slate-950">{count}</span>}
      <span className={cx("absolute -bottom-0.5 -right-0.5 h-2 w-2 rounded-full", mode === "live" ? "bg-emerald-400" : "bg-slate-500")} aria-hidden />
    </button>
  );
}

export function Toasts({ toasts, dismiss, onOpen }: { toasts: Toast[]; dismiss: (id: string) => void; onOpen: () => void }) {
  if (!toasts.length) return null;
  const tone = { amber: "border-amber-500/50 text-amber-100", green: "border-emerald-500/50 text-emerald-100", red: "border-rose-500/50 text-rose-100", sky: "border-sky-500/50 text-sky-100" };
  return (
    <div className="fixed bottom-4 right-4 z-50 flex w-80 flex-col gap-2" role="status" aria-live="polite" data-testid="toasts">
      {toasts.map((t) => (
        <div key={t.id} className={cx("flex items-start gap-2 rounded-lg border bg-slate-900 px-3 py-2 text-xs shadow-xl", tone[t.tone])}>
          <Bell className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <button type="button" className="flex-1 text-left hover:underline" onClick={() => { onOpen(); dismiss(t.id); }}>{t.text}</button>
          <button type="button" aria-label="Dismiss" onClick={() => dismiss(t.id)} className="text-slate-400 hover:text-white"><X className="h-3.5 w-3.5" /></button>
        </div>
      ))}
    </div>
  );
}
