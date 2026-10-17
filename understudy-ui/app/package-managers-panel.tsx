"use client";

/* Mission Control 8.2 - Package managers & build tools.
 * The admin decides which package managers the AI install assistant may offer (pip, npm/npx, Maven, Gradle, ...),
 * sees whether the server has what each one needs, downloads a missing build tool or runtime (Gradle, Maven, Node.js,
 * a Temurin JDK - checksum verified, registered for every install and run) or points Understudy at an existing install.
 * 8.3: nothing has to be installed by hand any more - "Let the AI set it up" asks the runtime advisor which official,
 * checksum-verified build provides the missing programs on this OS (Ruby + RubyGems, Go, .NET, ...), Understudy
 * downloads and registers it, starts every program once, and a failure comes back with the AI's diagnosis.
 */
import React, { useCallback, useEffect, useState } from "react";
import { Bot, CheckCircle2, Download, FolderOpen, Hammer, Loader2, Package, RefreshCw, Sparkles, Trash2, XCircle } from "lucide-react";
import { adminApi, withEolConfirm, type PackageManagerRow, type PackageManagersState, type RuntimeAdvice, type ToolchainJob } from "../lib/admin";
import { Badge, Card, cx } from "./wizard/ui";

const input = "w-full text-xs bg-slate-950 border border-slate-700 rounded-lg px-3 py-2 text-white placeholder:text-slate-400 focus:outline-none focus:border-sky-500";
const KINDS = [{ id: "gradle", label: "Gradle" }, { id: "maven", label: "Maven" }, { id: "jdk", label: "Java (JDK)" }, { id: "node", label: "Node.js" },
  { id: "ruby", label: "Ruby" }, { id: "go", label: "Go" }, { id: "dotnet", label: ".NET SDK" }];

function keyLabel(a: RuntimeAdvice, k: string): string {
  return a.labels?.[k] || k;
}

/** 8.3: the AI's plan for a missing runtime - what it downloads, from where, and what it checks afterwards. */
function AdviceCard({ advice, busy, running, onInstall, onClose }: {
  advice: RuntimeAdvice; busy: boolean; running: boolean; onInstall: (key: string) => void; onClose: () => void;
}) {
  const keys = [advice.recommended, ...advice.alternatives].filter((k) => k && k !== "manual");
  return (
    <div className="mt-2 space-y-1.5 rounded-lg border border-violet-500/40 bg-violet-500/5 p-2 text-[11px] text-slate-200" data-testid="runtime-advice">
      <div className="flex items-center gap-1.5 font-semibold text-violet-100">
        <Bot className="h-3.5 w-3.5 text-violet-300" />{advice.diagnosis ? "The AI looked at the failure" : "The AI's install plan"}
        <Badge tone={advice.agent_source === "ACTUAL_AI_API" ? "green" : "amber"}>{advice.agent_source === "ACTUAL_AI_API" ? (advice.model || "AI") : "built-in mapping"}</Badge>
        <button type="button" onClick={onClose} className="ml-auto text-slate-400 hover:text-white" aria-label="Close the plan">×</button>
      </div>
      {advice.diagnosis && <p className="text-rose-200">{advice.diagnosis}</p>}
      <p>{advice.summary}</p>
      {advice.steps?.length > 0 && <ol className="list-decimal space-y-0.5 pl-4 text-slate-300">{advice.steps.map((x, i) => <li key={i}>{x}</li>)}</ol>}
      {advice.expected_programs?.length > 0 && <p className="text-slate-400">Checked afterwards: <span className="font-mono">{advice.expected_programs.join(", ")}</span></p>}
      <div className="flex flex-wrap gap-1.5 pt-0.5">
        {keys.map((k, i) => (
          <button key={k} type="button" disabled={busy || running} onClick={() => onInstall(k)}
            className={cx("inline-flex items-center gap-1 rounded-lg px-2.5 py-1 text-[11px] font-semibold disabled:opacity-50",
              i === 0 ? "bg-violet-500 text-white hover:bg-violet-400" : "border border-violet-500/50 text-violet-100 hover:bg-violet-500/10")}>
            {running || busy ? <Loader2 className="h-3 w-3 animate-spin" /> : <Download className="h-3 w-3" />}
            {i === 0 ? "Download & register " : "Or: "}{keyLabel(advice, k)}</button>
        ))}
      </div>
      {advice.recommended === "manual" && <p className="text-amber-200">Only you can fix this on the server - see below, then click refresh.</p>}
      {advice.manual && <p className="text-slate-400"><b>Prefer to install it yourself?</b> {advice.manual} Then register the folder below.</p>}
    </div>
  );
}

