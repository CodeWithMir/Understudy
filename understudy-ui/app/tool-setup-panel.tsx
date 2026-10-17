"use client";

/* Mission Control > Access & tools > Tools (7.2, 8.0 synthesizer, 8.2 AI install assistant + smoke-test review).
 * Nothing is pre-built on a fresh install - not even Playwright. Tools get in two ways: the admin types one in the AI
 * install assistant, or a user requests a tool (or a version) that is neither approved nor blocked. The AI suggests the
 * stack (companion frameworks, languages, package managers, versions); the admin selects it; the AI prepares the setup
 * plan (refinable with instructions such as "download from our mirror ..."); approving installs it in the sandbox; the
 * AI writes the smoke test, the admin reviews it, and a passing run makes the tool ACTIVE with the chosen versions. */
import React, { useEffect, useState } from "react";
import { AlertTriangle, Ban, Bot, Check, ChevronDown, ChevronRight, Loader2, Package, Plus, Send, ShieldBan, Sparkles, Trash2, Wrench, X } from "lucide-react";
import { AdminApiError, adminApi, type AdminPolicy, type AgenticToolOptions, type ToolRequest } from "../lib/admin";
import { DiffBlock } from "./admin-extra";
import { ToolRow } from "./admin-panels";
import { SmokeReviewPanel, StackPicker, StackSummary } from "./install-assistant";
import { ManifestView, SetupChat, SetupLifecycle, SmokeView } from "./synthesizer";
import { Badge, Card, cx, FOLD_DEFAULT_OPEN, useFold } from "./wizard/ui";

type Run = (fn: () => Promise<AdminPolicy>) => Promise<boolean>;
const input = "w-full text-xs bg-slate-950 border border-slate-700 rounded-lg px-3 py-2 text-white placeholder:text-slate-400 focus:outline-none focus:border-sky-500";
const STATUS: Record<string, { label: string; tone: "amber" | "sky" | "green" | "red" | "violet" | "slate" }> = {
  pending: { label: "waiting for you", tone: "amber" }, suggesting: { label: "AI is working out the stack", tone: "violet" },
  stack_ready: { label: "choose the stack", tone: "sky" }, smoke_review: { label: "review the smoke test", tone: "amber" },
  planning: { label: "AI is preparing the setup", tone: "violet" },
  plan_ready: { label: "setup plan ready - review it", tone: "sky" }, installing: { label: "installing in the sandbox", tone: "violet" },
  smoke_testing: { label: "smoke testing", tone: "violet" }, awaiting_admin: { label: "the AI needs your answer", tone: "amber" },
  setup_failed: { label: "setup stopped", tone: "red" }, approved: { label: "approved", tone: "green" }, rejected: { label: "rejected", tone: "slate" },
};

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="mb-1 text-[11px] font-bold uppercase tracking-wide text-slate-400">{title}</div>
      {children}
    </div>
  );
}

function Code({ text }: { text: string }) {
  return <pre className="max-h-48 overflow-auto rounded bg-slate-950 p-2 font-mono text-[11px] leading-relaxed text-slate-200">{text}</pre>;
}

