"use client";

import React, { useCallback, useEffect, useRef, useState } from "react";
import {
  ArrowLeft, Check, CheckCircle2, ChevronRight, Clock, FolderPlus, History, Layers, Pencil, Plus, Search,
  Sparkles, Trash2, X,
} from "lucide-react";
import {
  StudioApiError, studioApi, type HistoryRef, type HistorySearch, type Project, type SessionSummary,
} from "../../lib/studio";
import type { WizardSession } from "../../lib/wizard";
import { Alert, Badge, Button, Card, Field, cx, inputCls } from "./ui";

/** 9.1: a session title you can rename in place (click the name or the pencil; Enter saves, Esc cancels) - like a
 * chat title. While the AI is still naming the session (Step 1 running) it shows "Naming...". */
export function EditableName({ name, auto, naming, onSave, className, testId }: {
  name: string; auto?: boolean; naming?: boolean; onSave: (name: string) => Promise<unknown>; className?: string; testId?: string;
}) {
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState(name);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const start = () => { setValue(name); setError(null); setEditing(true); };
  const save = async () => {
    const v = value.replace(/\s+/g, " ").trim();
    if (!v || v === name) { setEditing(false); return; }
    setBusy(true); setError(null);
    try { await onSave(v); setEditing(false); } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  };
  if (editing) {
    return (
      <span className={cx("inline-flex items-center gap-1", className)}>
        <input autoFocus aria-label="Session name" maxLength={80} value={value} disabled={busy} data-testid={testId ? `${testId}-input` : undefined}
          onChange={(e) => setValue(e.target.value)} onBlur={() => { void save(); }}
          onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); void save(); } if (e.key === "Escape") { e.preventDefault(); setEditing(false); } }}
          className="min-w-[12rem] rounded border border-sky-500/60 bg-slate-950 px-1.5 py-0.5 text-slate-100 focus:outline-none" />
        <button type="button" aria-label="Save name" onMouseDown={(e) => e.preventDefault()} onClick={() => { void save(); }}
          className="rounded p-0.5 text-emerald-300 hover:bg-slate-800"><Check className="h-3.5 w-3.5" /></button>
        {error && <span className="text-[11px] text-rose-300" role="alert">{error}</span>}
      </span>
    );
  }
  return (
    <span className={cx("group inline-flex min-w-0 items-center gap-1", className)} data-testid={testId}>
      <button type="button" onClick={start} title="Rename" className="truncate text-left hover:text-white">{name}</button>
      {naming && auto
        ? <span className="inline-flex items-center gap-0.5 text-[10px] text-sky-300" data-testid="naming"><Sparkles className="h-3 w-3 animate-pulse" />naming...</span>
        : null}
      <button type="button" onClick={start} aria-label={`Rename ${name}`}
        className="rounded p-0.5 text-slate-500 opacity-60 hover:bg-slate-800 hover:text-white group-hover:opacity-100"><Pencil className="h-3 w-3" /></button>
    </span>
  );
}

// 8.1: the signed-out screen (Log in / Sign up / Forgot password) and the Profile tab live in ./account.tsx.

/* ============================================================================================
 * Projects home: list + create
 * ========================================================================================== */
const stepNames = ["Watch", "Govern", "Build", "Execute", "Export"];

export function SessionStatus({ r }: { r: SessionSummary }) {
  if (r.status === "completed") return <Badge tone="green"><CheckCircle2 className="h-3 w-3" />done · ZIP ready</Badge>;
  if (r.status === "draft") return <Badge tone="slate">not started</Badge>;
  if (r.status === "needs_help" || r.status === "awaiting_approval") return <Badge tone="amber">{r.status === "needs_help" ? "needs help" : "fix awaiting review"}</Badge>;
  return <Badge tone="sky">step {r.current_step} · {stepNames[(r.current_step || 1) - 1]} · {r.pillars_passed}/5 passed</Badge>;
}