function JobLine({ j }: { j: ToolchainJob }) {
  const checked = Object.entries(j.verified || {});
  return (
    <>
      <div className="flex items-center gap-1.5">{j.status === "running" ? <Loader2 className="h-3 w-3 animate-spin" /> : j.status === "failed" ? <XCircle className="h-3 w-3" /> : <CheckCircle2 className="h-3 w-3" />}
        <b>{j.label || j.key}</b> {j.status === "running" ? "downloading..." : j.status === "failed" ? `failed: ${j.error}` : `ready${j.version ? ` (${j.version})` : ""}`}</div>
      {j.status === "running" && j.log.length > 0 && <p className="mt-0.5 font-mono text-[10px] text-slate-400">{j.log[j.log.length - 1]}</p>}
      {checked.length > 0 && (
        <p className="mt-0.5 font-mono text-[10px]">{checked.map(([n, v]) => (
          <span key={n} className={cx("mr-2", v.found ? "text-emerald-300" : "text-rose-300")}>{n} {v.found ? (v.version || "ok") : "missing"}</span>))}</p>
      )}
    </>
  );
}

function Switch({ on, disabled, onChange, label }: { on: boolean; disabled?: boolean; onChange: () => void; label: string }) {
  return (
    <button type="button" role="switch" aria-checked={on} aria-label={label} disabled={disabled} onClick={onChange}
      className={cx("relative h-5 w-9 shrink-0 rounded-full transition-colors disabled:opacity-50", on ? "bg-emerald-500" : "bg-slate-600")}>
      <span className={cx("absolute top-0.5 h-4 w-4 rounded-full bg-white shadow transition-all", on ? "left-[18px]" : "left-0.5")} />
    </button>
  );
}