/** One request: the AI setup plan, the admin's instructions, approval with versions, the sandbox install. */
function RequestCard({ r, run, onError }: { r: ToolRequest; run: Run; onError: (m: string | null) => void }) {
  const plan = r.plan;
  const [instruction, setInstruction] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [allowed, setAllowed] = useState<string[] | null>(null);
  const [def, setDef] = useState<string | null>(null);
  const [changeStack, setChangeStack] = useState(false);
  const preparedAt = plan?.prepared_at;
  useEffect(() => {                       // a new plan resets the version choice to the AI's suggestion
    const t = setTimeout(() => { setAllowed(null); setDef(null); }, 0);
    return () => clearTimeout(t);
  }, [preparedAt]);
  const chosen = allowed ?? plan?.versions?.allowed ?? [];
  const chosenDef = def ?? plan?.versions?.default ?? null;
  const registry = plan?.versions?.registry?.versions || [];
  const blocked = new Set(plan?.versions?.blocked || []);
  const choices = Array.from(new Set([...(plan?.versions?.allowed || []), ...registry.slice(0, 10)]));
  const st = STATUS[r.status] || { label: r.status, tone: "slate" as const };
  const isVersion = r.kind === "version";
  const working = r.status === "installing" || r.status === "smoke_testing" || r.status === "planning" || r.status === "suggesting";
  const haltStage = r.clarification?.stage || r.setup?.failure?.stage;

  const act = async (key: string, fn: () => Promise<AdminPolicy>) => {
    setBusy(key); onError(null);
    try { await run(fn); } finally { setBusy(null); }
  };
  const toggle = (v: string) => {
    if (isVersion && v === r.version) return;
    const next = chosen.includes(v) ? chosen.filter((x) => x !== v) : [...chosen, v];
    setAllowed(next);
    if (!next.includes(chosenDef || "")) setDef(next[0] || null);
  };

  const [open, setOpen] = useFold(`req-${r.id}`, FOLD_DEFAULT_OPEN);
  return (
    <div id={`req-${r.id}`} className={cx("scroll-mt-28 rounded-lg border p-3", open && "space-y-3", r.status === "setup_failed" ? "border-rose-500/40 bg-rose-950/10" : "border-amber-500/40 bg-amber-950/10")}
      data-testid="tool-request">
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <button type="button" onClick={() => setOpen(!open)} aria-expanded={open} title={open ? "Fold this request" : "Open this request"}
          className="rounded p-0.5 text-slate-400 hover:bg-slate-800 hover:text-white">
          {open ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}</button>
        <Badge tone={isVersion ? "violet" : "sky"}>{isVersion ? "new version" : "new tool"}</Badge>
        <strong className="text-amber-100">{r.tool_name}{r.version ? ` ${r.version}` : ""}</strong>
        <Badge tone={st.tone}>{working && <Loader2 className="h-3 w-3 animate-spin" />}{st.label}</Badge>
        <span className="ml-auto font-mono text-[10px] text-slate-400">{r.id}</span>
      </div>
      <div hidden={!open} className="space-y-3">
      <SetupLifecycle status={r.status} haltStage={haltStage} />
      <p className="text-[11px] text-slate-400">
        {r.source === "admin" ? "Added by the admin" : `Requested by ${(r.requested_by || []).join(", ") || "a user"} from ${r.sessions.length} session(s)`}
        {r.reason && r.source !== "admin" ? <> · <i className="text-slate-300">&ldquo;{r.reason}&rdquo;</i></> : null}
      </p>

      {r.status === "pending" && !plan?.spec && (
        <div className="space-y-2">
          {plan?.notes?.map((n, i) => <p key={i} className="text-[11px] text-amber-200">{n}</p>)}
          {r.stack?.notes?.map((n, i) => <p key={i} className="text-[11px] text-amber-200">{n}</p>)}
          <div className="flex flex-wrap gap-2">
            {!isVersion && (
              <button type="button" disabled={busy !== null} onClick={() => { void act("stack", () => adminApi.suggestStack(r.id)); }} data-testid="ask-stack"
                className="inline-flex items-center gap-1.5 rounded-lg bg-violet-500 px-3 py-1.5 text-xs font-bold text-white hover:bg-violet-400 disabled:opacity-50">
                {busy === "stack" ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Sparkles className="h-3.5 w-3.5" />}Ask the AI for the stack</button>
            )}
            <button type="button" disabled={busy !== null} onClick={() => { void act("plan", () => adminApi.planRequest(r.id)); }}
              className={cx("inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs disabled:opacity-50",
                isVersion ? "bg-violet-500 font-bold text-white hover:bg-violet-400" : "border border-slate-700 text-slate-300 hover:text-white")}>
              {busy === "plan" ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Bot className="h-3.5 w-3.5" />}{isVersion ? "Prepare setup with AI" : "Skip - prepare the setup directly"}</button>
          </div>
          {!isVersion && <p className="text-[11px] text-slate-400">The AI lists the companion frameworks, languages, package managers and versions for {r.tool_name}; you pick what to install.</p>}
        </div>
      )}
      {r.status === "suggesting" && (
        <p className="flex items-center gap-2 text-[11px] text-violet-200"><Loader2 className="h-3.5 w-3.5 animate-spin" />The AI is working out the stack for {r.tool_name}...</p>
      )}
      {r.status === "stack_ready" && <StackPicker r={r} run={run} onError={onError} />}
      {r.status === "plan_ready" && changeStack && r.stack?.suggestion && <StackPicker r={r} run={async (fn) => { const ok = await run(fn); if (ok) setChangeStack(false); return ok; }} onError={onError} onCancel={() => setChangeStack(false)} />}
      {r.stack?.selection && r.status !== "stack_ready" && (
        <div className="flex flex-wrap items-center gap-2">
          <StackSummary r={r} />
          {r.status === "plan_ready" && r.stack?.suggestion && !changeStack && (
            <button type="button" onClick={() => setChangeStack(true)} className="rounded border border-slate-700 px-2 py-0.5 text-[10px] text-slate-300 hover:text-white">Change the stack</button>
          )}
        </div>
      )}

      {plan?.spec && r.status !== "plan_ready" && r.status !== "pending" && r.status !== "planning" && (
        <details className="rounded-lg border border-slate-800 bg-slate-950/50 p-2 text-[11px] text-slate-300">
          <summary className="cursor-pointer font-semibold">Approved setup plan & manifest</summary>
          <div className="mt-2 space-y-2"><p>{plan.summary}</p>{plan.manifest && <ManifestView m={plan.manifest} />}</div>
        </details>
      )}
      {plan?.spec && r.status === "plan_ready" && (
        <div className="space-y-3 rounded-lg border border-slate-800 bg-slate-950/50 p-3" data-testid="setup-plan">
          <div className="flex items-start gap-2 text-xs text-slate-200">
            <Bot className="mt-0.5 h-4 w-4 shrink-0 text-violet-300" />
            <div><b>Setup plan</b> <span className="text-[10px] text-slate-400">({plan.agent_source === "ACTUAL_AI_API" ? `AI · ${plan.model}` : plan.agent_source === "LEARNED_SKILL" ? "learned skill" : "prepared without AI"})</span>
              <p className="mt-0.5 text-slate-300">{plan.summary}</p></div>
          </div>
          {plan.notes?.map((n, i) => <p key={i} className="text-[11px] text-amber-200">⚠ {n}</p>)}
          <Section title="1. How it will be downloaded and installed">
            <ol className="list-decimal space-y-0.5 pl-5 text-[11px] text-slate-300">{plan.download_steps?.map((x, i) => <li key={i}>{x}</li>)}</ol>
            {(plan.sources?.index_url || plan.sources?.local_dir) && (
              <p className="mt-1 text-[11px] text-violet-200">Package source: <code>{plan.sources.index_url || plan.sources.local_dir}</code></p>
            )}
            {plan.commands && plan.commands.length > 0 ? <div className="mt-1"><Code text={plan.commands.map((c) => "$ " + c).join("\n")} /></div>
              : <p className="mt-1 text-[11px] text-emerald-300">Already available on this server: {(plan.already_satisfied || []).join(", ") || "-"} - nothing to download.</p>}
          </Section>
          {plan.manifest && (
            <Section title="Manifest - what the sandbox gets">
              <ManifestView m={plan.manifest} />
            </Section>
          )}
          {plan.manifests && Object.keys(plan.manifests).length > 0 && (
            <Section title="2. Files Understudy writes for this tool (sandbox and every ZIP)">
              {Object.entries(plan.manifests).map(([f, c]) => <div key={f} className="mb-1"><div className="font-mono text-[11px] text-slate-400">{f}</div><Code text={c} /></div>)}
            </Section>
          )}
          <Section title="3. Change to the approved tools list">
            <p className="mb-1 text-[11px] text-slate-400">Understudy&apos;s code doesn&apos;t change - the tool is added as this entry (with the versions you choose below):</p>
            {plan.policy_diff ? <DiffBlock diff={plan.policy_diff} /> : <p className="text-[11px] text-slate-400">No change.</p>}
          </Section>
          <Section title="4. Versions users may pick">
            {isVersion && <p className="mb-1 text-[11px] text-slate-400">{r.version} is added to the versions already allowed.</p>}
            <div className="flex flex-wrap gap-1.5">
              {choices.map((v) => {
                const on = chosen.includes(v);
                const isBlocked = blocked.has(v);
                return (
                  <span key={v} className={cx("inline-flex items-center overflow-hidden rounded-md border text-[11px]",
                    isBlocked ? "border-rose-500/40 text-rose-300 line-through" : on ? "border-sky-500/60 bg-sky-500/10 text-sky-200" : "border-slate-700 text-slate-400")}>
                    <button type="button" disabled={isBlocked || r.status === "installing"} onClick={() => toggle(v)} className="px-2 py-0.5 font-mono">{v}</button>
                    {on && <button type="button" title="Make default" onClick={() => setDef(v)}
                      className={cx("border-l border-sky-500/40 px-1.5 py-0.5", chosenDef === v ? "text-amber-300" : "text-slate-400 hover:text-amber-200")}>★</button>}
                  </span>
                );
              })}
            </div>
            <p className="mt-1 text-[11px] text-slate-400">{chosen.length ? <>Allowed <b className="text-slate-200">{chosen.join(", ")}</b> · default <b className="text-amber-300">{chosenDef}</b> · the default is installed and checked now; you can change versions any time later.</> : "No restriction - users may pick any version from the registry."}
              {plan.versions?.notes ? ` ${plan.versions.notes}` : ""}</p>
          </Section>
          <Section title="5. How automation code is written">
            <p className="text-[11px] text-slate-300">{plan.code_generation}</p>
          </Section>
          {((plan.requirements?.length || 0) + (plan.risks?.length || 0)) > 0 && (
            <Section title="Check before approving">
              <ul className="list-disc space-y-0.5 pl-5 text-[11px] text-amber-100">{[...(plan.requirements || []), ...(plan.risks || [])].map((x, i) => <li key={i}>{x}</li>)}</ul>
            </Section>
          )}
          {(r.instructions?.length || 0) > 0 && (
            <Section title="Your instructions">
              <ul className="space-y-0.5 text-[11px] text-slate-300">{r.instructions!.map((x, i) => <li key={i}>“{x.text}” <span className="text-slate-500">- {x.by}</span></li>)}</ul>
              {plan.instructions_applied && plan.instructions_applied.length > 0 && (
                <ul className="mt-1 list-disc pl-5 text-[11px] text-emerald-200">{plan.instructions_applied.map((x, i) => <li key={i}>{x}</li>)}</ul>
              )}
            </Section>
          )}
        </div>
      )}

      {r.status === "smoke_review" && <SmokeReviewPanel r={r} run={run} onError={onError} />}
      {(r.status === "installing" || r.status === "smoke_testing") && r.setup && (
        <div className="rounded-lg border border-violet-500/40 bg-violet-500/5 p-2 text-[11px] text-violet-100">
          <div className="flex items-center gap-2"><Loader2 className="h-3.5 w-3.5 animate-spin" />
            {r.setup.status === "agent_working" ? `The setup agent is investigating the failure of ${r.tool_name} (read-only checks in the sandbox) - follow it in the conversation below...`
              : r.status === "smoke_testing" ? (r.setup.smoke_review?.approved ? `Running the smoke test you approved for ${r.tool_name} (dummy page, click, assert)...` : `The AI is writing the smoke test for ${r.tool_name}...`)
              : `Installing ${r.tool_name} ${r.setup.install_version || r.setup.default || ""} in the ${r.setup.sandbox?.kind || ""} sandbox...`}</div>
          {r.setup.logs?.length > 0 && <div className="mt-1"><Code text={r.setup.logs.slice(-12).join("\n")} /></div>}
        </div>
      )}
      {r.status === "setup_failed" && r.setup && (
        <div className="space-y-1 rounded-lg border border-rose-500/40 bg-rose-500/5 p-2 text-[11px] text-rose-100" role="alert">
          <div className="flex items-center gap-2 font-semibold"><AlertTriangle className="h-3.5 w-3.5" />The install failed: {r.setup.message}</div>
          {r.setup.logs?.length > 0 && <Code text={r.setup.logs.slice(-15).join("\n")} />}
          <p className="text-slate-300">Tell the AI what to change below (for example a mirror or a directory with the package files) and retry, or reject the request.</p>
        </div>
      )}
      {r.setup?.smoke && r.status !== "installing" && r.status !== "smoke_review" && <SmokeView smoke={r.setup.smoke} />}
      <SetupChat r={r} run={run} onError={onError} />

      {(r.status === "plan_ready" || r.status === "setup_failed") && (
        <div className="space-y-2">
          {!isVersion && (
            <div className="flex gap-2">
              <input className={input} value={instruction} onChange={(e) => setInstruction(e.target.value)} maxLength={2000}
                aria-label={`Instruction for the AI about ${r.tool_name}`}
                placeholder="Tell the AI what to change, e.g. 'download from https://pypi.mycorp.com/simple' or 'use the wheels in /opt/wheels' or 'only allow 4.25.0'" />
              <button type="button" disabled={!instruction.trim() || busy !== null}
                onClick={() => { void act("revise", () => adminApi.planRequest(r.id, instruction)).then(() => setInstruction("")); }}
                className="inline-flex shrink-0 items-center gap-1.5 rounded-lg border border-violet-500/60 px-3 py-1.5 text-xs text-violet-200 hover:bg-violet-500/10 disabled:opacity-50">
                {busy === "revise" ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Send className="h-3.5 w-3.5" />}Revise plan</button>
            </div>
          )}
          <div className="flex flex-wrap gap-2">
            <button type="button" disabled={busy !== null} onClick={() => { void act("setup", () => adminApi.setupRequest(r.id, chosen, chosenDef)); }}
              className="inline-flex items-center gap-1.5 rounded-lg bg-emerald-500 px-3 py-1.5 text-xs font-bold text-slate-950 hover:bg-emerald-400 disabled:opacity-50">
              {busy === "setup" ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Check className="h-3.5 w-3.5" />}
              {r.status === "setup_failed" ? "Retry the setup" : "Approve & set up"}</button>
          </div>
        </div>
      )}

      {!working && (
        <div className="flex gap-2 border-t border-slate-800 pt-2">
          <input className={input} value={note} onChange={(e) => setNote(e.target.value)} maxLength={500} placeholder="Note to the user if you reject (optional)" />
          <button type="button" disabled={busy !== null} onClick={() => { void act("reject", () => adminApi.reject(r.id, note)); }}
            className="flex shrink-0 items-center gap-1 rounded-lg bg-slate-800 px-2.5 py-1.5 text-[11px] text-slate-300 hover:bg-slate-700"><X className="h-3 w-3" />Reject</button>
        </div>
      )}
      </div>
    </div>
  );
}

