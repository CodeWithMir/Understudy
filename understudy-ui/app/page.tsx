"use client";

/**
 * Mission Control - the Understudy admin console.
 *
 * Tabs:
 *   Telemetry        - detailed activity log (user actions, exact AI prompts/responses, errors, heuristic fallbacks)
 *   Environments     - 8.4: runtime versions on the server (Java, Python, Node.js ...), the version each tool uses and needs
 *   Access & tools   - target-URL allowed + blocked lists, network mode, the zero-trust tool registry, the AI tool
 *                      synthesizer (setup plan, manifest, clarification chat, smoke test), the sandbox, self-healing retry limit
 * A live inbox (WebSocket, polling fallback) shows a bell with the requests that need the admin and toasts.
 *   Learned skills   - lessons Understudy wrote from real errors (SKILL.md files) and applies before the next run
 *   Code health      - AI self-heal of start-up warnings (verified on a copy, backup + rollback)
 *   AI & security    - Gemini key and model used by every AI call, Model Armor interceptions, admin account
 *
 * The console is password protected (default credentials in README.md; a changed password lasts until restart).
 */
import React, { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import {
  Activity, AlertTriangle, ArrowRight, BarChart3, BookOpen, Bot, CheckCircle2, Cpu, Eye, EyeOff, FileCode2, Flag, Key, LogOut,
  PanelLeftClose, PanelLeftOpen, ShieldAlert, ShieldCheck, Sliders, Trash2, Users, Wrench,
} from "lucide-react";
import {
  clearGoogleApiKey, clearSecurityAlerts, getApiKeyStatus, getAvailableModels, getSecurityAlerts, setGoogleApiKey,
  setSelectedModel, validateGoogleApiKey, type ApiKeyStatus, type DiscoveredModel, type SecurityAlert,
} from "../lib/api";
import { adminApi, clearAdminToken, getAdminToken, type ActivityPage, type AuthStatus, type ChangesSummary, type GcpCheckReport } from "../lib/admin";
import { AccountPanel, CodeHealthPanel, LoginScreen, RecallPanel, SkillsPanel, UsersPanel } from "./admin-extra";
import { BlockedUrlsPanel, NetworkModePanel, SelfHealingPanel, UrlExceptionsPanel, useAdminPolicy } from "./admin-panels";
import { ToolAccessPanel } from "./tool-setup-panel";
import { ModelArmorPanel } from "./model-armor-panel";
import { PackageManagersPanel } from "./package-managers-panel";
import { InboxBell, RegistryPanel, SandboxPanel, Toasts, useAdminInbox } from "./synthesizer";
import { TelemetryPanel } from "./telemetry-panel";
import { ThemeToggle } from "./theme-toggle";
import { EscalationsPanel } from "./escalations-panel";
import { EnvironmentsPanel } from "./environments-panel";
import { Badge, Card, CardsToolbar, cx, Fold } from "./wizard/ui";

type Tab = "telemetry" | "users" | "access" | "environments" | "escalations" | "skills" | "health" | "ai";
const FALLBACK_MODELS = ["gemini-flash-latest", "gemini-2.5-flash", "gemini-2.5-pro", "gemini-2.5-flash-lite"];

function StatTile({ icon, label, value, sub, tone = "slate" }: {
  icon: React.ReactNode; label: string; value: React.ReactNode; sub?: React.ReactNode; tone?: "slate" | "green" | "amber" | "red" | "sky";
}) {
  const ring = { slate: "border-slate-800", green: "border-emerald-500/30", amber: "border-amber-500/30", red: "border-rose-500/40", sky: "border-sky-500/30" }[tone];
  return (
    <div className={cx("rounded-xl border bg-slate-900/70 px-4 py-3", ring)}>
      <div className="flex items-center gap-2 text-[11px] font-semibold uppercase tracking-wider text-slate-400">{icon}{label}</div>
      <div className="mt-1 text-2xl font-bold text-white">{value}</div>
      {sub && <div className="text-[11px] text-slate-400">{sub}</div>}
    </div>
  );
}

function GeminiPanel({ status, onChanged }: { status: ApiKeyStatus | null; onChanged: () => Promise<void> }) {
  const [models, setModels] = useState<DiscoveredModel[]>([]);
  const [model, setModel] = useState<string>("");
  const [keyInput, setKeyInput] = useState("");
  const [show, setShow] = useState(false);
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean | null; text: string } | null>(null);
  const active = model || status?.selected_model || "gemini-flash-latest";
  const [listInfo, setListInfo] = useState<{ source: string; error: string; busy: boolean }>({ source: "", error: "", busy: false });
  const loadModels = async () => {
    setListInfo((i) => ({ ...i, busy: true }));
    try {
      const data = await getAvailableModels();
      if (data?.models?.length) setModels(data.models);
      setListInfo({ source: data?.source || "", error: data?.error || "", busy: false });
    } catch (e) {
      setListInfo({ source: "", error: (e as Error).message, busy: false });   // offline: fallback list
    }
  };

  useEffect(() => {
    const t = setTimeout(() => { void loadModels(); }, 0);
    return () => clearTimeout(t);
  }, []);

  const changeModel = async (m: string) => {
    setModel(m);
    try { await setSelectedModel(m); await onChanged(); } catch (e) { setMsg({ ok: false, text: (e as Error).message }); }
  };
  const save = async () => {
    const k = keyInput.trim();
    if (!k) return;
    setSaving(true);
    setMsg({ ok: null, text: `Validating the key with Google (${active})...` });
    try {
      const v = await validateGoogleApiKey(k, active);
      if (!v.valid) { setMsg({ ok: false, text: v.message || "Google rejected this key." }); return; }
      await setGoogleApiKey(k, active, false, true);
      await onChanged();
      adminApi.codeHealthRun().catch(() => null);      // with AI available, code health can now propose fixes
      setKeyInput("");
      setMsg({ ok: true, text: `Key active. Every AI call now uses ${active}.` });
    } catch (e) {
      setMsg({ ok: false, text: (e as Error).message || "Failed to save the key." });
    } finally {
      setSaving(false);
    }
  };
  const clear = async () => {
    try { await clearGoogleApiKey(); await onChanged(); setMsg({ ok: true, text: "Key removed. Understudy now uses its local heuristics." }); }
    catch { setMsg({ ok: false, text: "Failed to clear the key." }); }
  };

  const list = models.length ? models.map((m) => ({ id: m.id, label: `${m.displayName || m.id}${m.isRecommended ? " (recommended)" : ""}` }))
    : FALLBACK_MODELS.map((m) => ({ id: m, label: m }));
  if (!list.some((m) => m.id === active)) list.unshift({ id: active, label: active });

  const pf = status?.platform;
  const vertex = pf?.mode === "vertex";
  const aiActive = Boolean(status?.has_key || vertex);
  return (
    <Card title="Gemini (Google AI) & model" icon={<Key className="h-4 w-4 text-amber-400" />}
      right={<Badge tone={aiActive ? "green" : "amber"}>{aiActive ? "AI active" : "heuristic fallback"}</Badge>}>
      <div className="space-y-4">
        {pf && (
          <dl className="grid grid-cols-2 gap-2 rounded-lg border border-slate-800 bg-slate-950/60 p-3 text-[11px] sm:grid-cols-4">
            <div><dt className="text-slate-400">AI platform</dt><dd className="font-medium text-slate-200">{pf.platform}</dd></div>
            <div><dt className="text-slate-400">SDK</dt><dd className="font-mono text-slate-200">{pf.sdk || "-"}</dd></div>
            <div><dt className="text-slate-400">State store</dt><dd className={cx("font-medium", pf.store_degraded ? "text-rose-300" : "text-slate-200")}
              title={pf.store_degraded || undefined}>{pf.store === "firestore" ? "Cloud Firestore" : pf.store_degraded ? "Local files (Firestore unavailable)" : "Local files"}</dd></div>
            <div><dt className="text-slate-400">Recording uploads</dt>
              <dd className="text-slate-200">{pf.media_cache ? `${pf.media_cache.uploads} uploaded · ${pf.media_cache.reused} reused` : "-"}</dd></div>
            {vertex && <div className="col-span-2 sm:col-span-4 text-slate-400">Vertex AI project <b className="font-mono text-slate-200">{pf.project}</b> ({pf.location}) - authenticated with the service account (Application Default Credentials), so no API key is needed{pf.gcs_bucket ? "; recordings go through Cloud Storage" : ""}.</div>}
          </dl>
        )}
        <label className="block space-y-1.5">
          <span className="text-[11px] font-semibold uppercase tracking-wide text-slate-400">Model used by every AI call</span>
          <select value={active} onChange={(e) => { void changeModel(e.target.value); }}
            className="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 font-mono text-xs text-slate-200 focus:border-amber-500 focus:outline-none">
            {list.map((m) => <option key={m.id} value={m.id}>{m.label}</option>)}
          </select>
          <span className="block text-[11px] text-slate-400">WATCH video analysis, BUILD code generation, GOVERN rule extraction and review, the complexity summary, self-healing and agentic runs all use this model.</span>
          <span className="flex flex-wrap items-center gap-2 text-[11px] text-slate-400" data-testid="model-list-source">
            {listInfo.source === "vertex" ? `${models.length} models offered by Vertex AI to this project`
              : listInfo.source === "studio" ? `${models.length} models your API key can use`
              : "Built-in suggestions - the live list couldn't be loaded"}
            <button type="button" onClick={(e) => { e.preventDefault(); void loadModels(); }} disabled={listInfo.busy}
              className="rounded border border-slate-700 px-1.5 py-0.5 text-sky-300 hover:border-sky-500 disabled:opacity-50">
              {listInfo.busy ? "Refreshing..." : "Refresh list"}</button>
          </span>
          {listInfo.error && <span className="block text-[11px] text-amber-300">{listInfo.error}</span>}
        </label>
        {vertex ? (
          <p className="text-[11px] text-slate-400" data-testid="vertex-no-key">No API key on Vertex AI: every AI call uses the service account. A
            key pasted here would not be used.</p>
        ) : (
        <div className="space-y-1.5">
          <div className="flex items-center justify-between">
            <span className="text-[11px] font-semibold uppercase tracking-wide text-slate-400">API key</span>
            {status?.has_key && <span className="font-mono text-[11px] text-emerald-400">{status.masked_key}</span>}
          </div>
          <div className="flex gap-2">
            <div className="relative flex-1">
              <input type={show ? "text" : "password"} value={keyInput} onChange={(e) => setKeyInput(e.target.value)}
                placeholder={status?.has_key ? "Key active - paste a new key to replace it" : "Paste your Gemini API key (AIza...)"}
                className="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 pr-9 font-mono text-xs text-white placeholder:text-slate-400 focus:border-amber-500 focus:outline-none" />
              <button type="button" onClick={() => setShow(!show)} className="absolute right-2.5 top-2.5 text-slate-400 hover:text-slate-200" title={show ? "Hide" : "Show"}>
                {show ? <EyeOff className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}
              </button>
            </div>
            <button type="button" onClick={save} disabled={saving || !keyInput.trim()}
              className="rounded-lg bg-amber-500 px-3.5 py-2 text-xs font-bold text-black hover:bg-amber-400 disabled:opacity-50">{saving ? "Checking..." : "Save"}</button>
            {status?.has_key && (
              <button type="button" onClick={clear} className="rounded-lg border border-slate-700 bg-slate-800 px-2.5 py-2 text-xs text-slate-300 hover:text-rose-300">Clear</button>
            )}
          </div>
          {msg && <p className={cx("text-[11px]", msg.ok === true ? "text-emerald-400" : msg.ok === false ? "text-rose-400" : "text-amber-400")}>{msg.text}</p>}
          <p className="text-[11px] text-slate-400">The key is saved in the backend&apos;s .env so it survives restarts. It is never shown in logs or telemetry (masked), never put in the exported ZIP, and never passed to the automation process.</p>
        </div>
        )}
      </div>
    </Card>
  );
}

