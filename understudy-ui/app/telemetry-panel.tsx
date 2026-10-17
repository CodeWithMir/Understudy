"use client";

/**
 * Mission Control - detailed activity telemetry.
 *
 * Shows everything users do in Understudy and everything the AI layer does for them: user actions, the exact prompt
 * sent to Gemini (system + user prompt, model, masked key, media), the raw response, errors, and - when the AI could
 * not be used - which local heuristic ran instead and what it produced.
 *
 * Scrolling: the feed only follows new events while the admin is already at the bottom. Scrolling up stops the
 * follow, and a "N new events" button jumps back down. "Pause" freezes the feed entirely (new events are buffered).
 */
import React, { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle, ArrowDown, BookOpen, Bot, ChevronDown, ChevronRight, Cpu, Filter, Pause, Play, Search, ShieldAlert, Terminal,
  Trash2, User, Wrench,
} from "lucide-react";
import { adminApi, type ActivityEvent, type ActivityKind, type ActivityPage } from "../lib/admin";
import { Badge, cx, FOLD_DEFAULT_OPEN, useFold } from "./wizard/ui";

const MAX_EVENTS = 4000;
const KIND_META: Record<ActivityKind, { label: string; tone: "slate" | "green" | "amber" | "red" | "sky" | "violet" }> = {
  USER_ACTION: { label: "User", tone: "sky" },
  AI_REQUEST: { label: "AI request", tone: "violet" },
  AI_RESPONSE: { label: "AI response", tone: "green" },
  AI_REPAIR: { label: "AI repair round", tone: "amber" },
  AI_ERROR: { label: "AI error", tone: "red" },
  RECALL: { label: "Learned skill (offline)", tone: "violet" },
  FALLBACK: { label: "Heuristic fallback", tone: "amber" },
  STEP: { label: "Pipeline", tone: "slate" },
  EXECUTION: { label: "Execution", tone: "slate" },
  RUNTIME: { label: "Runtime", tone: "slate" },
  SECURITY: { label: "Model Armor", tone: "red" },
  ADMIN: { label: "Admin", tone: "sky" },
};
const ALL_KINDS = Object.keys(KIND_META) as ActivityKind[];
const DEFAULT_HIDDEN: ActivityKind[] = ["EXECUTION"];

function KindIcon({ kind }: { kind: ActivityKind }) {
  const c = "h-3.5 w-3.5";
  if (kind === "USER_ACTION") return <User className={cx(c, "text-sky-400")} />;
  if (kind === "AI_REQUEST" || kind === "AI_RESPONSE") return <Bot className={cx(c, "text-emerald-400")} />;
  if (kind === "AI_REPAIR") return <Wrench className={cx(c, "text-amber-400")} />;
  if (kind === "AI_ERROR") return <AlertTriangle className={cx(c, "text-rose-400")} />;
  if (kind === "RECALL") return <BookOpen className={cx(c, "text-violet-400")} />;
  if (kind === "FALLBACK") return <Cpu className={cx(c, "text-amber-400")} />;
  if (kind === "SECURITY") return <ShieldAlert className={cx(c, "text-rose-400")} />;
  if (kind === "RUNTIME" || kind === "ADMIN") return <Wrench className={cx(c, "text-slate-400")} />;
  return <Terminal className={cx(c, "text-slate-400")} />;
}

function Block({ label, value }: { label: string; value: unknown }) {
  if (value === undefined || value === null || value === "") return null;
  const text = typeof value === "string" ? value : JSON.stringify(value, null, 2);
  return (
    <div className="space-y-1">
      <div className="text-[10px] font-bold uppercase tracking-wider text-slate-400">{label}</div>
      <pre className="max-h-80 overflow-auto whitespace-pre-wrap break-words rounded-lg border border-slate-800 bg-slate-950 p-2.5 text-[11px] leading-relaxed text-slate-300">{text}</pre>
    </div>
  );
}