const EXAMPLES = ["Selenium", "Playwright", "Cypress", "WebdriverIO", "Robot Framework", "REST Assured", "Karate", "Appium"];

/** 8.2: the AI install assistant - the admin types a tool, the AI suggests the stack. */
function AddToolWithAi({ run, policy, onError }: { run: Run; policy: AdminPolicy | null; onError: (m: string | null) => void }) {
  const [name, setName] = useState("");
  const [instruction, setInstruction] = useState("");
  const [busy, setBusy] = useState(false);
  const aiOn = policy ? (policy.settings as { ai_available?: boolean }).ai_available !== false : true;
  const go = async (n: string) => {
    setBusy(true); onError(null);
    try {
      const ok = await run(() => adminApi.addToolWithAi(n, undefined, instruction.trim() || undefined, true));
      if (ok) { setName(""); setInstruction(""); }
    } finally { setBusy(false); }
  };
  const [aiOpen, setAiOpen] = useFold("ai-install-assistant", FOLD_DEFAULT_OPEN);
  return (
    <div className={cx("rounded-lg border border-violet-500/30 bg-violet-500/5 p-3", aiOpen && "space-y-2")} data-testid="add-tool-ai">
      <button type="button" onClick={() => setAiOpen(!aiOpen)} aria-expanded={aiOpen}
        className="flex w-full items-center gap-2 text-left text-xs font-semibold text-violet-100 hover:text-white">
        {aiOpen ? <ChevronDown className="h-3.5 w-3.5 text-slate-400" /> : <ChevronRight className="h-3.5 w-3.5 text-slate-400" />}
        <Sparkles className="h-4 w-4 text-violet-300" />AI install assistant</button>
      <div hidden={!aiOpen} className="space-y-2">
      <p className="text-[11px] text-slate-400">Nothing is pre-built. Type a tool and the AI tells you what it needs - companion frameworks (e.g. WebDriver,
        TestNG, JUnit, Cucumber), the languages it comes in and the package managers - with real versions from the package registry.
        You pick the stack; the AI installs it in the sandbox and writes a smoke test that you review before it runs.</p>
      <div className="grid gap-2 sm:grid-cols-[1fr_auto]">
        <input className={input} value={name} onChange={(e) => setName(e.target.value)} maxLength={200} aria-label="Tool to add"
          onKeyDown={(e) => { if (e.key === "Enter" && name.trim() && !busy) void go(name.trim()); }}
          placeholder="Tool name, e.g. Selenium, Playwright, Cypress, Karate, k6..." />
        <button type="button" disabled={!name.trim() || busy} onClick={() => { void go(name.trim()); }}
          className="inline-flex items-center justify-center gap-1.5 rounded-lg bg-violet-500 px-3 py-1.5 text-xs font-bold text-white hover:bg-violet-400 disabled:opacity-50">
          {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Plus className="h-3.5 w-3.5" />}Ask the AI</button>
      </div>
      <input className={input} value={instruction} onChange={(e) => setInstruction(e.target.value)} maxLength={1000} aria-label="Note for the AI (optional)"
        placeholder="Note for the AI (optional), e.g. 'we only use Java' or 'BDD with Cucumber'" />
      <div className="flex flex-wrap items-center gap-1.5">
        <span className="text-[11px] text-slate-400">Try:</span>
        {EXAMPLES.map((k) => (
          <button key={k} type="button" disabled={busy} onClick={() => setName(k)}
            className="rounded-full border border-slate-700 px-2 py-0.5 text-[11px] text-slate-300 hover:border-violet-400 hover:text-violet-200">{k}</button>
        ))}
      </div>
      {!aiOn && <p className="text-[11px] text-amber-300">The assistant needs Gemini - configure it under AI, security &amp; account.</p>}
      </div>
    </div>
  );
}

