"use client";

import React, { useState } from "react";
import { BookOpen, Bot, ChevronDown, ChevronRight, Cpu, Eye, EyeOff, HelpCircle, Loader2 } from "lucide-react";
import type { AgentSource, RecallInfo } from "../../lib/wizard";

export function cx(...c: (string | false | null | undefined)[]) {
  return c.filter(Boolean).join(" ");
}

/* 8.3: every box can fold away (the portals got crowded). The open / closed state is remembered per box in this
 * browser (localStorage - best effort, wrapped in try/catch), and "Collapse all / Expand all" (CardsToolbar) folds
 * every box on the page at once.
 * 8.5.1: every box starts COLLAPSED (title only) in both portals until it's opened; the storage key changed so boxes
 * opened under an older version start collapsed once too. */
const FOLD_EVENT = "understudy:fold-all";
/** Mission Control (admin): boxes start COLLAPSED (8.5.1). */
export const FOLD_DEFAULT_OPEN = false;
/** 9.2: the Studio (wizard users) starts with every box OPEN - users kept missing what was inside folded boxes. The
 * Studio uses its own storage key, so boxes folded under 8.5-9.1 open again once. */
const STUDIO_PATH = "/wizard";
const inStudio = () => typeof window !== "undefined" && window.location.pathname.startsWith(STUDIO_PATH);
const foldPrefix = () => (inStudio() ? "understudy.fold3." : "understudy.fold2.");
/** The default of a box that doesn't set one: open in the Studio, collapsed in Mission Control. */
const portalDefault = () => inStudio();

function foldKey(explicit: string | undefined, title: React.ReactNode): string | null {
  if (explicit) return explicit;
  if (typeof title === "string") return title;
  const text = React.Children.toArray(title).filter((c) => typeof c === "string" || typeof c === "number").join("");
  return text || null;
}

function readFold(key: string | null, fallback: boolean): boolean {
  if (!key || typeof window === "undefined") return fallback;
  try {
    const v = window.localStorage.getItem(foldPrefix() + window.location.pathname + ":" + key);
    return v === null ? fallback : v === "1";
  } catch { return fallback; }
}

function writeFold(key: string | null, open: boolean) {
  if (!key) return;
  try { window.localStorage.setItem(foldPrefix() + window.location.pathname + ":" + key, open ? "1" : "0"); } catch { /* storage off */ }
}

/** Open / closed state of a foldable box: remembered, and follows "Collapse all / Expand all". */
export function useFold(key: string | null, defaultOpen?: boolean, enabled = true): [boolean, (v: boolean) => void] {
  // undefined = the portal's default (Studio: open, Mission Control: collapsed), resolved after hydration
  const [open, setOpenState] = useState(defaultOpen ?? false);
  React.useEffect(() => {
    if (!enabled) return;
    const t = setTimeout(() => setOpenState(readFold(key, defaultOpen ?? portalDefault())), 0);     // after hydration
    const onAll = (e: Event) => {
      const v = (e as CustomEvent<string>).detail === "expand";
      setOpenState(v);
      writeFold(key, v);
    };
    window.addEventListener(FOLD_EVENT, onAll);
    return () => { clearTimeout(t); window.removeEventListener(FOLD_EVENT, onAll); };
  }, [key, defaultOpen, enabled]);
  const setOpen = React.useCallback((v: boolean) => { setOpenState(v); writeFold(key, v); }, [key]);
  return [open, setOpen];
}

/** "Collapse all" / "Expand all" for every foldable box on the page. */
export function CardsToolbar({ className }: { className?: string }) {
  const fire = (what: "collapse" | "expand") => window.dispatchEvent(new CustomEvent(FOLD_EVENT, { detail: what }));
  return (
    <div className={cx("inline-flex items-center gap-1 text-[11px]", className)} data-testid="cards-toolbar">
      <button type="button" onClick={() => fire("collapse")} title="Fold every box on this page"
        className="inline-flex items-center gap-1 rounded-md border border-slate-700 bg-slate-800/70 px-2 py-1 text-slate-300 hover:text-white">
        <ChevronRight className="h-3 w-3" />Collapse all</button>
      <button type="button" onClick={() => fire("expand")} title="Open every box on this page"
        className="inline-flex items-center gap-1 rounded-md border border-slate-700 bg-slate-800/70 px-2 py-1 text-slate-300 hover:text-white">
        <ChevronDown className="h-3 w-3" />Expand all</button>
    </div>
  );
}