export function ProjectsHome({ onOpen }: { onOpen: (projectId: string) => void }) {
  const [projects, setProjects] = useState<Project[] | null>(null);
  const [showNew, setShowNew] = useState(false);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(() => {
    studioApi.projects().then((r) => setProjects(r.projects)).catch((e) => setError((e as Error).message));
  }, []);
  useEffect(load, [load]);
  const create = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const r = await studioApi.createProject(name, description);
      setName(""); setDescription(""); setShowNew(false);
      onOpen(r.project.project_id);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const empty = !!projects && projects.length === 0;
  return (
    <div className="space-y-5">
      {/* 9.2: the main action sits top-left, next to the title (it used to be a card on the far right) */}
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-xl font-bold text-white">Your projects</h1>
        {projects && <Badge>{projects.length} project{projects.length === 1 ? "" : "s"}</Badge>}
        {!empty && (
          <Button onClick={() => setShowNew(!showNew)} data-testid="new-project" aria-expanded={showNew}>
            <FolderPlus className="h-4 w-4" />New project</Button>
        )}
      </div>
      {(showNew || empty) && (
        <div className={cx(empty && !showNew && "mx-auto max-w-xl")}>
          <Card title={empty ? "Create your first project" : "New project"} collapsible={false}
            icon={<FolderPlus className="h-4 w-4 text-sky-400" />}>
            {empty && <p className="mb-3 text-sm text-slate-300">A project groups the sessions (runs of the 5 steps) for one
              process or application - e.g. all your supplier-onboarding automations.</p>}
            <form onSubmit={create} className="grid gap-3 md:grid-cols-2" data-testid="new-project-form">
              <Field label="Project Name">
                <input autoFocus className={inputCls} value={name} onChange={(e) => setName(e.target.value)} required maxLength={80}
                  placeholder="e.g. Supplier onboarding" />
              </Field>
              <Field label="Project Description" hint="What the automations in this project are for. Required.">
                <textarea className={cx(inputCls, "h-20")} value={description} onChange={(e) => setDescription(e.target.value)}
                  required maxLength={1000} placeholder="e.g. Create new suppliers in our procurement portal from the onboarding form" />
              </Field>
              {error && <div className="md:col-span-2"><Alert tone="red">{error}</Alert></div>}
              <div className="flex gap-2 md:col-span-2">
                <Button type="submit" busy={busy} disabled={!name.trim() || !description.trim()}>
                  <Plus className="h-4 w-4" />Create project</Button>
                {!empty && <Button variant="ghost" onClick={() => setShowNew(false)}>Cancel</Button>}
              </div>
            </form>
          </Card>
        </div>
      )}
      {!projects && !error && <p className="text-sm text-slate-400">Loading...</p>}
      {!showNew && error && <Alert tone="red">{error}</Alert>}
      <ul className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
        {projects?.map((p) => (
          <li key={p.project_id}>
            <button type="button" onClick={() => onOpen(p.project_id)}
              className="flex h-full w-full items-start gap-3 rounded-xl border border-slate-800 bg-slate-900/70 p-4 text-left hover:border-sky-500/60 focus:outline-none focus-visible:ring-2 focus-visible:ring-sky-400">
              <Layers className="mt-0.5 h-4 w-4 shrink-0 text-sky-400" aria-hidden />
              <span className="min-w-0 flex-1">
                <span className="block truncate font-semibold text-white">{p.name}</span>
                <span className="line-clamp-2 block text-xs text-slate-400">{p.description}</span>
                <span className="mt-2 flex flex-wrap gap-1.5">
                  <Badge>{p.session_count} session{p.session_count === 1 ? "" : "s"}</Badge>
                  {p.completed > 0 && <Badge tone="green">{p.completed} done</Badge>}
                  <Badge tone="slate"><Clock className="h-3 w-3" />{String(p.updated_at).slice(0, 10)}</Badge>
                </span>
              </span>
              <ChevronRight className="mt-1 h-4 w-4 text-slate-400" aria-hidden />
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}

/* ============================================================================================
 * One project: its sessions + create a session
 * ========================================================================================== */
export function ProjectView({ projectId, onBack, onOpenSession }: {
  projectId: string; onBack: () => void; onOpenSession: (s: WizardSession | { session_id: string }) => void;
}) {
  const [project, setProject] = useState<Project | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(() => {
    studioApi.project(projectId).then((r) => setProject(r.project)).catch((e) => setError((e as Error).message));
  }, [projectId]);
  useEffect(load, [load]);
  const create = async () => {
    setBusy(true);
    setError(null);
    try {
      const r = await studioApi.createSession(projectId);           // 9.1: the AI names it after Step 1
      onOpenSession(r.session);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const removeSession = async (r: SessionSummary) => {
    if (!window.confirm(`Delete the session "${r.name}"? Its recording, generated code and evidence are removed.`)) return;
    try { await studioApi.deleteSession(r.session_id); load(); } catch (err) { setError((err as Error).message); }
  };
  const removeProject = async () => {
    if (!project) return;
    const typed = window.prompt(`Delete the project "${project.name}" and all ${project.session_count} of its sessions? Type the project name to confirm.`);
    if (typed !== project.name) return;
    try { await studioApi.deleteProject(project.project_id); onBack(); } catch (err) { setError((err as Error).message); }
  };
  const empty = !!project && project.sessions.length === 0;
  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center gap-2">
        <Button variant="ghost" onClick={onBack}><ArrowLeft className="h-4 w-4" />All projects</Button>
        {project && <Button variant="ghost" className="ml-auto text-rose-300" onClick={removeProject}><Trash2 className="h-4 w-4" />Delete project</Button>}
      </div>
      {/* 9.2: the main action sits top-left under the project name (it used to be a card on the far right) */}
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-xl font-bold text-white">{project?.name || "Project"}</h1>
        {project && <Badge>{project.session_count} session{project.session_count === 1 ? "" : "s"}</Badge>}
        {!empty && (
          <Button busy={busy} onClick={() => { void create(); }} data-testid="new-session">
            <Plus className="h-4 w-4" />New session</Button>
        )}
      </div>
      {project && <p className="-mt-3 text-sm text-slate-400">{project.description}</p>}
      {error && <Alert tone="red">{error}</Alert>}
      {empty && (
        <div className="mx-auto max-w-xl rounded-xl border border-dashed border-sky-500/40 bg-sky-500/5 p-8 text-center" data-testid="sessions-empty">
          <Layers className="mx-auto h-8 w-8 text-sky-400" aria-hidden />
          <h2 className="mt-2 text-lg font-semibold text-white">Start your first session</h2>
          <p className="mt-1 text-sm text-slate-300">A session is one complete run of the 5 steps (Watch, Build, Govern, Execute,
            Export). No name needed - the AI names it from your recording, and you can rename it any time.</p>
          <div className="mt-4 flex justify-center">
            <Button busy={busy} onClick={() => { void create(); }} data-testid="new-session"><Plus className="h-4 w-4" />New session</Button>
          </div>
        </div>
      )}
      {!empty && (
        <>
          <p className="text-xs text-slate-400">Each session is one complete run of the 5 steps. The AI names a new session after
            Step 1 - click a session to open it.</p>
          <ul className="grid gap-3 md:grid-cols-2">
            {project?.sessions.map((r) => (
              <li key={r.session_id} className="flex items-start gap-2 rounded-xl border border-slate-800 bg-slate-900/70 p-3 hover:border-sky-500/60">
                <button type="button" onClick={() => onOpenSession({ session_id: r.session_id })}
                  className="min-w-0 flex-1 text-left focus:outline-none focus-visible:ring-2 focus-visible:ring-sky-400">
                  <span className="block truncate font-semibold text-white">{r.name}
                    {r.name_source && r.name_source !== "USER" && !r.name_auto
                      ? <Sparkles className="ml-1 inline h-3 w-3 text-sky-300" aria-label="named by the AI" /> : null}</span>
                  <span className="block truncate text-xs text-slate-400">
                    {r.process_name || "not analysed yet"}{r.target_url ? ` · ${r.target_url}` : ""}{r.framework ? ` · ${r.framework}` : ""}
                  </span>
                  <span className="mt-1 flex flex-wrap gap-1.5"><SessionStatus r={r} />
                    <Badge tone="slate"><Clock className="h-3 w-3" />{String(r.created_at).slice(0, 16).replace("T", " ")}</Badge></span>
                </button>
                <button type="button" onClick={() => removeSession(r)} aria-label={`Delete session ${r.name}`}
                  className="rounded p-1 text-slate-400 hover:bg-slate-800 hover:text-rose-300"><Trash2 className="h-4 w-4" /></button>
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}

/* ============================================================================================
 * Step 1: previous-session context picker (search by name, sessions and whole projects)
 * ========================================================================================== */
export function HistoryPicker({ value, onChange, excludeSession }: {
  value: HistoryRef[]; onChange: (refs: HistoryRef[]) => void; excludeSession?: string;
}) {
  const [q, setQ] = useState("");
  const [res, setRes] = useState<HistorySearch | null>(null);
  const [error, setError] = useState<string | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => {
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => {
      studioApi.searchHistory(q, excludeSession).then((r) => { setRes(r); setError(null); })
        .catch((e) => setError((e as StudioApiError).message));
    }, 250);
    return () => { if (timer.current) clearTimeout(timer.current); };
  }, [q, excludeSession]);
  const has = (t: string, id: string) => value.some((v) => v.type === t && v.id === id);
  const add = (r: HistoryRef) => { if (!has(r.type, r.id) && value.length < 20) onChange([...value, r]); };
  const remove = (r: HistoryRef) => onChange(value.filter((v) => !(v.type === r.type && v.id === r.id)));
  return (
    <div>
      {value.length > 0 && (
        <ul className="mb-3 flex flex-wrap gap-2" aria-label="Context added to this run">
          {value.map((r) => (
            <li key={r.type + r.id} className="flex items-center gap-1 rounded-full border border-violet-500/40 bg-violet-500/10 py-0.5 pl-2.5 pr-1 text-xs text-violet-100">
              {r.type === "project" ? <Layers className="h-3 w-3" aria-hidden /> : <History className="h-3 w-3" aria-hidden />}
              {r.type === "project" ? `All sessions of ${r.name}` : `${r.project_name ? r.project_name + " / " : ""}${r.name}`}
              <button type="button" onClick={() => remove(r)} aria-label={`Remove ${r.name}`} className="rounded-full p-0.5 hover:bg-violet-500/30">
                <X className="h-3 w-3" />
              </button>
            </li>
          ))}
        </ul>
      )}
      <label className="relative block">
        <span className="sr-only">Search previous sessions and projects by name</span>
        <Search className="pointer-events-none absolute left-3 top-2.5 h-4 w-4 text-slate-400" aria-hidden />
        <input className={cx(inputCls, "pl-9")} value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search your sessions and projects by name..." />
      </label>
      {error && <p className="mt-2 text-xs text-rose-300">{error}</p>}
      {res && (
        <div className="mt-2 max-h-64 space-y-3 overflow-auto rounded-lg border border-slate-800 bg-slate-950/60 p-2">
          {res.projects.length > 0 && (
            <div>
              <div className="px-1 text-[11px] uppercase tracking-wide text-slate-400">Projects - adds every analysed session in it</div>
              <ul>
                {res.projects.map((p) => (
                  <li key={p.project_id} className="flex items-center gap-2 rounded px-1 py-1 text-sm hover:bg-slate-900">
                    <Layers className="h-3.5 w-3.5 shrink-0 text-sky-400" aria-hidden />
                    <span className="min-w-0 flex-1 truncate text-slate-200">{p.name}
                      <span className="ml-2 text-[11px] text-slate-400">{p.usable_sessions}/{p.session_count} sessions with an analysis</span></span>
                    <Button variant="secondary" className="px-2 py-1 text-xs" disabled={has("project", p.project_id) || p.usable_sessions === 0}
                      onClick={() => add({ type: "project", id: p.project_id, name: p.name, sessions: p.usable_sessions })}>
                      {has("project", p.project_id) ? "Added" : "Add project"}
                    </Button>
                  </li>
                ))}
              </ul>
            </div>
          )}
          <div>
            <div className="px-1 text-[11px] uppercase tracking-wide text-slate-400">Sessions</div>
            {res.sessions.length === 0 && <p className="px-1 py-1 text-xs text-slate-400">{q ? "No session matches that name." : "No previous sessions yet."}</p>}
            <ul>
              {res.sessions.map((r) => (
                <li key={r.session_id} className="flex items-center gap-2 rounded px-1 py-1 text-sm hover:bg-slate-900">
                  <History className="h-3.5 w-3.5 shrink-0 text-violet-300" aria-hidden />
                  <span className="min-w-0 flex-1 truncate text-slate-200">{r.name}
                    <span className="ml-2 text-[11px] text-slate-400">{r.project_name} · {r.process_name || "no analysis"}</span></span>
                  <SessionStatus r={r} />
                  <Button variant="secondary" className="px-2 py-1 text-xs" disabled={!r.usable || has("session", r.session_id)}
                    title={r.usable ? undefined : "This session never finished Step 1, so it has nothing to learn from yet"}
                    onClick={() => add({ type: "session", id: r.session_id, name: r.name, project_name: r.project_name })}>
                    {has("session", r.session_id) ? "Added" : "Add"}
                  </Button>
                </li>
              ))}
            </ul>
          </div>
        </div>
      )}
    </div>
  );
}

/** What the WATCH agent did with the chosen history (shown in the review). */
export function HistoryUsed({ s }: { s: WizardSession }) {
  const h = s.history_context;
  if (!h || (!h.sessions.length && !h.skipped.length && !h.blocked.length)) return null;
  return (
    <div className="mt-3 rounded-lg border border-violet-500/40 bg-violet-500/10 p-3 text-[13px] text-violet-100" data-testid="history-used">
      <div className="mb-1 flex items-center gap-2 font-semibold"><History className="h-4 w-4 text-violet-300" />
        Context from {h.sessions.length} previous session{h.sessions.length === 1 ? "" : "s"}{" "}
        {h.used_by === "ACTUAL_AI_API" ? "was given to the AI" : "was used by the offline planner (proven selectors and parameter names on the same site)"}</div>
      <p className="text-xs opacity-90">{h.sessions.map((x) => `${x.project_name ? x.project_name + " / " : ""}${x.name}`).join(", ")}</p>
      {h.applied && h.applied.length > 0 ? (
        <ul className="mt-2 list-disc space-y-0.5 pl-5 text-xs">{h.applied.map((a, i) => <li key={i}>{a}</li>)}</ul>
      ) : <p className="mt-1 text-xs opacity-80">No specific re-use was reported for this analysis.</p>}
      {h.skipped.length > 0 && <p className="mt-1 text-xs text-amber-200">Left out: {h.skipped.map((x) => `${x.name} (${x.why})`).join("; ")}</p>}
      {h.blocked.length > 0 && <p className="mt-1 text-xs text-rose-200">{h.blocked.map((x) => x.message).join(" ")}</p>}
    </div>
  );
}