/** Mission Control > AI, security & account: one click checks the whole Google Cloud / Gemini set-up for real. */
function GoogleCloudCheckPanel() {
  const [rep, setRep] = useState<GcpCheckReport | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const run = async (network: boolean) => {
    setBusy(true); setErr("");
    try { setRep(await adminApi.gcpCheck(network)); } catch (e) { setErr((e as Error).message); } finally { setBusy(false); }
  };
  const tone = { PASS: "text-emerald-300", FAIL: "text-rose-300", WARN: "text-amber-300", SKIP: "text-slate-400" } as const;
  return (
    <Card title="Google Cloud set-up" icon={<CheckCircle2 className="h-4 w-4 text-sky-400" />}
      right={rep ? <Badge tone={rep.status === "PASS" ? "green" : rep.status === "FAIL" ? "red" : "amber"}>{rep.status}</Badge> : null}>
      <p className="text-xs text-slate-400">
        Checks Gemini access (API key or Vertex AI), Google credentials, Firestore and Cloud Storage. The full check makes a
        tiny real Gemini call and writes, reads and deletes a test record and a test file. Every problem shows the command that fixes it.
        Set-up steps: README &gt; &quot;Google Cloud set-up, step by step&quot;.
      </p>
      <div className="mt-3 flex flex-wrap gap-2">
        <button type="button" disabled={busy} onClick={() => { void run(true); }}
          className="rounded-lg bg-sky-500 px-3 py-1.5 text-xs font-bold text-slate-950 hover:bg-sky-400 disabled:opacity-50">
          {busy ? "Checking..." : "Check Google Cloud set-up"}</button>
        <button type="button" disabled={busy} onClick={() => { void run(false); }}
          className="rounded-lg border border-slate-700 bg-slate-800 px-3 py-1.5 text-xs text-slate-200 hover:text-white disabled:opacity-50">
          Configuration only (no calls)</button>
      </div>
      {err && <p className="mt-2 text-[11px] text-rose-400">{err}</p>}
      {rep && (
        <ul className="mt-3 space-y-2" aria-live="polite">
          {rep.checks.map((c) => (
            <li key={c.id} className="rounded-lg border border-slate-800 bg-slate-950/60 px-3 py-2 text-[11px]">
              <div className="flex items-start gap-2">
                <span className={cx("w-10 shrink-0 font-mono font-bold", tone[c.status])}>{c.status}</span>
                <span className="font-semibold text-slate-200">{c.name}</span>
                <span className="min-w-0 flex-1 break-words text-slate-400">{c.detail}</span>
              </div>
              {c.fix && c.status !== "PASS" && (
                <div className="mt-1 pl-12 text-slate-300">Fix: <code className="break-all rounded bg-slate-900 px-1 text-sky-200">{c.fix}</code></div>
              )}
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

function SecurityPanel({ alerts, onClear }: { alerts: SecurityAlert[]; onClear: () => void }) {
  return (
    <Card title="Model Armor interceptions" icon={<ShieldAlert className="h-4 w-4 text-rose-400" />}
      right={<div className="flex items-center gap-2">
        <Badge tone={alerts.length ? "red" : "green"}>{alerts.length ? `${alerts.length} blocked` : "all gates secure"}</Badge>
        {alerts.length > 0 && <button type="button" onClick={onClear} title="Clear" className="rounded p-1 text-slate-400 hover:bg-slate-800 hover:text-rose-300"><Trash2 className="h-3.5 w-3.5" /></button>}
      </div>}>
      <p className="mb-3 text-xs text-slate-400">Prompt injections and unsafe inputs stopped in URLs, SOPs, payload values, video transcripts and tool names.</p>
      <div className="max-h-[420px] space-y-2 overflow-y-auto pr-1">
        {alerts.length === 0 ? (
          <div className="rounded-lg border border-slate-800 bg-slate-950/60 p-4 text-center text-xs text-slate-400">
            <CheckCircle2 className="mx-auto mb-1 h-5 w-5 text-emerald-500/60" />Nothing blocked yet.
          </div>
        ) : alerts.map((a) => (
          <div key={a.alert_id} className="space-y-1.5 rounded-lg border border-rose-500/30 bg-rose-950/20 p-3 text-xs">
            <div className="flex items-center justify-between">
              <Badge tone="red">{a.channel}</Badge>
              <span className="font-mono text-[10px] text-slate-400">{new Date(a.timestamp).toLocaleString()}</span>
            </div>
            <code className="block break-all rounded border border-rose-900/50 bg-slate-950 px-2 py-1 font-mono text-[11px] text-rose-300">{a.prompt_injection}</code>
            <div className="text-[11px] text-slate-400">Target: <span className="text-slate-200">{a.portal}</span>{a.model_name ? <> · inspector <span className="font-mono text-emerald-400">{a.model_name}</span></> : null}</div>
          </div>
        ))}
      </div>
    </Card>
  );
}

/** Password gate: checks the stored session on load and shows the login screen when needed. */
export default function MissionControl() {
  const [auth, setAuth] = useState<AuthStatus | null>(null);
  const [state, setState] = useState<"checking" | "login" | "in">("checking");
  useEffect(() => {
    const t = setTimeout(async () => {
      if (!getAdminToken()) { setState("login"); return; }
      try { setAuth(await adminApi.me()); setState("in"); } catch { clearAdminToken(); setState("login"); }
    }, 0);
    return () => clearTimeout(t);
  }, []);
  const logout = useCallback(() => {
    adminApi.logout().catch(() => null);
    clearAdminToken();
    setAuth(null);
    setState("login");
  }, []);
  if (state === "checking") return <div className="min-h-screen bg-slate-950" />;
  if (state === "login") {
    return <LoginScreen onLoggedIn={() => { adminApi.me().then((a) => { setAuth(a); setState("in"); }).catch(() => setState("login")); }} />;
  }
  return <Console auth={auth} setAuth={setAuth} onLogout={logout} />;
}

function Console({ auth, setAuth, onLogout }: { auth: AuthStatus | null; setAuth: (a: AuthStatus) => void; onLogout: () => void }) {
  const admin = useAdminPolicy(true, onLogout);
  const [tab, setTab] = useState<Tab>("telemetry");
  const [navSlim, setNavSlimState] = useState(false);
  useEffect(() => {
    const t = setTimeout(() => { try { setNavSlimState(window.localStorage.getItem("understudy.mc.navSlim") === "1"); } catch { /* storage off */ } }, 0);
    return () => clearTimeout(t);
  }, []);
  const setNavSlim = (v: boolean) => { setNavSlimState(v); try { window.localStorage.setItem("understudy.mc.navSlim", v ? "1" : "0"); } catch { /* storage off */ } };
  const [stats, setStats] = useState<ActivityPage["stats"] | null>(null);
  const [keyStatus, setKeyStatus] = useState<ApiKeyStatus | null>(null);
  const [alerts, setAlerts] = useState<SecurityAlert[]>([]);
  const [aiChanges, setAiChanges] = useState<ChangesSummary | null>(null);

  const refreshKey = useCallback(async () => {
    try { setKeyStatus(await getApiKeyStatus()); } catch { /* backend offline */ }
  }, []);
  const refreshAlerts = useCallback(async () => {
    try { const d = await getSecurityAlerts(); if (Array.isArray(d?.alerts)) setAlerts(d.alerts); } catch { /* backend offline */ }
    try { setAiChanges((await adminApi.codeHealth()).changes); } catch { /* backend offline */ }
  }, []);

  useEffect(() => {
    const first = setTimeout(() => { void refreshKey(); void refreshAlerts(); }, 0);
    const id = setInterval(() => { void refreshKey(); void refreshAlerts(); }, 4000);
    return () => { clearTimeout(first); clearInterval(id); };
  }, [refreshKey, refreshAlerts]);

  const runPolicy = admin.run;
  const refreshPolicy = useCallback(() => { void runPolicy(adminApi.policy); }, [runPolicy]);
  const inbox = useAdminInbox(refreshPolicy);
  const escOpen = inbox.escalations;
  const openInbox = useCallback(() => {
    // 8.3: the bell goes where the admin is needed - an escalation when nothing else is waiting
    if (escOpen && !(admin.policy?.tools.pending_count ?? 0)) setTab("escalations"); else setTab("access");
  }, [escOpen, admin.policy]);
  const openRequest = useCallback((id?: string) => {
    setTab("access");
    if (id) setTimeout(() => document.getElementById(`req-${id}`)?.scrollIntoView({ behavior: "smooth", block: "start" }), 150);
  }, []);
  const pending = admin.policy?.tools.pending_count ?? 0;
  const standin = keyStatus?.standin || null;
  const aiOn = Boolean(standin || keyStatus?.has_key || keyStatus?.platform?.mode === "vertex");
  const aiOk = (stats?.AI_RESPONSE ?? 0);
  const aiFail = stats?.ai_failures ?? 0;
  const tabs: { id: Tab; label: string; icon: React.ReactNode; badge?: React.ReactNode }[] = [
    { id: "telemetry", label: "Telemetry", icon: <Activity className="h-4 w-4" /> },
    { id: "users", label: "Users", icon: <Users className="h-4 w-4" /> },
    { id: "access", label: "Access & tools", icon: <Wrench className="h-4 w-4" />, badge: pending ? <Badge tone="amber">{pending}</Badge> : null },
    { id: "environments", label: "Environments", icon: <Cpu className="h-4 w-4" /> },
    { id: "escalations", label: "Escalations", icon: <Flag className="h-4 w-4" />, badge: escOpen ? <Badge tone="amber">{escOpen}</Badge> : null },
    { id: "skills", label: "Learned skills", icon: <BookOpen className="h-4 w-4" /> },
    { id: "health", label: "Code health", icon: <FileCode2 className="h-4 w-4" />, badge: aiChanges?.in_place ? <Badge tone="amber">{aiChanges.in_place} AI change{aiChanges.in_place === 1 ? "" : "s"}</Badge> : null },
    { id: "ai", label: "AI, security & account", icon: <ShieldCheck className="h-4 w-4" />, badge: alerts.length ? <Badge tone="red">{alerts.length}</Badge> : null },
  ];

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100">
      <header className="sticky top-0 z-40 border-b border-slate-800 bg-slate-950/85 backdrop-blur">
        <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2 px-4 py-3 md:flex-nowrap md:px-6">
          <div className="flex items-center gap-3">
            <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-gradient-to-br from-sky-400 to-emerald-400 font-black text-slate-950">U</div>
            <div>
              <div className="flex items-center gap-2">
                <h1 className="text-base font-bold tracking-wide text-white">Understudy</h1>
                <Badge tone="violet"><Sliders className="h-3 w-3" />Mission Control</Badge>
              </div>
              <p className="hidden text-[11px] text-slate-400 sm:block">Admin console - what users do, what the AI was asked and answered, and what they&apos;re allowed to use</p>
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <span className={cx("hidden items-center gap-1.5 rounded-lg border px-2.5 py-1.5 text-[11px] font-medium sm:inline-flex",
              aiOn ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-300" : "border-amber-500/30 bg-amber-500/10 text-amber-300")}>
              {aiOn ? <Bot className="h-3.5 w-3.5" /> : <Cpu className="h-3.5 w-3.5" />}
              {standin ? `AI · ${standin}` : aiOn ? `${keyStatus?.platform?.mode === "vertex" ? "Vertex AI" : "AI"} · ${keyStatus?.selected_model || "gemini"}` : "No AI key - heuristics"}
            </span>
            <Link href="/wizard" className="inline-flex items-center gap-1.5 rounded-lg bg-sky-500 px-3 py-1.5 text-xs font-bold text-slate-950 hover:bg-sky-400">
              Open Understudy <ArrowRight className="h-3.5 w-3.5" />
            </Link>
            <InboxBell count={inbox.needs || pending} mode={inbox.mode} onClick={openInbox} />
            <ThemeToggle />
            <button type="button" onClick={onLogout} title={`Signed in as ${auth?.username || "admin"}`}
              className="inline-flex items-center gap-1.5 rounded-lg border border-slate-700 bg-slate-800 px-2.5 py-1.5 text-xs text-slate-300 hover:text-white">
              <LogOut className="h-3.5 w-3.5" />{auth?.username || "admin"} · Log out
            </button>
          </div>
        </div>
      </header>

      <div className="flex min-h-[calc(100vh-61px)] flex-col md:flex-row">
        {/* 8.5.1: the sections live in a sidebar on the left (a scrollable row on small screens) */}
        <aside className={cx("shrink-0 border-slate-800 bg-slate-950/60 md:sticky md:top-[61px] md:h-[calc(100vh-61px)] md:border-r",
          "border-b md:border-b-0", navSlim ? "md:w-16" : "md:w-60")} data-testid="mc-sidebar">
          <div className="flex h-full flex-col">
            <nav className="flex gap-1 overflow-x-auto p-2 md:flex-1 md:flex-col md:overflow-y-auto md:overflow-x-visible" role="tablist"
              aria-orientation="vertical" aria-label="Mission Control sections">
              {tabs.map((t) => (
                <button key={t.id} type="button" onClick={() => setTab(t.id)} role="tab" aria-selected={tab === t.id}
                  title={navSlim ? t.label : undefined}
                  className={cx("relative inline-flex shrink-0 items-center gap-2.5 rounded-lg px-3 py-2 text-left text-sm font-medium transition-colors",
                    tab === t.id ? "bg-sky-500/15 text-white ring-1 ring-sky-500/40" : "text-slate-400 hover:bg-slate-800/70 hover:text-slate-200",
                    navSlim && "md:justify-center md:px-0")}>
                  <span className="shrink-0">{t.icon}</span>
                  <span className={cx("whitespace-nowrap", navSlim && "md:hidden")}>{t.label}</span>
                  {t.badge && <span className={cx("ml-auto", navSlim && "md:absolute md:right-0.5 md:top-0.5 md:ml-0 md:scale-75")}>{t.badge}</span>}
                </button>
              ))}
            </nav>
            <div className="hidden space-y-2 border-t border-slate-800 p-2 md:block">
              {!navSlim && <CardsToolbar className="flex flex-wrap" />}
              <button type="button" onClick={() => setNavSlim(!navSlim)} aria-label={navSlim ? "Show the menu labels" : "Show icons only"}
                title={navSlim ? "Show the menu labels" : "Icons only"}
                className="inline-flex w-full items-center justify-center gap-1.5 rounded-md px-2 py-1 text-[11px] text-slate-400 hover:bg-slate-800 hover:text-white">
                {navSlim ? <PanelLeftOpen className="h-4 w-4" /> : <><PanelLeftClose className="h-4 w-4" />Icons only</>}
              </button>
            </div>
          </div>
        </aside>
      <main id="main" tabIndex={-1} className="mx-auto w-full min-w-0 max-w-7xl flex-1 space-y-5 px-4 py-6 focus:outline-none md:px-6">
        <CardsToolbar className="md:hidden" />
        {!!aiChanges?.in_place && tab !== "health" && (
          <div className="flex flex-wrap items-center gap-2 rounded-lg border border-amber-500/40 bg-amber-950/30 px-3 py-2 text-xs text-amber-100" data-testid="ai-change-alert">
            <Wrench className="h-4 w-4 text-amber-400" />The AI has changed Understudy&apos;s own code: {aiChanges.in_place} change{aiChanges.in_place === 1 ? " is" : "s are"} in place
            ({aiChanges.files_in_place.join(", ")}){aiChanges.restart_needed ? " - restart the backend to load it" : ""}.
            <button type="button" className="font-semibold text-amber-300 underline" onClick={() => setTab("health")}>See what changed / revert</button>
          </div>
        )}
        {auth?.default_password && (
          <div className="flex flex-wrap items-center gap-2 rounded-lg border border-amber-500/40 bg-amber-950/30 px-3 py-2 text-xs text-amber-100">
            <AlertTriangle className="h-4 w-4 text-amber-400" />You are using the default admin password.
            <button type="button" className="font-semibold text-amber-300 underline" onClick={() => setTab("ai")}>Change it</button>
            <span className="text-amber-200/70">(a change lasts until Understudy restarts)</span>
          </div>
        )}
        {admin.error && !admin.needsToken && (
          <div className="flex items-center gap-2 rounded-lg border border-rose-500/30 bg-rose-950/30 px-3 py-2 text-xs text-rose-200">
            <AlertTriangle className="h-4 w-4" />{admin.error}
          </div>
        )}

        <Fold title="Overview" icon={<BarChart3 className="h-3.5 w-3.5 text-sky-300" />} foldId="mc-overview">
        <section className="grid grid-cols-2 gap-3 lg:grid-cols-5">
          <StatTile icon={<Users className="h-3.5 w-3.5" />} label="Sessions" value={stats?.sessions.length ?? 0} sub="seen in telemetry" tone="sky" />
          <StatTile icon={<Bot className="h-3.5 w-3.5" />} label="AI calls" value={stats?.ai_calls ?? 0} sub={`${aiOk} answered · ${aiFail} failed`} tone={aiFail ? "amber" : "green"} />
          <StatTile icon={<Cpu className="h-3.5 w-3.5" />} label="Heuristic fallbacks" value={stats?.fallbacks ?? 0} sub="AI result not used" tone={(stats?.fallbacks ?? 0) ? "amber" : "slate"} />
          <StatTile icon={<ShieldAlert className="h-3.5 w-3.5" />} label="Blocked inputs" value={alerts.length} sub="Model Armor" tone={alerts.length ? "red" : "slate"} />
          <StatTile icon={<Wrench className="h-3.5 w-3.5" />} label="Tool requests" value={pending} sub={`${admin.policy?.tools.whitelist.length ?? 0} tools approved`} tone={pending ? "amber" : "slate"} />
        </section>
        </Fold>

        {/* Telemetry keeps polling while other tabs are open, so the stats stay live. */}
        <div className={tab === "telemetry" ? "" : "hidden"}><TelemetryPanel onStats={setStats} /></div>

        {tab === "access" && (
          <div className="grid items-start gap-5 lg:grid-cols-5">
            <div className="space-y-5 lg:col-span-3">
              <ToolAccessPanel policy={admin.policy} run={admin.run} />
            </div>
            <div className="space-y-5 lg:col-span-2">
              <RegistryPanel policy={admin.policy} run={admin.run} onOpenRequest={openRequest} />
              <PackageManagersPanel />
              <SandboxPanel />
              <UrlExceptionsPanel policy={admin.policy} run={admin.run} />
              <BlockedUrlsPanel policy={admin.policy} run={admin.run} />
              <NetworkModePanel policy={admin.policy} run={admin.run} />
              <SelfHealingPanel policy={admin.policy} run={admin.run} />
            </div>
          </div>
        )}

        {tab === "users" && <UsersPanel />}
        {tab === "environments" && <EnvironmentsPanel />}
        {tab === "escalations" && <EscalationsPanel refreshKey={escOpen} />}
        {tab === "skills" && <div className="space-y-5"><RecallPanel /><SkillsPanel /></div>}
        {tab === "health" && <CodeHealthPanel />}

        {tab === "ai" && (
          <div className="grid items-start gap-5 lg:grid-cols-2">
            <div className="space-y-5">
              <GeminiPanel status={keyStatus} onChanged={refreshKey} />
              <GoogleCloudCheckPanel />
              <AccountPanel auth={auth} onChanged={setAuth} />
            </div>
            <div className="space-y-5">
              <ModelArmorPanel />
              <SecurityPanel alerts={alerts} onClear={() => { void clearSecurityAlerts().then(() => setAlerts([])).catch(() => null); }} />
            </div>
          </div>
        )}
      </main>
      </div>
      <Toasts toasts={inbox.toasts} dismiss={inbox.dismiss} onOpen={openInbox} />
    </div>
  );
}