export function Card({ title, icon, children, right, className, collapsible = true, defaultOpen, foldId, summary }: {
  title?: React.ReactNode; icon?: React.ReactNode; children: React.ReactNode; right?: React.ReactNode; className?: string;
  /** 8.3: every titled box folds by default; pass collapsible={false} for one that must always stay open. */
  collapsible?: boolean; defaultOpen?: boolean;
  /** stable key for remembering the state when the title isn't plain text */
  foldId?: string;
  /** shown next to the title while the box is folded (e.g. "3 open") */
  summary?: React.ReactNode;
}) {
  const canFold = collapsible && !!title;
  const [open, setOpen] = useFold(foldId || foldKey(undefined, title), defaultOpen, canFold);
  const shown = !canFold || open;
  const bodyId = React.useId();
  return (
    <section className={cx("rounded-xl border border-slate-800 bg-slate-900/70 shadow-lg", shown ? "p-5" : "px-5 py-3", className)}
      data-folded={shown ? undefined : "true"}>
      {(title || right) && (
        <div className={cx("flex items-start justify-between gap-3", shown && "mb-4")}>
          {canFold ? (
            <h3 className="min-w-0 text-sm font-semibold text-white">
              <button type="button" aria-expanded={open} aria-controls={bodyId} onClick={() => setOpen(!open)}
                title={open ? "Fold this box" : "Open this box"}
                className="flex items-center gap-2 rounded text-left hover:text-sky-200">
                {open ? <ChevronDown className="h-4 w-4 shrink-0 text-slate-400" aria-hidden /> : <ChevronRight className="h-4 w-4 shrink-0 text-slate-400" aria-hidden />}
                {icon}{title}
                {!open && summary ? <span className="ml-1 text-[11px] font-normal text-slate-400">{summary}</span> : null}
              </button>
            </h3>
          ) : (
            <h3 className="flex items-center gap-2 text-sm font-semibold text-white">{icon}{title}</h3>
          )}
          {right}
        </div>
      )}
      <div id={bodyId} hidden={!shown}>{children}</div>
    </section>
  );
}

/** 8.3: a foldable sub-box inside a card (e.g. the AI install assistant, one request, the Studio side panels). */
export function Fold({ title, icon, children, defaultOpen, foldId, className, right, tone = "slate" }: {
  title: React.ReactNode; icon?: React.ReactNode; children: React.ReactNode; defaultOpen?: boolean; foldId?: string;
  className?: string; right?: React.ReactNode; tone?: "slate" | "violet" | "rose" | "amber" | "sky";
}) {
  const [open, setOpen] = useFold(foldId || foldKey(undefined, title), defaultOpen);
  const bodyId = React.useId();
  const t = { slate: "border-slate-800 bg-slate-900/60", violet: "border-violet-500/40 bg-violet-500/5",
    rose: "border-rose-900/60 bg-rose-950/20", amber: "border-amber-500/40 bg-amber-500/5", sky: "border-sky-500/40 bg-sky-500/5" }[tone];
  return (
    <div className={cx("rounded-xl border", t, open ? "p-4" : "px-4 py-2.5", className)} data-folded={open ? undefined : "true"}>
      <div className={cx("flex items-center gap-2", open && "mb-2")}>
        <button type="button" aria-expanded={open} aria-controls={bodyId} onClick={() => setOpen(!open)}
          className="flex min-w-0 flex-1 items-center gap-2 text-left text-xs font-semibold text-slate-100 hover:text-sky-200">
          {open ? <ChevronDown className="h-3.5 w-3.5 shrink-0 text-slate-400" aria-hidden /> : <ChevronRight className="h-3.5 w-3.5 shrink-0 text-slate-400" aria-hidden />}
          {icon}<span className="min-w-0 truncate">{title}</span>
        </button>
        {right}
      </div>
      <div id={bodyId} hidden={!open}>{children}</div>
    </div>
  );
}

