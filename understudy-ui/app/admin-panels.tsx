"use client";

import React, { useCallback, useEffect, useState } from "react";
import {
  Check, ChevronDown, ChevronRight, ExternalLink, Globe, Layers, Network, Plus, RefreshCw,
  RotateCw, ShieldBan, Trash2, Users,
} from "lucide-react";
import {
  AdminApiError, adminApi, type AccessMode, type AdminPolicy, type AvailableVersions, type StudioUser,
  type WhitelistedTool,
} from "../lib/admin";
import { Badge, Card, cx } from "./wizard/ui";

const input = "w-full text-xs bg-slate-950 border border-slate-700 rounded-lg px-3 py-2 text-white placeholder:text-slate-400 focus:outline-none focus:border-sky-500";
type Run = (fn: () => Promise<AdminPolicy>) => Promise<boolean>;

/** Loads the admin policy while signed in; a 401 means the session ended (restart, password change, logout). */
export function useAdminPolicy(enabled = true, onUnauthorized?: () => void) {
  const [policy, setPolicy] = useState<AdminPolicy | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [needsToken, setNeedsToken] = useState(false);

  const run = useCallback(async (fn: () => Promise<AdminPolicy>) => {
    try {
      const p = await fn();
      setPolicy(p);
      setError(null);
      setNeedsToken(false);
      return true;
    } catch (e) {
      const err = e as AdminApiError;
      if (err.status === 401) {
        setNeedsToken(true);
        onUnauthorized?.();
      }
      setError(err.message);
      return false;
    }
  }, [onUnauthorized]);

  useEffect(() => {
    if (!enabled) return;
    const load = () => { void run(adminApi.policy); };
    const first = setTimeout(load, 0);
    const id = setInterval(load, 4000);
    return () => { clearTimeout(first); clearInterval(id); };
  }, [run, enabled]);

  return { policy, error, needsToken, run };
}

/** Host of what the admin typed (same rules as the server), "" when it can't be read. */
function hostOf(raw: string): string {
  const t = raw.trim();
  if (!t) return "";
  try { return new URL(t.includes("://") ? t : `http://${t}`).hostname.toLowerCase().replace(/\.$/, ""); } catch { return ""; }
}
function covers(pattern: string, host: string): boolean {
  if (pattern.startsWith("*.")) return host.endsWith(pattern.slice(1)) && host.length > pattern.length - 1;
  return pattern === host;
}
function overlaps(a: string, b: string): boolean {
  return covers(a, b) || covers(b, a) || (a.startsWith("*.") && b.startsWith("*.") && (a.slice(2).endsWith(b.slice(2)) || b.slice(2).endsWith(a.slice(2))));
}
/** The entry on the OTHER list that a new link would collide with (the server enforces the same rule). */
function conflictWith(url: string, other: { host: string }[] | undefined): string | null {
  const h = hostOf(url) || (url.trim().startsWith("*.") ? url.trim().toLowerCase() : "");
  if (!h) return null;
  const hit = (other ?? []).find((e) => overlaps(e.host, h));
  return hit ? hit.host : null;
}

