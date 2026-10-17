"use client";

/* 8.0 IDE-style workspace explorer (left sidebar of the Studio).
 *
 * Workspace -> Projects (folders; hover/focus shows the description) -> Sessions (file items with a pipeline badge).
 * Click a session to load it into the 5-step wizard on the right. "+" creates projects and sessions. Right-click (or
 * Shift+F10 / the context-menu key / the "..." button) opens Duplicate, Delete and View Execution Logs.
 * Keyboard: a WAI-ARIA tree with roving focus - Up/Down move, Right expands / enters, Left collapses / goes to the parent,
 * Enter opens, Delete deletes a session. */
import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ChevronDown, ChevronRight, Copy, FileCode2, Folder, FolderOpen, FolderPlus, Layers, MoreHorizontal, PanelLeftClose,
  Pencil, Plus, RefreshCw, ScrollText, Trash2, X,
} from "lucide-react";
import { studioApi, type Project, type SessionSummary } from "../../lib/studio";
import { wizardApi, type ExecutionLogs, type WizardSession } from "../../lib/wizard";
import { Alert, Button, Field, cx, inputCls } from "./ui";

type Badge = NonNullable<SessionSummary["badge"]>;
const BADGE_STYLE: Record<Badge, string> = {
  DRAFT: "border-slate-600 text-slate-300",
  ANALYSED: "border-sky-500/50 text-sky-300",
  BUILT: "border-indigo-500/50 text-indigo-300",
  GOVERNED: "border-violet-500/50 text-violet-300",
  RUNNING: "border-sky-400/60 text-sky-200 animate-pulse",
  PENDING: "border-amber-500/60 text-amber-300",
  FAILED: "border-rose-500/60 text-rose-300",
  VERIFIED: "border-emerald-500/60 text-emerald-300",
};
const BADGE_HELP: Record<Badge, string> = {
  DRAFT: "Step 1 not submitted yet", ANALYSED: "The AI analysed the recording", BUILT: "Automation code generated",
  GOVERNED: "Payload complies with the SOP", RUNNING: "A step is running right now",
  PENDING: "Waiting for your admin to enable a tool", FAILED: "A step failed - open it to see why",
  VERIFIED: "All 5 steps passed - the ZIP is ready",
};

export function SessionBadge({ b }: { b?: SessionSummary["badge"] }) {
  const k = (b || "DRAFT") as Badge;
  return (
    <span title={BADGE_HELP[k]} className={cx("shrink-0 rounded border px-1 py-px font-mono text-[9px] font-bold leading-none tracking-wide", BADGE_STYLE[k])}>
      {k}
    </span>
  );
}

const EXP_KEY = "understudy_explorer_expanded";

type TreeNode =
  | { kind: "root"; key: string; level: 1 }
  | { kind: "project"; key: string; level: 2; project: Project }
  | { kind: "session"; key: string; level: 3; project: Project; session: SessionSummary };

type Menu = { x: number; y: number; node: TreeNode } | null;