function Row({ r, st, busy, act, onError }: { r: PackageManagerRow; st: PackageManagersState; busy: string | null;
  act: (key: string, fn: () => Promise<PackageManagersState | null>) => void; onError: (m: string) => void }) {
  const running = r.downloads.find((d) => st.jobs[d.key]?.status === "running")
    || Object.values(st.jobs).find((j) => j.status === "running" && r.missing.length > 0 && j.advice);
  const [advice, setAdvice] = useState<RuntimeAdvice | null>(null);
  const [asking, setAsking] = useState(false);
  const failed = Object.values(st.jobs).find((j) => j.status === "failed" && j.diagnosis && (r.downloads.some((d) => d.key === j.key)
    || (advice && [advice.recommended, ...advice.alternatives].includes(j.key))));
  const ask = async () => {
    setAsking(true);
    try { setAdvice((await adminApi.adviseToolchain(r.missing, `${r.name} (${r.languages.join(", ")} package manager) needs it`)).advice); }
    catch (e) { onError((e as Error).message); } finally { setAsking(false); }
  };
  const install = (key: string, a: RuntimeAdvice | null) =>
    act(`d-${key}`, () => withEolConfirm((ok) => adminApi.downloadToolchain(key, a?.expected_programs, a?.id, ok)));
  return (
    <li className={cx("rounded-lg border px-2.5 py-2 text-xs", r.enabled ? "border-slate-800 bg-slate-950/60" : "border-slate-800/60 bg-slate-950/20 opacity-75")}
      data-testid={`pm-${r.id}`}>
      <div className="flex flex-wrap items-center gap-2">
        <Switch on={r.enabled} disabled={busy !== null} label={`Allow ${r.name}`} onChange={() => act(`t-${r.id}`, () => adminApi.setPackageManager(r.id, !r.enabled))} />
        <b className="text-slate-100">{r.name}</b>
        <Badge tone={r.kind === "build_tool" ? "violet" : "slate"}>{r.kind === "build_tool" ? "build tool" : "package manager"}</Badge>
        <span className="text-[10px] text-slate-400">{r.languages.join(", ")}</span>
        <span className="ml-auto">{r.ready ? <Badge tone="green"><CheckCircle2 className="h-3 w-3" />ready on this server</Badge>
          : <Badge tone="amber"><XCircle className="h-3 w-3" />missing {r.missing.join(", ")}</Badge>}</span>
      </div>
      <p className="mt-1 text-[11px] text-slate-400">{r.note}</p>
      <div className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 font-mono text-[10px] text-slate-400">
        {r.needs.map((n) => (
          <span key={n.name} className={n.found ? "text-slate-300" : "text-amber-300"} title={n.path || undefined}>
            {n.name} {n.found ? (n.version || "found") : "not found"}
          </span>
        ))}
      </div>
      {!r.ready && r.enabled && (
        <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
          <button type="button" disabled={asking || busy !== null || !!running} onClick={() => { void ask(); }} data-testid={`pm-ai-${r.id}`}
            className="inline-flex items-center gap-1 rounded-lg bg-violet-500/90 px-2.5 py-1 text-[10px] font-semibold text-white hover:bg-violet-400 disabled:opacity-50">
            {asking ? <Loader2 className="h-3 w-3 animate-spin" /> : <Sparkles className="h-3 w-3" />}Let the AI set up {r.missing.join(" + ")}</button>
          {r.downloads.filter((d) => !d.registered).length > 0 && <span className="text-[10px] text-slate-400">or download directly:</span>}
          {r.downloads.filter((d) => !d.registered).map((d) => (
            <button key={d.key} type="button" disabled={busy !== null || !!running} onClick={() => install(d.key, null)}
              className="inline-flex items-center gap-1 rounded border border-sky-500/50 px-2 py-0.5 text-[10px] text-sky-100 hover:bg-sky-500/10 disabled:opacity-50">
              {st.jobs[d.key]?.status === "running" || busy === `d-${d.key}` ? <Loader2 className="h-3 w-3 animate-spin" /> : <Download className="h-3 w-3" />}{d.label}</button>
          ))}
        </div>
      )}
      {advice && !r.ready && <AdviceCard advice={advice} busy={busy !== null} running={!!running} onClose={() => setAdvice(null)}
        onInstall={(k) => install(k, advice)} />}
      {failed?.diagnosis && !r.ready && !running && <AdviceCard advice={failed.diagnosis} busy={busy !== null} running={false}
        onClose={() => setAdvice(null)} onInstall={(k) => install(k, failed.diagnosis || null)} />}
    </li>
  );
}

