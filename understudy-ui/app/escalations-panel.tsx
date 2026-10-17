"use client";

/* Mission Control - Escalations (runtime help).
 * Self-healing found a fix only the admin can make (allow a version, add a package, a run variable, a server setting).
 * The admin applies it, rejects with a reason, or asks the user a question. Every decision reaches the user as a
 * Studio notification and by email. 9.3: the admin chooses who gets the fix - everyone using the tool, or only the
 * user who asked (packages, allowed versions, run variables, PATH folders); server settings are always shared.
 * (Before 9.0 escalations were also used to confirm runs the verifier couldn't check.)
 */
import React, { useCallback, useEffect, useState } from "react";
import { CheckCircle2, Download, Eye, FileText, Flag, Globe, HelpCircle, Image as ImageIcon, Loader2, MessageSquare, RefreshCw, Trash2, User, XCircle } from "lucide-react";
import { adminApi, openEscalationFile, type AdminFixAction, type Escalation, type EscalationRow, type EscalationScope, type UserOverride } from "../lib/admin";
import { Badge, Card, cx, Fold } from "./wizard/ui";

const input = "w-full text-xs bg-slate-950 border border-slate-700 rounded-lg px-3 py-2 text-white placeholder:text-slate-400 focus:outline-none focus:border-sky-500";
const STATUS: Record<Escalation["status"], { tone: "amber" | "sky" | "green" | "red" | "slate"; label: string }> = {
  open: { tone: "amber", label: "needs you" }, info_requested: { tone: "sky", label: "waiting for the user" },
  confirmed: { tone: "green", label: "confirmed" }, rejected: { tone: "red", label: "rejected" },
  superseded: { tone: "slate", label: "superseded" }, withdrawn: { tone: "slate", label: "withdrawn" },
};

function size(n: number): string {
  return n > 1024 * 1024 ? `${(n / 1024 / 1024).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1024))} KB`;
}