export function Explorer({ user, selectedSession, selectedProject, refreshKey, onHome, onOpenProject, onOpenSession,
  onSessionGone, onCollapse, onRenamed }: {
  user: string | null; selectedSession: string | null; selectedProject: string | null; refreshKey: number;
  onHome: () => void; onOpenProject: (id: string) => void; onOpenSession: (sid: string, projectId: string) => void;
  onSessionGone: (sid: string) => void; onCollapse?: () => void;
  /** 9.1: a session was renamed here - the open session reloads its title */
  onRenamed?: () => void;
}) {
  const [projects, setProjects] = useState<Project[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Set<string>>(() => {
    try { return new Set(JSON.parse(window.localStorage.getItem(EXP_KEY) || "[]")); } catch { return new Set(); }
  });
  const [focusKey, setFocusKey] = useState<string>("root");
  const [menu, setMenu] = useState<Menu>(null);
  const [addOpen, setAddOpen] = useState(false);
  const [dialog, setDialog] = useState<null | { kind: "project" } | { kind: "rename"; session: SessionSummary } |
    { kind: "logs"; sid: string; name: string } | { kind: "delete"; session: SessionSummary } |
    { kind: "delete-project"; project: Project }>(null);
  const [tip, setTip] = useState<{ text: string; x: number; y: number } | null>(null);
  const itemRefs = useRef<Record<string, HTMLElement | null>>({});

  const load = useCallback(() => {
    studioApi.projects().then((r) => { setProjects(r.projects); setError(null); })
      .catch((e) => setError((e as Error).message));
  }, []);
  useEffect(load, [load, refreshKey]);
  useEffect(() => {
    try { window.localStorage.setItem(EXP_KEY, JSON.stringify([...expanded])); } catch { /* ignore */ }
  }, [expanded]);
  // the open session's project is always expanded
  useEffect(() => {
    if (!selectedProject) return;
    const t = setTimeout(() => setExpanded((e) => (e.has(selectedProject) ? e : new Set([...e, selectedProject]))), 0);
    return () => clearTimeout(t);
  }, [selectedProject]);

  const visible: TreeNode[] = useMemo(() => {
    const out: TreeNode[] = [{ kind: "root", key: "root", level: 1 }];
    if (!expanded.has("__root_collapsed")) {
      for (const p of projects || []) {
        out.push({ kind: "project", key: p.project_id, level: 2, project: p });
        if (expanded.has(p.project_id)) {
          for (const s of p.sessions) out.push({ kind: "session", key: s.session_id, level: 3, project: p, session: s });
        }
      }
    }
    return out;
  }, [projects, expanded]);

  // "+" > New session goes into the project that is open, or the one the keyboard / mouse is on
  const targetProject = (projects || []).find((p) => p.project_id === selectedProject)
    || (projects || []).find((p) => p.project_id === focusKey || p.sessions.some((x) => x.session_id === focusKey)) || null;

  const focus = (key: string) => {
    setFocusKey(key);
    setTimeout(() => itemRefs.current[key]?.focus(), 0);
  };
  const toggle = (key: string, open?: boolean) => setExpanded((e) => {
    const n = new Set(e);
    const isOpen = key === "__root_collapsed" ? !n.has(key) : n.has(key);
    const want = open === undefined ? !isOpen : open;
    if (key === "__root_collapsed") { if (want) n.delete(key); else n.add(key); } else if (want) n.add(key); else n.delete(key);
    return n;
  });
  const activate = (n: TreeNode) => {
    if (n.kind === "root") onHome();
    else if (n.kind === "project") { onOpenProject(n.project.project_id); toggle(n.project.project_id, true); }
    else onOpenSession(n.session.session_id, n.project.project_id);
  };
  const openMenuAt = (n: TreeNode, el: HTMLElement | null, x?: number, y?: number) => {
    const r = el?.getBoundingClientRect();
    setMenu({ node: n, x: x ?? (r ? r.left + 24 : 40), y: y ?? (r ? r.bottom : 40) });
  };

  const onKey = (e: React.KeyboardEvent, n: TreeNode) => {
    const i = visible.findIndex((v) => v.key === n.key);
    const isOpen = n.kind === "root" ? !expanded.has("__root_collapsed") : n.kind === "project" && expanded.has(n.key);
    switch (e.key) {
      case "ArrowDown": e.preventDefault(); if (i < visible.length - 1) focus(visible[i + 1].key); break;
      case "ArrowUp": e.preventDefault(); if (i > 0) focus(visible[i - 1].key); break;
      case "Home": e.preventDefault(); focus(visible[0].key); break;
      case "End": e.preventDefault(); focus(visible[visible.length - 1].key); break;
      case "ArrowRight":
        e.preventDefault();
        if (n.kind !== "session" && !isOpen) toggle(n.kind === "root" ? "__root_collapsed" : n.key, true);
        else if (n.kind !== "session" && i < visible.length - 1 && visible[i + 1].level > n.level) focus(visible[i + 1].key);
        break;
      case "ArrowLeft":
        e.preventDefault();
        if (n.kind !== "session" && isOpen) toggle(n.kind === "root" ? "__root_collapsed" : n.key, false);
        else if (n.kind === "session") focus(n.project.project_id);
        else if (n.kind === "project") focus("root");
        break;
      case "Enter": case " ": e.preventDefault(); activate(n); break;
      case "Delete": if (n.kind === "session") { e.preventDefault(); setDialog({ kind: "delete", session: n.session }); } break;
      case "F10": if (e.shiftKey) { e.preventDefault(); openMenuAt(n, itemRefs.current[n.key]); } break;
      case "ContextMenu": e.preventDefault(); openMenuAt(n, itemRefs.current[n.key]); break;
      default: break;
    }
  };

  // 9.1: like a new chat - no name to type; the AI names the session after Step 1
  const newSession = async (p: Project) => {
    setMenu(null); setAddOpen(false);
    try {
      const r = await studioApi.createSession(p.project_id);
      load(); setExpanded((e) => new Set([...e, p.project_id])); onOpenSession(r.session.session_id, p.project_id);
    } catch (e) { setError((e as Error).message); }
  };

  const duplicate = async (s: SessionSummary, p: Project) => {
    setMenu(null);
    try {
      const d: WizardSession = await wizardApi.duplicate(s.session_id);
      load();
      onOpenSession(d.session_id, p.project_id);
    } catch (e) { setError((e as Error).message); }
  };

  return (
    <nav aria-label="Workspace explorer" className="flex h-full min-h-0 flex-col border-r border-slate-800 bg-slate-950/80">
      <div className="flex items-center gap-1 border-b border-slate-800 px-3 py-2">
        <span className="text-[11px] font-bold uppercase tracking-wider text-slate-400">Explorer</span>
        <div className="relative ml-auto flex items-center gap-0.5">
          <button type="button" aria-label="New project or session" aria-haspopup="menu" aria-expanded={addOpen}
            onClick={() => setAddOpen(!addOpen)} data-testid="explorer-add"
            className="rounded p-1 text-slate-400 hover:bg-slate-800 hover:text-white"><Plus className="h-4 w-4" /></button>
          <button type="button" aria-label="Refresh" onClick={load} className="rounded p-1 text-slate-400 hover:bg-slate-800 hover:text-white">
            <RefreshCw className="h-3.5 w-3.5" /></button>
          {onCollapse && <button type="button" aria-label="Hide the explorer" onClick={onCollapse}
            className="rounded p-1 text-slate-400 hover:bg-slate-800 hover:text-white"><PanelLeftClose className="h-3.5 w-3.5" /></button>}
          {addOpen && (
            <MenuList onClose={() => setAddOpen(false)} style={{ right: 0, top: 28 }} items={[
              { label: "New project...", icon: <FolderPlus className="h-3.5 w-3.5" />, onSelect: () => setDialog({ kind: "project" }) },
              targetProject
                ? { label: `New session in ${targetProject.name}`, icon: <Plus className="h-3.5 w-3.5" />,
                    onSelect: () => { void newSession(targetProject); } }
                : { label: "New session... (select a project first)", icon: <Plus className="h-3.5 w-3.5" />, disabled: true,
                    onSelect: () => undefined },
            ]} />
          )}
        </div>
      </div>
      {/* 9.2: the two main actions as labelled buttons at the top of the left sidebar (not only the small "+") */}
      <div className="flex gap-1.5 border-b border-slate-800 px-3 py-2" data-testid="explorer-actions">
        <button type="button" onClick={() => { if (targetProject) void newSession(targetProject); }} disabled={!targetProject}
          title={targetProject ? `New session in ${targetProject.name}` : "Open or select a project first"}
          className="inline-flex flex-1 items-center justify-center gap-1 rounded-md bg-sky-500 px-2 py-1.5 text-xs font-semibold text-slate-950 hover:bg-sky-400 disabled:cursor-not-allowed disabled:opacity-40">
          <Plus className="h-3.5 w-3.5" />New session</button>
        <button type="button" onClick={() => setDialog({ kind: "project" })}
          className="inline-flex flex-1 items-center justify-center gap-1 rounded-md border border-slate-700 px-2 py-1.5 text-xs font-semibold text-slate-200 hover:border-sky-500 hover:text-white">
          <FolderPlus className="h-3.5 w-3.5" />New project</button>
      </div>
      {error && <div className="p-2"><Alert tone="red">{error}</Alert></div>}
      <ul role="tree" aria-label="Workspace" className="min-h-0 flex-1 overflow-auto py-1 text-[13px]" data-testid="explorer-tree">
        {visible.map((n) => {
          const selected = (n.kind === "session" && n.session.session_id === selectedSession)
            || (n.kind === "project" && !selectedSession && n.project.project_id === selectedProject)
            || (n.kind === "root" && !selectedSession && !selectedProject);
          const isOpen = n.kind === "root" ? !expanded.has("__root_collapsed") : n.kind === "project" && expanded.has(n.key);
          const label = n.kind === "root" ? `${user || "local"}'s workspace` : n.kind === "project" ? n.project.name : n.session.name;
          return (
            <li key={n.key} role="treeitem" aria-level={n.level} aria-selected={selected}
              aria-expanded={n.kind === "session" ? undefined : isOpen}
              aria-label={n.kind === "session" ? `${label}, ${n.session.badge || "DRAFT"}` : label}
              tabIndex={focusKey === n.key ? 0 : -1}
              ref={(el) => { itemRefs.current[n.key] = el; }}
              data-testid={n.kind === "session" ? `tree-session-${n.session.session_id}` : n.kind === "project" ? `tree-project-${n.key}` : "tree-root"}
              onKeyDown={(e) => onKey(e, n)}
              onFocus={() => setFocusKey(n.key)}
              onClick={(e) => { e.stopPropagation(); setFocusKey(n.key); activate(n); }}
              onContextMenu={(e) => { if (n.kind === "root") return; e.preventDefault(); setFocusKey(n.key); openMenuAt(n, null, e.clientX, e.clientY); }}
              onMouseEnter={(e) => { if (n.kind === "project") { const r = e.currentTarget.getBoundingClientRect(); setTip({ text: n.project.description, x: r.right + 8, y: r.top }); } }}
              onMouseLeave={() => setTip(null)}
              className={cx("group relative flex cursor-pointer select-none items-center gap-1.5 py-1 pr-2 outline-none",
                "focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-sky-400",
                selected ? "bg-sky-500/15 text-white" : "text-slate-300 hover:bg-slate-800/70")}
              style={{ paddingLeft: 8 + (n.level - 1) * 14 }}>
              {n.kind !== "session" ? (
                <button type="button" tabIndex={-1} aria-hidden onClick={(e) => { e.stopPropagation(); toggle(n.kind === "root" ? "__root_collapsed" : n.key); }}
                  className="rounded text-slate-400 hover:text-white">
                  {isOpen ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
                </button>
              ) : <span className="w-3.5" aria-hidden />}
              {n.kind === "root" && <Layers className="h-4 w-4 shrink-0 text-sky-400" aria-hidden />}
              {n.kind === "project" && (isOpen ? <FolderOpen className="h-4 w-4 shrink-0 text-amber-300" aria-hidden />
                : <Folder className="h-4 w-4 shrink-0 text-amber-300" aria-hidden />)}
              {n.kind === "session" && <FileCode2 className="h-4 w-4 shrink-0 text-slate-400" aria-hidden />}
              <span className={cx("min-w-0 flex-1 truncate", n.kind === "root" && "text-[11px] font-bold uppercase tracking-wide text-slate-400")}
                aria-describedby={n.kind === "project" && focusKey === n.key ? `desc-${n.key}` : undefined}>{label}</span>
              {n.kind === "project" && <span className="text-[10px] text-slate-500">{n.project.session_count}</span>}
              {n.kind === "session" && <SessionBadge b={n.session.badge} />}
              {n.kind !== "root" && (
                <button type="button" tabIndex={-1} aria-hidden onClick={(e) => { e.stopPropagation(); openMenuAt(n, e.currentTarget as HTMLElement); }}
                  className="hidden rounded p-0.5 text-slate-400 hover:bg-slate-700 hover:text-white group-hover:block">
                  <MoreHorizontal className="h-3.5 w-3.5" /></button>
              )}
              {n.kind === "project" && focusKey === n.key && (
                <span id={`desc-${n.key}`} className="sr-only">{n.project.description}</span>
              )}
            </li>
          );
        })}
        {projects && projects.length === 0 && (
          <li role="none" className="px-4 py-3 text-xs text-slate-400">No projects yet. Use <b>+</b> to create one.</li>
        )}
        {!projects && !error && <li role="none" className="px-4 py-3 text-xs text-slate-400">Loading...</li>}
      </ul>
      {tip && tip.text && (
        <div role="tooltip" className="pointer-events-none fixed z-50 max-w-xs rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-xs text-slate-200 shadow-xl"
          style={{ left: tip.x, top: tip.y }} data-testid="project-tooltip">{tip.text}</div>
      )}
      {menu && (
        <MenuList onClose={() => setMenu(null)} style={{ left: menu.x, top: menu.y, position: "fixed" }}
          items={menu.node.kind === "session" ? [
            { label: "Open", icon: <FileCode2 className="h-3.5 w-3.5" />, onSelect: () => activate(menu.node) },
            { label: "Duplicate", icon: <Copy className="h-3.5 w-3.5" />, onSelect: () => {
              const n = menu.node as Extract<TreeNode, { kind: "session" }>; void duplicate(n.session, n.project); } },
            { label: "View Execution Logs", icon: <ScrollText className="h-3.5 w-3.5" />, onSelect: () => {
              const n = menu.node as Extract<TreeNode, { kind: "session" }>; setDialog({ kind: "logs", sid: n.session.session_id, name: n.session.name }); } },
            { label: "Rename...", icon: <Pencil className="h-3.5 w-3.5" />, onSelect: () => {
              const n = menu.node as Extract<TreeNode, { kind: "session" }>; setDialog({ kind: "rename", session: n.session }); } },
            { label: "Delete...", icon: <Trash2 className="h-3.5 w-3.5" />, danger: true, onSelect: () => {
              const n = menu.node as Extract<TreeNode, { kind: "session" }>; setDialog({ kind: "delete", session: n.session }); } },
          ] : menu.node.kind === "project" ? [
            { label: "New session", icon: <Plus className="h-3.5 w-3.5" />, onSelect: () => {
              const n = menu.node as Extract<TreeNode, { kind: "project" }>; void newSession(n.project); } },
            { label: "Open project", icon: <FolderOpen className="h-3.5 w-3.5" />, onSelect: () => activate(menu.node) },
            { label: "Delete project...", icon: <Trash2 className="h-3.5 w-3.5" />, danger: true, onSelect: () => {
              const n = menu.node as Extract<TreeNode, { kind: "project" }>; setDialog({ kind: "delete-project", project: n.project }); } },
          ] : []} />
      )}
      {dialog?.kind === "project" && <NewProjectDialog onClose={() => setDialog(null)} onCreated={(p) => {
        setDialog(null); load(); setExpanded((e) => new Set([...e, p.project_id])); onOpenProject(p.project_id); }} />}
      {dialog?.kind === "rename" && <RenameSessionDialog session={dialog.session} onClose={() => setDialog(null)} onRenamed={() => {
        setDialog(null); load(); onRenamed?.(); }} />}
      {dialog?.kind === "logs" && <LogsDialog sid={dialog.sid} name={dialog.name} onClose={() => setDialog(null)} />}
      {dialog?.kind === "delete" && (
        <ConfirmDialog title={`Delete "${dialog.session.name}"?`} confirm="Delete session" onClose={() => setDialog(null)}
          body="Its recording, generated code and evidence are removed. This can't be undone."
          onConfirm={async () => {
            const sid = dialog.session.session_id;
            await studioApi.deleteSession(sid);
            setDialog(null); load(); onSessionGone(sid);
          }} />
      )}
      {dialog?.kind === "delete-project" && (
        <ConfirmDialog title={`Delete the project "${dialog.project.name}"?`} confirm="Delete project" onClose={() => setDialog(null)}
          body={`All ${dialog.project.session_count} session(s) in it are deleted too. Type the project name to confirm.`}
          typeToConfirm={dialog.project.name}
          onConfirm={async () => {
            await studioApi.deleteProject(dialog.project.project_id);
            setDialog(null); load(); onHome();
          }} />
      )}
    </nav>
  );
}

/* ------------------------------------------------------------------------------------------------------------------ */
type MenuItem = { label: string; icon?: React.ReactNode; onSelect: () => void; danger?: boolean; disabled?: boolean };

function MenuList({ items, onClose, style }: { items: MenuItem[]; onClose: () => void; style: React.CSSProperties }) {
  const ref = useRef<HTMLUListElement>(null);
  useEffect(() => {
    const first = ref.current?.querySelector<HTMLElement>("[role=menuitem]:not([aria-disabled=true])");
    first?.focus();
    const away = (e: MouseEvent) => { if (!ref.current?.contains(e.target as Node)) onClose(); };
    const t = setTimeout(() => document.addEventListener("mousedown", away), 0);
    return () => { clearTimeout(t); document.removeEventListener("mousedown", away); };
  }, [onClose]);
  const onKey = (e: React.KeyboardEvent) => {
    const els = Array.from(ref.current?.querySelectorAll<HTMLElement>("[role=menuitem]") || []);
    const i = els.indexOf(document.activeElement as HTMLElement);
    if (e.key === "Escape" || e.key === "Tab") { e.preventDefault(); onClose(); }
    else if (e.key === "ArrowDown") { e.preventDefault(); els[(i + 1) % els.length]?.focus(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); els[(i - 1 + els.length) % els.length]?.focus(); }
  };
  return (
    <ul ref={ref} role="menu" onKeyDown={onKey} data-testid="context-menu"
      className="absolute z-50 min-w-[200px] rounded-lg border border-slate-700 bg-slate-900 py-1 text-[13px] shadow-2xl" style={style}>
      {items.map((it) => (
        <li key={it.label} role="menuitem" tabIndex={-1} aria-disabled={it.disabled || undefined}
          onClick={() => { if (it.disabled) return; onClose(); it.onSelect(); }}
          onKeyDown={(e) => { if ((e.key === "Enter" || e.key === " ") && !it.disabled) { e.preventDefault(); onClose(); it.onSelect(); } }}
          className={cx("flex cursor-pointer items-center gap-2 px-3 py-1.5 outline-none",
            it.disabled ? "cursor-not-allowed text-slate-500" : it.danger ? "text-rose-300 hover:bg-rose-500/10 focus:bg-rose-500/10"
              : "text-slate-200 hover:bg-slate-800 focus:bg-slate-800")}>
          {it.icon}{it.label}
        </li>
      ))}
    </ul>
  );
}

export function Modal({ title, onClose, children, wide }: { title: string; onClose: () => void; children: React.ReactNode; wide?: boolean }) {
  const ref = useRef<HTMLDivElement>(null);
  const titleId = React.useId();
  // 8.3 fix: parents re-render every few seconds (Mission Control polls) and pass a new onClose each time. The focus
  // effect used to depend on onClose, so every poll moved the cursor back to the FIRST field - e.g. while the admin
  // typed the username to confirm a Delete, the letters jumped into the Reason box. Focus once, on open, only.
  const closeRef = useRef(onClose);
  useEffect(() => { closeRef.current = onClose; }, [onClose]);
  useEffect(() => {
    const prev = document.activeElement as HTMLElement | null;
    ref.current?.querySelector<HTMLElement>("input, textarea, button:not([data-close])")?.focus();
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") closeRef.current(); };
    document.addEventListener("keydown", onKey);
    return () => { document.removeEventListener("keydown", onKey); prev?.focus?.(); };
  }, []);
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4" onMouseDown={(e) => { if (e.target === e.currentTarget) closeRef.current(); }}>
      <div ref={ref} role="dialog" aria-modal="true" aria-labelledby={titleId}
        className={cx("max-h-[90vh] w-full overflow-auto rounded-xl border border-slate-700 bg-slate-900 p-5 shadow-2xl", wide ? "max-w-4xl" : "max-w-md")}>
        <div className="mb-3 flex items-center gap-2">
          <h2 id={titleId} className="text-sm font-semibold text-white">{title}</h2>
          <button type="button" data-close aria-label="Close" onClick={onClose} className="ml-auto rounded p-1 text-slate-400 hover:bg-slate-800 hover:text-white">
            <X className="h-4 w-4" /></button>
        </div>
        {children}
      </div>
    </div>
  );
}

function NewProjectDialog({ onClose, onCreated }: { onClose: () => void; onCreated: (p: Project) => void }) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  return (
    <Modal title="New project" onClose={onClose}>
      <form className="space-y-3" onSubmit={async (e) => {
        e.preventDefault(); setBusy(true); setError(null);
        try { onCreated((await studioApi.createProject(name, description)).project); }
        catch (err) { setError((err as Error).message); } finally { setBusy(false); }
      }}>
        <Field label="Project Name"><input className={inputCls} value={name} onChange={(e) => setName(e.target.value)} maxLength={80} required /></Field>
        <Field label="Project Description" hint="What the automations in this project are for. Shown when you hover the folder.">
          <textarea className={cx(inputCls, "h-20")} value={description} onChange={(e) => setDescription(e.target.value)} maxLength={1000} required />
        </Field>
        {error && <Alert tone="red">{error}</Alert>}
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
          <Button type="submit" busy={busy} disabled={!name.trim() || !description.trim()}>Create project</Button>
        </div>
      </form>
    </Modal>
  );
}

