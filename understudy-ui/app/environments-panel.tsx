"use client";

/* Mission Control 8.4 - Environments.
 * What the admin needs to decide which runtime version a tool should get:
 *  - Server runtimes: every installed Java (JDK), Python, Node.js, Ruby, Go, .NET SDK, Maven and Gradle version on the
 *    server, with its path and where it came from (installed, downloaded by Understudy, registered), which one is the
 *    default, which tools use it - and checksum-verified downloads that install another version side by side.
 *  - Tools: for every approved tool the version of each runtime its runs use, what it needs (Understudy's rule, the AI
 *    stack assistant's requirement or the admin's own), the versions its smoke test passed with, a dropdown to switch to
 *    any other installed version (no paths to type) and a one-click fix when a requirement isn't met.
 */
import React, { useCallback, useEffect, useMemo, useState } from "react";
import { AlertTriangle, CheckCircle2, Cpu, Download, Globe, Layers, Loader2, Pencil, RefreshCw, Server, XCircle } from "lucide-react";
import { adminApi, withEolConfirm, type EnvironmentsState, type RuntimeMirror, type RuntimeFix, type RuntimeInstall, type ToolEnvironment, type ToolRuntimeRow } from "../lib/admin";
import { Badge, Button, Card, cx } from "./wizard/ui";

const input = "w-full text-xs bg-slate-950 border border-slate-700 rounded-lg px-2 py-1.5 text-white placeholder:text-slate-500 focus:outline-none focus:border-sky-500";
const SOURCE_TONE: Record<string, "slate" | "violet" | "sky" | "green"> = { backend: "violet", downloaded: "sky", registered: "green", system: "slate" };
const REQ_SOURCE: Record<string, string> = { rule: "Understudy rule", ai: "AI stack assistant", admin: "you" };