/* ------------------------------------------------------------------------------------------------ */
export function UrlExceptionsPanel({ policy, run }: { policy: AdminPolicy | null; run: Run }) {
  const [url, setUrl] = useState("");
  const [note, setNote] = useState("");
  const clash = conflictWith(url, policy?.url_blocklist);
  const add = async () => {
    const ok = await run(() => adminApi.addUrl(url, note));
    if (ok) { setUrl(""); setNote(""); }
  };
  return (
    <Card title="Target URL exceptions" icon={<Globe className="h-4 w-4 text-sky-400" />}
      right={<Badge tone="sky">{policy?.url_exceptions.length ?? 0} allowed</Badge>}>
      <p className="mb-3 text-xs text-slate-400">
        Understudy blocks internal, private and unresolvable addresses by default (SSRF protection). Add a URL to let users
        test against it: its host is allowed on any port and path, and it appears as a one-click target in Understudy.
      </p>
      <div className="max-h-[260px] space-y-2 overflow-y-auto pr-1">
        {policy?.url_exceptions.map((e) => (
          <div key={e.id} className="flex items-center justify-between gap-2 rounded-lg border border-slate-800 bg-slate-950/70 p-2.5">
            <div className="min-w-0">
              <div className="truncate text-xs font-semibold text-slate-200">{e.note || e.host}</div>
              <div className="truncate font-mono text-[11px] text-slate-400">{e.url}</div>
              <div className="text-[10px] text-slate-400">host <b>{e.host}</b> · added by {e.added_by}</div>
            </div>
            <div className="flex shrink-0 items-center gap-1">
              <a href={e.url} target="_blank" rel="noreferrer" title="Open" className="rounded-lg p-1.5 text-slate-400 hover:bg-slate-800 hover:text-sky-300"><ExternalLink className="h-4 w-4" /></a>
              <button title="Remove" onClick={() => run(() => adminApi.removeUrl(e.id))} className="rounded-lg p-1.5 text-slate-400 hover:bg-slate-800 hover:text-rose-300"><Trash2 className="h-4 w-4" /></button>
            </div>
          </div>
        ))}
      </div>
      <div className="mt-3 space-y-2 border-t border-slate-800 pt-3">
        <input className={input} value={url} onChange={(e) => setUrl(e.target.value)} placeholder="http://10.0.4.12:8080/erp or https://uat.intranet.local" />
        <div className="flex gap-2">
          <input className={input} value={note} onChange={(e) => setNote(e.target.value)} placeholder="Label (e.g. UAT ERP)" />
          <button disabled={!url.trim() || !!clash} onClick={add} className="flex items-center gap-1 whitespace-nowrap rounded-lg bg-sky-500 px-3 py-2 text-xs font-bold text-slate-950 hover:bg-sky-400 disabled:opacity-50">
            <Plus className="h-3.5 w-3.5" />Add
          </button>
        </div>
        {clash && <p role="alert" data-testid="allow-conflict" className="rounded-lg border border-rose-500/40 bg-rose-950/20 p-2 text-[11px] text-rose-200">
          &ldquo;{clash}&rdquo; is on the <b>blocked</b> list. A link cannot be both allowed and blocked - remove it from the blocked list first.</p>}
        <p className="text-[10px] text-slate-400">Cloud-metadata / link-local addresses (e.g. 169.254.169.254) can never be added.</p>
      </div>
    </Card>
  );
}

/* ------------------------------------------------------------------------------------------------ */
export function BlockedUrlsPanel({ policy, run }: { policy: AdminPolicy | null; run: Run }) {
  const [url, setUrl] = useState("");
  const [note, setNote] = useState("");
  const clash = conflictWith(url, policy?.url_exceptions);
  const add = async () => {
    const ok = await run(() => adminApi.addBlocked(url, note));
    if (ok) { setUrl(""); setNote(""); }
  };
  return (
    <Card title="Blocked URLs" icon={<ShieldBan className="h-4 w-4 text-rose-400" />}
      right={<Badge tone="red">{policy?.url_blocklist?.length ?? 0} blocked</Badge>}>
      <p className="mb-3 text-xs text-slate-400">
        Hosts Understudy must never touch: the analysis browser, snapshots, redirects and the generated automation&apos;s own
        network traffic are all refused. Use <code className="rounded bg-slate-800 px-1">*.example.com</code> to block every sub-domain.
        A link can&apos;t be on both the allowed and the blocked list.
      </p>
      <div className="max-h-[220px] space-y-2 overflow-y-auto pr-1" data-testid="blocked-list">
        {(policy?.url_blocklist ?? []).length === 0 && <p className="text-xs text-slate-400">Nothing blocked yet.</p>}
        {policy?.url_blocklist?.map((e) => (
          <div key={e.id} className="flex items-center justify-between gap-2 rounded-lg border border-rose-500/30 bg-rose-950/10 p-2.5">
            <div className="min-w-0">
              <div className="truncate text-xs font-semibold text-slate-200">{e.note || e.host}</div>
              <div className="truncate font-mono text-[11px] text-slate-300">{e.host}</div>
              <div className="text-[10px] text-slate-400">added by {e.added_by}</div>
            </div>
            <button aria-label={`Unblock ${e.host}`} title="Unblock" onClick={() => run(() => adminApi.removeBlocked(e.id))}
              className="rounded-lg p-1.5 text-slate-400 hover:bg-slate-800 hover:text-rose-300"><Trash2 className="h-4 w-4" /></button>
          </div>
        ))}
      </div>
      <div className="mt-3 space-y-2 border-t border-slate-800 pt-3">
        <input aria-label="URL to block" className={input} value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://social.example.com or *.tracker.example.net" />
        <div className="flex gap-2">
          <input aria-label="Reason" className={input} value={note} onChange={(e) => setNote(e.target.value)} placeholder="Reason (e.g. No social media)" />
          <button disabled={!url.trim() || !!clash} onClick={add} className="flex items-center gap-1 whitespace-nowrap rounded-lg bg-rose-500 px-3 py-2 text-xs font-bold text-white hover:bg-rose-400 disabled:opacity-50">
            <Plus className="h-3.5 w-3.5" />Block
          </button>
        </div>
        {clash && <p role="alert" data-testid="block-conflict" className="rounded-lg border border-rose-500/40 bg-rose-950/20 p-2 text-[11px] text-rose-200">
          &ldquo;{clash}&rdquo; is on the <b>allowed</b> list. A link cannot be both allowed and blocked - remove it from the allowed list first.</p>}
        <p className="text-[10px] text-slate-400">Always blocked, nothing to add: {(policy?.always_blocked ?? []).join(", ")}.</p>
      </div>
    </Card>
  );
}