function RenameSessionDialog({ session, onClose, onRenamed }: { session: SessionSummary; onClose: () => void; onRenamed: () => void }) {
  const [name, setName] = useState(session.name);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  return (
    <Modal title="Rename session" onClose={onClose}>
      <form className="space-y-3" onSubmit={async (e) => {
        e.preventDefault(); setBusy(true); setError(null);
        try { await wizardApi.rename(session.session_id, name); onRenamed(); }
        catch (err) { setError((err as Error).message); } finally { setBusy(false); }
      }}>
        <Field label="Session name" hint="Unique within the project. The AI won't rename it after you do.">
          <input autoFocus className={inputCls} value={name} onChange={(e) => setName(e.target.value)} maxLength={80} required />
        </Field>
        {error && <Alert tone="red">{error}</Alert>}
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
          <Button type="submit" busy={busy} disabled={!name.trim() || name.trim() === session.name}><Pencil className="h-4 w-4" />Rename</Button>
        </div>
      </form>
    </Modal>
  );
}

function ConfirmDialog({ title, body, confirm, onClose, onConfirm, typeToConfirm }: {
  title: string; body: string; confirm: string; onClose: () => void; onConfirm: () => Promise<void>; typeToConfirm?: string;
}) {
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  return (
    <Modal title={title} onClose={onClose}>
      <p className="text-sm text-slate-300">{body}</p>
      {typeToConfirm && <input aria-label="Type the name to confirm" className={cx(inputCls, "mt-3")} value={typed} onChange={(e) => setTyped(e.target.value)} />}
      {error && <div className="mt-3"><Alert tone="red">{error}</Alert></div>}
      <div className="mt-4 flex justify-end gap-2">
        <Button variant="ghost" onClick={onClose}>Cancel</Button>
        <Button variant="danger" busy={busy} disabled={!!typeToConfirm && typed !== typeToConfirm} onClick={async () => {
          setBusy(true); setError(null);
          try { await onConfirm(); } catch (e) { setError((e as Error).message); setBusy(false); }
        }}><Trash2 className="h-4 w-4" />{confirm}</Button>
      </div>
    </Modal>
  );
}