function BlockedTools({ policy, run }: { policy: AdminPolicy | null; run: Run }) {
  const [name, setName] = useState("");
  const [versions, setVersions] = useState("");
  const [note, setNote] = useState("");
  const list = policy?.tools.blocklist || [];
  return (
    <div className="space-y-2" data-testid="blocked-tools">
      <div className="flex items-center gap-1 text-[11px] font-bold uppercase text-rose-300"><ShieldBan className="h-3 w-3" />Blocked tools ({list.length})</div>
      <p className="text-[11px] text-slate-400">Users can&apos;t use or request a blocked tool (or blocked versions). A tool can&apos;t be approved and blocked at the same time.</p>
      {list.map((b) => (
        <div key={b.id} className="flex items-center gap-2 rounded-lg border border-rose-500/30 bg-rose-950/10 px-2.5 py-1.5 text-xs">
          <Ban className="h-3.5 w-3.5 text-rose-400" />
          <b className="text-rose-100">{b.name}</b>
          <span className="text-[11px] text-slate-400">{b.versions.length ? `versions ${b.versions.join(", ")}` : "all versions"}{b.note ? ` · ${b.note}` : ""}</span>
          <button type="button" aria-label={`Unblock ${b.name}`} onClick={() => { void run(() => adminApi.unblockTool(b.id)); }}
            className="ml-auto rounded p-1 text-slate-400 hover:bg-slate-800 hover:text-emerald-300"><Trash2 className="h-3.5 w-3.5" /></button>
        </div>
      ))}
      <div className="grid gap-2 sm:grid-cols-[1fr_140px_1fr_auto]">
        <input className={input} value={name} onChange={(e) => setName(e.target.value)} maxLength={80} aria-label="Tool to block" placeholder="Tool or package, e.g. Selenium" />
        <input className={input} value={versions} onChange={(e) => setVersions(e.target.value)} aria-label="Versions to block" placeholder="versions (empty = all), e.g. 3.*" />
        <input className={input} value={note} onChange={(e) => setNote(e.target.value)} maxLength={200} aria-label="Reason for blocking" placeholder="Reason (shown to users)" />
        <button type="button" disabled={!name.trim()}
          onClick={() => { void run(() => adminApi.blockTool(name.trim(), versions.split(/[\s,]+/).filter(Boolean), note)).then((ok) => { if (ok) { setName(""); setVersions(""); setNote(""); } }); }}
          className="rounded-lg bg-rose-600 px-3 py-1.5 text-xs font-bold text-white hover:bg-rose-500 disabled:opacity-50">Block</button>
      </div>
    </div>
  );
}