export function PackageManagersPanel() {
  const [st, setSt] = useState<PackageManagersState | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [kind, setKind] = useState("gradle");
  const [path, setPath] = useState("");
  const load = useCallback(() => {
    adminApi.packageManagers().then(setSt).catch((e) => setMsg({ ok: false, text: (e as Error).message }));
  }, []);
  useEffect(() => { const t = setTimeout(load, 0); return () => clearTimeout(t); }, [load]);
  const running = st ? Object.values(st.jobs).some((j) => j.status === "running") : false;
  useEffect(() => {                          // follow a download until it finishes
    if (!running) return;
    const t = setInterval(load, 2500);
    return () => clearInterval(t);
  }, [running, load]);
  const act = (key: string, fn: () => Promise<PackageManagersState | null>) => {
    setBusy(key); setMsg(null);
    fn().then((s) => { if (s) setSt(s); if (key.startsWith("p-")) setPath(""); })
      .catch((e) => setMsg({ ok: false, text: (e as Error).message })).finally(() => setBusy(null));
  };
  const jobs = st ? Object.values(st.jobs) : [];
  return (
    <Card title="Package managers & build tools" icon={<Hammer className="h-4 w-4 text-violet-400" />}
      right={<button type="button" onClick={load} className="rounded p-1 text-slate-400 hover:text-white" title="Check the server again"><RefreshCw className="h-3.5 w-3.5" /></button>}>
      <div className="space-y-3" data-testid="package-managers">
        <p className="text-[11px] text-slate-400">The AI install assistant only offers the package managers you allow here. Switching one off
          affects new tool setups (active tools keep working). A build tool or runtime the server lacks can be downloaded
          (checksum verified) or registered from a folder - registered toolchains are used for every install, smoke test and run.
          Missing Ruby, Go, .NET, Java or Node.js? <b className="text-violet-200">Let the AI set it up</b> - it picks the official build for this OS.</p>
        {st?.java_for_builds && (
          <p className="text-[11px] text-slate-300">Java for Maven / Gradle builds: <b>Java {st.java_for_builds.major}</b>
            <span className="font-mono text-slate-400"> {st.java_for_builds.home}</span> ({st.java_for_builds.source})</p>
        )}
        {msg && <p className={cx("text-[11px]", msg.ok ? "text-emerald-300" : "text-rose-300")} role="alert">{msg.text}</p>}
        {jobs.slice(-3).map((j) => (
          <div key={j.key} className={cx("rounded-lg border p-2 text-[11px]", j.status === "failed" ? "border-rose-500/40 text-rose-200" : j.status === "running" ? "border-sky-500/40 text-sky-100" : "border-emerald-500/40 text-emerald-200")}>
            <JobLine j={j} />
          </div>
        ))}
        {!st && <p className="flex items-center gap-1 text-[11px] text-slate-400"><Loader2 className="h-3 w-3 animate-spin" />Checking the server...</p>}
        {st && (
          <ul className="space-y-1.5">
            {st.package_managers.map((r) => <Row key={r.id} r={r} st={st} busy={busy} act={act} onError={(m) => setMsg({ ok: false, text: m })} />)}
          </ul>
        )}

        <div className="space-y-1.5 border-t border-slate-800 pt-2">
          <div className="flex items-center gap-1 text-[11px] font-bold uppercase text-slate-400"><Package className="h-3 w-3" />Registered toolchains</div>
          {st?.toolchains.length === 0 && <p className="text-[11px] text-slate-500">None - Understudy uses what is on the server&apos;s PATH.</p>}
          {st?.toolchains.map((t) => (
            <div key={t.key} className="flex flex-wrap items-center gap-2 rounded-lg border border-slate-800 bg-slate-950/60 px-2.5 py-1.5 text-[11px]">
              <b className="text-slate-100">{t.kind}</b>{t.version && <span className="font-mono text-slate-300">{t.version}</span>}
              <Badge tone={t.source === "download" ? "sky" : "slate"}>{t.source === "download" ? "downloaded" : "your folder"}</Badge>
              <span className="truncate font-mono text-[10px] text-slate-400" title={t.home}>{t.home}</span>
              {!t.exists && <Badge tone="red">folder missing</Badge>}
              <button type="button" aria-label={`Remove ${t.kind}`} disabled={busy !== null} onClick={() => act(`r-${t.key}`, () => adminApi.removeToolchain(t.key))}
                className="ml-auto rounded p-1 text-slate-400 hover:bg-slate-800 hover:text-rose-300"><Trash2 className="h-3.5 w-3.5" /></button>
            </div>
          ))}
          <div className="grid gap-2 sm:grid-cols-[130px_1fr_auto]">
            <select value={kind} onChange={(e) => setKind(e.target.value)} aria-label="Toolchain" className={input}>
              {KINDS.map((k) => <option key={k.id} value={k.id}>{k.label}</option>)}
            </select>
            <input className={input} value={path} onChange={(e) => setPath(e.target.value)} maxLength={500} aria-label="Folder of an existing install"
              placeholder="Existing install on the server, e.g. C:\Ruby33-x64, /opt/gradle-8.10 or C:\Tools\gradle-8.10" />
            <button type="button" disabled={!path.trim() || busy !== null} onClick={() => act("p-reg", () => adminApi.registerToolchain(kind, path.trim()))}
              className="inline-flex items-center justify-center gap-1 rounded-lg border border-violet-500/60 px-3 py-1.5 text-xs text-violet-100 hover:bg-violet-500/10 disabled:opacity-50">
              {busy === "p-reg" ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <FolderOpen className="h-3.5 w-3.5" />}Use this folder</button>
          </div>
        </div>
      </div>
    </Card>
  );
}