export function Button({ children, onClick, disabled, variant = "primary", busy, type = "button", className, title,
  "aria-label": ariaLabel, "data-testid": testId }: {
  children: React.ReactNode; onClick?: () => void; disabled?: boolean; busy?: boolean; title?: string;
  variant?: "primary" | "secondary" | "danger" | "ghost" | "success"; type?: "button" | "submit"; className?: string;
  "aria-label"?: string; "data-testid"?: string;
}) {
  const styles = {
    primary: "bg-sky-500 hover:bg-sky-400 text-slate-950",
    success: "bg-emerald-500 hover:bg-emerald-400 text-slate-950",
    secondary: "bg-slate-800 hover:bg-slate-700 text-slate-100 border border-slate-700",
    danger: "bg-rose-600 hover:bg-rose-500 text-white",
    ghost: "text-slate-300 hover:text-white hover:bg-slate-800",
  }[variant];
  return (
    <button type={type} title={title} onClick={onClick} disabled={disabled || busy} aria-label={ariaLabel} data-testid={testId}
      className={cx("inline-flex items-center justify-center gap-2 rounded-lg px-4 py-2 text-sm font-semibold transition-colors",
        "disabled:cursor-not-allowed disabled:opacity-50", styles, className)}>
      {busy && <Loader2 className="h-4 w-4 animate-spin" />}
      {children}
    </button>
  );
}

export function Badge({ children, tone = "slate", title }: { children: React.ReactNode; tone?: "slate" | "green" | "amber" | "red" | "sky" | "violet"; title?: string }) {
  const t = {
    slate: "bg-slate-800 text-slate-300 border-slate-700",
    green: "bg-emerald-500/10 text-emerald-300 border-emerald-500/30",
    amber: "bg-amber-500/10 text-amber-300 border-amber-500/30",
    red: "bg-rose-500/10 text-rose-300 border-rose-500/30",
    sky: "bg-sky-500/10 text-sky-300 border-sky-500/30",
    violet: "bg-violet-500/10 text-violet-300 border-violet-500/30",
  }[tone];
  return <span title={title} className={cx("inline-flex items-center gap-1 rounded-md border px-2 py-0.5 text-[11px] font-medium", t)}>{children}</span>;
}

export function SourceBadge({ source, model }: { source?: AgentSource | string; model?: string | null }) {
  if (source === "ACTUAL_AI_API") {
    return <Badge tone="green" title={model || undefined}><Bot className="h-3 w-3" />AI{model ? ` · ${model}` : ""}</Badge>;
  }
  if (source === "LEARNED_SKILL") {
    return <Badge tone="violet" title={(model || "") + " - re-used offline from an earlier AI session"}><BookOpen className="h-3 w-3" />Learned skill</Badge>;
  }
  if (source === "USER") return <Badge tone="sky">Your choice</Badge>;
  return <Badge tone="amber" title="Deterministic fallback - no AI call was made"><Cpu className="h-3 w-3" />Rule-based{model ? ` · ${model}` : ""}</Badge>;
}

export function Help({ children }: { children: React.ReactNode }) {
  // A disclosure: open on first sight, one click to fold away once the user knows what the step does.
  return (
    <details open className="group rounded-lg border border-sky-900/60 bg-sky-950/30 p-3 text-xs leading-relaxed text-sky-100/90">
      <summary className="flex cursor-pointer list-none items-center gap-2 font-semibold text-sky-200">
        <HelpCircle className="h-4 w-4 shrink-0 text-sky-400" aria-hidden />What happens in this step
        <ChevronDown className="ml-auto h-4 w-4 text-sky-300 transition-transform group-open:rotate-180" aria-hidden />
      </summary>
      <div className="mt-2 pl-6">{children}</div>
    </details>
  );
}

export function Alert({ tone, title, children, icon }: { tone: "red" | "amber" | "green" | "sky"; title?: React.ReactNode; children?: React.ReactNode; icon?: React.ReactNode }) {
  const t = {
    red: "border-rose-500/40 bg-rose-500/10 text-rose-100",
    amber: "border-amber-500/40 bg-amber-500/10 text-amber-100",
    green: "border-emerald-500/40 bg-emerald-500/10 text-emerald-100",
    sky: "border-sky-500/40 bg-sky-500/10 text-sky-100",
  }[tone];
  return (
    <div role={tone === "red" ? "alert" : "status"} className={cx("rounded-lg border p-3 text-sm", t)}>
      {title && <div className="mb-1 flex items-center gap-2 font-semibold">{icon}{title}</div>}
      {children && <div className="text-[13px] leading-relaxed opacity-90">{children}</div>}
    </div>
  );
}

