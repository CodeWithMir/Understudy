"use client";

/* Mission Control 8.2 - the AI install assistant and the smoke-test review.
 *
 *   StackPicker        the AI suggested the stack for a tool: the admin picks the language, ticks the package managers
 *                      the server has, chooses the components (core + companion frameworks such as TestNG, Cucumber,
 *                      JUnit, pytest) and a version for each - the versions are the real lists from Maven Central /
 *                      PyPI / npm, the AI's recommendation pre-selected. "Prepare the setup plan" hands it to the AI.
 *   SmokeReviewPanel   after the install the AI writes the smoke test; nothing runs until the admin approves the code
 *                      or tells the AI what to change (it rewrites it and it comes back for review).
 *
 * Nothing is pre-built: every framework - Playwright included - goes through this assistant.
 */
import React, { useEffect, useMemo, useState } from "react";
import { Bot, Check, CheckCircle2, Code2, Cpu, FlaskConical, Layers, Loader2, Lock, RefreshCw, Send, Sparkles, XCircle } from "lucide-react";
import { adminApi, type AdminPolicy, type StackComponent, type StackVersions, type ToolRequest } from "../lib/admin";
import { Badge, cx } from "./wizard/ui";

type Run = (fn: () => Promise<AdminPolicy>) => Promise<boolean>;
const input = "w-full text-xs bg-slate-950 border border-slate-700 rounded-lg px-3 py-2 text-white placeholder:text-slate-400 focus:outline-none focus:border-sky-500";
const ROLE_LABEL: Record<string, string> = {
  core: "core", test_runner: "test runner", bdd: "BDD", assertions: "assertions", driver_manager: "drivers", reporting: "reporting", other: "add-on",
};

function Step({ n, title, children }: { n: number; title: string; children: React.ReactNode }) {
  return (
    <section className="space-y-1.5">
      <h5 className="flex items-center gap-2 text-[11px] font-bold uppercase tracking-wide text-slate-300">
        <span className="inline-flex h-4 w-4 items-center justify-center rounded-full bg-violet-500/30 text-[10px] text-violet-100">{n}</span>{title}
      </h5>
      {children}
    </section>
  );
}

function pkgFor(c: StackComponent, lang: string, pm: string, eco: string) {
  return c.packages.find((p) => p.language === lang && p.package_manager === pm)
    || c.packages.find((p) => p.language === lang && ecoOf(p.package_manager) === eco)
    || null;
}
const ECO: Record<string, string> = { pip: "pip", pipenv: "pip", poetry: "pip", npm: "npm", npx: "npm", yarn: "npm", pnpm: "npm",
  maven: "maven", mvn: "maven", gradle: "gradle", gem: "gem", bundler: "gem", go: "go", nuget: "nuget", dotnet: "nuget" };
function ecoOf(pm: string) { return ECO[pm] || ""; }