function Detail({ id, onChanged }: { id: string; onChanged: () => void }) {
  const [e, setE] = useState<Escalation | null>(null);
  const [note, setNote] = useState("");
  // 8.3: the admin's own fix for a runtime-help request (no AI needed)
  const [fix, setFix] = useState<{ pkg: string | null; version: string; envName: string; envValue: string; path: string; bypass: string;
    network: "" | "restricted" | "public"; rerun: boolean }>({ pkg: null, version: "", envName: "", envValue: "", path: "", bypass: "", network: "", rerun: true });
  const [scope, setScope] = useState<EscalationScope>("everyone");
  const [busy, setBusy] = useState<string | null>(null);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const load = useCallback(() => { adminApi.escalation(id).then((r) => setE(r.escalation)).catch((x) => setMsg({ ok: false, text: (x as Error).message })); }, [id]);
  useEffect(() => { const t = setTimeout(load, 0); return () => clearTimeout(t); }, [load]);
  if (!e) return <p className="flex items-center gap-1 text-xs text-slate-400"><Loader2 className="h-3 w-3 animate-spin" />Loading...</p>;
  const ev = e.evidence;
  const open = e.status === "open" || e.status === "info_requested";
  const help = e.kind === "runtime_help";
  const act = e.admin_action;
  // 8.5.3: the package the request asks for - pre-filled (also for requests sent before 8.5.3)
  const askedPkg = act?.package || packageIn(act?.request || e.message || "");
  const pkg = (fix.pkg ?? askedPkg).trim();
  const fixActions = (): AdminFixAction[] => {
    const a: AdminFixAction[] = [];
    if (pkg) a.push({ type: "add_package", name: pkg });
    if (fix.version.trim()) a.push({ type: "allow_version", version: fix.version.trim() });
    if (fix.envName.trim()) a.push({ type: "set_env", name: fix.envName.trim().toUpperCase(), value: fix.envValue });
    if (fix.path.trim()) a.push({ type: "path_prepend", path: fix.path.trim() });
    if (fix.bypass.trim()) a.push({ type: "proxy_bypass", host: fix.bypass.trim() });
    if (fix.network) a.push({ type: "network_mode", mode: fix.network });
    return a;
  };
  const decide = async (d: "confirm" | "reject" | "ask") => {
    setBusy(d); setMsg(null);
    try {
      const acts = d === "confirm" && e.kind === "runtime_help" ? fixActions() : [];
      const r = await adminApi.decideEscalation(e.id, d, note.trim(), acts, d === "confirm" && e.kind === "runtime_help" && fix.rerun,
        d === "confirm" ? scope : "everyone");
      setE(r.escalation); setNote("");
      const helpDone = r.escalation.kind === "runtime_help";
      setMsg({ ok: true, text: d === "confirm"
        ? (helpDone ? `Done - ${((r.escalation as Escalation & { admin_actions_done?: string[] }).admin_actions_done?.join(", ") || "the user was told it's fixed").replace(/ for you\b/g, ` for ${r.escalation.owner} only`).replace(/ for your runs\b/g, ` for ${r.escalation.owner}'s runs`)}. The user was notified`
          + ((r.escalation as Escalation & { rerun_started?: boolean }).rerun_started ? " and their run was started again." : " and can run it again.")
          : "Confirmed - the user was notified and can download the ZIP.")
        : d === "reject" ? "Rejected - the user was told why." : "Question sent to the user." });
      onChanged();
    } catch (x) { setMsg({ ok: false, text: (x as Error).message }); } finally { setBusy(null); }
  };
  const file = (path: string, name: string, inline: boolean) => { openEscalationFile(path, name, inline).catch((x) => setMsg({ ok: false, text: (x as Error).message })); };
  const attName = (aid: string) => e.attachments.find((a) => a.id === aid)?.name || aid;
  return (
    <div className="space-y-3 text-xs text-slate-300" data-testid="escalation-detail">
      <dl className="grid grid-cols-2 gap-2 rounded-lg border border-slate-800 bg-slate-950/60 p-2.5 sm:grid-cols-4">
        <div><dt className="text-slate-400">User</dt><dd className="font-medium text-slate-100">{e.owner}</dd></div>
        <div><dt className="text-slate-400">Project / session</dt><dd>{e.project_name || "-"} / {e.session_name || e.session_id}</dd></div>
        <div><dt className="text-slate-400">Tool</dt><dd>{ev?.tool || "-"}{ev?.tool_version ? ` ${ev.tool_version}` : ""}</dd></div>
        <div><dt className="text-slate-400">Target</dt><dd className="break-all font-mono text-[11px]">{ev?.target_url || "-"}</dd></div>
      </dl>

      {help && (
        <div className="rounded-lg border border-violet-500/40 bg-violet-500/5 p-2.5" data-testid="runtime-help">
          <div className="mb-1 flex items-center gap-2 font-semibold text-violet-100"><Badge tone="violet">runtime help</Badge>Self-healing needs you</div>
          <p className="whitespace-pre-wrap text-slate-200">{act?.request || e.message}</p>
          {askedPkg && <p className="mt-1 text-slate-400" data-testid="asked-package">One click below adds <b className="text-slate-200">{askedPkg}</b> to {act?.tool_name || act?.tool_id || "the tool"} - the user&apos;s next run installs it (open &quot;Fix it yourself&quot; to change or clear it).</p>}
          {act?.action === "allow_version" && <p className="mt-1 text-slate-400">One click below adds version <b className="text-slate-200">{act.version}</b> to the allowed versions of {act.tool_name || act.tool_id}.</p>}
          {act?.action === "use_version" && act.fix && (
            <p className="mt-1 text-slate-400" data-testid="runtime-switch">
              {act.fix.type === "switch"
                ? <>One click below switches {act.tool_name || act.tool_id} to <b className="text-slate-200">{act.kind} {act.fix.version}</b> (<span className="font-mono">{act.fix.path}</span>) for every run - see Mission Control &gt; Environments.</>
                : <>One click below downloads <b className="text-slate-200">{act.fix.label.replace(/^Download /, "").replace(/ and use it$/, "")}</b> side by side and switches {act.tool_name || act.tool_id} to it once it&apos;s verified. The user is notified when it&apos;s ready.</>}
            </p>
          )}
          {(ev as { ai_diagnosis?: string } | undefined)?.ai_diagnosis && <p className="mt-1 text-[11px] text-slate-400">AI diagnosis: {(ev as { ai_diagnosis?: string }).ai_diagnosis}</p>}
        </div>
      )}
      <Fold title="Conversation" icon={<MessageSquare className="h-3.5 w-3.5 text-sky-300" />} foldId={`esc-conv`}>
        <ul className="space-y-1.5">
          {e.conversation.map((m, i) => (
            <li key={i} className={cx("rounded-md px-2 py-1.5", m.role === "admin" ? "bg-sky-500/5" : "bg-slate-800/40")}>
              <div className="flex gap-2 text-[10px] text-slate-400"><b className="uppercase">{m.role === "admin" ? `admin · ${m.by}` : m.by}</b>
                <span className="ml-auto font-mono">{m.at.slice(0, 16).replace("T", " ")}</span></div>
              <p className="whitespace-pre-wrap text-slate-100">{m.text}</p>
              {!!m.attachments?.length && <p className="text-[10px] text-slate-400">Attached: {m.attachments.map(attName).join(", ")}</p>}
            </li>
          ))}
        </ul>
      </Fold>

      <Fold title={`Attachments (${e.attachments.length})`} icon={<FileText className="h-3.5 w-3.5 text-violet-300" />} foldId="esc-files">
        {e.attachments.length === 0 && <p className="text-slate-500">The user attached nothing.</p>}
        <ul className="flex flex-wrap gap-1.5">
          {e.attachments.map((a) => {
            const img = a.content_type.startsWith("image/");
            return (
              <li key={a.id}>
                <button type="button" onClick={() => file(`/escalations/${e.id}/files/${a.id}`, a.name, img)}
                  className="inline-flex items-center gap-1 rounded-md border border-slate-700 px-2 py-1 text-[11px] text-slate-200 hover:bg-slate-800">
                  {img ? <ImageIcon className="h-3 w-3" /> : <Download className="h-3 w-3" />}{a.name} <span className="text-slate-500">{size(a.size)}</span></button>
              </li>
            );
          })}
        </ul>
      </Fold>

      <Fold title="What Understudy saw" icon={<Eye className="h-3.5 w-3.5 text-emerald-300" />} foldId="esc-evidence" defaultOpen={false}>
        <div className="space-y-2">
          {ev?.verify?.summary ? <p><b className="text-slate-100">AI verification:</b> {ev.verify.summary} {ev.verify.method ? <span className="text-slate-500">({ev.verify.method})</span> : null}</p> : null}
          {!!ev?.verify?.checks?.length && (
            <ul className="space-y-0.5">{ev.verify.checks.map((c, i) => (
              <li key={i} className={c.passed ? "text-emerald-300" : "text-rose-300"}>{c.passed ? "✓" : "✗"} {c.check || c.name}</li>))}</ul>
          )}
          <div className="flex gap-1.5">
            {ev?.verify?.has_before && <button type="button" className="rounded border border-slate-700 px-2 py-0.5 hover:bg-slate-800" onClick={() => file(`/escalations/${e.id}/evidence/before`, "before.png", true)}>Before screenshot</button>}
            {ev?.verify?.has_after && <button type="button" className="rounded border border-slate-700 px-2 py-0.5 hover:bg-slate-800" onClick={() => file(`/escalations/${e.id}/evidence/after`, "after.png", true)}>After screenshot</button>}
          </div>
          <p><b className="text-slate-100">Execute:</b> {ev?.execute?.status} · {ev?.execute?.strategy} · {ev?.execute?.duration_ms} ms · {ev?.retries ?? 0} self-healing retries</p>
          {!!ev?.parameters?.length && (
            <table className="w-full text-[11px]"><tbody>{ev.parameters.map((p) => (
              <tr key={p.key} className="border-b border-slate-800/60"><td className="py-0.5 pr-2 text-slate-400">{p.label || p.key}</td><td className="font-mono">{p.value}</td></tr>))}</tbody></table>
          )}
          {!!ev?.execute?.log_tail?.length && (
            <details><summary className="cursor-pointer text-slate-400">Run log (last lines)</summary>
              <pre className="mt-1 max-h-60 overflow-auto rounded bg-slate-950 p-2 font-mono text-[10px] text-slate-300">{ev.execute.log_tail.join("\n")}</pre></details>
          )}
        </div>
      </Fold>

      {open && help && (
        <Fold title="Fix it yourself (optional - no AI needed)" icon={<CheckCircle2 className="h-3.5 w-3.5 text-emerald-300" />} foldId="esc-admin-fix" tone="sky">
          <p className="mb-2 text-[11px] text-slate-400">Fill in only what you want to change; it is applied when you click the green button. The AI&apos;s suggestion
            above is just a suggestion - you decide.</p>
          <div className="grid gap-2 sm:grid-cols-2" data-testid="admin-fix">
            <label className="space-y-0.5 sm:col-span-2"><span className="text-[11px] text-slate-400">Add a package to {act?.tool_name || "the tool"}
              {askedPkg ? <> - <b className="text-emerald-300">the request asks for {askedPkg}</b></> : null} (installed on the user&apos;s next run)</span>
              <input className={input} value={fix.pkg ?? askedPkg} onChange={(x) => setFix({ ...fix, pkg: x.target.value })}
                placeholder="e.g. robotframework-jsonlibrary, @wdio/allure-reporter, org.testng:testng (empty = don't add)" aria-label="Package to add to the tool" /></label>
            <label className="space-y-0.5"><span className="text-[11px] text-slate-400">Allow version of {act?.tool_name || "the tool"}</span>
              <input className={input} value={fix.version} onChange={(x) => setFix({ ...fix, version: x.target.value })} placeholder={act?.version || "e.g. 4.25.0"} /></label>
            <label className="space-y-0.5"><span className="text-[11px] text-slate-400">Let runs reach this host without the run proxy</span>
              <input className={input} value={fix.bypass} onChange={(x) => setFix({ ...fix, bypass: x.target.value })} placeholder="e.g. erp.intranet.local" /></label>
            <label className="space-y-0.5"><span className="text-[11px] text-slate-400">Run setting (environment variable) for this tool</span>
              <span className="flex gap-1"><input className={input} value={fix.envName} onChange={(x) => setFix({ ...fix, envName: x.target.value })} placeholder="NAME" />
                <input className={input} value={fix.envValue} onChange={(x) => setFix({ ...fix, envValue: x.target.value })} placeholder="value (empty = remove)" /></span></label>
            <label className="space-y-0.5"><span className="text-[11px] text-slate-400">Folder to put first on PATH (on the server)</span>
              <input className={input} value={fix.path} onChange={(x) => setFix({ ...fix, path: x.target.value })} placeholder="e.g. C:\Program Files\Google\Chrome\Application" /></label>
            <label className="space-y-0.5"><span className="text-[11px] text-slate-400">Network for all runs</span>
              <select className={input} value={fix.network} onChange={(x) => setFix({ ...fix, network: x.target.value as "" | "restricted" | "public" })}>
                <option value="">unchanged</option><option value="restricted">restricted (target + allowed list)</option><option value="public">public internet</option>
              </select></label>
            <label className="flex items-center gap-2 pt-4 text-[11px] text-slate-300">
              <input type="checkbox" checked={fix.rerun} onChange={(x) => setFix({ ...fix, rerun: x.target.checked })} />Run the user&apos;s session again after the fix</label>
          </div>
        </Fold>
      )}
      {open && help && (() => {
        // 9.3: who gets the fix. Server settings (proxy bypass, network, runtime switch / download) exist once per server.
        const acts = fixActions();
        const serverOnly = (acts.length > 0 && acts.every((a) => a.type === "proxy_bypass" || a.type === "network_mode"))
          || (!acts.length && act?.action === "use_version");
        const shared = acts.some((a) => a.type === "proxy_bypass" || a.type === "network_mode") || (!acts.length && act?.action === "use_version");
        const tool = act?.tool_name || act?.tool_id || "the tool";
        return (
          <div className="rounded-lg border border-slate-800 bg-slate-950/60 p-2.5" data-testid="escalation-scope">
            <p className="mb-1.5 text-[11px] font-semibold text-slate-200">Apply this fix to</p>
            <div role="radiogroup" aria-label="Apply this fix to" className="grid gap-1.5 sm:grid-cols-2">
              {([["everyone", <Globe key="g" className="h-3.5 w-3.5" />, `Everyone using ${tool}`, "Changes the tool for all users - nobody else hits this problem."],
                ["user", <User key="u" className="h-3.5 w-3.5" />, `Only ${e.owner}`, `Only ${e.owner}'s runs get it; other users keep the tool as it is.`]] as const)
                .map(([v, icon, label, hint]) => (
                  <label key={v} className={cx("flex cursor-pointer gap-2 rounded-md border p-2",
                    scope === v ? "border-sky-500/70 bg-sky-500/10" : "border-slate-800 hover:border-slate-600",
                    v === "user" && serverOnly && "cursor-not-allowed opacity-50")}>
                    <input type="radio" name={`scope-${e.id}`} className="mt-0.5" checked={scope === v} disabled={v === "user" && serverOnly}
                      onChange={() => setScope(v)} data-testid={`scope-${v}`} />
                    <span><span className="flex items-center gap-1 text-xs font-semibold text-slate-100">{icon}{label}</span>
                      <span className="block text-[11px] text-slate-400">{hint}</span></span>
                  </label>
                ))}
            </div>
            {shared && <p className="mt-1.5 text-[11px] text-amber-300">
              {serverOnly ? "This is a server setting (proxy, network or the runtime installed on the server) - it always applies to everyone."
                : "The proxy / network part is a server setting and applies to everyone; the rest follows your choice."}</p>}
          </div>
        );
      })()}
      {open ? (
        <div className="space-y-2 rounded-lg border border-amber-500/40 bg-amber-500/5 p-2.5">
          <textarea className={cx(input, "h-20")} value={note} onChange={(x) => setNote(x.target.value)} maxLength={2000}
            placeholder="Note for the user - required to reject or to ask a question, optional when you confirm" aria-label="Note for the user" />
          <div className="flex flex-wrap gap-2">
            <button type="button" disabled={busy !== null} onClick={() => { void decide("confirm"); }}
              className="inline-flex items-center gap-1 rounded-lg bg-emerald-500 px-3 py-1.5 text-xs font-semibold text-slate-950 hover:bg-emerald-400 disabled:opacity-50">
              {busy === "confirm" ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <CheckCircle2 className="h-3.5 w-3.5" />}
              {help ? (pkg && fixActions().length === 1 ? `Add ${pkg} & notify the user`
                : fixActions().length ? "Apply my fix & notify the user"
                : act?.action === "allow_version" ? `Allow ${act.tool_name || act.tool_id} ${act.version} & notify the user`
                : act?.action === "use_version" && act.fix ? (act.fix.type === "switch" ? `Switch to ${act.kind} ${act.fix.version} & notify the user`
                  : "Download it, switch & notify the user") : "Done on the server - notify the user")
                : "Confirm - it worked (unlock the ZIP)"}</button>
            <button type="button" disabled={busy !== null || note.trim().length < 5} onClick={() => { void decide("ask"); }}
              className="inline-flex items-center gap-1 rounded-lg border border-sky-500/60 px-3 py-1.5 text-xs text-sky-100 hover:bg-sky-500/10 disabled:opacity-50">
              {busy === "ask" ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <HelpCircle className="h-3.5 w-3.5" />}Ask the user</button>
            <button type="button" disabled={busy !== null || note.trim().length < 5} onClick={() => { void decide("reject"); }}
              className="inline-flex items-center gap-1 rounded-lg border border-rose-500/60 px-3 py-1.5 text-xs text-rose-200 hover:bg-rose-500/10 disabled:opacity-50">
              {busy === "reject" ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <XCircle className="h-3.5 w-3.5" />}Reject</button>
          </div>
          {e.status === "info_requested" && <p className="text-sky-200">You asked: {e.question} - waiting for the user&apos;s answer.</p>}
        </div>
      ) : (
        <p className="text-slate-400">{STATUS[e.status].label}{e.decided_by ? ` by ${e.decided_by}` : ""}
          {help && e.status === "confirmed" ? ` · applied to ${e.scope === "user" ? `${e.owner} only` : "everyone"}` : ""}{e.admin_note ? `: ${e.admin_note}` : ""}</p>
      )}
      {msg && <p className={msg.ok ? "text-emerald-300" : "text-rose-300"} role={msg.ok ? "status" : "alert"}>{msg.text}</p>}
    </div>
  );
}