/** 9.9: agentic tools on Understudy's own agent - the admin picks the browser stack like a traditional tool stack.
 * Understudy installs it in the sandbox and runs its agent smoke test (self-test mode, no Gemini key); it becomes
 * ACTIVE only when the test passes. (The built-in Understudy Gemini Agent is always available and not shown here.) */
function AgenticToolBuilder({ run, onError }: { run: Run; onError: (m: string | null) => void }) {
  const [opts, setOpts] = useState<AgenticToolOptions | null>(null);
  const [driver, setDriver] = useState("playwright");
  const [browser, setBrowser] = useState("chromium");
  const [name, setName] = useState("");
  const [dv, setDv] = useState("");
  const [gv, setGv] = useState("");
  const [extra, setExtra] = useState("");
  const [model, setModel] = useState("");
  const [busy, setBusy] = useState(false);
  const [open, setOpen] = useFold("agentic-tool-builder", FOLD_DEFAULT_OPEN);
  useEffect(() => {
    let live = true;
    adminApi.agenticToolOptions().then((o) => { if (live) setOpts(o); }).catch(() => { /* older backend */ });
    return () => { live = false; };
  }, []);
  const drv = opts?.drivers.find((d) => d.id === driver);
  const pickDriver = (id: string) => {
    setDriver(id);
    const d = opts?.drivers.find((x) => x.id === id);
    if (d && !d.browsers.some((b) => b.id === browser)) setBrowser(d.browsers[0].id);
  };
  const go = async () => {
    setBusy(true); onError(null);
    try {
      const ok = await run(() => adminApi.createAgenticTool({
        driver, browser, name: name.trim(), driver_version: dv.trim(), genai_version: gv.trim(), model: model.trim(),
        extra_packages: extra.split(/[,\n]/).map((x) => x.trim()).filter(Boolean),
      }));
      if (ok) { setName(""); setDv(""); setGv(""); setExtra(""); setModel(""); }
    } finally { setBusy(false); }
  };
  if (!opts) return null;
  return (
    <div className={cx("rounded-lg border border-violet-500/30 bg-violet-500/5 p-3", open && "space-y-2")} data-testid="agentic-tool-builder">
      <button type="button" onClick={() => setOpen(!open)} aria-expanded={open}
        className="flex w-full items-center gap-2 text-left text-xs font-semibold text-violet-100 hover:text-white">
        {open ? <ChevronDown className="h-3.5 w-3.5 text-slate-400" /> : <ChevronRight className="h-3.5 w-3.5 text-slate-400" />}
        <Bot className="h-4 w-4 text-violet-300" />Agentic tool on Understudy&apos;s agent</button>
      <div hidden={!open} className="space-y-2">
        <p className="text-[11px] text-slate-400">Set up agentic tools on Understudy&apos;s own agent with the browser stack you choose - the browser library, the browser, versions and extra packages. Understudy installs it in the
          sandbox and runs its agent smoke test (no Gemini key needed); it becomes active for users when the test passes.</p>
        <div className="grid gap-2 sm:grid-cols-2">
          <label className="space-y-1 text-[11px] text-slate-400">Browser library
            <select className={input} value={driver} onChange={(e) => pickDriver(e.target.value)} data-testid="agentic-driver">
              {opts.drivers.map((d) => <option key={d.id} value={d.id}>{d.label} ({opts.language})</option>)}
            </select></label>
          <label className="space-y-1 text-[11px] text-slate-400">Browser
            <select className={input} value={browser} onChange={(e) => setBrowser(e.target.value)} data-testid="agentic-browser">
              {(drv?.browsers || []).map((b) => <option key={b.id} value={b.id}>{b.label}</option>)}
            </select></label>
          <label className="space-y-1 text-[11px] text-slate-400">{drv?.label || "Library"} version
            <input className={input} value={dv} onChange={(e) => setDv(e.target.value)} maxLength={40} placeholder={`${drv?.default_version || ""} (default) or e.g. 4.25.0`} /></label>
          <label className="space-y-1 text-[11px] text-slate-400">{opts.genai_package} version
            <input className={input} value={gv} onChange={(e) => setGv(e.target.value)} maxLength={40} placeholder=">=1.0.0 (default)" /></label>
          <label className="space-y-1 text-[11px] text-slate-400">Extra packages (pip, comma-separated)
            <input className={input} value={extra} onChange={(e) => setExtra(e.target.value)} maxLength={400} placeholder="e.g. requests==2.32.3" /></label>
          <label className="space-y-1 text-[11px] text-slate-400">Default Gemini model (optional)
            <input className={input} value={model} onChange={(e) => setModel(e.target.value)} maxLength={60} placeholder="e.g. gemini-flash-latest" /></label>
        </div>
        <div className="grid gap-2 sm:grid-cols-[1fr_auto]">
          <input className={input} value={name} onChange={(e) => setName(e.target.value)} maxLength={60} aria-label="Tool name (optional)"
            placeholder={`Name (optional) - default: Understudy Agent (${drv?.label || ""} + ${browser})`} />
          <button type="button" disabled={busy} onClick={() => { void go(); }} data-testid="agentic-create"
            className="inline-flex items-center justify-center gap-1.5 rounded-lg bg-violet-500 px-3 py-1.5 text-xs font-bold text-white hover:bg-violet-400 disabled:opacity-50">
            {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Plus className="h-3.5 w-3.5" />}Set up and smoke-test</button>
        </div>
      </div>
    </div>
  );
}