/** Step 1 of the assistant: the admin chooses the stack the AI suggested. */
export function StackPicker({ r, run, onError, onCancel }: { r: ToolRequest; run: Run; onError: (m: string | null) => void; onCancel?: () => void }) {
  const sug = r.stack?.suggestion;
  const prev = r.stack?.selection;
  const langs = useMemo(() => sug?.languages || [], [sug]);
  const initialLang = prev?.language || langs.find((l) => l.recommended)?.id || langs[0]?.id || "";
  const [lang, setLang] = useState(initialLang);
  const pmsForLang = useMemo(() => (sug?.package_managers || []).filter((p) => p.language === lang || !p.language), [sug, lang]);
  const [available, setAvailable] = useState<string[] | null>(null);
  const [pm, setPm] = useState<string>("");
  const [comps, setComps] = useState<string[] | null>(null);
  const [versions, setVersions] = useState<Record<string, string>>({});
  const [vinfo, setVinfo] = useState<StackVersions | null>(null);
  const [loadingV, setLoadingV] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [note, setNote] = useState("");

  // defaults whenever the language changes: package managers the server has, the first supported one, default components
  const avail = available ?? pmsForLang.filter((p) => p.supported && p.available).map((p) => p.id);
  const installable = pmsForLang.filter((p) => p.supported && avail.includes(p.id));
  const chosenPm = installable.some((p) => p.id === pm) ? pm : (prev?.language === lang && installable.some((p) => p.id === prev?.package_manager) ? prev!.package_manager : installable[0]?.id || "");
  const eco = ecoOf(chosenPm);
  const forLang = (sug?.components || []).filter((c) => pkgFor(c, lang, chosenPm, eco));
  const selected = comps ?? (prev?.language === lang ? prev.components : forLang.filter((c) => c.default_selected || c.required).map((c) => c.id));

  useEffect(() => {
    if (!lang || !chosenPm) return;
    let stop = false;
    const t = setTimeout(() => {
      setLoadingV(true);
      adminApi.stackVersions(r.id, lang, chosenPm).then((v) => { if (!stop) setVinfo(v); })
        .catch((e) => { if (!stop) onError((e as Error).message); })
        .finally(() => { if (!stop) setLoadingV(false); });
    }, 0);
    return () => { stop = true; clearTimeout(t); };
  }, [r.id, lang, chosenPm, onError]);

  if (!sug) return null;
  const toggle = (c: StackComponent) => {
    if (c.required) return;
    const on = selected.includes(c.id);
    let next = on ? selected.filter((x) => x !== c.id) : [...selected, c.id];
    if (!on) {                       // picking an alternative (e.g. TestNG) unticks the other one (JUnit)
      const clash = new Set([...c.conflicts_with, ...sug.components.filter((o) => o.conflicts_with.includes(c.id)).map((o) => o.id)]);
      next = next.filter((x) => x === c.id || !clash.has(x));
    }
    setComps(next);
  };
  const versionOf = (cid: string) => versions[cid] ?? (prev?.language === lang ? prev.versions[cid] : undefined) ?? vinfo?.components[cid]?.recommended ?? "";
  const submit = async () => {
    setBusy("select"); onError(null);
    try {
      await run(() => adminApi.selectStack(r.id, {
        language: lang, package_manager: chosenPm, package_managers_available: avail, components: selected,
        versions: Object.fromEntries(selected.map((cid) => [cid, versionOf(cid)]).filter(([, v]) => v)),
      }));
    } finally { setBusy(null); }
  };
  const again = async () => {
    setBusy("again"); onError(null);
    try { if (await run(() => adminApi.suggestStack(r.id, note.trim() || undefined))) { setNote(""); setComps(null); setVersions({}); setAvailable(null); } }
    finally { setBusy(null); }
  };

  return (
    <div className="space-y-3 rounded-lg border border-violet-500/40 bg-violet-500/5 p-3" data-testid="stack-picker">
      <div className="flex items-start gap-2 text-xs text-slate-200">
        <Bot className="mt-0.5 h-4 w-4 shrink-0 text-violet-300" />
        <div><b>AI install assistant - {sug.tool_family || r.tool_name}</b>{" "}
          <span className="text-[10px] text-slate-400">({sug.agent_source === "ACTUAL_AI_API" ? `AI · ${sug.model}` : sug.agent_source === "LEARNED_SKILL" ? "learned skill" : sug.agent_source})</span>
          <p className="mt-0.5 text-slate-300">{sug.summary}</p>
          {sug.os_requirements.length > 0 && <p className="mt-1 text-[11px] text-amber-200">Server needs: {sug.os_requirements.join(" · ")}</p>}
          {sug.questions.length > 0 && <ul className="mt-1 list-disc pl-4 text-[11px] text-sky-200">{sug.questions.map((q, i) => <li key={i}>{q}</li>)}</ul>}
          {sug.notes?.map((n, i) => <p key={i} className="text-[11px] text-amber-200">⚠ {n}</p>)}
        </div>
      </div>

      <Step n={1} title="Language">
        <div className="flex flex-wrap gap-1.5" role="radiogroup" aria-label="Language">
          {langs.map((l) => (
            <button key={l.id} type="button" role="radio" aria-checked={lang === l.id}
              onClick={() => { setLang(l.id); setPm(""); setComps(null); setVersions({}); setAvailable(null); }}
              className={cx("rounded-lg border px-3 py-1.5 text-xs", lang === l.id ? "border-violet-400 bg-violet-500/15 text-white" : "border-slate-700 text-slate-300 hover:text-white")}
              title={l.notes || undefined}>
              {l.name}{l.recommended && <span className="ml-1 text-[10px] text-amber-300">★</span>}
            </button>
          ))}
        </div>
      </Step>

      <Step n={2} title="Package managers on your server">
        <p className="text-[11px] text-slate-400">Tick the ones your Understudy server has (pre-ticked: found on this server). Understudy installs with the one you pick.
          Locked ones are switched off or not supported - see <b>Package managers &amp; build tools</b> on this page.</p>
        <div className="flex flex-wrap gap-1.5">
          {pmsForLang.map((p) => {
            const on = avail.includes(p.id);
            return (
              <label key={p.id + p.language} className={cx("inline-flex cursor-pointer items-center gap-1.5 rounded-lg border px-2.5 py-1 text-[11px]",
                !p.supported ? "border-slate-800 text-slate-500" : on ? "border-sky-500/60 bg-sky-500/10 text-sky-100" : "border-slate-700 text-slate-300")}
                title={p.server}>
                <input type="checkbox" className="accent-sky-500" checked={on} disabled={!p.supported}
                  onChange={() => setAvailable(on ? avail.filter((x) => x !== p.id) : [...avail, p.id])} />
                {p.name}
                {!p.supported ? <Lock className="h-3 w-3" /> : p.available ? <CheckCircle2 className="h-3 w-3 text-emerald-400" /> : <XCircle className="h-3 w-3 text-amber-400" />}
                {p.disabled_by_admin && <span className="text-[10px]">off - Package managers</span>}
                {!p.supported && !p.disabled_by_admin && <span className="text-[10px]">not supported yet</span>}
              </label>
            );
          })}
        </div>
        {installable.length > 1 && (
          <div className="flex flex-wrap items-center gap-1.5 text-[11px] text-slate-400">Install with:
            {installable.map((p) => (
              <button key={p.id} type="button" onClick={() => { setPm(p.id); setComps(null); setVersions({}); }}
                className={cx("rounded border px-2 py-0.5", chosenPm === p.id ? "border-sky-400 text-sky-100" : "border-slate-700 text-slate-300")}>{p.name}</button>
            ))}
          </div>
        )}
        {!chosenPm && <p className="text-[11px] text-rose-300">Tick a package manager Understudy can install with for this language.</p>}
      </Step>

      <Step n={3} title="Components and versions">
        {loadingV && <p className="flex items-center gap-1 text-[11px] text-slate-400"><Loader2 className="h-3 w-3 animate-spin" />Looking up the real versions in the package registry...</p>}
        <ul className="space-y-1.5" aria-label="Components">
          {forLang.map((c) => {
            const on = selected.includes(c.id);
            const vi = vinfo?.components[c.id];
            const p = pkgFor(c, lang, chosenPm, eco);
            const list = Array.from(new Set([versionOf(c.id), ...(vi?.versions || [])].filter(Boolean)));
            return (
              <li key={c.id} className={cx("flex flex-wrap items-center gap-2 rounded-lg border px-2.5 py-1.5 text-xs",
                on ? "border-violet-500/50 bg-slate-950/60" : "border-slate-800 bg-slate-950/30")}>
                <label className="inline-flex min-w-0 flex-1 cursor-pointer items-start gap-2">
                  <input type="checkbox" className="mt-0.5 accent-violet-500" checked={on} disabled={c.required} onChange={() => toggle(c)}
                    aria-label={`Install ${c.name}`} />
                  <span className="min-w-0">
                    <span className="font-semibold text-slate-100">{c.name}</span>{" "}
                    <Badge tone={c.role === "core" ? "green" : "slate"}>{ROLE_LABEL[c.role] || c.role}</Badge>
                    {c.required && <Badge tone="slate">required</Badge>}
                    <span className="block text-[11px] text-slate-400">{c.purpose}{c.purpose && p ? " · " : ""}{p ? <code className="text-slate-300">{p.name}</code> : null}</span>
                    {c.conflicts_with.length > 0 && <span className="block text-[10px] text-slate-500">alternative to {c.conflicts_with.join(", ")}</span>}
                  </span>
                </label>
                {on && (
                  <select value={versionOf(c.id)} onChange={(e) => setVersions({ ...versions, [c.id]: e.target.value })}
                    aria-label={`${c.name} version`} className="rounded border border-slate-700 bg-slate-950 px-2 py-1 font-mono text-[11px] text-slate-100">
                    {list.length === 0 && <option value="">latest</option>}
                    {list.map((v) => <option key={v} value={v}>{v}{v === vi?.recommended ? " (AI)" : v === vi?.latest ? " (latest)" : ""}</option>)}
                  </select>
                )}
                {on && vi?.error && <span className="text-[10px] text-amber-300" title={vi.error}>registry offline - AI&apos;s version</span>}
              </li>
            );
          })}
          {forLang.length === 0 && <li className="text-[11px] text-slate-400">The AI listed no package for this language and package manager.</li>}
        </ul>
        <button type="button" onClick={() => { setLoadingV(true); adminApi.stackVersions(r.id, lang, chosenPm, true).then(setVinfo).catch((e) => onError((e as Error).message)).finally(() => setLoadingV(false)); }}
          disabled={!chosenPm || loadingV} className="inline-flex items-center gap-1 text-[10px] text-slate-400 hover:text-white disabled:opacity-50">
          <RefreshCw className="h-3 w-3" />Refresh versions</button>
      </Step>

      <div className="flex flex-wrap items-center gap-2 border-t border-slate-800 pt-2">
        <button type="button" disabled={busy !== null || !chosenPm || selected.length === 0} onClick={() => { void submit(); }} data-testid="stack-select"
          className="inline-flex items-center gap-1.5 rounded-lg bg-violet-500 px-3 py-1.5 text-xs font-bold text-white hover:bg-violet-400 disabled:opacity-50">
          {busy === "select" ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Layers className="h-3.5 w-3.5" />}Prepare the setup plan</button>
        {onCancel && <button type="button" onClick={onCancel} className="rounded-lg border border-slate-700 px-3 py-1.5 text-xs text-slate-300 hover:text-white">Keep the current plan</button>}
        <div className="flex min-w-[240px] flex-1 gap-2">
          <input className={input} value={note} onChange={(e) => setNote(e.target.value)} maxLength={1000} aria-label="Note for the AI"
            placeholder="Not right? Tell the AI, e.g. 'we use JUnit 5, not TestNG' or 'add Allure reporting'" />
          <button type="button" disabled={busy !== null} onClick={() => { void again(); }}
            className="inline-flex shrink-0 items-center gap-1 rounded-lg border border-violet-500/60 px-2.5 py-1.5 text-[11px] text-violet-100 hover:bg-violet-500/10 disabled:opacity-50">
            {busy === "again" ? <Loader2 className="h-3 w-3 animate-spin" /> : <Sparkles className="h-3 w-3" />}Ask again</button>
        </div>
      </div>
    </div>
  );
}