function LogsDialog({ sid, name, onClose }: { sid: string; name: string; onClose: () => void }) {
  const [logs, setLogs] = useState<ExecutionLogs | null>(null);
  const [tab, setTab] = useState<"run" | "deps" | "retries">("run");
  const [error, setError] = useState<string | null>(null);
  useEffect(() => { wizardApi.executionLogs(sid).then(setLogs).catch((e) => setError((e as Error).message)); }, [sid]);
  const lines = !logs ? [] : tab === "run" ? logs.lines : tab === "deps" ? logs.dependency_lines
    : logs.retries.map((r) => `attempt ${r.attempt}: ${r.category || ""} ${r.diagnosis || ""} -> ${r.outcome || ""}`);
  return (
    <Modal title={`Execution logs - ${name}`} onClose={onClose} wide>
      {error && <Alert tone="red">{error}</Alert>}
      {logs && (
        <>
          <p className="mb-2 text-xs text-slate-400">{logs.label} · run status <b className="text-slate-200">{logs.status || "not run yet"}</b>
            {logs.attempt ? ` · attempt ${logs.attempt}` : ""}{logs.strategy ? ` · ${logs.strategy}` : ""}</p>
          <div role="tablist" aria-label="Log type" className="mb-2 flex gap-1 text-xs">
            {([["run", `Run (${logs.lines.length})`], ["deps", `Dependencies (${logs.dependency_lines.length})`], ["retries", `Self-healing (${logs.retries.length})`]] as const).map(([k, l]) => (
              <button key={k} role="tab" aria-selected={tab === k} type="button" onClick={() => setTab(k)}
                className={cx("rounded-md px-2.5 py-1", tab === k ? "bg-sky-500/20 text-sky-200" : "text-slate-400 hover:bg-slate-800")}>{l}</button>
            ))}
            <button type="button" className="ml-auto rounded-md px-2.5 py-1 text-slate-400 hover:bg-slate-800"
              onClick={() => { void navigator.clipboard?.writeText(lines.join("\n")); }}><Copy className="inline h-3 w-3" /> Copy</button>
          </div>
          <pre role="log" aria-label="Log lines" className="max-h-[60vh] overflow-auto rounded-lg border border-slate-800 bg-black/60 p-3 font-mono text-[11.5px] leading-relaxed text-slate-300"
            data-testid="logs-dialog">{lines.length ? lines.join("\n") : "Nothing logged yet."}</pre>
          {logs.egress && logs.egress.denied?.length > 0 && (
            <p className="mt-2 text-xs text-amber-300">Network policy denied: {logs.egress.denied.map((d) => d.host).join(", ")}</p>
          )}
        </>
      )}
    </Modal>
  );
}