export function ToolAccessPanel({ policy, run }: { policy: AdminPolicy | null; run: Run }) {
  const [err, setErr] = useState<string | null>(null);
  const runE: Run = async (fn) => {
    try { return await run(async () => { try { return await fn(); } catch (e) { setErr((e as AdminApiError).message); throw e; } }); }
    catch { return false; }
  };
  const active = (policy?.tools.requests || []).filter((r) => !["approved", "rejected"].includes(r.status));
  const decided = (policy?.tools.requests || []).filter((r) => ["approved", "rejected"].includes(r.status)).slice(0, 6);
  const approved = policy?.tools.whitelist || [];
  const missingAny = approved.filter((w) => w.toolchain_missing?.length);
  return (
    <Card title="Tools, versions & requests" icon={<Wrench className="h-4 w-4 text-violet-400" />}
      right={<Badge tone={active.length ? "amber" : "green"}>{active.length ? `${active.length} request(s) waiting` : "no open requests"}</Badge>}>
      <div className="space-y-4">
        <p className="text-xs text-slate-400">
          Users can only automate with the tools approved here, in the versions you allow. A tool or version that is neither
          approved nor blocked reaches you as a request; the AI prepares its setup for you to review before anything is installed.
        </p>
        {err && <p className="rounded-lg border border-rose-500/40 bg-rose-500/10 p-2 text-[11px] text-rose-200" role="alert">{err}</p>}
        <AddToolWithAi run={runE} policy={policy} onError={setErr} />
        <AgenticToolBuilder run={runE} onError={setErr} />

        {active.length > 0 && (
          <div className="space-y-2">
            <div className="text-[11px] font-bold uppercase text-amber-300">Requests</div>
            {active.map((r) => <RequestCard key={r.id} r={r} run={runE} onError={setErr} />)}
          </div>
        )}

        {missingAny.length > 0 && (
          <div className="flex gap-2 rounded-lg border border-rose-500/30 bg-rose-950/20 p-2.5 text-[11px] text-rose-100">
            <AlertTriangle className="h-4 w-4 shrink-0 text-rose-400" />
            <div>{missingAny.map((w) => w.name).join(", ")} can&apos;t run on this backend yet: {Array.from(new Set(missingAny.flatMap((w) => w.toolchain_missing))).join(", ")} is
              not installed (or not on the backend&apos;s PATH). Open <b>Package managers &amp; build tools</b> and click <b>Let the AI set it up</b> -
              Understudy downloads and registers it (no restart needed).</div>
          </div>
        )}

        <div className="space-y-1.5" data-testid="approved-tools">
          <div className="flex items-center gap-1 text-[11px] font-bold uppercase text-slate-400"><Package className="h-3 w-3" />Approved tools ({approved.length})</div>
          {approved.length === 0 && <p className="text-[11px] text-slate-400">None yet - nothing is pre-built. Users can&apos;t run automations until you add a tool with the AI install assistant (or approve a request).</p>}
          {approved.map((t) => <ToolRow key={t.id} tool={t} run={runE} />)}
        </div>

        <BlockedTools policy={policy} run={runE} />

        {decided.length > 0 && (
          <div className="space-y-0.5 border-t border-slate-800 pt-2 text-[10px] text-slate-400">
            {decided.map((r) => <div key={r.id}>{r.id} · {r.tool_name}{r.version ? ` ${r.version}` : ""}: <b className={r.status === "approved" ? "text-emerald-400" : "text-rose-400"}>{r.status}</b>{r.admin_note ? ` - ${r.admin_note}` : ""}</div>)}
          </div>
        )}
      </div>
    </Card>
  );
}