/** The AI-written smoke test, waiting for the admin's review. */
export function SmokeReviewPanel({ r, run, onError }: { r: ToolRequest; run: Run; onError: (m: string | null) => void }) {
  const review = r.setup?.smoke_review;
  const files = useMemo(() => Object.keys(review?.files || {}).sort((a, b) => (a.endsWith(".xml") ? 1 : 0) - (b.endsWith(".xml") ? 1 : 0)), [review]);
  const [file, setFile] = useState<string | null>(null);
  const [fb, setFb] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  if (!review) return null;
  const shown = file && review.files[file] !== undefined ? file : files[0];
  const rewriting = !!r.setup?.rewriting;
  const act = async (key: string, fn: () => Promise<AdminPolicy>) => {
    setBusy(key); onError(null);
    try { if (await run(fn)) setFb(""); } finally { setBusy(null); }
  };
  return (
    <div className="space-y-2 rounded-lg border border-amber-500/40 bg-amber-500/5 p-3" data-testid="smoke-review">
      <div className="flex items-start gap-2 text-xs text-amber-100">
        <FlaskConical className="mt-0.5 h-4 w-4 shrink-0 text-amber-300" />
        <div><b>Review the smoke test the AI wrote</b>{review.revision ? <span className="text-[10px] text-slate-400"> · revision {review.revision}</span> : null}
          <p className="mt-0.5 text-slate-300">It runs in the {r.setup?.sandbox?.kind || "local"} sandbox against a local dummy page (type, click, assert) and must
            exit 0 before {r.tool_name} becomes ACTIVE. Nothing runs until you approve it.{r.setup?.java?.release ? ` pom.xml targets Java ${r.setup.java.release} (the JDK Maven uses here: ${r.setup.java.major ?? "?"}).` : ""}</p>
          {review.ai_notes && <p className="mt-1 text-[11px] text-violet-200"><Bot className="mr-1 inline h-3 w-3" />{review.ai_notes}</p>}
          {review.notes?.map((n, i) => <p key={i} className="text-[11px] text-amber-200">⚠ {n}</p>)}
          {review.rewrite_error && <p className="text-[11px] text-rose-300" role="alert">{review.rewrite_error}</p>}
        </div>
      </div>
      <div className="rounded-lg border border-slate-800 bg-slate-950/70">
        <div className="flex flex-wrap gap-1 border-b border-slate-800 px-2 py-1">
          {files.map((f) => (
            <button key={f} type="button" onClick={() => setFile(f)}
              className={cx("inline-flex items-center gap-1 rounded px-1.5 py-0.5 font-mono text-[10px]", shown === f ? "bg-slate-800 text-white" : "text-slate-400 hover:text-slate-200")}>
              <Code2 className="h-3 w-3" />{f}{f === "pom.xml" ? " (Understudy)" : ""}</button>
          ))}
        </div>
        <pre className="max-h-80 overflow-auto p-2 font-mono text-[11px] leading-relaxed text-slate-200" aria-label={`Smoke test file ${shown}`}>{shown ? review.files[shown] : ""}</pre>
      </div>
      {review.feedback?.length > 0 && (
        <ul className="space-y-0.5 text-[11px] text-slate-400">{review.feedback.map((x, i) => <li key={i}>Your comment {i + 1}: <span className="text-slate-200">“{x}”</span></li>)}</ul>
      )}
      <div className="flex flex-wrap gap-2">
        <button type="button" disabled={busy !== null || rewriting} onClick={() => { void act("approve", () => adminApi.approveSmoke(r.id)); }} data-testid="smoke-approve"
          className="inline-flex items-center gap-1.5 rounded-lg bg-emerald-500 px-3 py-1.5 text-xs font-bold text-slate-950 hover:bg-emerald-400 disabled:opacity-50">
          {busy === "approve" ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Check className="h-3.5 w-3.5" />}Approve &amp; run smoke test</button>
        <div className="flex min-w-[260px] flex-1 gap-2">
          <input className={input} value={fb} onChange={(e) => setFb(e.target.value)} maxLength={2000} aria-label="What should the AI change in the smoke test?"
            onKeyDown={(e) => { if (e.key === "Enter" && fb.trim()) void act("revise", () => adminApi.reviseSmoke(r.id, fb.trim())); }}
            placeholder="Or tell the AI what to change, e.g. 'use a TestNG @Test with assertEquals' or 'add a Cucumber feature file'" />
          <button type="button" disabled={!fb.trim() || busy !== null || rewriting} onClick={() => { void act("revise", () => adminApi.reviseSmoke(r.id, fb.trim())); }}
            className="inline-flex shrink-0 items-center gap-1.5 rounded-lg border border-violet-500/60 px-3 py-1.5 text-xs text-violet-100 hover:bg-violet-500/10 disabled:opacity-50">
            {busy === "revise" || rewriting ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Send className="h-3.5 w-3.5" />}{rewriting ? "AI is rewriting..." : "Ask the AI to rewrite"}</button>
        </div>
      </div>
    </div>
  );
}

/** Compact summary of the stack the admin chose (shown on the plan and after). */
export function StackSummary({ r }: { r: ToolRequest }) {
  const sel = r.stack?.selection;
  if (!sel) return null;
  return (
    <div className="flex flex-wrap items-center gap-1.5 text-[11px] text-slate-300" data-testid="stack-summary">
      <Cpu className="h-3.5 w-3.5 text-violet-300" /><b>Stack:</b>
      <Badge tone="violet">{sel.language}</Badge><Badge tone="sky">{sel.package_manager}</Badge>
      {sel.used.map((u) => <span key={u.id} className="rounded border border-slate-700 px-1.5 py-0.5 font-mono text-[10px]">{u.name} {u.version || "latest"}</span>)}
    </div>
  );
}