function EventDetail({ e }: { e: ActivityEvent }) {
  const d = e.data || {};
  const known = new Set(["system_prompt", "user_prompt", "raw_response", "parsed", "heuristic_output", "dropped_by_validation",
    "validation_errors", "friendly", "reason", "answer"]);
  const meta = Object.fromEntries(Object.entries(d).filter(([k]) => !known.has(k)));
  return (
    <div className="mt-2 space-y-2 border-t border-slate-800 pt-2">
      {e.detail && <Block label="Detail" value={e.detail} />}
      {Object.keys(meta).length > 0 && <Block label="Metadata" value={meta} />}
      <Block label="System prompt sent to the API" value={d.system_prompt} />
      <Block label="User prompt sent to the API" value={d.user_prompt} />
      <Block label="Raw API response" value={d.raw_response} />
      <Block label="Parsed & accepted JSON" value={d.parsed} />
      <Block label="Dropped by output validation" value={(d.dropped_by_validation as unknown[] | undefined)?.length ? d.dropped_by_validation : null} />
      <Block label="Validation errors" value={d.validation_errors} />
      <Block label="Re-used learned answer" value={d.answer} />
      <Block label="Why the heuristic ran" value={d.reason} />
      <Block label="What the local heuristic produced" value={d.heuristic_output} />
    </div>
  );
}

function EventRow({ e, open, onToggle }: { e: ActivityEvent; open: boolean; onToggle: () => void }) {
  const meta = KIND_META[e.kind] || KIND_META.STEP;
  const model = (e.data?.model as string | undefined) || "";
  const dur = e.data?.duration_ms as number | undefined;
  return (
    <div className={cx("rounded-lg border px-3 py-2 transition-colors",
      e.level === "error" ? "border-rose-500/40 bg-rose-950/20" : e.level === "warn" ? "border-amber-500/30 bg-amber-950/10" : "border-slate-800 bg-slate-950/60",
      open && "ring-1 ring-sky-500/40")}>
      <button type="button" onClick={onToggle} className="flex w-full items-start gap-2 text-left">
        {open ? <ChevronDown className="mt-0.5 h-3.5 w-3.5 shrink-0 text-slate-400" /> : <ChevronRight className="mt-0.5 h-3.5 w-3.5 shrink-0 text-slate-400" />}
        <KindIcon kind={e.kind} />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-1.5">
            <Badge tone={meta.tone}>{meta.label}</Badge>
            {e.stage && <span className="font-mono text-[10px] text-slate-400">{e.stage}</span>}
            {model && <span className="font-mono text-[10px] text-emerald-400/80">{model}</span>}
            {dur !== undefined && <span className="font-mono text-[10px] text-slate-400">{Math.round(dur)} ms</span>}
            <span className="ml-auto font-mono text-[10px] text-slate-400">
              {e.session_id || "system"} · {new Date(e.ts).toLocaleTimeString()}
            </span>
          </div>
          {e.context_label && <div className="mt-1 font-mono text-[10px] text-sky-300/90" data-testid="event-context">{e.context_label}</div>}
          <div className="mt-1 break-words text-xs text-slate-200">{e.title}</div>
        </div>
      </button>
      {open && <EventDetail e={e} />}
    </div>
  );
}

