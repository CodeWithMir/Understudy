"use client";

import React, { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import {
  BookOpen, Bot, CheckCircle2, ChevronRight, Circle, Code2, Cpu, Eye, LogOut, PanelLeftOpen, Play, RotateCw, ShieldAlert,
  ShieldCheck, Sparkles, User, X, XCircle,
} from "lucide-react";
import { WizardApiError, wizardApi, type PillarName, type WizardSession } from "../../lib/wizard";
import { LOGIN_EVENT, clearUserToken, getUserName, getUserToken, studioApi } from "../../lib/studio";
import { BuildStep, ExecuteStep, GovernStep, PendingBanner, ExportStep, WatchReview, WatchStart, type ActionError } from "./steps";
import { Explorer } from "./explorer";
import { useLiveEvents, type LiveEventMsg } from "../../lib/live";
import { EditableName, ProjectView, ProjectsHome } from "./studio";
import { AuthScreen, ProfileView } from "./account";
import { Alert, Badge, Button, CardsToolbar, cx, Fold } from "./ui";
import { NotificationBell } from "./notifications";
import { ThemeToggle } from "../theme-toggle";

const STEPS: { n: number; pillar: PillarName; title: string; blurb: string; icon: React.ReactNode }[] = [
  { n: 1, pillar: "WATCH", title: "Watch", blurb: "AI watches your video", icon: <Eye className="h-4 w-4" /> },
  { n: 2, pillar: "GOVERN", title: "Govern", blurb: "Check data & SOP rules", icon: <ShieldCheck className="h-4 w-4" /> },
  { n: 3, pillar: "BUILD", title: "Build", blurb: "Code with the rules in it", icon: <Code2 className="h-4 w-4" /> },
  { n: 4, pillar: "BUILD-EXECUTE", title: "Execute", blurb: "Run it for real", icon: <Play className="h-4 w-4" /> },
  { n: 5, pillar: "EXPORT", title: "Export", blurb: "Download the ZIP", icon: <Sparkles className="h-4 w-4" /> },
];

type Route = { kind: "projects" } | { kind: "project"; id: string } | { kind: "session"; sid: string; projectId?: string | null }
  | { kind: "profile" };
const ROUTE_KEY = "understudy_studio_route";
const EXPLORER_KEY = "understudy_explorer_open";

export default function WizardPage() {
  const [auth, setAuth] = useState<{ state: "checking" | "login" | "ok"; user: string | null }>({ state: "checking", user: null });
  const [route, setRouteState] = useState<Route>({ kind: "projects" });
  const setRoute = useCallback((r: Route) => {
    setRouteState(r);
    try { window.sessionStorage.setItem(ROUTE_KEY, JSON.stringify(r)); } catch { /* ignore */ }
  }, []);
  const [ui, setUi] = useState<{ session: WizardSession | null; view: number }>({ session: null, view: 1 });
  const { session, view } = ui;
  const setView = (v: number) => setUi((u) => ({ ...u, view: v }));
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const [error, setError] = useState<ActionError | null>(null);
  const [aiMode, setAiMode] = useState<{ ai_mode: string; model: string | null; learned?: { enabled: boolean; total: number; reuses: number } } | null>(null);

  const [explorerOpen, setExplorerOpenState] = useState(true);
  const [drawer, setDrawer] = useState(false);
  const [treeRefresh, setTreeRefresh] = useState(0);
  useEffect(() => {
    try {
      if (window.localStorage.getItem(EXPLORER_KEY) === "0") { const t = setTimeout(() => setExplorerOpenState(false), 0); return () => clearTimeout(t); }
    } catch { /* ignore */ }
  }, []);
  const setExplorerOpen = (v: boolean) => {
    setExplorerOpenState(v);
    try { window.localStorage.setItem(EXPLORER_KEY, v ? "1" : "0"); } catch { /* ignore */ }
  };
  const step = session?.current_step;
  useEffect(() => { wizardApi.info().then(setAiMode).catch(() => setAiMode(null)); }, [step]);

  // Who is logged in? (Accounts come from Mission Control; with login switched off the Studio works anonymously.)
  useEffect(() => {
    let alive = true;
    // Restore where the user was after a page refresh.
    const restore = () => {
      let r: Route | null = null;
      try { r = JSON.parse(window.sessionStorage.getItem(ROUTE_KEY) || "null"); } catch { r = null; }
      if (!r) return;
      setRouteState(r);
      if (r.kind === "session") {
        const back: Route = r.projectId ? { kind: "project", id: r.projectId } : { kind: "projects" };
        wizardApi.get(r.sid).then((s) => { if (alive) setUi({ session: s, view: s.current_step }); })
          .catch(() => { if (alive) setRouteState(back); });
      }
    };
    const check = async () => {
      try {
        const info = await wizardApi.info();
        if (!getUserToken()) {
          if (info.user_login_required) { if (alive) setAuth({ state: "login", user: null }); return; }
          if (alive) { setAuth({ state: "ok", user: null }); restore(); }
          return;
        }
        const me = await studioApi.me();
        if (alive) { setAuth({ state: "ok", user: me.username || getUserName() || null }); restore(); }
      } catch {
        if (alive) setAuth({ state: "login", user: null });
      }
    };
    void check();
    const onLogin = () => { setAuth({ state: "login", user: null }); setUi({ session: null, view: 1 }); };
    window.addEventListener(LOGIN_EVENT, onLogin);
    return () => { alive = false; window.removeEventListener(LOGIN_EVENT, onLogin); };
  }, []);

  const adopt = useCallback((s: WizardSession) => {
    setUi((u) => ({ session: s, view: u.session && u.session.session_id === s.session_id ? u.view : s.current_step }));
  }, []);

  const openSession = useCallback((sid: string, projectId?: string | null) => {
    setRoute({ kind: "session", sid, projectId });
    setUi({ session: null, view: 1 });
    wizardApi.get(sid).then((s) => setUi({ session: s, view: s.current_step }))
      .catch((e) => { setError({ message: (e as Error).message }); setRoute(projectId ? { kind: "project", id: projectId } : { kind: "projects" }); });
  }, [setRoute]);


  const [authNotice, setAuthNotice] = useState<string | null>(null);
  const logout = async (notice: string | null = null) => {
    setAuthNotice(notice);
    try { await studioApi.logout(); } catch { /* token may already be invalid */ }
    clearUserToken();
    try { window.sessionStorage.removeItem(ROUTE_KEY); } catch { /* ignore */ }
    setUi({ session: null, view: 1 });
    setRouteState({ kind: "projects" });
    const info = await wizardApi.info().catch(() => ({ user_login_required: true }));
    setAuth({ state: info.user_login_required ? "login" : "ok", user: null });
  };

  const act = useCallback(async (fn: () => Promise<WizardSession>, key?: string) => {
    setBusyKey(key || "busy");
    setError(null);
    try {
      const s = await fn();
      adopt(s);
      return s;
    } catch (e) {
      const err = e as WizardApiError;
      setError({ message: err.message || String(e), details: err.details });
      const sid = (err.details as { session_id?: string } | undefined)?.session_id;
      if (sid) wizardApi.get(sid).then(adopt).catch(() => undefined);
      return null;
    } finally {
      setBusyKey(null);
    }
  }, [adopt]);

  // Poll while background work is running.
  const needsPoll = !!session && (session.job?.status === "running" ||
    ["INSTALLING", "PENDING"].includes(session.dependencies?.status || "") ||
    session.framework_options.some((o) => o.access_request?.status === "pending"));
  useEffect(() => {
    if (!session || !needsPoll) return;
    const id = setInterval(() => { wizardApi.get(session.session_id).then(adopt).catch(() => undefined); },
      session.job?.status === "running" ? 1200 : 3000);
    return () => clearInterval(id);
  }, [session, needsPoll, adopt]);

  // The explorer badge of the open session follows its progress (ANALYSED -> BUILT -> GOVERNED -> RUNNING -> VERIFIED).
  const badgeSig = session ? [session.session_id, session.status, session.job?.status || "", !!session.analysis,
    session.session_name || "",
    ...Object.entries(session.pillars || {}).map(([k, p]) => `${k}:${p?.status || ""}`)].join("|") : "";
  useEffect(() => {
    if (!badgeSig) return;
    const t = setTimeout(() => setTreeRefresh((n) => n + 1), 300);          // after the backend re-indexed it
    return () => clearTimeout(t);
  }, [badgeSig]);

  // 8.0 live events (WebSocket, polling fallback): the explorer and a paused session update the moment the admin acts.
  const lastEvent = useRef(0);
  const [bellTick, setBellTick] = useState(0);
  const sessionRef = useRef<WizardSession | null>(null);
  useEffect(() => { sessionRef.current = session; }, [session]);
  const onLive = useCallback((e: LiveEventMsg) => {
    lastEvent.current = Math.max(lastEvent.current, e.at || 0);
    if (e.type === "notification" || e.type === "escalation.updated") setBellTick((n) => n + 1);   // 8.3: the bell
    if (e.type === "notification") return;
    setTreeRefresh((n) => n + 1);
    const cur = sessionRef.current;
    const d = (e.data || {}) as { session_id?: string; request?: string };
    if (cur && (d.session_id === cur.session_id || (d.request && cur.pending_request?.id === d.request))) {
      wizardApi.get(cur.session_id).then(adopt).catch(() => undefined);
    }
  }, [adopt]);
  const liveMode = useLiveEvents({
    path: "/api/wizard/ws", token: getUserToken(), enabled: auth.state === "ok", onEvent: onLive,
    poll: () => { wizardApi.events(lastEvent.current).then((r) => r.events.forEach((ev) => onLive(ev as LiveEventMsg))).catch(() => undefined); },
    pollMs: 6000,
  });

  // Surface background-job errors.
  const jobErr = session?.job?.status === "error" ? session.job.error : null;

  const backToProject = () => {
    const pid = session?.project_id || (route.kind === "session" ? route.projectId : null);
    setUi({ session: null, view: 1 }); setError(null);
    setRoute(pid ? { kind: "project", id: pid } : { kind: "projects" });
  };
  const inSession = route.kind === "session";
  const draft = !!session && !session.media && !session.analysis;

  const status = (p: PillarName) => session?.pillars?.[p]?.status;
  const reachable = (n: number) => !!session && (n <= (session.current_step || 1));

  const props = session ? { session, act, busyKey, goTo: (n: number) => { setError(null); setView(n); }, error } : null;
  const selectedProject = session?.project_id || (route.kind === "project" ? route.id : route.kind === "session" ? route.projectId || null : null);
  const explorer = (onCollapse: () => void) => (
    <Explorer user={auth.user} refreshKey={treeRefresh}
      selectedSession={route.kind === "session" ? route.sid : null} selectedProject={selectedProject}
      onHome={() => { setDrawer(false); setUi({ session: null, view: 1 }); setError(null); setRoute({ kind: "projects" }); }}
      onOpenProject={(id) => { setDrawer(false); setUi({ session: null, view: 1 }); setError(null); setRoute({ kind: "project", id }); }}
      onOpenSession={(sid, pid) => { setDrawer(false); setError(null); openSession(sid, pid); }}
      onSessionGone={(sid) => { if (route.kind === "session" && route.sid === sid) backToProject(); }}
      onRenamed={() => { if (session) wizardApi.get(session.session_id).then(adopt).catch(() => undefined); }}
      onCollapse={onCollapse} />
  );

  return (
    <div className="flex h-screen flex-col bg-slate-950 text-slate-100">
      <header className="shrink-0 border-b border-slate-800 bg-slate-950/90 backdrop-blur">
        <div className="flex flex-wrap items-center gap-3 px-4 py-3">
          {auth.state === "ok" && (
            <button type="button" onClick={() => { if (window.matchMedia("(min-width: 1024px)").matches) setExplorerOpen(!explorerOpen); else setDrawer(true); }}
              aria-label={explorerOpen ? "Toggle the explorer" : "Show the explorer"} data-testid="explorer-toggle"
              className="rounded-md p-1.5 text-slate-400 hover:bg-slate-800 hover:text-white"><PanelLeftOpen className="h-4 w-4" /></button>
          )}
          <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-gradient-to-tr from-sky-500 to-indigo-600 font-bold">U</div>
          <div>
            <h1 className="text-base font-bold">Understudy</h1>
            <p className="text-[11px] text-slate-400">Video + URL + rules → tested, governed automation you can download</p>
          </div>
          <div className="ml-auto flex flex-wrap items-center gap-2">
            {aiMode && (aiMode.ai_mode === "ACTUAL_AI_API"
              ? <Badge tone="green"><Bot className="h-3 w-3" />AI mode · {aiMode.model}</Badge>
              : <Badge tone="amber" title="Add a Gemini API key in Mission Control to enable AI"><Cpu className="h-3 w-3" />Offline mode (no AI key)</Badge>)}
            {aiMode?.learned?.enabled && aiMode.learned.total > 0 && (
              <Badge tone="violet" title="Validated AI answers from earlier sessions. Offline, Understudy re-uses them for similar problems.">
                <BookOpen className="h-3 w-3" />{aiMode.learned.total} learned answer{aiMode.learned.total === 1 ? "" : "s"}</Badge>)}
            {auth.state === "ok" && <NotificationBell tick={bellTick} onOpenSession={(sid) => { setDrawer(false); setError(null); openSession(sid); }} />}
            <ThemeToggle />
            <Link href="/" className="rounded-md px-2 py-1 text-xs text-slate-400 hover:bg-slate-800 hover:text-white">Mission Control</Link>
            {auth.user && (
              <button type="button" onClick={() => { setUi({ session: null, view: 1 }); setRoute({ kind: "profile" }); }} data-testid="open-profile"
                title="Profile - your email and password reset" className="rounded-md focus:outline-none focus-visible:ring-2 focus-visible:ring-sky-400">
                <Badge tone="sky"><User className="h-3 w-3" />{auth.user} · Profile</Badge></button>)}
            {auth.state === "ok" && getUserToken() && <Button variant="ghost" onClick={() => { void logout(); }}><LogOut className="h-4 w-4" />Log out</Button>}
          </div>
        </div>
        {auth.state === "ok" && (
          <nav aria-label="Breadcrumb" className="flex flex-wrap items-center gap-1 px-4 pb-2 text-xs text-slate-400">
            <button type="button" className="hover:text-white" onClick={() => { setUi({ session: null, view: 1 }); setRoute({ kind: "projects" }); }}>
              {auth.user || "local"} · Projects</button>
            {(session?.project_name || route.kind === "project") && <ChevronRight className="h-3 w-3" aria-hidden />}
            {session?.project_name && <button type="button" className="hover:text-white" onClick={backToProject}>{session.project_name}</button>}
            {session && <><ChevronRight className="h-3 w-3" aria-hidden />
              <EditableName key={`${session.session_id}:${session.session_name}`} className="text-slate-200" testId="session-title"
                name={session.session_name || session.session_id} auto={session.name_auto}
                naming={session.job?.status === "running" && !session.analysis}
                onSave={async (n) => { adopt(await wizardApi.rename(session.session_id, n)); setTreeRefresh((x) => x + 1); }} /></>}
            <CardsToolbar className="ml-auto hidden sm:inline-flex" />
            <span className="text-[10px] text-slate-500" title="How this page gets updates from your admin">
              {liveMode === "live" ? "● live" : liveMode === "polling" ? "○ polling" : ""}</span>
          </nav>
        )}
      </header>

      {auth.state === "checking" && <p className="mx-auto max-w-7xl px-4 py-10 text-sm text-slate-400">Loading...</p>}
      {auth.state === "login" && <div className="flex-1 overflow-auto"><AuthScreen key={authNotice || "auth"} initialNotice={authNotice}
        onLoggedIn={(u) => { setAuthNotice(null); setAuth({ state: "ok", user: u }); setRoute({ kind: "projects" }); }} /></div>}
      {auth.state === "ok" && (
      <div className="flex min-h-0 flex-1">
        {explorerOpen && (
          <aside className="hidden w-72 shrink-0 lg:block" aria-label="Explorer sidebar">{explorer(() => setExplorerOpen(false))}</aside>
        )}
        {drawer && (
          <div className="fixed inset-0 z-40 flex lg:hidden" role="dialog" aria-modal="true" aria-label="Explorer">
            <div className="w-80 max-w-[85vw] bg-slate-950">{explorer(() => setDrawer(false))}</div>
            <button type="button" aria-label="Close the explorer" className="flex-1 bg-black/60" onClick={() => setDrawer(false)}>
              <X className="ml-auto mr-3 mt-3 h-5 w-5 text-slate-300" /></button>
          </div>
        )}
        <div className="min-w-0 flex-1 overflow-auto" id="workspace-pane">
      {!inSession && (
        <main id="main" className="mx-auto max-w-7xl px-4 py-6">
          {error && <div className="mb-4"><Alert tone="red">{error.message}</Alert></div>}
          {route.kind === "projects" && <ProjectsHome onOpen={(id) => setRoute({ kind: "project", id })} />}
          {route.kind === "profile" && <ProfileView onBack={() => setRoute({ kind: "projects" })}
            onSignedOut={(m) => { void logout(m); }} />}
          {route.kind === "project" && <ProjectView projectId={route.id} onBack={() => setRoute({ kind: "projects" })}
            onOpenSession={(s) => { setTreeRefresh((n) => n + 1); openSession(s.session_id, route.id); }} />}
        </main>
      )}
      {inSession && (<>
      {session?.pending_request && (
        <div className="mx-auto max-w-7xl px-4 pt-5"><PendingBanner session={session} act={act} busyKey={busyKey} /></div>
      )}
      <nav aria-label="Understudy steps" className="mx-auto max-w-7xl px-4 pt-5">
        <ol className="grid grid-cols-5 gap-2">
          {STEPS.map((st) => {
            const s = status(st.pillar);
            const active = view === st.n;
            return (
              <li key={st.n}>
                <button disabled={!reachable(st.n)} aria-current={active ? "step" : undefined}
                  aria-label={`Step ${st.n}: ${st.title}${s === "PASS" ? ", done" : s === "FAIL" ? ", needs attention" : ""}`}
                  onClick={() => { setError(null); setView(st.n); }}
                  className={cx("flex w-full items-center gap-2 rounded-lg border px-3 py-2.5 text-left transition-colors disabled:cursor-not-allowed disabled:opacity-40",
                    active ? "border-sky-500 bg-sky-500/10" : "border-slate-800 bg-slate-900/60 hover:border-slate-600")}>
                  <span className={cx("flex h-7 w-7 shrink-0 items-center justify-center rounded-full text-xs font-bold",
                    s === "PASS" ? "bg-emerald-500 text-slate-950" : s === "FAIL" ? "bg-rose-600 text-white" : active ? "bg-sky-500 text-slate-950" : "bg-slate-800 text-slate-300")}>
                    {s === "PASS" ? <CheckCircle2 className="h-4 w-4" /> : s === "FAIL" ? <XCircle className="h-4 w-4" /> : st.n}
                  </span>
                  <span className="hidden min-w-0 sm:block">
                    <span className="block truncate text-sm font-semibold">{st.title}</span>
                    <span className="block truncate text-[11px] text-slate-400">{st.blurb}</span>
                  </span>
                </button>
              </li>
            );
          })}
        </ol>
      </nav>

      <main id="main" tabIndex={-1} className="mx-auto grid max-w-7xl gap-6 px-4 py-6 focus:outline-none lg:grid-cols-[1fr_300px]">
        <div className="min-w-0 space-y-4">
          {(error || jobErr) && (
            <Alert tone="red" title={session?.job?.security || /Model Armor|security guard/i.test(error?.message || "") ? "Blocked for your safety" : "Something needs your attention"}
              icon={<ShieldAlert className="h-4 w-4" />}>
              {error?.message || jobErr}
            </Alert>
          )}
          {!session && <Alert tone="sky">Opening the session...</Alert>}
          {props && view === 1 && (draft ? <WatchStart session={props.session} act={act} busyKey={busyKey} /> : <WatchReview {...props} />)}
          {props && view === 2 && <GovernStep {...props} />}
          {props && view === 3 && <BuildStep {...props} />}
          {props && view === 4 && <ExecuteStep {...props} />}
          {props && view === 5 && <ExportStep {...props} />}
        </div>

        <aside className="space-y-4">
          <Fold title="Progress" foldId="studio-progress">
            <h4 id="progress-h" className="sr-only">Progress</h4>
            <ul aria-labelledby="progress-h" aria-live="polite" className="space-y-1.5 text-xs">
              {STEPS.map((st) => {
                const p = session?.pillars?.[st.pillar];
                return (
                  <li key={st.n} className="flex items-start gap-2">
                    {p?.status === "PASS" ? <CheckCircle2 className="mt-0.5 h-3.5 w-3.5 shrink-0 text-emerald-400" />
                      : p?.status === "FAIL" ? <XCircle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-rose-400" />
                      : <Circle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-slate-400" />}
                    <span className="text-slate-300"><b>{st.title}</b>{p?.reason ? <span className="block text-[11px] text-slate-400">{p.reason.slice(0, 140)}</span> : null}</span>
                  </li>
                );
              })}
            </ul>
            {session?.job?.status === "running" && (
              <div className="mt-3 flex items-center gap-2 text-[11px] text-sky-300"><RotateCw className="h-3 w-3 animate-spin" />Working: {session.job.name}</div>
            )}
          </Fold>

          {session && session.agent_trace.length > 0 && (
            <Fold title="What the agents did" foldId="studio-agents">
              <ul className="max-h-80 space-y-2 overflow-auto text-[11px]">
                {[...session.agent_trace].reverse().slice(0, 14).map((t, i) => (
                  <li key={i} className="border-l-2 pl-2" style={{ borderColor: t.agent_source === "ACTUAL_AI_API" ? "#10b981" : "#f59e0b" }}>
                    <span className="text-slate-400">{t.stage}</span> <span className="text-slate-300">{t.action}</span>
                  </li>
                ))}
              </ul>
              <p className="mt-2 text-[10px] text-slate-400"><span className="text-emerald-400">green</span> = AI call · <span className="text-amber-400">amber</span> = deterministic code</p>
            </Fold>
          )}

          {session && session.security_events.length > 0 && (
            <Fold title="Model Armor" tone="rose" icon={<ShieldAlert className="h-3.5 w-3.5 text-rose-300" />} foldId="studio-armor">
              <ul className="space-y-1.5 text-[11px] text-rose-100/80">
                {session.security_events.slice(-5).map((e, i) => <li key={i}><b>{e.channel}</b>: {e.pattern || e.message}</li>)}
              </ul>
            </Fold>
          )}

          <Fold title="Glossary" foldId="studio-glossary" className="text-[11px] leading-relaxed text-slate-400">
            <p><b className="text-slate-200">Payload</b> - the data values the automation types in.</p>
            <p><b className="text-slate-200">SOP</b> - your written rules (standard operating procedure).</p>
            <p><b className="text-slate-200">Framework</b> - the tool that drives the app (e.g. Playwright).</p>
            <p><b className="text-slate-200">Selector</b> - how the code finds a button or field on the page.</p>
            <p><b className="text-slate-200">Self-healing</b> - when a run fails, the AI inspects the page and fixes its own steps.</p>
          </Fold>
          <Button variant="secondary" className="w-full" onClick={backToProject}>Back to the project</Button>
        </aside>
      </main>
      </>)}
        </div>
      </div>
      )}
    </div>
  );
}