function msg(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

function InstallRow({ r, users }: { r: RuntimeInstall; users: string[] }) {
  return (
    <li className="rounded-lg border border-slate-800 bg-slate-950/50 px-2.5 py-1.5" data-testid="runtime-install">
      <div className="flex flex-wrap items-center gap-1.5">
        <span className="font-mono text-xs font-semibold text-white">{r.version || "?"}</span>
        {r.default && <Badge tone="green" title="What a tool gets when nobody picked a version for it">default</Badge>}
        {r.sources.map((s, i) => <Badge key={s} tone={SOURCE_TONE[s] || "slate"}>{r.source_labels?.[i] || s}</Badge>)}
      </div>
      <p className="mt-0.5 break-all font-mono text-[10px] text-slate-400">{r.path}</p>
      {users.length > 0 && <p className="mt-0.5 text-[10px] text-slate-300">Used by: {users.join(", ")}</p>}
    </li>
  );
}

function ServerRuntimes({ st, busy, onDownload }: { st: EnvironmentsState; busy: string | null; onDownload: (key: string) => void }) {
  const users = useMemo(() => {
    const m: Record<string, string[]> = {};
    for (const t of st.tools) for (const r of t.runtimes) if (r.current.install_id) (m[r.current.install_id] ||= []).push(t.name);
    return m;
  }, [st.tools]);
  return (
    <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
      {st.kinds.map((k) => {
        const all = st.downloadable.filter((d) => d.kind === k.kind && !d.installed);
        const dls = all.filter((d) => !d.eol);
        const old = all.filter((d) => d.eol);
        return (
          <div key={k.kind} className="rounded-xl border border-slate-800 bg-slate-900/60 p-3" data-testid={`runtime-kind-${k.kind}`}>
            <div className="mb-2 flex items-center justify-between gap-2">
              <h4 className="text-xs font-semibold text-white">{k.label}</h4>
              <Badge tone={k.installs.length ? "slate" : "amber"}>{k.installs.length ? `${k.installs.length} installed` : "none"}</Badge>
            </div>
            {k.installs.length ? (
              <ul className="space-y-1.5">{k.installs.map((r) => <InstallRow key={r.id} r={r} users={users[r.id] || []} />)}</ul>
            ) : <p className="text-[11px] text-slate-400">Not installed on the server.</p>}
            {dls.length > 0 && (
              <div className="mt-2 flex flex-wrap gap-1">
                {dls.map((d) => {
                  const job = st.jobs[d.key];
                  const running = job?.status === "running";
                  return (
                    <button key={d.key} type="button" disabled={!!busy || running} onClick={() => onDownload(d.key)}
                      title={`Checksum-verified download, installed next to the others - nothing else changes${d.lts ? " (long-term support)" : ""}${d.eol_date ? ` - supported until ${d.eol_date}` : ""}`}
                      className="inline-flex items-center gap-1 rounded-lg border border-sky-500/40 px-2 py-0.5 text-[10px] font-semibold text-sky-200 hover:bg-sky-500/10 disabled:opacity-50">
                      {running || busy === d.key ? <Loader2 className="h-3 w-3 animate-spin" /> : <Download className="h-3 w-3" />}
                      {running ? `${d.label}…` : `Add ${d.label}`}{d.lts ? " (LTS)" : ""}
                    </button>
                  );
                })}
              </div>
            )}
            {old.length > 0 && (
              <select className="mt-1.5 w-full rounded-lg border border-slate-700 bg-slate-950 px-2 py-1 text-[10px] text-slate-300"
                value="" disabled={!!busy} aria-label={`Older ${k.label} versions (end of life)`} data-testid={`eol-${k.kind}`}
                onChange={(e) => { if (e.target.value) onDownload(e.target.value); }}>
                <option value="">Older versions (end of life) - {old.length}</option>
                {old.map((d) => <option key={d.key} value={d.key}>{d.label}{d.eol_date ? ` - end of life ${d.eol_date}` : d.note ? ` - ${d.note}` : ""}</option>)}
              </select>
            )}
            {Object.values(st.jobs).filter((j) => st.downloadable.some((d) => d.key === j.key && d.kind === k.kind) && j.status !== "running" && j.status === "failed")
              .map((j) => <p key={j.key} className="mt-1 text-[10px] text-rose-300">{j.label}: {j.error}</p>)}
          </div>
        );
      })}
    </div>
  );
}

function FixButton({ fix, busy, onApply }: { fix: RuntimeFix; busy: boolean; onApply: () => void }) {
  return (
    <button type="button" disabled={busy} onClick={onApply} data-testid="runtime-fix"
      className="mt-1 inline-flex items-center gap-1 rounded-lg bg-amber-500 px-2 py-0.5 text-[10px] font-semibold text-slate-950 hover:bg-amber-400 disabled:opacity-50">
      {busy ? <Loader2 className="h-3 w-3 animate-spin" /> : fix.type === "download" ? <Download className="h-3 w-3" /> : <CheckCircle2 className="h-3 w-3" />}
      {fix.label}
    </button>
  );
}

function RequirementCell({ row, busy, onSave }: { row: ToolRuntimeRow; busy: boolean; onSave: (text: string) => void }) {
  const [edit, setEdit] = useState(false);
  const [text, setText] = useState(row.requirement?.spec || "");
  if (edit) {
    return (
      <form className="flex items-center gap-1" onSubmit={(e) => { e.preventDefault(); onSave(text); setEdit(false); }}>
        <input className={input} value={text} onChange={(e) => setText(e.target.value)} autoFocus aria-label={`${row.label} requirement`}
          placeholder="17+, >=3.9,<3.13, any, or empty = rule" />
        <button type="submit" disabled={busy} className="rounded bg-sky-500 px-2 py-1 text-[10px] font-semibold text-slate-950">Save</button>
        <button type="button" onClick={() => setEdit(false)} className="px-1 text-[10px] text-slate-400 hover:text-white">Cancel</button>
      </form>
    );
  }
  return (
    <div className="flex items-start gap-1">
      <div className="min-w-0">
        {row.requirement ? (
          <>
            <span className="font-mono text-xs text-white">{row.requirement.spec}</span>
            <span className="ml-1 text-[10px] text-slate-400">({row.requirement_text})</span>
            <p className="text-[10px] text-slate-400" title={row.requirement.reason || ""}>
              from {REQ_SOURCE[row.requirement.source] || row.requirement.source}{row.requirement.reason ? ` - ${row.requirement.reason}` : ""}</p>
          </>
        ) : <span className="text-[11px] text-slate-500">no requirement</span>}
      </div>
      <button type="button" onClick={() => { setText(row.requirement?.spec || ""); setEdit(true); }} title="Change what this tool needs"
        aria-label={`Edit the ${row.label} requirement`} className="ml-auto shrink-0 rounded p-0.5 text-slate-400 hover:text-white">
        <Pencil className="h-3 w-3" />
      </button>
    </div>
  );
}

function ToolCard({ t, st, busy, act }: { t: ToolEnvironment; st: EnvironmentsState; busy: string | null;
  act: (id: string, fn: () => Promise<unknown>, okMsg?: string) => void }) {
  if (t.error) return <div className="rounded-xl border border-rose-900/60 p-3 text-xs text-rose-200">{t.name}: {t.error}</div>;
  const v = t.verify || {};
  return (
    <div className="rounded-xl border border-slate-800 bg-slate-900/60 p-3" data-testid={`tool-env-${t.tool_id}`}>
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <h4 className="text-sm font-semibold text-white">{t.name}</h4>
        <Badge>{t.ecosystem}</Badge>
        {t.ok ? <Badge tone="green"><CheckCircle2 className="h-3 w-3" />requirements met</Badge>
          : <Badge tone="amber"><AlertTriangle className="h-3 w-3" />needs attention</Badge>}
        {v.status === "running" && <Badge tone="sky"><Loader2 className="h-3 w-3 animate-spin" />re-running its smoke test</Badge>}
        {v.status === "passed" && <Badge tone="green">smoke test passed with the new version</Badge>}
        {v.status === "failed" && <Badge tone="red" title={v.detail || v.output || ""}><XCircle className="h-3 w-3" />smoke test failed - switch back?</Badge>}
      </div>
      {v.status === "failed" && (v.detail || v.output) && (
        <pre className="mb-2 max-h-28 overflow-auto whitespace-pre-wrap rounded bg-slate-950 p-2 font-mono text-[10px] text-rose-200">{v.detail || v.output}</pre>
      )}
      <div className="overflow-x-auto">
        <table className="w-full min-w-[640px] table-fixed text-left text-xs">
          <colgroup><col className="w-[14%]" /><col className="w-[38%]" /><col className="w-[34%]" /><col className="w-[14%]" /></colgroup>
          <thead className="text-[10px] uppercase tracking-wide text-slate-400">
            <tr><th className="py-1 pr-2 font-medium">Runtime</th><th className="py-1 pr-2 font-medium">Its runs use</th>
              <th className="py-1 pr-2 font-medium">It needs</th><th className="py-1 pr-2 font-medium">Smoke test passed with</th></tr>
          </thead>
          <tbody>
            {t.runtimes.map((r) => {
              const installs = st.kinds.find((k) => k.kind === r.kind)?.installs || [];
              const def = installs.find((i) => i.default);
              const id = `${t.tool_id}:${r.kind}`;
              const value = r.current.picked ? r.current.install_id : "";
              return (
                <tr key={r.kind} className="border-t border-slate-800 align-top">
                  <td className="py-2 pr-2 font-semibold text-slate-200">{r.label}
                    {!r.ok && <AlertTriangle className="ml-1 inline h-3 w-3 text-amber-300" aria-label="requirement not met" />}</td>
                  <td className="py-2 pr-2">
                    <select className={input} value={value} disabled={busy === id}
                      aria-label={`${r.label} version for ${t.name}`}
                      onChange={(e) => act(id, () => adminApi.selectToolRuntime(t.tool_id, r.kind, e.target.value || null),
                        `${t.name} now uses ${e.target.value ? installs.find((i) => i.id === e.target.value)?.version : "the default"} - re-running its smoke test`)}>
                      <option value="">Server default{def ? ` (${def.version})` : ""}</option>
                      {installs.filter((i) => !(r.kind === "python" && i.id === "python-backend")).map((i) => (
                        <option key={i.id} value={i.id}>{i.version} - {i.path}</option>
                      ))}
                    </select>
                    <p className={cx("mt-0.5 text-[10px]", r.ok ? "text-slate-400" : "text-amber-200")}>
                      {r.current.found ? <>Now: <b className="font-mono">{r.current.version || "?"}</b> · {r.current.source}</> : "Not found for this tool"}
                    </p>
                  </td>
                  <td className="py-2 pr-2">
                    <RequirementCell row={r} busy={busy === `${id}:req`}
                      onSave={(text) => act(`${id}:req`, () => adminApi.setToolRequirements(t.tool_id, { [r.kind]: text }), "Requirement saved")} />
                    {!r.ok && r.fix && <FixButton fix={r.fix} busy={busy === `${id}:fix`} onApply={() => act(`${id}:fix`,
                      () => r.fix!.type === "switch" ? adminApi.selectToolRuntime(t.tool_id, r.kind, r.fix!.install_id)
                        : adminApi.downloadRuntime(r.fix!.key, t.tool_id),
                      r.fix!.type === "switch" ? "Switched - re-running its smoke test" : "Downloading - the tool switches when it's verified")} />}
                    {!r.ok && !r.fix && <p className="mt-1 text-[10px] text-amber-200">No installed or downloadable version fits - install one on the server, then refresh.</p>}
                  </td>
                  <td className="py-2 pr-2 font-mono text-[11px] text-slate-300">{r.tested_with || "—"}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

/** 9.9: where runtime downloads come from - the official sites, or the admin's mirror (Artifactory / Nexus). */
function MirrorRow({ m, onSaved }: { m: RuntimeMirror; onSaved: (rows: RuntimeMirror[]) => void }) {
  const [url, setUrl] = useState(m.mirror);
  const [busy, setBusy] = useState<string | null>(null);
  const [res, setRes] = useState<{ ok: boolean | null; text: string } | null>(null);
  const run = async (what: "save" | "test" | "reset") => {
    setBusy(what); setRes(null);
    try {
      if (what === "test") {
        const r = await adminApi.testRuntimeMirror(m.id, url.trim() || undefined);
        setRes({ ok: r.ok, text: r.ok === null ? r.detail : `${r.ok ? "Works" : "Failed"} - ${r.url} ${r.detail}` });
      } else {
        const r = await adminApi.setRuntimeMirror(m.id, what === "reset" ? "" : url.trim());
        onSaved(r.sources);
        setRes({ ok: true, text: what === "reset" ? "Back to the official site." : "Saved - the next download uses the mirror." });
      }
    } catch (e) { setRes({ ok: false, text: msg(e) }); } finally { setBusy(null); }
  };
  const changed = url.trim() !== m.mirror;
  return (
    <li className="rounded-lg border border-slate-800 bg-slate-950/60 p-2 text-xs" data-testid={`mirror-${m.id}`}>
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium text-slate-100">{m.label}</span>
        {m.mirror ? <Badge tone="violet">mirror</Badge> : <Badge tone="slate">official site</Badge>}
      </div>
      <p className="mt-0.5 font-mono text-[10px] text-slate-500">{m.official}</p>
      <div className="mt-1.5 flex flex-wrap gap-1.5">
        <input className={cx(input, "min-w-[14rem] flex-1 font-mono")} value={url} onChange={(e) => setUrl(e.target.value)}
          placeholder="https://artifactory.example.com/artifactory/…/ (empty = official site)" aria-label={`Mirror for ${m.label}`} />
        <Button variant="secondary" className="px-2 py-1 text-[11px]" busy={busy === "test"} disabled={!!busy || (!url.trim() && !m.testable)}
          onClick={() => run("test")}>Test</Button>
        <Button className="px-2 py-1 text-[11px]" busy={busy === "save"} disabled={!!busy || !changed || !url.trim()} onClick={() => run("save")}>Save</Button>
        {m.mirror && <Button variant="ghost" className="px-2 py-1 text-[11px]" busy={busy === "reset"} disabled={!!busy} onClick={() => run("reset")}>Use official site</Button>}
      </div>
      {res && <p className={cx("mt-1 text-[11px]", res.ok === false ? "text-rose-300" : res.ok ? "text-emerald-300" : "text-slate-400")}>{res.text}</p>}
    </li>
  );
}

function MirrorsCard() {
  const [rows, setRows] = useState<RuntimeMirror[] | null>(null);
  const [err, setErr] = useState("");
  useEffect(() => { adminApi.runtimeMirrors().then((r) => setRows(r.sources)).catch((e) => setErr(msg(e))); }, []);
  const n = rows?.filter((r) => r.mirror).length || 0;
  return (
    <Card title="Download sources" icon={<Globe className="h-4 w-4 text-sky-300" />} foldId="env-mirrors"
      summary={rows ? (n ? `${n} mirror${n === 1 ? "" : "s"}` : "official sites") : undefined}>
      <p className="mb-3 text-xs text-slate-400">
        Runtimes are downloaded from their official sites. If this server may only use an internal mirror (Artifactory, Nexus …),
        set it per source - the same files and published checksums are expected, and the checksum is still checked before anything
        is unpacked. Only https addresses without a user name or password; every change is in the audit log.
      </p>
      {err && <p className="text-xs text-rose-300">{err}</p>}
      {!rows && !err && <p className="flex items-center gap-2 text-xs text-slate-400"><Loader2 className="h-3.5 w-3.5 animate-spin" />Loading…</p>}
      {rows && <ul className="grid gap-2 lg:grid-cols-2">{rows.map((m) => <MirrorRow key={`${m.id}:${m.mirror}`} m={m} onSaved={setRows} />)}</ul>}
    </Card>
  );
}

export function EnvironmentsPanel() {
  const [st, setSt] = useState<EnvironmentsState | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [note, setNote] = useState<{ tone: "ok" | "err"; text: string } | null>(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async (refresh = false) => {
    setLoading(true);
    try {
      setSt(await adminApi.environments(refresh));
    } catch (e) {
      setNote({ tone: "err", text: msg(e) });
    } finally {
      setLoading(false);
    }
  }, []);
  useEffect(() => { const h = setTimeout(() => { void load(false); }, 0); return () => clearTimeout(h); }, [load]);

  const working = !!st && (Object.values(st.jobs).some((j) => j.status === "running") || st.tools.some((t) => t.verify?.status === "running"));
  useEffect(() => {
    if (!working) return;
    const h = setInterval(() => { void load(false); }, 3000);
    return () => clearInterval(h);
  }, [working, load]);

  const act = useCallback(async (id: string, fn: () => Promise<unknown>, okMsg?: string) => {
    setBusy(id);
    setNote(null);
    try {
      await fn();
      if (okMsg) setNote({ tone: "ok", text: okMsg });
      await load(false);
    } catch (e) {
      setNote({ tone: "err", text: msg(e) });
    } finally {
      setBusy(null);
    }
  }, [load]);

  const attention = st?.tools.filter((t) => !t.ok).length || 0;
  return (
    <div className="space-y-5">
      {note && (
        <div role={note.tone === "err" ? "alert" : "status"}
          className={cx("rounded-lg border px-3 py-2 text-xs", note.tone === "err" ? "border-rose-500/40 bg-rose-500/10 text-rose-200" : "border-emerald-500/40 bg-emerald-500/10 text-emerald-200")}>
          {note.text}</div>
      )}
      <Card title="Server runtimes" icon={<Server className="h-4 w-4 text-sky-300" />} foldId="env-server"
        summary={st ? `${st.kinds.reduce((n, k) => n + k.installs.length, 0)} installs` : undefined}
        right={<Button variant="secondary" className="px-2.5 py-1 text-xs" busy={loading} onClick={() => load(true)}>
          <RefreshCw className="h-3.5 w-3.5" />Rescan</Button>}>
        <p className="mb-3 text-xs text-slate-400">
          Every language runtime and build tool Understudy found on this server{st ? ` (${st.os}, Understudy itself runs on Python ${st.backend_python})` : ""}.
          Several versions can live side by side - <b className="text-slate-200">Add</b> downloads another one (checksum verified) without changing
          what any tool uses; pick it per tool below.
        </p>
        {!st ? <p className="flex items-center gap-2 text-xs text-slate-400"><Loader2 className="h-3.5 w-3.5 animate-spin" />Scanning the server…</p>
          : <ServerRuntimes st={st} busy={busy} onDownload={(key) => act(key, () => withEolConfirm((ok) => adminApi.downloadRuntime(key, undefined, ok)), "Downloading - it appears here when it's verified")} />}
        {st && st.browsers.length > 0 && (
          <div className="mt-3">
            <h4 className="mb-1 flex items-center gap-1.5 text-xs font-semibold text-white"><Globe className="h-3.5 w-3.5" />Browsers</h4>
            <ul className="grid gap-1 md:grid-cols-2">{st.browsers.map((b) => (
              <li key={b.name} className="rounded border border-slate-800 px-2 py-1 text-[11px]"><b className="text-slate-200">{b.name.replace("_", " ")}</b>
                {b.version && <span className="ml-1 font-mono text-slate-300">{b.version}</span>}
                <span className="block break-all font-mono text-[10px] text-slate-500">{b.path}</span></li>))}</ul>
          </div>
        )}
      </Card>

      <MirrorsCard />
      <Card title="Tools and the versions they use" icon={<Layers className="h-4 w-4 text-violet-300" />} foldId="env-tools"
        summary={attention ? `${attention} need attention` : undefined}>
        <p className="mb-3 text-xs text-slate-400">
          Pick the version each tool runs on - the tool&apos;s smoke test runs again right away. <b className="text-slate-200">It needs</b> comes from
          Understudy&apos;s rules, the AI stack assistant (recorded when the tool was set up) or you; it&apos;s checked before every run, and a run that
          doesn&apos;t meet it is held until it&apos;s fixed (users can send you the one-click fix from their run).
        </p>
        {!st ? null : st.tools.length === 0 ? <p className="text-xs text-slate-400">No approved tools yet.</p> : (
          <div className="space-y-3">{st.tools.map((t) => <ToolCard key={t.tool_id} t={t} st={st} busy={busy} act={act} />)}</div>
        )}
      </Card>
      {st && <p className="flex items-center gap-1 text-[10px] text-slate-500"><Cpu className="h-3 w-3" />Scanned {new Date(st.scanned_at + "Z").toLocaleTimeString()}</p>}
    </div>
  );
}