export function Field({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <label className="block space-y-1.5">
      <span className="text-xs font-semibold uppercase tracking-wide text-slate-300">{label}</span>
      {children}
      {hint && <span className="block text-[11px] text-slate-400">{hint}</span>}
    </label>
  );
}

export const inputCls =
  "w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 placeholder-slate-400 focus:border-sky-500 focus:outline-none focus:ring-1 focus:ring-sky-500";

/** Explains an answer that was re-used offline from what Understudy learned while AI was available. */
export function RecallNote({ recall, what }: { recall?: RecallInfo | null; what: string }) {
  if (!recall) return null;
  const same = recall.exact || recall.detail?.same_recording === true;
  return (
    <div className="rounded-lg border border-violet-500/40 bg-violet-500/10 p-3 text-[13px] leading-relaxed text-violet-100" data-testid="recall-note">
      <div className="mb-1 flex flex-wrap items-center gap-2 font-semibold">
        <BookOpen className="h-4 w-4 text-violet-300" />Answered offline from a learned skill
        {recall.detail?.same_recording === false
          ? <Badge tone="violet">new recording · page &amp; SOP {Math.round(recall.score * 100)}% alike</Badge>
          : <Badge tone="violet">{same ? "same problem" : `${Math.round(recall.score * 100)}% match`}</Badge>}
      </div>
      <p className="opacity-90">
        No AI right now{recall.why_no_ai ? ` (${recall.why_no_ai.replace(/\.$/, "")})` : ""}. Instead of guessing, Understudy re-used {what} it
        learned on {String(recall.learned_at).slice(0, 10)} with <b>{recall.learned_with || "the AI"}</b>: <i>{recall.label}</i>.
        {recall.detail?.same_recording === false
          ? " This recording itself could not be watched without AI, so its values come from your SOP and the page - check them in Step 2."
          : same ? "" : " Only the parts that fit this session were kept, so please check them."}
      </p>
      <details className="mt-2 text-[12px]">
        <summary className="cursor-pointer font-semibold text-violet-200">Why did this match?</summary>
        <ul className="mt-1 list-disc space-y-0.5 pl-5 text-violet-100/90">
          {typeof recall.detail?.form_match === "number" && <li>Form fields {Math.round(recall.detail.form_match * 100)}% alike</li>}
          {typeof recall.detail?.sop_match === "number" && <li>SOP wording {Math.round(recall.detail.sop_match * 100)}% alike</li>}
          {typeof recall.detail?.step_match === "number" && <li>Step sequence {Math.round(recall.detail.step_match * 100)}% alike</li>}
          {typeof recall.detail?.key_match === "number" && <li>Parameter names {Math.round(recall.detail.key_match * 100)}% alike</li>}
          {Array.isArray(recall.detail?.soft_terms) && (recall.detail.soft_terms as string[][]).length > 0 && (
            <li>Treated as the same thing: {(recall.detail.soft_terms as string[][]).map((p) => `${p[0]} ≈ ${p[1]}`).join(", ")}</li>)}
          <li>The safety gates (same site, same SOP sentences and numbers) passed before any of this was scored.</li>
        </ul>
      </details>
    </div>
  );
}

/** 8.3: a password field with an eye button to show / hide what was typed. */
export function PasswordInput({ className, ...props }: Omit<React.InputHTMLAttributes<HTMLInputElement>, "type">) {
  const [show, setShow] = useState(false);
  return (
    <span className="relative block">
      <input {...props} type={show ? "text" : "password"} className={cx(className, "pr-10")} />
      <button type="button" onClick={() => setShow(!show)} aria-label={show ? "Hide password" : "Show password"}
        aria-pressed={show} title={show ? "Hide password" : "Show password"} tabIndex={0}
        className="absolute inset-y-0 right-0 flex items-center px-3 text-slate-400 hover:text-white">
        {show ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
      </button>
    </span>
  );
}