/** Restricted network for generated automation: who a run may talk to. */
export function NetworkModePanel({ policy, run }: { policy: AdminPolicy | null; run: Run }) {
  const mode = policy?.settings.network_mode ?? "restricted";
  const opt = (m: "restricted" | "public", title: string, text: string) => (
    <label className={`flex cursor-pointer gap-2 rounded-lg border p-2.5 text-xs ${mode === m ? "border-emerald-500/60 bg-emerald-950/20" : "border-slate-800 bg-slate-950/70"}`}>
      <input type="radio" name="netmode" className="mt-0.5" checked={mode === m} onChange={() => run(() => adminApi.setNetworkMode(m))} />
      <span><b className="text-slate-100">{title}</b><br /><span className="text-slate-400">{text}</span></span>
    </label>
  );
  return (
    <Card title="Network for automation runs" icon={<Network className="h-4 w-4 text-emerald-400" />}
      right={<Badge tone="green">{mode}</Badge>}>
      <p className="mb-3 text-xs text-slate-400">
        Every run goes through its own egress proxy. The blocked list, cloud-metadata and private ranges are denied in both modes.
      </p>
      <div role="radiogroup" aria-label="Network mode" className="space-y-2">
        {opt("restricted", "Restricted (recommended)", "The run may reach only its target host and the allowed list above.")}
        {opt("public", "Public internet", "Any public host except the blocked list (for pages that load third-party assets).")}
      </div>
      <ProxyBypassEditor policy={policy} run={run} />
    </Card>
  );
}