export function TelemetryPanel({ onStats }: { onStats?: (s: ActivityPage["stats"]) => void }) {
  const [events, setEvents] = useState<ActivityEvent[]>([]);
  const [paused, setPaused] = useState(false);
  const [buffered, setBuffered] = useState<ActivityEvent[]>([]);
  const [hidden, setHidden] = useState<Set<ActivityKind>>(new Set(DEFAULT_HIDDEN));
  const [session, setSession] = useState<string>("");
  const [userF, setUserF] = useState<string>("");
  const [projectF, setProjectF] = useState<string>("");
  const [q, setQ] = useState("");
  const [open, setOpen] = useState<Set<number>>(new Set());
  const [atBottom, setAtBottom] = useState(true);
  const [unseen, setUnseen] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const lastSeq = useRef(0);
  const pausedRef = useRef(paused);
  const boxRef = useRef<HTMLDivElement>(null);
  const followRef = useRef(true);
  const onStatsRef = useRef(onStats);

  useEffect(() => { pausedRef.current = paused; }, [paused]);
  useEffect(() => { onStatsRef.current = onStats; }, [onStats]);

  const poll = useCallback(async () => {
    try {
      const page = await adminApi.telemetry(lastSeq.current);
      setError(null);
      onStatsRef.current?.(page.stats);
      if (page.last_seq < lastSeq.current) {           // backend restarted or log cleared
        lastSeq.current = 0;
        setEvents([]);
        return;
      }
      if (!page.events.length) return;
      lastSeq.current = page.events[page.events.length - 1].seq;
      if (pausedRef.current) {
        setBuffered((b) => [...b, ...page.events].slice(-MAX_EVENTS));
      } else {
        setEvents((ev) => [...ev, ...page.events].slice(-MAX_EVENTS));
        if (!followRef.current) setUnseen((n) => n + page.events.length);
      }
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  useEffect(() => {
    const first = setTimeout(() => { void poll(); }, 0);
    const id = setInterval(() => { void poll(); }, 1500);
    return () => { clearTimeout(first); clearInterval(id); };
  }, [poll]);

  const visible = useMemo(() => {
    const t = q.trim().toLowerCase();
    return events.filter((e) => !hidden.has(e.kind) && (!session || e.session_id === session) &&
      (!userF || e.context?.user === userF) && (!projectF || e.context?.project === projectF) &&
      (!t || `${e.title} ${e.detail} ${e.context_label || ""}`.toLowerCase().includes(t)));
  }, [events, hidden, session, q, userF, projectF]);

  const sessions = useMemo(() => {
    const m = new Map<string, string>();
    events.forEach((e) => { if (e.session_id) m.set(e.session_id, e.context ? `${e.context.user} | ${e.context.project} | ${e.context.session_name}` : e.session_id); });
    return Array.from(m.entries()).reverse();
  }, [events]);
  const usersSeen = useMemo(() => Array.from(new Set(events.map((e) => e.context?.user).filter(Boolean) as string[])).sort(), [events]);
  const projectsSeen = useMemo(() => Array.from(new Set(events.map((e) => e.context?.project).filter(Boolean) as string[])).sort(), [events]);

  // Follow new events ONLY when the admin is already at the bottom (never yank them away from what they're reading).
  useLayoutEffect(() => {
    const box = boxRef.current;
    if (box && followRef.current) box.scrollTop = box.scrollHeight;
  }, [visible.length]);

  const onScroll = () => {
    const box = boxRef.current;
    if (!box) return;
    const bottom = box.scrollHeight - box.scrollTop - box.clientHeight < 48;
    followRef.current = bottom;
    setAtBottom(bottom);
    if (bottom) setUnseen(0);
  };

  const jumpToLatest = () => {
    const box = boxRef.current;
    if (!box) return;
    followRef.current = true;
    box.scrollTo({ top: box.scrollHeight, behavior: "smooth" });
    setUnseen(0);
    setAtBottom(true);
  };

  const togglePause = () => {
    if (paused) {
      setEvents((ev) => [...ev, ...buffered].slice(-MAX_EVENTS));
      if (!followRef.current) setUnseen((n) => n + buffered.length);
      setBuffered([]);
    }
    setPaused(!paused);
  };

  const toggleKind = (k: ActivityKind) => setHidden((h) => {
    const n = new Set(h);
    if (n.has(k)) n.delete(k); else n.add(k);
    return n;
  });

  const toggleOpen = (seq: number) => {
    followRef.current = false;          // expanding an event must never cause a jump
    setOpen((o) => {
      const n = new Set(o);
      if (n.has(seq)) n.delete(seq); else n.add(seq);
      return n;
    });
  };

  const [foldOpen, setFoldOpen] = useFold("telemetry", FOLD_DEFAULT_OPEN);
  const folded = !foldOpen;

  return (
    <div className={cx("flex flex-col rounded-2xl border border-slate-800 bg-slate-900/70 shadow-xl",
      folded ? "" : "h-[calc(100vh-260px)] min-h-[560px]")} data-folded={folded ? "true" : undefined}>
      <div className={cx("space-y-3 p-4", !folded && "border-b border-slate-800")}>
        <div className="flex flex-wrap items-center gap-2">
          <button type="button" onClick={() => setFoldOpen(folded)} aria-expanded={!folded} title={folded ? "Open the telemetry" : "Fold the telemetry"}
            className="flex items-center gap-2 rounded text-left hover:text-sky-200">
            {folded ? <ChevronRight className="h-4 w-4 text-slate-400" /> : <ChevronDown className="h-4 w-4 text-slate-400" />}
            <Terminal className="h-4 w-4 text-emerald-400" />
            <h2 className="text-sm font-semibold text-white">Live activity telemetry</h2>
          </button>
          <span className="text-[11px] text-slate-400">{visible.length} of {events.length} events</span>
          <div className="ml-auto flex items-center gap-2">
            <button type="button" onClick={togglePause}
              className={cx("inline-flex items-center gap-1.5 rounded-lg border px-2.5 py-1 text-[11px] font-semibold",
                paused ? "border-amber-500/50 bg-amber-500/10 text-amber-300" : "border-slate-700 bg-slate-800 text-slate-200 hover:bg-slate-700")}>
              {paused ? <><Play className="h-3 w-3" />Resume{buffered.length ? ` (${buffered.length} new)` : ""}</> : <><Pause className="h-3 w-3" />Pause</>}
            </button>
            <button type="button" title="Clear the telemetry log" onClick={async () => {
              if (!window.confirm("Clear all telemetry events on the server?")) return;
              await adminApi.clearTelemetry().catch(() => null);
              lastSeq.current = 0; setEvents([]); setBuffered([]); setOpen(new Set());
            }} className="rounded-lg border border-slate-700 bg-slate-800 p-1.5 text-slate-400 hover:text-rose-300">
              <Trash2 className="h-3.5 w-3.5" />
            </button>
          </div>
        </div>
        <div className={cx("flex flex-wrap items-center gap-2", folded && "hidden")}>
          <div className="relative min-w-[180px] flex-1">
            <Search className="absolute left-2.5 top-2 h-3.5 w-3.5 text-slate-400" />
            <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search titles and details..."
              className="w-full rounded-lg border border-slate-700 bg-slate-950 py-1.5 pl-8 pr-2 text-xs text-slate-100 placeholder-slate-400 focus:border-sky-500 focus:outline-none" />
          </div>
          <select value={userF} onChange={(e) => setUserF(e.target.value)} aria-label="Filter by user"
            className="rounded-lg border border-slate-700 bg-slate-950 px-2 py-1.5 text-xs text-slate-200">
            <option value="">All users</option>
            {usersSeen.map((u) => <option key={u} value={u}>{u}</option>)}
          </select>
          <select value={projectF} onChange={(e) => setProjectF(e.target.value)} aria-label="Filter by project"
            className="rounded-lg border border-slate-700 bg-slate-950 px-2 py-1.5 text-xs text-slate-200">
            <option value="">All projects</option>
            {projectsSeen.map((p) => <option key={p} value={p}>{p}</option>)}
          </select>
          <select value={session} onChange={(e) => setSession(e.target.value)} aria-label="Filter by session"
            className="rounded-lg border border-slate-700 bg-slate-950 px-2 py-1.5 font-mono text-xs text-slate-200">
            <option value="">All sessions</option>
            {sessions.map(([id, label]) => <option key={id} value={id}>{label}</option>)}
          </select>
        </div>
        <div className={cx("flex flex-wrap items-center gap-1.5", folded && "hidden")}>
          <Filter className="h-3 w-3 text-slate-400" />
          {ALL_KINDS.map((k) => (
            <button key={k} type="button" onClick={() => toggleKind(k)}
              className={cx("rounded-full border px-2 py-0.5 text-[10px] font-semibold transition-colors",
                hidden.has(k) ? "border-slate-800 text-slate-400 line-through" : "border-slate-600 bg-slate-800 text-slate-200")}>
              {KIND_META[k].label}
            </button>
          ))}
        </div>
        {error && <div className="text-[11px] text-rose-300">Telemetry unavailable: {error}</div>}
      </div>

      <div className={cx("relative min-h-0 flex-1", folded && "hidden")}>
        <div ref={boxRef} onScroll={onScroll} className="h-full space-y-1.5 overflow-y-auto p-3 [overflow-anchor:none]">
          {visible.length === 0 ? (
            <div className="flex h-full flex-col items-center justify-center text-center text-xs text-slate-400">
              <Terminal className="mb-2 h-8 w-8 text-emerald-400 opacity-30" />
              <p>No events yet. Everything users do in Understudy - and every AI request, response and fallback - appears here.</p>
            </div>
          ) : visible.map((e) => <EventRow key={e.seq} e={e} open={open.has(e.seq)} onToggle={() => toggleOpen(e.seq)} />)}
        </div>
        {!atBottom && (
          <button type="button" onClick={jumpToLatest}
            className="absolute bottom-3 left-1/2 inline-flex -translate-x-1/2 items-center gap-1.5 rounded-full bg-sky-500 px-3 py-1.5 text-[11px] font-bold text-slate-950 shadow-lg hover:bg-sky-400">
            <ArrowDown className="h-3.5 w-3.5" />{unseen ? `${unseen} new event${unseen === 1 ? "" : "s"}` : "Jump to latest"}
          </button>
        )}
      </div>
    </div>
  );
}