/** 8.5.3: "Please add 'robotframework-jsonlibrary' ..." -> robotframework-jsonlibrary */
function packageIn(text: string): string {
  const m = /\b(?:add|install|include)\b[^'"`]{0,60}['"`](@?[A-Za-z0-9][A-Za-z0-9._~/:-]{0,150})['"`]/i.exec(text || "");
  return m ? m[1] : "";
}

/** 9.3: every fix approved for a single user, with Remove. */
function UserOverridesCard({ refreshKey }: { refreshKey: number }) {
  const [rows, setRows] = useState<UserOverride[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const load = useCallback(() => { adminApi.userOverrides().then((r) => { setRows(r.overrides); setErr(null); }).catch((e) => setErr((e as Error).message)); }, []);
  useEffect(() => { const t = setTimeout(load, 0); return () => clearTimeout(t); }, [load, refreshKey]);
  const label = { package: "package", version: "allowed version", env: "run variable", path: "PATH folder" } as const;
  return (
    <Card title="Fixes for single users" icon={<User className="h-4 w-4 text-sky-400" />} foldId="user-overrides"
      summary={rows ? `${rows.length} active` : undefined}>
      <p className="mb-2 text-[11px] text-slate-400">What you approved for one user only. Remove one and that user&apos;s next run no longer gets it.</p>
      {err && <p className="text-xs text-rose-300" role="alert">{err}</p>}
      {rows && rows.length === 0 && <p className="text-xs text-slate-500">None - every fix so far applies to everyone.</p>}
      <ul className="space-y-1" data-testid="user-overrides">
        {(rows || []).map((o) => (
          <li key={`${o.user}-${o.tool_id}-${o.kind}-${o.key}`} className="flex items-center gap-2 rounded border border-slate-800 bg-slate-950/60 px-2 py-1.5 text-[11px]">
            <Badge tone="sky">{o.user}</Badge><span className="text-slate-400">{o.tool_id}</span>
            <span className="text-slate-200">{label[o.kind]}: <b>{o.key}</b>{o.value ? ` = ${o.value}` : ""}</span>
            <span className="ml-auto text-slate-500">{o.by ? `by ${o.by}` : ""}{o.at ? ` · ${o.at.slice(0, 10)}` : ""}</span>
            <button type="button" aria-label={`Remove ${o.key} for ${o.user}`} className="rounded p-0.5 text-slate-400 hover:text-rose-300"
              onClick={() => { adminApi.removeUserOverride(o).then((r) => setRows(r.overrides)).catch((e) => setErr((e as Error).message)); }}>
              <Trash2 className="h-3.5 w-3.5" /></button>
          </li>
        ))}
      </ul>
    </Card>
  );
}

export function EscalationsPanel({ refreshKey = 0 }: { refreshKey?: number }) {
  const [rows, setRows] = useState<EscalationRow[] | null>(null);
  const [filter, setFilter] = useState<"open_any" | "">("open_any");
  const [sel, setSel] = useState<string | null>(null);
  const [bump, setBump] = useState(0);
  const [err, setErr] = useState<string | null>(null);
  const load = useCallback(() => {
    adminApi.escalations(filter || undefined).then((r) => { setRows(r.escalations); setErr(null); }).catch((e) => setErr((e as Error).message));
  }, [filter]);
  useEffect(() => { const t = setTimeout(load, 0); return () => clearTimeout(t); }, [load, refreshKey]);
  return (
    <div className="grid items-start gap-5 xl:grid-cols-5">
      <div className="xl:col-span-2">
        <Card title="Escalations" icon={<Flag className="h-4 w-4 text-amber-400" />} summary={rows ? `${rows.filter((r) => r.status === "open").length} need you` : undefined}
          right={<div className="flex items-center gap-1.5">
            <select value={filter} onChange={(e) => setFilter(e.target.value as "open_any" | "")} aria-label="Show"
              className="rounded border border-slate-700 bg-slate-950 px-1.5 py-1 text-[11px] text-slate-200">
              <option value="open_any">Open</option><option value="">All</option>
            </select>
            <button type="button" onClick={load} aria-label="Refresh" className="rounded p-1 text-slate-400 hover:text-white"><RefreshCw className="h-3.5 w-3.5" /></button>
          </div>}>
          <p className="mb-2 text-[11px] text-slate-400"><b>Runtime help</b>: fixes self-healing found that only you can make (allow a version, add a
            package, a run setting ...). Check the evidence, choose whether the fix applies to everyone or only to that user, then apply it,
            reject it, or ask a question.</p>
          {err && <p className="text-xs text-rose-300" role="alert">{err}</p>}
          {rows && rows.length === 0 && <p className="text-xs text-slate-500">Nothing to check.</p>}
          <ul className="space-y-1.5" data-testid="escalations-list">
            {(rows || []).map((r) => (
              <li key={r.id}>
                <button type="button" onClick={() => setSel(r.id)}
                  className={cx("w-full rounded-lg border px-2.5 py-2 text-left text-xs", sel === r.id ? "border-sky-500/60 bg-sky-500/5" : "border-slate-800 bg-slate-950/60 hover:border-slate-600")}>
                  <div className="flex items-center gap-2"><b className="text-slate-100">{r.id}</b><Badge tone={STATUS[r.status].tone}>{STATUS[r.status].label}</Badge>
                    {r.kind === "runtime_help" && <Badge tone="violet">runtime help</Badge>}
                    <span className="ml-auto text-[10px] text-slate-400">{r.created_at.slice(0, 16).replace("T", " ")}</span></div>
                  <div className="mt-0.5 text-[11px] text-slate-300">{r.owner} · {r.session_name || r.session_id}{r.tool ? ` · ${r.tool}` : ""}{r.attachments ? ` · ${r.attachments} file(s)` : ""}</div>
                  <p className="mt-0.5 line-clamp-2 text-[11px] text-slate-400">{r.message}</p>
                </button>
              </li>
            ))}
          </ul>
        </Card>
      </div>
      <div className="space-y-5 xl:col-span-3">
        {sel ? (
          <Card title={`Escalation ${sel}`} foldId="escalation-detail" icon={<Flag className="h-4 w-4 text-amber-400" />}>
            <Detail key={sel} id={sel} onChanged={() => { load(); setBump((b) => b + 1); }} />
          </Card>
        ) : (
          <div className="rounded-xl border border-dashed border-slate-800 p-6 text-center text-xs text-slate-500">Pick an escalation to see the evidence and decide.</div>
        )}
        <UserOverridesCard refreshKey={refreshKey + bump} />
      </div>
    </div>
  );
}