/** 8.3: hosts runs reach WITHOUT the run proxy. Loopback is always there (the tool's local driver lives on localhost). */
function ProxyBypassEditor({ policy, run }: { policy: AdminPolicy | null; run: Run }) {
  const all = policy?.settings.proxy_bypass || ["localhost", "127.0.0.1", "::1"];
  const fixed = ["localhost", "127.0.0.1", "::1"];
  const extra = all.filter((h) => !fixed.includes(h));
  const [text, setText] = useState("");
  return (
    <div className="mt-3 space-y-1.5 border-t border-slate-800 pt-2 text-[11px]" data-testid="proxy-bypass">
      <div className="font-semibold text-slate-200">Reached without the run proxy</div>
      <div className="flex flex-wrap gap-1">
        {fixed.map((h) => <Badge key={h} tone="slate">{h}</Badge>)}
        {extra.map((h) => (
          <span key={h} className="inline-flex items-center gap-1 rounded-md border border-sky-500/50 bg-sky-500/10 px-2 py-0.5 font-mono text-sky-100">{h}
            <button type="button" aria-label={`Remove ${h}`} onClick={() => run(() => adminApi.setProxyBypass(extra.filter((x) => x !== h)))} className="text-slate-400 hover:text-rose-300">×</button></span>
        ))}
      </div>
      <p className="text-slate-400">localhost is always direct: the tool&apos;s own driver (ChromeDriver, geckodriver) listens there. Add an internal host only if
        the proxy can&apos;t reach it.</p>
      <div className="flex gap-1">
        <input className={input} value={text} onChange={(e) => setText(e.target.value)} placeholder="host, *.domain or IP" aria-label="Host to bypass the run proxy" />
        <button type="button" disabled={!text.trim()} onClick={async () => { if (await run(() => adminApi.setProxyBypass([...extra, text.trim()]))) setText(""); }}
          className="rounded bg-sky-500 px-2.5 font-semibold text-slate-950 hover:bg-sky-400 disabled:opacity-50">Add</button>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------------------------------------ */
/** Choose which versions of a tool users may run (from the live package registry) and the default. */
export function ToolVersionsEditor({ tool, run }: { tool: WhitelistedTool; run: Run }) {
  const [info, setInfo] = useState<AvailableVersions | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [draft, setDraft] = useState<{ allowed: string[]; def: string | null } | null>(null);
  const [showAll, setShowAll] = useState(false);
  const [typed, setTyped] = useState("");

  const load = useCallback(async (refresh: boolean) => {
    setLoading(true);
    try {
      const v = await adminApi.versions(tool.id, refresh);
      setInfo(v);
      setErr(null);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [tool.id]);

  useEffect(() => {
    const t = setTimeout(() => { void load(false); }, 0);
    return () => clearTimeout(t);
  }, [load]);

  const allowed = draft?.allowed ?? tool.versions.allowed;
  const def = draft ? draft.def : tool.versions.default;
  const dirty = draft !== null;
  const list = Array.from(new Set([...(info?.versions || []), ...tool.versions.allowed, ...(draft?.allowed || [])]));
  const shown = showAll ? list : list.slice(0, 12);

  const toggle = (v: string) => {
    const next = allowed.includes(v) ? allowed.filter((x) => x !== v) : [...allowed, v];
    const nextDef = next.length === 0 ? null : def && next.includes(def) ? def : next[0];
    setDraft({ allowed: next, def: nextDef });
  };

  return (
    <div className="mt-2 space-y-2 rounded-lg border border-slate-800 bg-slate-950/80 p-3">
      <div className="flex flex-wrap items-center gap-2 text-[11px] text-slate-400">
        <Layers className="h-3.5 w-3.5 text-sky-400" />
        <span>Package <code className="text-slate-200">{info?.package || tool.packages[0]?.name}</code> ({tool.ecosystem})</span>
        {info && (
          <Badge tone={info.source === "registry" ? "green" : "amber"}>
            {info.source === "registry" ? "live from registry" : "registry unreachable - partial list"}
          </Badge>
        )}
        {info?.latest && <span>latest <b className="text-slate-200">{info.latest}</b></span>}
        {info?.installed && <span>installed on server <b className="text-slate-200">{info.installed}</b></span>}
        <button type="button" onClick={() => { void load(true); }} className="ml-auto inline-flex items-center gap-1 text-sky-400 hover:underline">
          <RefreshCw className={cx("h-3 w-3", loading && "animate-spin")} />Refresh
        </button>
      </div>
      {err && <div className="text-[11px] text-rose-300">{err}</div>}
      {info?.error && info.source !== "registry" && <div className="text-[10px] text-amber-300/80">{info.error}</div>}
      {loading && !info ? <div className="text-[11px] text-slate-400">Loading available versions...</div> : (
        <>
          <p className="text-[11px] text-slate-400">
            Tick the versions users may choose. The starred one is the default. Leave all unticked for no restriction
            (uses <code>{info?.catalog_constraint || "the vetted constraint"}</code>).
          </p>
          <div className="flex flex-wrap gap-1.5">
            {shown.map((v) => {
              const on = allowed.includes(v);
              return (
                <span key={v} className={cx("inline-flex items-center overflow-hidden rounded-md border text-[11px]",
                  on ? "border-sky-500/60 bg-sky-500/10 text-sky-200" : "border-slate-700 text-slate-400")}>
                  <button type="button" onClick={() => toggle(v)} className="px-2 py-0.5 font-mono">{v}</button>
                  {on && (
                    <button type="button" title="Make default" onClick={() => setDraft({ allowed, def: v })}
                      className={cx("border-l border-sky-500/40 px-1.5 py-0.5", def === v ? "text-amber-300" : "text-slate-400 hover:text-amber-200")}>★</button>
                  )}
                </span>
              );
            })}
            <span className="inline-flex items-center gap-1">
              <input value={typed} onChange={(e) => setTyped(e.target.value.trim())} placeholder="add version"
                aria-label={`Add a version of ${tool.name}`} className="w-24 rounded-md border border-slate-700 bg-slate-950 px-1.5 py-0.5 font-mono text-[11px] text-slate-100" />
              <button type="button" disabled={!/^[0-9A-Za-z.+_-]{1,40}$/.test(typed) || allowed.includes(typed)}
                onClick={() => { setDraft({ allowed: [...allowed, typed], def: def || typed }); setTyped(""); }}
                className="rounded-md border border-slate-700 px-1.5 py-0.5 text-[11px] text-sky-300 disabled:opacity-40" title="For versions on a private mirror that the public registry doesn't list">Add</button>
            </span>
            {list.length > 12 && (
              <button type="button" onClick={() => setShowAll(!showAll)} className="text-[11px] text-sky-400 hover:underline">
                {showAll ? "fewer" : `+${list.length - 12} more`}
              </button>
            )}
          </div>
          <div className="flex items-center gap-2 pt-1">
            <span className="text-[11px] text-slate-400">
              {allowed.length ? <>Users may use <b className="text-slate-200">{allowed.join(", ")}</b> · default <b className="text-amber-300">{def}</b></> : "No restriction"}
            </span>
            <div className="ml-auto flex gap-2">
              {dirty && <button type="button" onClick={() => setDraft(null)} className="rounded-lg px-2.5 py-1 text-[11px] text-slate-400 hover:bg-slate-800">Cancel</button>}
              {allowed.length > 0 && !dirty && (
                <button type="button" onClick={() => setDraft({ allowed: [], def: null })} className="rounded-lg px-2.5 py-1 text-[11px] text-slate-400 hover:bg-slate-800">Remove restriction</button>
              )}
              <button type="button" disabled={!dirty}
                onClick={async () => { if (await run(() => adminApi.saveVersions(tool.id, allowed, def))) setDraft(null); }}
                className="rounded-lg bg-sky-500 px-3 py-1 text-[11px] font-bold text-slate-950 hover:bg-sky-400 disabled:opacity-40">Save versions</button>
            </div>
          </div>
        </>
      )}
    </div>
  );
}

/** 9.0: an approved tool is open to every user, or only to the users the admin picks; 9.9: or to no user. */
const ACCESS_LABEL: Record<AccessMode, string> = { all: "All users", selected: "Only selected users", none: "No user" };
function ToolAccessEditor({ tool, run, onDone }: { tool: WhitelistedTool; run: Run; onDone: () => void }) {
  const cur = tool.access || { mode: "all", users: [] };
  const [mode, setMode] = useState<AccessMode>(cur.mode);
  const [picked, setPicked] = useState<string[]>(cur.users);
  const [users, setUsers] = useState<StudioUser[] | null>(null);
  const [filter, setFilter] = useState("");
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    adminApi.users().then((r) => { if (live) setUsers(r.users); }).catch((e) => { if (live) setErr((e as Error).message); });
    return () => { live = false; };
  }, []);
  const toggle = (u: string) => setPicked(picked.includes(u) ? picked.filter((x) => x !== u) : [...picked, u]);
  const shown = (users || []).filter((u) => !filter || `${u.username} ${u.display_name} ${u.email || ""}`.toLowerCase().includes(filter.toLowerCase()));
  return (
    <div className="mt-2 space-y-2 rounded-lg border border-sky-500/30 bg-sky-500/5 p-2 text-[11px]" data-testid="tool-access">
      <div role="radiogroup" aria-label={`Who may use ${tool.name}`} className="flex flex-wrap gap-3">
        {(["all", "selected", "none"] as const).map((m) => (
          <label key={m} className="flex cursor-pointer items-center gap-1.5 text-slate-200">
            <input type="radio" name={`access-${tool.id}`} checked={mode === m} onChange={() => setMode(m)} />
            {ACCESS_LABEL[m]}
          </label>
        ))}
      </div>
      {mode === "none" && <p className="text-slate-400" data-testid="tool-access-none">Nobody can pick or run {tool.name}; it stays
        installed and approved, so you can open it again any time. Sessions already using it are told to ask you for access.</p>}
      {mode === "selected" && (
        <div className="space-y-1.5">
          <input className={input} value={filter} onChange={(e) => setFilter(e.target.value)} placeholder="Search users" aria-label="Search users" />
          {err && <p className="text-rose-300">{err}</p>}
          {!users && !err && <p className="text-slate-400">Loading users...</p>}
          {users && users.length === 0 && <p className="text-slate-400">No Studio users yet - add them in Users.</p>}
          <div className="grid max-h-44 gap-1 overflow-y-auto sm:grid-cols-2">
            {shown.map((u) => (
              <label key={u.username} className="flex cursor-pointer items-center gap-1.5 rounded px-1 py-0.5 hover:bg-slate-800">
                <input type="checkbox" checked={picked.includes(u.username.toLowerCase())} onChange={() => toggle(u.username.toLowerCase())} />
                <span className="text-slate-100">{u.display_name || u.username}</span>
                <span className="truncate text-slate-500">{u.username}{u.email ? ` · ${u.email}` : ""}</span>
              </label>
            ))}
            {users && picked.filter((p) => !users.some((u) => u.username.toLowerCase() === p)).map((p) => (
              <label key={p} className="flex cursor-pointer items-center gap-1.5 rounded px-1 py-0.5 hover:bg-slate-800" title="No Studio account with this name (deleted?)">
                <input type="checkbox" checked onChange={() => toggle(p)} />
                <span className="text-slate-100">{p}</span><span className="text-amber-300">no such account</span>
              </label>
            ))}
          </div>
          <p className="text-slate-400">{picked.length} selected - everyone else sees {tool.name} as locked.</p>
        </div>
      )}
      <div className="flex justify-end gap-2">
        <button type="button" onClick={onDone} className="rounded-lg px-2.5 py-1 text-slate-400 hover:bg-slate-800">Cancel</button>
        <button type="button" disabled={mode === "selected" && picked.length === 0}
          onClick={async () => { if (await run(() => adminApi.setToolAccess(tool.id, mode, mode === "selected" ? picked : []))) onDone(); }}
          className="rounded-lg bg-sky-500 px-3 py-1 font-bold text-slate-950 hover:bg-sky-400 disabled:opacity-40">Save access</button>
      </div>
    </div>
  );
}

export function ToolRow({ tool, run }: { tool: WhitelistedTool; run: Run }) {
  const [open, setOpen] = useState(false);
  const [accessOpen, setAccessOpen] = useState(false);
  const acc = tool.access || { mode: "all", users: [] };
  const missing = tool.toolchain_missing || [];
  const pinned = tool.versions?.allowed || [];
  const src = tool.sources || {};
  return (
    <div className="rounded-lg border border-slate-700 bg-slate-950/70 p-2.5" data-testid="approved-tool">
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <Check className="h-3.5 w-3.5 text-emerald-400" aria-hidden />
        <span className="font-medium text-slate-100">{tool.name}</span>
        <span className="text-[10px] text-slate-400">{tool.language} · {tool.ecosystem} · AI-written code
          {tool.whitelist_source ? ` · ${tool.whitelist_source}` : ""}</span>
        {(src.index_url || src.local_dir) && <Badge tone="violet" title="Packages come from here instead of the public registry">{src.index_url || src.local_dir}</Badge>}
        <span className="ml-auto flex items-center gap-1.5">
          {missing.length > 0 && <Badge tone="red" title="Not installed on the machine running the backend">needs {missing.join(", ")}</Badge>}
          {pinned.length ? <Badge tone="sky">v{tool.versions.default}{pinned.length > 1 ? ` +${pinned.length - 1}` : ""}</Badge> : <Badge tone="slate">any version</Badge>}
          <button type="button" onClick={() => setAccessOpen(!accessOpen)} data-testid="tool-access-toggle"
            title={acc.mode === "all" ? "Every user can use this tool - click to limit it"
              : acc.mode === "none" ? "No user can use this tool - click to open it" : `Only: ${acc.users.join(", ")}`}
            className={cx("inline-flex items-center gap-1 rounded-md border px-1.5 py-0.5 text-[11px] hover:border-sky-500",
              acc.mode === "none" ? "border-rose-500/50 text-rose-200" : "border-slate-700 text-slate-200")}>
            <Users className="h-3 w-3" />{acc.mode === "all" ? "All users" : acc.mode === "none" ? "No user"
              : `${acc.users.length} user${acc.users.length === 1 ? "" : "s"}`}
          </button>
          <button type="button" onClick={() => setOpen(!open)} className="inline-flex items-center gap-0.5 text-[11px] text-sky-400 hover:underline">
            {open ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}Versions
          </button>
          {!tool.builtin && <button type="button" aria-label={`Delete ${tool.name}`} title="Delete this tool for everyone"
            onClick={() => { if (window.confirm(`Delete ${tool.name}? No user can pick or run it any more, and what its setup installed on the server is removed. Sessions that used it will be told it was removed. You can set it up again later with the AI install assistant.`)) void run(() => adminApi.revokeTool(tool.id)); }}
            className="rounded p-1 text-slate-400 hover:bg-slate-800 hover:text-rose-300"><Trash2 className="h-3.5 w-3.5" /></button>}
        </span>
      </div>
      {accessOpen && <ToolAccessEditor tool={tool} run={run} onDone={() => setAccessOpen(false)} />}
      {open && <ToolVersionsEditor tool={tool} run={run} />}
      {open && <ToolRuntimeEditor tool={tool} run={run} />}
    </div>
  );
}

/** 8.3: the run settings every run of this tool gets - environment variables and PATH folders - edited by the admin. */
function ToolRuntimeEditor({ tool, run }: { tool: WhitelistedTool; run: Run }) {
  const rt = tool.runtime || { env_vars: {}, path_prepend: [] };
  const [rows, setRows] = useState<{ k: string; v: string }[]>(() => Object.entries(rt.env_vars || {}).map(([k, v]) => ({ k, v })));
  const [paths, setPaths] = useState((rt.path_prepend || []).join("\n"));
  const save = () => run(() => adminApi.setToolRuntime(tool.id, Object.fromEntries(rows.filter((r) => r.k.trim()).map((r) => [r.k.trim().toUpperCase(), r.v])),
    paths.split(/\r?\n/).map((p) => p.trim()).filter(Boolean)));
  return (
    <div className="mt-2 space-y-1.5 rounded-lg border border-slate-800 bg-slate-950/60 p-2 text-[11px]" data-testid="tool-runtime">
      <div className="font-semibold text-slate-200">Run settings (every run of {tool.name})</div>
      <p className="text-slate-400">Environment variables and PATH folders the tool gets - e.g. CHROME_BIN, JAVA_HOME, NO_PROXY. Secrets don&apos;t belong
        here; secret values come from the user&apos;s payload.</p>
      {rows.map((r, i) => (
        <div key={i} className="flex gap-1">
          <input className={input} value={r.k} placeholder="NAME" aria-label="Variable name" onChange={(e) => setRows(rows.map((x, j) => j === i ? { ...x, k: e.target.value } : x))} />
          <input className={input} value={r.v} placeholder="value" aria-label="Variable value" onChange={(e) => setRows(rows.map((x, j) => j === i ? { ...x, v: e.target.value } : x))} />
          <button type="button" aria-label="Remove variable" onClick={() => setRows(rows.filter((_, j) => j !== i))} className="px-1 text-slate-400 hover:text-rose-300"><Trash2 className="h-3.5 w-3.5" /></button>
        </div>
      ))}
      <button type="button" onClick={() => setRows([...rows, { k: "", v: "" }])} className="text-sky-400 hover:underline">+ add variable</button>
      <textarea className={cx(input, "h-14 font-mono")} value={paths} onChange={(e) => setPaths(e.target.value)} aria-label="PATH folders"
        placeholder="PATH folders on the server, one per line (put first on PATH)" />
      <button type="button" onClick={() => { void save(); }} className="rounded bg-emerald-500 px-2.5 py-1 font-semibold text-slate-950 hover:bg-emerald-400">Save run settings</button>
    </div>
  );
}

/** 8.5: the setup repair agent - on/off and the stronger model for an error that comes back after a fix. */
function SetupAgentSetting({ policy, run }: { policy: AdminPolicy | null; run: Run }) {
  const st = policy?.settings?.setup_agent;
  const [model, setModel] = useState<string | null>(null);
  const value = model ?? st?.escalation_model ?? "";
  if (!st) return null;
  return (
    <div className="mb-3 space-y-2 border-t border-slate-800 pt-3 text-xs" data-testid="setup-agent-setting">
      <div className="text-[11px] font-bold uppercase text-slate-400">Setup agent (failed tool setups)</div>
      <label className="flex cursor-pointer items-start gap-2">
        <input type="checkbox" className="mt-0.5" checked={st.enabled} onChange={() => run(() => adminApi.setSetupAgent(!st.enabled))} />
        <span><b className="text-slate-100">Let the AI agent work on failed setups until they pass</b><br />
          <span className="text-slate-400">It runs read-only checks in the sandbox (dry run of the test, installed packages, the real API of the installed
            version), checks its own fix, and waits for your approval before changing anything. If the setup still fails it continues with what it
            already tried. It always uses the selected model; a stronger one is only offered to you as a button.{policy?.settings?.setup_lessons ? ` ${policy.settings.setup_lessons} solved problem(s) remembered.` : ""}</span></span>
      </label>
      <div className="flex items-center gap-2">
        <span className="shrink-0 text-slate-400">Stronger model to offer when the same error comes back:</span>
        <input className={input} value={value} onChange={(e) => setModel(e.target.value)} placeholder="gemini-pro-latest (empty = don't offer one)"
          aria-label="Escalation model for the setup agent" />
        <button type="button" disabled={model === null || model === st.escalation_model}
          onClick={async () => { if (await run(() => adminApi.setSetupAgent(undefined, value.trim()))) setModel(null); }}
          className="rounded-lg bg-emerald-500 px-3 py-2 text-xs font-bold text-slate-950 hover:bg-emerald-400 disabled:opacity-50">Save</button>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------------------------------------ */
export function SelfHealingPanel({ policy, run }: { policy: AdminPolicy | null; run: Run }) {
  const current = policy?.settings?.max_self_heal_retries ?? 5;
  const [draft, setDraft] = useState<string | null>(null);
  const value = draft ?? String(current);
  const mode = policy?.settings?.self_heal_approval ?? "ask";
  const setupMode = policy?.settings?.setup_fix_approval ?? "ask";
  const opt = (m: "ask" | "automatic", title: string, text: string) => (
    <label className={cx("flex cursor-pointer gap-2 rounded-lg border p-2.5 text-xs", mode === m ? "border-emerald-500/60 bg-emerald-950/20" : "border-slate-800 bg-slate-950/70")}>
      <input type="radio" name="healmode" className="mt-0.5" checked={mode === m} onChange={() => run(() => adminApi.setHealApproval(m))} />
      <span><b className="text-slate-100">{title}</b><br /><span className="text-slate-400">{text}</span></span>
    </label>
  );
  return (
    <Card title="Self-healing retries" icon={<RotateCw className="h-4 w-4 text-emerald-400" />}
      right={<Badge tone="green">{mode === "ask" ? `up to ${current}, user approves each` : `up to ${current} automatic`}</Badge>}>
      <div role="radiogroup" aria-label="Self-healing approval" className="mb-3 space-y-2">
        {opt("ask", "Ask the user before every retry (recommended)", "The user sees what went wrong, what the AI changed and the script diff, and approves or corrects it.")}
        {opt("automatic", "Retry automatically", "Fixes are applied and re-run without asking, until it works or the limit is reached.")}
      </div>
      <div role="radiogroup" aria-label="AI fixes for tool setups" className="mb-3 space-y-2 border-t border-slate-800 pt-3">
        <div className="text-[11px] font-bold uppercase text-slate-400">AI fixes for tool setups (Mission Control)</div>
        {(["ask", "automatic"] as const).map((m) => (
          <label key={m} className={cx("flex cursor-pointer gap-2 rounded-lg border p-2.5 text-xs", setupMode === m ? "border-emerald-500/60 bg-emerald-950/20" : "border-slate-800 bg-slate-950/70")}>
            <input type="radio" name="setupfixmode" className="mt-0.5" checked={setupMode === m} onChange={() => run(() => adminApi.setSetupFixApproval(m))} />
            <span><b className="text-slate-100">{m === "ask" ? "Show me the AI's fix first (recommended)" : "Apply safe fixes automatically"}</b><br />
              <span className="text-slate-400">{m === "ask" ? "When a setup fails, the AI's fix appears as one click - \"Apply the AI's fix\" - with exactly what it changes."
                : "Missing packages, missing init steps and smoke-test rewrites are applied by themselves (max 4 per setup); everything else still asks you."}</span></span>
          </label>
        ))}
      </div>
      <SetupAgentSetting policy={policy} run={run} />
      <p className="mb-3 text-xs text-slate-400">
        Maximum retries per run (1-50, default 5). Users can stop at any time.
      </p>
      <div className="flex gap-2">
        <input type="number" min={1} max={50} aria-label="Maximum automatic retries" className={input} value={value} onChange={(e) => setDraft(e.target.value)} />
        <button disabled={draft === null || draft === String(current) || draft === "" || Number(value) < 1 || Number(value) > 50}
          onClick={async () => { if (await run(() => adminApi.saveSettings(Number(value)))) setDraft(null); }}
          className="rounded-lg bg-emerald-500 px-3 py-2 text-xs font-bold text-slate-950 hover:bg-emerald-400 disabled:opacity-50">Save</button>
      </div>
    </Card>
  );
}
