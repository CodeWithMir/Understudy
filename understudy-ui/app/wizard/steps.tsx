"use client";

import React, { useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle, ArrowRight, Gauge, BookOpen, CheckCircle2, MessageSquare, Square, Code2, Download, FileText, FileVideo, Globe, Hourglass, Info, ListChecks, Lock,
  Package, Play, Plus, RotateCw, Send, ShieldCheck, Sparkles, Trash2, Upload, Wand2, Wrench, XCircle,
} from "lucide-react";
import {
  SAMPLE_SOP, WizardApiError, wizardApi, type BuildAgentTrace, type Complexity, type Constraints, type FrameworkOption,
  type PageEntry, type Parameter, type Rule, type UserVersions, type WizardSession,
} from "../../lib/wizard";
import { Alert, Badge, Button, Card, Field, Help, RecallNote, SourceBadge, cx, inputCls } from "./ui";
import { HistoryPicker, HistoryUsed } from "./studio";
import { AgentAiNote, DifficultyCard } from "./difficulty";
import type { HistoryRef } from "../../lib/studio";

export type Act = (fn: () => Promise<WizardSession>, key?: string) => Promise<WizardSession | null>;

export interface ActionError { message: string; details?: unknown }

interface StepProps {
  session: WizardSession;
  act: Act;
  busyKey: string | null;
  goTo: (step: number) => void;
  error: ActionError | null;
}

const jobRunning = (s: WizardSession, name?: string) => s.job?.status === "running" && (!name || s.job.name === name);

/* ============================================================================================
 * STEP 1 - WATCH
 * ========================================================================================== */
const MAX_URLS = 10;

export function WatchStart({ session: s, act, busyKey }: { session: WizardSession; act: Act; busyKey: string | null }) {
  const [refs, setRefs] = useState<HistoryRef[]>([]);
  // 9.4: every page the automation visits, in order - the first one is where it starts
  const [urls, setUrls] = useState<string[]>([""]);
  const [sopText, setSopText] = useState("");
  const [media, setMedia] = useState<File | null>(null);
  const [useSample, setUseSample] = useState(false);
  const [drag, setDrag] = useState(false);
  const [uploadPct, setUploadPct] = useState<number | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const [targets, setTargets] = useState<{ url: string; note: string }[]>([]);
  useEffect(() => { wizardApi.targets().then((t) => setTargets(t.targets || [])).catch(() => setTargets([])); }, []);
  // 9.9: the real limit of this deployment (Cloud Run without Cloud Storage: 31 MB; otherwise 250 MB)
  const [limit, setLimit] = useState<{ mb: number; why: string } | null>(null);
  useEffect(() => {
    wizardApi.info().then((i) => setLimit(i?.max_media_mb ? { mb: i.max_media_mb, why: i.max_media_why || "" } : null)).catch(() => setLimit(null));
  }, []);
  const [tooBig, setTooBig] = useState<string | null>(null);
  const pick = (f: File | null) => {
    if (f && limit && f.size > limit.mb * 1024 * 1024) {
      setTooBig(`${f.name} is ${(f.size / 1048576).toFixed(0)} MB - the limit here is ${limit.mb} MB${limit.why ? ` (${limit.why})` : ""}. `
        + "Upload a shorter recording, or lower its resolution (720p and 10 frames per second are plenty for the AI).");
      setMedia(null);
      return;
    }
    setTooBig(null);
    setMedia(f);
    if (f) setUseSample(false);
  };

  const setUrl = (i: number, v: string) => setUrls((u) => u.map((x, j) => (j === i ? v : x)));
  const addUrl = (v = "") => setUrls((u) => (u.length >= MAX_URLS ? u : [...u, v]));
  const removeUrl = (i: number) => setUrls((u) => (u.length <= 1 ? [""] : u.filter((_, j) => j !== i)));
  const pickApproved = (v: string) => setUrls((u) => {
    const empty = u.findIndex((x) => !x.trim());
    if (empty >= 0) return u.map((x, j) => (j === empty ? v : x));
    return u.includes(v) || u.length >= MAX_URLS ? u : [...u, v];
  });
  const cleanUrls = urls.map((u) => u.trim()).filter(Boolean);
  const aiOn = s.ai_mode === "ACTUAL_AI_API";
  // 9.4: with AI the addresses are read from the recording - typing them is optional. 9.9: without AI too, when this
  // recording was analysed with AI before (the server checks its learned skills and says so if there is none)
  const ready = Boolean(media || useSample);
  return (
    <div className="space-y-5">
      <Help>
        <b>Step 1 - show the AI what you do.</b> Upload a screen recording of yourself doing the task once (voice narration helps),
        and describe any business rules in plain English. The AI reads the addresses of the pages you visit from the recording - you check
        them afterwards (and can type any it can&apos;t see). Business rules are things like
        which values are allowed, limits, or valid dates. The AI watches the video, opens the page to find its real fields and
        buttons, and proposes how to automate it. Optionally add what earlier sessions learned (section 4).
      </Help>
      <div className="grid gap-5 lg:grid-cols-2">
        <Card title="1. Recording of the process" icon={<FileVideo className="h-4 w-4 text-sky-400" />}>
          <div
            onDragOver={(e) => { e.preventDefault(); setDrag(true); }}
            onDragLeave={() => setDrag(false)}
            onDrop={(e) => { e.preventDefault(); setDrag(false); const f = e.dataTransfer.files?.[0]; if (f) pick(f); }}
            onClick={() => fileRef.current?.click()}
            className={cx("flex cursor-pointer flex-col items-center justify-center gap-2 rounded-lg border-2 border-dashed p-6 text-center transition-colors",
              drag ? "border-sky-400 bg-sky-500/10" : "border-slate-700 hover:border-slate-500", useSample && "opacity-40")}>
            <Upload className="h-7 w-7 text-slate-400" />
            <div className="text-sm text-slate-200">{media ? media.name : "Drop a video/audio file here or click to browse"}</div>
            <div className="text-[11px] text-slate-400" data-testid="upload-limit"
              title={limit?.why || undefined}>MP4, WebM, MOV, MKV, AVI, MP3, WAV, M4A · up to {limit ? limit.mb : "…"} MB · checked by Model Armor</div>
            <input ref={fileRef} type="file" accept="video/*,audio/*" className="hidden"
              onChange={(e) => { pick(e.target.files?.[0] || null); e.target.value = ""; }} />
          </div>
          {tooBig && <p className="mt-2 text-xs text-rose-300" role="alert" data-testid="upload-too-big">{tooBig}</p>}
          <label className="mt-3 flex items-center gap-2 text-xs text-slate-300">
            <input type="checkbox" checked={useSample} onChange={(e) => setUseSample(e.target.checked)} className="accent-sky-500" />
            No recording yet? Use the sample invoice-entry video.
          </label>
        </Card>
        <Card title="2. Page addresses - optional" icon={<Globe className="h-4 w-4 text-sky-400" />}>
          <Field label={aiOn ? "The AI reads them from your recording" : "Where the process starts (no AI right now)"}
            hint={aiOn ? "Leave this empty and the AI lists every page it sees in the recording (address bar, typed links, narration). Type an address only if it is not visible in the recording - you check the full list after the analysis. Internal or private addresses only work if your admin has added them in Mission Control."
              : "AI is not available, so a new recording can't be read: type where the process starts (page 1) and the other pages it opens, in order. You can leave it empty only for a recording that was analysed with AI before - Understudy then uses the page addresses it learned. Internal or private addresses only work if your admin has added them in Mission Control."}>
            <div className="space-y-2" data-testid="target-urls">
              {urls.map((u, i) => (
                <div key={i} className="flex items-center gap-2">
                  <span className="w-16 shrink-0 text-[11px] text-slate-400">{i === 0 ? "Start page" : `Page ${i + 1}`}</span>
                  <input className={inputCls} value={u} onChange={(e) => setUrl(i, e.target.value)} aria-label={i === 0 ? "Start page URL" : `Page ${i + 1} URL`}
                    placeholder={i === 0 ? "https://any-public-site.example.com/form" : "https://any-public-site.example.com/next-page"} />
                  {urls.length > 1 && (
                    <button type="button" onClick={() => removeUrl(i)} title="Remove this page" aria-label={`Remove page ${i + 1}`}
                      className="rounded p-1.5 text-slate-400 hover:bg-slate-800 hover:text-rose-300"><Trash2 className="h-3.5 w-3.5" /></button>
                  )}
                </div>
              ))}
            </div>
          </Field>
          <div className="mt-2 flex items-center justify-between">
            <Button variant="ghost" disabled={urls.length >= MAX_URLS} onClick={() => addUrl()}>
              <Plus className="h-3.5 w-3.5" /> Add another page
            </Button>
            {urls.length >= MAX_URLS && <span className="text-[11px] text-slate-500">Up to {MAX_URLS} pages</span>}
          </div>
          {targets.length > 0 && (
            <div className="mt-3 space-y-1.5">
              <div className="text-[11px] uppercase tracking-wide text-slate-400">Approved by your admin</div>
              <div className="flex flex-wrap gap-2">
                {targets.map((t) => (
                  <Button key={t.url} variant="secondary" title={t.url} onClick={() => pickApproved(t.url)}>{t.note || t.url}</Button>
                ))}
              </div>
            </div>
          )}
        </Card>
      </div>
      <Card title="3. Rules & values (SOP) - optional" icon={<FileText className="h-4 w-4 text-sky-400" />}
        right={<Button variant="ghost" onClick={() => setSopText(SAMPLE_SOP)}>Load example</Button>}>
        <textarea className={cx(inputCls, "h-36 font-mono text-xs")} value={sopText} onChange={(e) => setSopText(e.target.value)}
          placeholder={"Write constraints in plain English, e.g.\nAllowed countries: Germany, France.\nThe quantity must not exceed 50.\nDates must be between 2026-01-01 and 2026-12-31."} />
        <p className="mt-2 text-[11px] text-slate-400">No separate SOP portal is needed - these rules become the checks in Step 2 (Govern) and part of the generated script.</p>
      </Card>
      <Card title="4. Context of previous sessions - optional" icon={<BookOpen className="h-4 w-4 text-violet-300" />}>
        <p className="mb-3 text-xs text-slate-400">
          Add sessions you ran before (search by name), or a whole project to add all of its sessions. The AI gets a summary of
          each - the steps and selectors that worked, parameter names, how success was verified, and what self-healing had to fix -
          and builds this run&apos;s strategy on it. No values from earlier runs are passed on.
        </p>
        <HistoryPicker value={refs} onChange={setRefs} excludeSession={s.session_id} />
      </Card>
      <div className="flex items-center justify-end gap-3">
        {uploadPct !== null && busyKey === "create" && (
          <span className="text-xs text-slate-400" role="status" aria-live="polite">
            {uploadPct < 100 ? `Uploading the recording to Cloud Storage... ${uploadPct}%` : "Recording uploaded - starting the analysis..."}</span>
        )}
        <Button disabled={!ready} busy={busyKey === "create"}
          onClick={() => act(async () => {
            setUploadPct(null);
            try { return await wizardApi.start(s.session_id, { targetUrls: cleanUrls, sopText, media, useSample, refs }, setUploadPct); }
            finally { setUploadPct(null); }
          }, "create")}>
          <Sparkles className="h-4 w-4" /> Analyse with AI
        </Button>
      </div>
    </div>
  );
}

/** "mm:ss" or "h:mm:ss" -> seconds */
function toSeconds(t: string): number {
  return t.split(":").reduce((acc, part) => acc * 60 + Number(part || 0), 0);
}

/** The step plan, moment by moment: each step the AI grounded in the recording shows its time; clicking it plays
 *  the recording from that moment so the user can check what the AI saw. */
function StepPlan({ s }: { s: WizardSession }) {
  const a = s.analysis!;
  const video = useRef<HTMLVideoElement>(null);
  const timed = a.steps.some((st) => st.video_time);
  const playable = timed && s.media?.kind === "video";
  const seek = (t?: string) => {
    if (!t || !video.current) return;
    video.current.currentTime = toSeconds(t);
    void video.current.play().catch(() => null);
    video.current.scrollIntoView({ block: "nearest", behavior: "smooth" });
  };
  return (
    <div className={cx("mt-4", playable && "grid gap-4 lg:grid-cols-5")}>
      <ol className={cx("space-y-1.5", playable && "lg:col-span-3")} aria-label="Step plan">
        {a.steps.map((st) => (
          <li key={st.order} className="flex items-start gap-3 text-sm">
            <span className="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-slate-800 text-[11px] text-slate-300" aria-hidden>{st.order}</span>
            <span className="min-w-0 flex-1">
              <span className="text-slate-200">{st.description}</span>
              {st.evidence && <span className="block text-[11px] text-slate-400">Seen in the video: {st.evidence}</span>}
            </span>
            {st.video_time && (playable ? (
              <button type="button" onClick={() => seek(st.video_time)}
                className="shrink-0 rounded bg-sky-500/10 px-1.5 py-0.5 font-mono text-[11px] text-sky-300 hover:bg-sky-500/20 focus:outline-none focus-visible:ring-2 focus-visible:ring-sky-400"
                aria-label={`Play the recording at ${st.video_time}`} title="Play the recording from this moment">
                <Play className="mr-0.5 inline h-2.5 w-2.5" aria-hidden />{st.video_time}
              </button>
            ) : <span className="shrink-0 rounded bg-slate-800 px-1.5 py-0.5 font-mono text-[11px] text-slate-400" title="Moment in the recording">{st.video_time}</span>)}
            {st.selector && <code title={st.selector} className="hidden max-w-[45%] truncate rounded bg-slate-950 px-1.5 py-0.5 text-[11px] text-slate-400 sm:inline">{st.selector}</code>}
            {st.grounded === true && <span className="shrink-0" title="This element was found on the target page"><Badge tone="green">on page</Badge></span>}
            {st.grounded === false && <span className="shrink-0" title="Not on the start page - it is on a later page or appears after an earlier step. The runner falls back to its label; self-healing re-reads the page if needed."><Badge tone="amber">later page</Badge></span>}
          </li>
        ))}
      </ol>
      {playable && (
        <div className="lg:col-span-2">
          <video ref={video} src={wizardApi.mediaUrl(s.session_id)} controls preload="metadata"
            className="w-full rounded-lg border border-slate-800 bg-black" aria-label="Your recording" />
          <p className="mt-1 text-[11px] text-slate-400">Click a time next to a step to see that moment.</p>
        </div>
      )}
    </div>
  );
}

/** How the AI's steps were checked against the real page (selectors, payload schema). */
function GroundingNote({ s }: { s: WizardSession }) {
  const g = s.analysis?.grounding;
  if (!g) return null;
  if (g.skipped) return <p className="mt-3 text-[11px] text-amber-300">Selectors could not be checked against the page: {g.skipped}</p>;
  return (
    <details className="mt-3 rounded-lg border border-slate-800 bg-slate-950/50 p-2.5 text-xs text-slate-300" data-testid="grounding">
      <summary className="cursor-pointer font-semibold text-slate-200">
        Checked against the live page: {g.on_page}/{g.checked} element{g.checked === 1 ? "" : "s"} found
        {g.repaired.length > 0 && `, ${g.repaired.length} selector${g.repaired.length === 1 ? "" : "s"} corrected`}
        {g.constraints > 0 && `, ${g.constraints} field rule${g.constraints === 1 ? "" : "s"} read from the page`}
        <span className="ml-1 font-normal text-slate-400">({g.renderer === "chromium" ? "rendered in a browser" : "page HTML"})</span>
      </summary>
      <ul className="mt-2 list-disc space-y-0.5 pl-5">
        {g.repaired.map((r) => <li key={"r" + r.step}>Step {r.step}: <code>{r.from || "(none)"}</code> → <code>{r.to}</code> - {r.why}</li>)}
        {g.not_found.map((r) => <li key={"n" + r.step} className="text-amber-200">Step {r.step}: <code>{r.selector || "(no selector)"}</code> {r.why}</li>)}
        {g.type_fixes.map((t, i) => <li key={"t" + i}>Type: {t}</li>)}
        {g.value_fixes.map((t, i) => <li key={"v" + i}>Value: {t}</li>)}
      </ul>
    </details>
  );
}

/** The page's own rules for a field, in words. */
export function constraintText(c?: Constraints): string {
  if (!c) return "";
  const parts: string[] = [];
  if (c.required) parts.push("required");
  if (c.options?.length) parts.push(`one of: ${c.options.slice(0, 6).join(", ")}${c.options.length > 6 ? " ..." : ""}`);
  if (c.min || c.max) parts.push(`range ${c.min ?? "…"} to ${c.max ?? "…"}`);
  if (c.minlength) parts.push(`min ${c.minlength} chars`);
  if (c.maxlength) parts.push(`max ${c.maxlength} chars`);
  if (c.pattern) parts.push("format checked by the page");
  return parts.filter(Boolean).join(" · ");
}

function DifficultyBadge({ d }: { d: string }) {
  return <Badge tone={d === "beginner" ? "green" : d === "intermediate" ? "amber" : "red"}>{d}</Badge>;
}

const OPEN_REQUEST = ["pending", "planning", "plan_ready", "installing", "smoke_testing", "awaiting_admin", "setup_failed"];

type ToolFit = Complexity["tool_fit"][number];

function FrameworkCard({ f, selected, onPick, disabled, fit }: { f: FrameworkOption; selected: boolean; onPick: () => void; disabled: boolean; fit?: ToolFit }) {
  const locked = f.allowed === false;
  const req = f.access_request;
  const blocked = f.blocked;
  const restricted = !!f.restricted;
  return (
    <button type="button" disabled={disabled || !!blocked || restricted} onClick={onPick} data-testid={`tool-${f.id}`}
      className={cx("relative flex h-full flex-col rounded-xl border p-4 text-left transition-all disabled:opacity-60",
        selected ? "border-sky-400 bg-sky-500/10 ring-1 ring-sky-400" : "border-slate-800 bg-slate-950/60 hover:border-slate-600",
        locked && "opacity-75")}>
      {f.recommended && <span className="absolute -top-2 right-3 rounded-full bg-sky-500 px-2 py-0.5 text-[10px] font-bold text-slate-950">RECOMMENDED</span>}
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-1.5 font-semibold text-white">{locked && <Lock className="h-3.5 w-3.5 text-amber-400" />}{f.name}</div>
        <DifficultyBadge d={f.difficulty} />
      </div>
      <div className="mt-1 text-[11px] text-slate-400">{f.language} · {f.category} · {f.ecosystem}</div>
      <p className="mt-2 flex-1 text-xs leading-relaxed text-slate-300">{f.beginner_summary}</p>
      <div className="mt-3 space-y-1 text-[11px]">
        {f.pros.slice(0, 2).map((p) => <div key={p} className="text-emerald-300">+ {p}</div>)}
        {f.cons.slice(0, 1).map((c) => <div key={c} className="text-amber-300">- {c}</div>)}
      </div>
      {fit && (
        <p className="mt-2 text-[11px] text-slate-300" data-testid={`tool-fit-${f.id}`}>
          <Badge tone={fit.fit === "good" ? "green" : fit.fit === "ok" ? "sky" : "amber"}>fit for this automation: {fit.fit}</Badge>
          {fit.why ? <span className="ml-1 text-slate-400">{fit.why}</span> : null}</p>
      )}
      <div className="mt-3 flex flex-wrap gap-1">
        {!locked && <Badge tone={f.builtin ? "violet" : "green"}>{f.builtin ? "built into Understudy" : "approved by admin"}</Badge>}
        {blocked && <Badge tone="red">blocked by your admin</Badge>}
        {restricted && <Badge tone="slate">{f.access_mode === "none" ? "turned off by your admin" : "available to selected users only"}</Badge>}
        {locked && !blocked && !restricted && !req && <Badge tone="amber">needs admin approval - click to request</Badge>}
        {locked && !blocked && req && OPEN_REQUEST.includes(req.status) && <Badge tone="amber">{["installing", "smoke_testing", "awaiting_admin"].includes(req.status) ? "admin is setting it up" : "request with your admin"} ({req.id})</Badge>}
        {locked && req?.status === "rejected" && <Badge tone="red" title={req.admin_note}>request rejected</Badge>}
      </div>
      {locked && req?.status === "rejected" && req.admin_note && <p className="mt-1 text-[11px] text-rose-300">Admin: {req.admin_note}</p>}
      {blocked && <p className="mt-1 text-[11px] text-rose-300">{blocked.note ? `Admin: ${blocked.note}` : "Your admin doesn't allow this tool."}</p>}
      {restricted && <p className="mt-1 text-[11px] text-slate-400">{f.access_mode === "none"
        ? "Your admin turned this tool off for all users for now - ask your admin if you need it."
        : "Your admin gave access to this tool to selected users only - ask your admin to add you."}</p>}
    </button>
  );
}

function RequestAccessBox({ target, session: s, act, busyKey, onClose }: {
  target: { framework_id?: string; custom_name?: string; name: string }; session: WizardSession; act: Act; busyKey: string | null; onClose: () => void;
}) {
  const [reason, setReason] = useState("");
  const [sent, setSent] = useState(false);
  return (
    <Alert tone="amber" title={`${target.name} needs your admin's approval`} icon={<Lock className="h-4 w-4" />}>
      {sent ? (
        <p>Request sent. This session is now paused until your admin enables the tool in Mission Control (the AI sets it up
          and smoke-tests it first). It continues by itself with the tool selected - this page updates live. To carry on
          now instead, pick an approved tool (that cancels the wait).</p>
      ) : (
        <>
          <p>Only tools your admin has approved can be used. Tell them why you need this one (optional):</p>
          <textarea className={cx(inputCls, "mt-2 h-16 text-xs")} value={reason} onChange={(e) => setReason(e.target.value)}
            placeholder="e.g. Our QA team already writes tests in this tool" />
          <div className="mt-2 flex gap-2">
            <Button busy={busyKey === "request"} onClick={async () => {
              const r = await act(() => wizardApi.requestTool(s.session_id, { framework_id: target.framework_id, custom_name: target.custom_name, reason }), "request");
              if (r) setSent(true);
            }}>Send request to admin</Button>
            <Button variant="ghost" onClick={onClose}>Cancel</Button>
          </div>
        </>
      )}
    </Alert>
  );
}

function currentVersion(fw: WizardSession["framework"]): string {
  const v = fw?.packages?.[0]?.version || "";
  return v.replace(/^==/, "");
}

function VersionPicker({ session: s, act, busyKey }: { session: WizardSession; act: Act; busyKey: string | null }) {
  const fw = s.framework!;
  const opt = s.framework_options.find((o) => o.id === fw.id);
  const pol = opt?.versions;
  const cur = currentVersion(fw);
  const pkg = fw.packages?.[0]?.name;
  const restricted = !!pol && pol.allowed.length > 0;
  const [info, setInfo] = useState<UserVersions | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    if (restricted || !pkg) return;
    let alive = true;
    wizardApi.frameworkVersions(fw.id).then((v) => { if (alive) setInfo(v); }).catch((e) => { if (alive) setErr((e as Error).message); });
    return () => { alive = false; };
  }, [fw.id, restricted, pkg]);
  if (!pkg) return null;
  const pick = (v: string) => { void act(() => wizardApi.chooseFramework(s.session_id, { framework_id: fw.id, ...(v ? { version: v } : {}) }), "framework"); };
  const list = restricted ? pol!.allowed : (info?.versions || []);
  const pinned = (fw.packages?.[0]?.version || "").startsWith("==");
  const selected = restricted || pinned ? cur : "";
  return (
    <div className="mt-4 flex flex-wrap items-center gap-2 rounded-lg border border-slate-800 bg-slate-950/50 px-3 py-2 text-xs text-slate-300">
      <Package className="h-3.5 w-3.5 text-sky-400" />
      <span>{fw.name} package <code className="text-slate-200">{pkg}</code></span>
      {restricted && pol!.allowed.length === 1 ? (
        <span className="text-slate-400">version <b className="text-slate-200">{cur}</b> (set by your admin)</span>
      ) : (
        <>
          <span className="text-slate-400">version</span>
          <select aria-label="Tool version" className="rounded border border-slate-700 bg-slate-900 px-2 py-1 text-xs text-slate-100"
            value={selected} disabled={busyKey !== null || (!restricted && !info)} onChange={(e) => pick(e.target.value)}>
            {!restricted && <option value="">Recommended ({info?.constraint || "vetted range"})</option>}
            {list.map((v) => <option key={v} value={v}>{v}{restricted && v === pol!.default ? " (default)" : ""}{!restricted && v === info?.latest ? " (latest)" : ""}</option>)}
            {selected && !list.includes(selected) && <option value={selected}>{selected}</option>}
          </select>
          <span className="text-slate-400">
            {restricted ? "- versions your admin allows" : info ? (info.source === "registry" ? "- any version from the package registry" : "- registry unreachable, showing what's known") : err ? `- ${err}` : "- loading versions..."}
          </span>
        </>
      )}
      {restricted && <VersionRequest session={s} act={act} busyKey={busyKey} toolId={fw.id} requests={opt?.version_requests || []} />}
    </div>
  );
}

/** 7.2: a version the admin hasn't allowed (and hasn't blocked) can be requested; the admin's AI setup handles it. */
function VersionRequest({ session: s, act, busyKey, toolId, requests }: {
  session: WizardSession; act: Act; busyKey: string | null; toolId: string; requests: { id: string; version: string; status: string; admin_note: string }[];
}) {
  const [open, setOpen] = useState(false);
  const [version, setVersion] = useState("");
  const [reason, setReason] = useState("");
  const shown = requests.filter((r) => r.status !== "approved").slice(0, 3);
  return (
    <div className="basis-full">
      {shown.map((r) => (
        <div key={r.id} className={cx("text-[11px]", r.status === "rejected" ? "text-rose-300" : "text-amber-300")}>
          Version {r.version}: {r.status === "rejected" ? `rejected${r.admin_note ? ` - ${r.admin_note}` : ""}` : `requested from your admin (${r.id})`}
        </div>
      ))}
      {!open ? (
        <button type="button" onClick={() => setOpen(true)} className="text-[11px] text-sky-300 hover:underline">Need another version? Ask your admin</button>
      ) : (
        <div className="mt-1 flex flex-wrap items-center gap-2">
          <div className="w-32 shrink-0"><input className={cx(inputCls, "py-1 font-mono text-xs")} value={version} onChange={(e) => setVersion(e.target.value.trim())}
            placeholder="e.g. 1.57.0" aria-label="Version to request" /></div>
          <div className="min-w-0 flex-1"><input className={cx(inputCls, "py-1 text-xs")} value={reason} onChange={(e) => setReason(e.target.value)} maxLength={500}
            placeholder="Why (optional)" aria-label="Why you need this version" /></div>
          <Button variant="secondary" disabled={!version} busy={busyKey === "version-request"}
            onClick={async () => {
              const r = await act(() => wizardApi.requestTool(s.session_id, { framework_id: toolId, version, reason }), "version-request");
              if (r) { setOpen(false); setVersion(""); setReason(""); }
            }}>Request</Button>
        </div>
      )}
    </div>
  );
}

/** 9.4: the pages the process visits - read from the recording by the AI; the user checks, edits, re-orders, and can
 *  ask the AI to watch the recording again. Build waits until the list is checked. */
function PagesCard({ session: s, act, busyKey }: { session: WizardSession; act: Act; busyKey: string | null }) {
  const saved = useMemo<PageEntry[]>(() => (s.pages && s.pages.length ? s.pages
    : (s.target_urls || (s.target_url ? [s.target_url] : [])).map((url) => ({ url, source: "user" as const }))), [s.pages, s.target_urls, s.target_url]);
  const savedKey = saved.map((p) => p.url).join("\n");
  const [rows, setRows] = useState<string[]>(saved.length ? saved.map((p) => p.url) : [""]);
  const [note, setNote] = useState("");
  const [lastKey, setLastKey] = useState(savedKey);
  if (savedKey !== lastKey) { setLastKey(savedKey); setRows(saved.length ? saved.map((p) => p.url) : [""]); }
  const meta = (u: string) => saved.find((p) => p.url === u.trim());
  const clean = rows.map((r) => r.trim()).filter(Boolean);
  const dirty = clean.join("\n") !== savedKey;
  const busy = jobRunning(s, "watch") || jobRunning(s, "pages") || busyKey === "pages" || busyKey === "rewatch" || busyKey === "pages-ok";
  const needsCheck = s.pages_confirmed === false;
  const move = (i: number, d: number) => setRows((r) => {
    const j = i + d;
    if (j < 0 || j >= r.length) return r;
    const n = [...r]; [n[i], n[j]] = [n[j], n[i]]; return n;
  });
  const fromVideo = saved.filter((p) => p.seen_in_video).length;
  const iconBtn = "rounded p-1.5 text-slate-400 hover:bg-slate-800 hover:text-slate-100 disabled:opacity-30";
  return (
    <div data-testid="pages-card">
      <Card title="Pages the automation visits" icon={<Globe className="h-4 w-4 text-sky-400" />}
        right={needsCheck ? <Badge tone="amber">check these</Badge> : saved.length ? <Badge tone="green"><CheckCircle2 className="h-3 w-3" />checked</Badge> : undefined}>
        <p className="mb-3 text-xs text-slate-400">
          {fromVideo ? <>The AI read {fromVideo} address{fromVideo === 1 ? "" : "es"} from your recording{s.pages_meta?.model ? ` (${s.pages_meta.model})` : ""}. </> : null}
          Check them in the order the process visits them - fix an address, add a page it missed, remove one it should not use.
          <b> Save</b> keeps the AI&apos;s understanding of the steps; <b>Watch again</b> lets the AI re-watch the recording with your list
          (and your note). Build starts once the list is checked.
        </p>
        {s.pages_meta?.notes ? <p className="mb-2 text-[11px] text-slate-400">AI note: {s.pages_meta.notes}</p> : null}
        {s.pages_meta?.error ? <p className="mb-2 text-[11px] text-amber-300">The AI could not read the addresses: {s.pages_meta.error}</p> : null}
        <ol className="space-y-2">
          {rows.map((u, i) => {
            const m = meta(u);
            return (
              <li key={i} className="rounded-lg border border-slate-800 bg-slate-950/40 p-2">
                <div className="flex items-center gap-2">
                  <span className="w-14 shrink-0 text-[11px] text-slate-400">{i === 0 ? "Start" : `Page ${i + 1}`}</span>
                  <input className={inputCls} value={u} aria-label={i === 0 ? "Start page URL" : `Page ${i + 1} URL`}
                    onChange={(e) => setRows((r) => r.map((x, j) => (j === i ? e.target.value : x)))} placeholder="https://..." />
                  <button type="button" className={iconBtn} title="Move up" aria-label={`Move page ${i + 1} up`} disabled={i === 0} onClick={() => move(i, -1)}>↑</button>
                  <button type="button" className={iconBtn} title="Move down" aria-label={`Move page ${i + 1} down`} disabled={i === rows.length - 1} onClick={() => move(i, 1)}>↓</button>
                  <button type="button" className={iconBtn} title="Remove" aria-label={`Remove page ${i + 1}`}
                    onClick={() => setRows((r) => (r.length <= 1 ? [""] : r.filter((_, j) => j !== i)))}><Trash2 className="h-3.5 w-3.5" /></button>
                </div>
                {m && (
                  <div className="mt-1 flex flex-wrap items-center gap-2 pl-16 text-[11px] text-slate-400">
                    {m.seen_in_video ? <Badge tone="violet">seen in the recording{m.video_time ? ` at ${m.video_time}` : ""}</Badge>
                      : <Badge tone="slate">added by you</Badge>}
                    {m.source === "video" && needsCheck ? <Badge tone="amber">found by the AI - please check</Badge> : null}
                    {m.purpose ? <span className="text-slate-300">{m.purpose}</span> : null}
                    {m.evidence ? <span>- {m.evidence}</span> : null}
                  </div>
                )}
              </li>
            );
          })}
        </ol>
        <div className="mt-2">
          <Button variant="ghost" disabled={rows.length >= MAX_URLS} onClick={() => setRows((r) => [...r, ""])}><Plus className="h-3.5 w-3.5" /> Add a page</Button>
        </div>
        {(s.pages_rejected?.length ?? 0) > 0 && (
          <div className="mt-2 rounded-lg border border-amber-900/50 bg-amber-950/20 p-2 text-[11px] text-amber-200" data-testid="pages-rejected">
            <b>Also seen in the recording, not used:</b>
            <ul className="mt-1 space-y-0.5">{s.pages_rejected!.map((r, i) => (
              <li key={i}><code className="break-all">{r.url}</code>{r.video_time ? ` (${r.video_time})` : ""} - {r.reason}</li>
            ))}</ul>
          </div>
        )}
        <label className="mt-3 block text-[11px] text-slate-400">Note for the AI when it watches again (optional)
          <textarea className={cx(inputCls, "mt-1 h-16 text-xs")} value={note} onChange={(e) => setNote(e.target.value)}
            placeholder="e.g. After Save the app opens the approvals page; the first page is a sign-in." />
        </label>
        <div className="mt-3 flex flex-wrap gap-2">
          {needsCheck && !dirty && s.analysis && (
            <Button variant="success" disabled={busy || !clean.length} busy={busyKey === "pages-ok"}
              onClick={() => act(() => wizardApi.confirmPages(s.session_id), "pages-ok")}><CheckCircle2 className="h-4 w-4" /> These pages are right</Button>
          )}
          {(dirty || !s.analysis) && (
            <Button variant="secondary" disabled={busy || !clean.length} busy={busyKey === "pages"}
              onClick={() => act(() => wizardApi.setPages(s.session_id, { urls: clean }), "pages")}>Save pages</Button>
          )}
          <Button variant={dirty || !s.analysis ? "primary" : "secondary"} disabled={busy || !clean.length} busy={busyKey === "rewatch"}
            onClick={async () => { const r = await act(() => wizardApi.setPages(s.session_id, { urls: clean, rewatch: true, note }), "rewatch"); if (r) setNote(""); }}>
            <RotateCw className="h-4 w-4" /> Watch again with these pages
          </Button>
          {jobRunning(s, "pages") && <span className="self-center text-xs text-slate-400">Checking the pages...</span>}
        </div>
      </Card>
    </div>
  );
}

function ClarifyCard({ session: s, act, busyKey }: { session: WizardSession; act: Act; busyKey: string | null }) {
  const [text, setText] = useState("");
  const history = s.clarifications || [];
  const busy = jobRunning(s, "clarify") || busyKey === "clarify";
  const send = async () => {
    const r = await act(() => wizardApi.clarify(s.session_id, text), "clarify");
    if (r) setText("");
  };
  return (
    <Card title="Did the AI get it right?" icon={<MessageSquare className="h-4 w-4 text-sky-400" />}
      right={s.understanding_confirmed ? <Badge tone="green"><CheckCircle2 className="h-3 w-3" />confirmed</Badge>
        : history.length ? <Badge tone="sky">{history.length} correction{history.length === 1 ? "" : "s"}</Badge> : undefined}>
      <p className="mb-3 text-xs text-slate-400">
        If something is wrong or missing, explain it in your own words - a step it skipped, a value it read wrongly, how to
        check the result. The AI re-watches your recording with <b>all</b> your explanations so far and updates its
        understanding. Repeat until it&apos;s right. Every explanation passes Model Armor first.
      </p>
      {history.length > 0 && (
        <ol className="mb-3 space-y-2">
          {history.map((c) => (
            <li key={c.round} className="rounded-lg border border-slate-800 bg-slate-950/50 p-2.5 text-xs">
              <div className="flex items-center gap-2 text-slate-400">
                <span>Correction {c.round}</span>
                {c.status === "applied" && <Badge tone="green">applied</Badge>}
                {c.status === "not_applied" && <Badge tone="amber">not applied</Badge>}
                {c.status === "pending" && <Badge tone="sky">re-analysing</Badge>}
                {c.status === "blocked" && <Badge tone="red">blocked</Badge>}
                {c.agent_source && <SourceBadge source={c.agent_source} />}
              </div>
              <p className="mt-1 whitespace-pre-wrap text-slate-200">{c.text}</p>
              {c.notes?.map((n, i) => <p key={i} className="mt-1 text-amber-300">{n}</p>)}
            </li>
          ))}
        </ol>
      )}
      {busy ? (
        <div className="flex items-center gap-2 text-sm text-slate-300"><RotateCw className="h-4 w-4 animate-spin text-sky-400" />Re-analysing with your explanation...</div>
      ) : (
        <>
          <textarea aria-label="Explain what the AI missed" className={cx(inputCls, "h-20 text-sm")} value={text} maxLength={4000}
            onChange={(e) => setText(e.target.value)}
            placeholder="e.g. You missed that I pick the cost centre from the dropdown before saving. The amount is 725.50, not 72.55." />
          <div className="mt-2 flex flex-wrap gap-2">
            <Button disabled={!text.trim()} busy={busyKey === "clarify"} onClick={() => { void send(); }}>
              <Wand2 className="h-4 w-4" />Explain &amp; re-analyse</Button>
            {!s.understanding_confirmed && (
              <Button variant="ghost" busy={busyKey === "confirm"} onClick={() => act(() => wizardApi.confirmWatch(s.session_id), "confirm")}>
                <CheckCircle2 className="h-4 w-4" />Yes, that&apos;s right</Button>
            )}
          </div>
        </>
      )}
    </Card>
  );
}

function AppliedSkills({ session: s }: { session: WizardSession }) {
  const applied = s.applied_skills || [];
  if (!applied.length) return null;
  return (
    <Alert tone="sky" title={`Understudy applied ${applied.length} lesson${applied.length === 1 ? "" : "s"} it learned from earlier errors`} icon={<BookOpen className="h-4 w-4" />}>
      <ul className="mt-1 space-y-1 text-xs">
        {applied.map((a) => (
          <li key={a.skill_id + a.lesson_id}><Badge tone="slate">{a.category}</Badge> <span className="text-slate-300">{a.applied.join(", ")}</span>
            <span className="block text-[11px] text-slate-400">{a.signature}</span></li>
        ))}
      </ul>
    </Alert>
  );
}

// 9.5.3: filter the tool cards by what the user types ("selenium", "playwright js", "cypress 13.6" ...)
const VERSION_TOKEN = /^v?\d+(\.[\dx*]+)*$/i;
function queryTokens(q: string): string[] {
  return q.toLowerCase().split(/[\s,/]+/).map((t) => t.replace(/[^a-z0-9.+#-]/g, "")).filter((t) => t && !VERSION_TOKEN.test(t));
}
function hasVersion(q: string): boolean {
  return q.toLowerCase().split(/[\s,/]+/).some((t) => VERSION_TOKEN.test(t.trim()));
}
export function toolMatches(tools: FrameworkOption[], q: string): FrameworkOption[] | null {
  const tokens = queryTokens(q);
  if (!q.trim()) return null;
  if (!tokens.length) return [];
  const squash = (x: string) => x.toLowerCase().replace(/[^a-z0-9+#]/g, "");
  return tools.filter((f) => {
    const hay = [f.name, f.id, f.language, f.ecosystem, f.category, ...(f.packages || []).map((p) => p.name)].join(" ").toLowerCase();
    const hayS = squash(hay);
    const lang: Record<string, string[]> = { js: ["javascript"], ts: ["typescript"], py: ["python"], cs: ["c#", "csharp", ".net"] };
    return tokens.every((t) => hay.includes(t) || hayS.includes(squash(t)) || (lang[t] || []).some((a) => hay.includes(a)));
  });
}

export function WatchReview({ session: s, act, busyKey, goTo }: StepProps) {
  const [custom, setCustom] = useState("");
  const [requestFor, setRequestFor] = useState<{ framework_id?: string; custom_name?: string; name: string } | null>(null);
  const [confirmFor, setConfirmFor] = useState<FrameworkOption | null>(null);
  const pick = (f: FrameworkOption, override = false) => {
    if (f.blocked || f.restricted) return;
    if (f.allowed === false) {
      if (!OPEN_REQUEST.includes(f.access_request?.status || "")) setRequestFor({ framework_id: f.id, name: f.name });
      return;
    }
    setRequestFor(null);
    setConfirmFor(null);
    void act(async () => {
      try {
        return await wizardApi.chooseFramework(s.session_id, { framework_id: f.id, override_sop_tool: override });
      } catch (e) {
        const d = (e as WizardApiError).details as { sop_tool_required?: unknown } | undefined;
        if (d?.sop_tool_required) setConfirmFor(f);           // 8.0: the SOP names another tool - ask first
        throw e;
      }
    }, "framework");
  };
  const chooseCustom = async () => {
    setRequestFor(null);
    await act(async () => {
      try {
        return await wizardApi.chooseFramework(s.session_id, { custom_name: custom });
      } catch (e) {
        const err = e as WizardApiError;
        const d = err.details as { needs_approval?: boolean; tool_name?: string; blocked?: boolean } | undefined;
        // not approved yet - or a tool Understudy can't set up by itself yet: the admin's AI setup can
        if (d?.needs_approval || (err.status === 422 && !d?.blocked)) setRequestFor({ custom_name: custom, name: d?.tool_name || custom });
        throw e;
      }
    }, "custom");
  };
  const [showLogs, setShowLogs] = useState(false);
  const [allTools, setAllTools] = useState(false);
  const a = s.analysis;
  if (!a && !jobRunning(s, "watch") && s.media) {
    // 9.4: WATCH stopped (e.g. no page address readable in the recording) - fix the pages and watch again
    return (
      <div className="space-y-5">
        <Alert tone="amber" title="Step 1 needs your help" icon={<AlertTriangle className="h-4 w-4" />}>
          {s.job?.error || s.pillars.WATCH?.reason || "The recording could not be analysed."}
        </Alert>
        <PagesCard session={s} act={act} busyKey={busyKey} />
      </div>
    );
  }
  if (jobRunning(s, "watch") || !a) {
    return (
      <Card>
        <div className="flex items-center gap-3 text-sm text-slate-200">
          <RotateCw className="h-5 w-5 animate-spin text-sky-400" />
          {s.ai_mode === "ACTUAL_AI_API" ? "The AI is watching your recording and reading the page and your SOP..."
            : "No AI key: checking the skills learned in earlier AI sessions, then reading the target page's form..."}
          {s.ai_mode === "ACTUAL_AI_API" && !s.analysis && <span className="text-xs text-slate-400"> First it lists the page addresses it sees.</span>}
        </div>
      </Card>
    );
  }
  const deps = s.dependencies;
  const depTone = deps?.status === "READY" ? "green" : deps?.status === "FAILED" ? "red" : deps?.status === "INSTALLING" ? "sky" : "amber";
  return (
    <div className="space-y-5">
      <ToolRequirementBanner session={s} act={act} busyKey={busyKey} />
      <RequestOutcomeNotice session={s} />
      {confirmFor && s.tool_requirement && (
        <Alert tone="amber" title={`Your SOP requires ${s.tool_requirement.name} - use ${confirmFor.name} anyway?`} icon={<AlertTriangle className="h-4 w-4" />}>
          <p>&ldquo;{(s.tool_requirement.quote || "").replace(/[.\s]+$/, "")}&rdquo;. Using another tool is recorded in your session&apos;s activity log for your admin.</p>
          <div className="mt-2 flex flex-wrap gap-2">
            <Button variant="secondary" busy={busyKey === "framework"} onClick={() => pick(confirmFor, true)}>Use {confirmFor.name} anyway</Button>
            <Button variant="ghost" onClick={() => setConfirmFor(null)}>Cancel</Button>
          </div>
        </Alert>
      )}
      <PagesCard session={s} act={act} busyKey={busyKey} />

      <Card title={`What the AI understood: ${a.process_name}`} icon={<Sparkles className="h-4 w-4 text-sky-400" />}
        right={<SourceBadge source={s.analysis_meta?.agent_source} model={s.analysis_meta?.model} />}>
        {s.analysis_meta?.recall && <div className="mb-3"><RecallNote recall={s.analysis_meta.recall} what="the process" /></div>}
        {!a.media_interpreted && !s.analysis_meta?.recall && (
          <div className="mb-3"><Alert tone="amber" title="The video was not interpreted" icon={<Info className="h-4 w-4" />}>
            {s.analysis_meta?.ai_error && s.analysis_meta?.ai_configured
              ? <>The AI call did not succeed, so Understudy built the plan from the target page&apos;s form instead of watching your
                  recording. <b>Reason:</b> {s.analysis_meta.ai_error} The full request and response are in Mission Control &gt; Telemetry.</>
              : s.analysis_meta?.ai_error
              ? <>AI is not set up, so Understudy built the plan from the target page&apos;s form instead of watching your
                  recording. {s.analysis_meta.ai_error} You can still correct the plan below.</>
              : <>AI was not used for this recording, so Understudy built the plan from the target page&apos;s form instead of
                  watching it.</>}</Alert></div>
        )}
        <p className="text-sm leading-relaxed text-slate-300">{a.summary}</p>
        <StepPlan s={s} />
        <HistoryUsed s={s} />
        <GroundingNote s={s} />
      </Card>

      <ClarifyCard session={s} act={act} busyKey={busyKey} />

      {s.difficulty && <DifficultyCard session={s} act={act} busyKey={busyKey} onPick={pick} />}
      <ComplexityCard session={s} />

      <Card title="Choose how to automate it" icon={<Wand2 className="h-4 w-4 text-sky-400" />}>
        <p className="mb-4 text-xs text-slate-400">These are the AI&apos;s suggestions for this app, plus the tools your admin has approved.
          Tools with a lock need admin approval first. You can also type any other tool below and request it.</p>
        {(() => {
          // Less to read: the selected tool, the AI's recommendation and the best allowed picks first (3), the rest
          // one click away.
          // a tool the user asked the admin for stays among the first cards, so its status (and the admin's note) is visible
          const rank = (f: FrameworkOption) => (s.framework?.id === f.id ? 0 : f.recommended ? 1 : f.allowed ? 2 : f.access_request ? 2.5 : 3);
          // 8.3: one card per tool id, even if an older backend sends the same tool twice (several cards shared one id,
          // so selecting one highlighted all of them)
          const unique = s.framework_options.filter((f, i, all) => all.findIndex((x) => x.id === f.id) === i);
          const ordered = [...unique].sort((x, y) => rank(x) - rank(y));
          // 9.5.3: typing in the box below filters the cards (all matches shown, versions like "13.6" ignored)
          const matches = toolMatches(ordered, custom);
          const filtering = matches !== null;
          const shown = filtering ? matches : allTools ? ordered : ordered.slice(0, 3);
          const hidden = ordered.length - shown.length;
          return (
            <>
              {filtering && (
                <p className="mb-3 text-xs text-slate-400" role="status" aria-live="polite">
                  {matches.length
                    ? <>{matches.length === 1 ? "1 tool matches" : `${matches.length} tools match`} &ldquo;{custom.trim()}&rdquo; - click a card to use it
                        {matches.length === 1 && !hasVersion(custom) ? " (or press Enter)" : ""}, or &ldquo;Use this tool&rdquo; to ask for exactly what you typed{hasVersion(custom) ? " (with that version)" : ""}.</>
                    : <>No listed tool matches &ldquo;{custom.trim()}&rdquo;. &ldquo;Use this tool&rdquo; asks your admin to set it up.</>}
                </p>
              )}
              <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3" id="tool-options">
                {shown.map((f) => (
                  <FrameworkCard key={f.id} f={f} selected={s.framework?.id === f.id} disabled={busyKey !== null}
                    fit={s.complexity?.tool_fit.find((x) => x.tool_id === f.id)} onPick={() => pick(f)} />
                ))}
              </div>
              {!filtering && (hidden > 0 || allTools) && ordered.length > 3 && (
                <button type="button" onClick={() => setAllTools(!allTools)} aria-expanded={allTools} aria-controls="tool-options"
                  className="mt-3 text-xs font-semibold text-sky-300 hover:text-sky-200">
                  {allTools ? "Show fewer tools" : `Show ${hidden} more tool${hidden === 1 ? "" : "s"}`}
                </button>
              )}
            </>
          );
        })()}
        <div className="mt-4 flex flex-col gap-2 sm:flex-row">
          <input className={inputCls} value={custom} onChange={(e) => setCustom(e.target.value)} placeholder="Find or add a tool - type to filter, e.g. Selenium, Cypress 13.6, WebdriverIO, k6 ..."
            aria-label="Find a tool, or another tool to request (optionally with a version)" aria-controls="tool-options"
            onKeyDown={(e) => {
              if (e.key !== "Enter" || !custom.trim() || busyKey !== null) return;
              e.preventDefault();
              const m = toolMatches(s.framework_options.filter((f, i, all) => all.findIndex((x) => x.id === f.id) === i), custom);
              if (m && m.length === 1 && !hasVersion(custom)) pick(m[0]); else void chooseCustom();
            }} />
          <Button variant="secondary" className="whitespace-nowrap" disabled={!custom.trim()} busy={busyKey === "custom"}
            onClick={() => { void chooseCustom(); }}>Use this tool</Button>
        </div>
        {s.framework && <VersionPicker session={s} act={act} busyKey={busyKey} />}
        {requestFor && (
          <div className="mt-4">
            <RequestAccessBox key={requestFor.name} target={requestFor} session={s} act={act} busyKey={busyKey} onClose={() => setRequestFor(null)} />
          </div>
        )}
      </Card>

      <AppliedSkills session={s} />
      {s.framework && deps && (
        <Card title={`Setting up ${s.framework.name}`} icon={<Package className="h-4 w-4 text-sky-400" />}
          collapsible right={<Badge tone={depTone}>{deps.status}</Badge>}>
          <p className="text-sm text-slate-300">{deps.message}</p>
          {!!deps.versions?.length && (
            <div className="mt-2 flex flex-wrap gap-2">
              {deps.versions.map((v) => (
                <span key={v.name} className="inline-flex items-center gap-1.5 rounded-md border border-slate-700 bg-slate-950 px-2 py-1 text-xs"
                  title={`requested ${v.requested} · ${v.source}`}>
                  <Package className="h-3 w-3 text-sky-400" />
                  <code className="text-slate-200">{v.name}</code>
                  <b className={v.installed ? "text-emerald-300" : "text-amber-300"}>{v.installed || "not installed"}</b>
                  <span className="text-slate-400">({v.source})</span>
                </span>
              ))}
            </div>
          )}
          {!!s.dependency_lock?.extras?.length && (
            <p className="mt-2 text-xs text-slate-300">Extra packages the generated code needs:{" "}
              {s.dependency_lock.extras.map((e) => <code key={e.name} className="mr-1 rounded bg-slate-950 px-1" title={e.why}>{e.name}{e.version ? ` ${e.version}` : ""}</code>)}
            </p>
          )}
          {s.dependency_lock?.notes?.map((n, i) => <p key={i} className="mt-1 text-[11px] text-amber-300">{n}</p>)}
          <p className="mt-2 text-[11px] text-slate-400">
            {s.dependency_lock?.chosen_version ? <>Version <b>{s.dependency_lock.chosen_version}</b> is pinned. </> : null}
            Each run first checks the isolated sandbox has exactly these versions (and installs them if not); the ZIP pins the same versions.
          </p>
          {deps.logs?.length > 0 && (
            <button className="mt-2 text-[11px] text-sky-400 hover:underline" onClick={() => setShowLogs(!showLogs)}>
              {showLogs ? "Hide" : "Show"} install details
            </button>
          )}
          {showLogs && <pre className="mt-2 max-h-48 overflow-auto rounded bg-slate-950 p-2 text-[11px] text-slate-400">{deps.logs.join("\n")}</pre>}
        </Card>
      )}
      <div className="flex items-center justify-end gap-3">
        {s.status === "pending_admin_approval" && (
          <span className="text-xs text-slate-400" data-testid="continue-paused">Paused until your admin enables {s.pending_request?.tool_name || "the tool"} - or pick an approved tool.</span>
        )}
        {s.pages_confirmed === false && (
          <span className="mr-3 self-center text-xs text-amber-300">Check the pages the AI found (top of this step) first.</span>
        )}
        <Button disabled={s.pillars.WATCH?.status !== "PASS" || jobRunning(s) || s.status === "pending_admin_approval" || s.pages_confirmed === false} onClick={() => goTo(2)}>
          Continue to Build <ArrowRight className="h-4 w-4" />
        </Button>
      </div>
    </div>
  );
}

/* ============================================================================================
 * STEP 2 - BUILD
 * ========================================================================================== */
const TYPES: Parameter["type"][] = ["text", "number", "date", "email", "select", "boolean", "secret"];

/** 9.9: the payload table - part of Step 2 (Govern): the rules are checked against exactly these values. */
function PayloadCard({ session: s, act, busyKey, error }: StepProps) {
  const [draft, setDraft] = useState<Parameter[] | null>(null);
  const rows = draft ?? s.parameters;
  const dirty = draft !== null;
  const setRows = (r: Parameter[]) => setDraft(r);
  const setDirty = (d: boolean) => { if (!d) setDraft(null); };
  const problems = ((error?.details as { problems?: { key: string; message: string }[] } | undefined)?.problems) || [];
  const usedKeys = useMemo(() => new Set((s.analysis?.steps || []).map((st) => st.param_key).filter(Boolean)), [s.analysis]);
  const update = (i: number, patch: Partial<Parameter>) => { setRows(rows.map((r, j) => (j === i ? { ...r, ...patch } : r))); setDirty(true); };
  const save = async () => {
    const res = await act(() => wizardApi.saveParameters(s.session_id, rows), "params");
    if (res) setDirty(false);
    return res;
  };
  if (jobRunning(s, "prepare_parameters")) {
    return <Card><div className="flex items-center gap-3 text-sm"><RotateCw className="h-5 w-5 animate-spin text-sky-400" />Preparing the payload table...</div></Card>;
  }
  return (
    <>
      {(() => {
        const blank = rows.filter((p) => p.type !== "secret" && p.type !== "boolean" && !String(p.value ?? "").trim() && p.source !== "user");
        return blank.length > 0 ? (
          <Alert tone="amber" title={`${blank.length} value${blank.length === 1 ? " needs" : "s need"} your input`} icon={<Info className="h-4 w-4" />}>
            These came from a recording that could not be watched right now (offline), so the value isn&apos;t known. Type them in the
            highlighted fields - the field names and steps were already re-used from what Understudy learned earlier.
            <button type="button" className="ml-2 font-semibold underline"
              onClick={() => document.querySelector<HTMLInputElement>("[data-needs-value=true]")?.focus()}>Go to the first one</button>
          </Alert>
        ) : null;
      })()}
      <Card title="Payload parameters" icon={<ListChecks className="h-4 w-4 text-sky-400" />}
        right={<Button variant="secondary" onClick={() => { setRows([...rows, { key: `new_field_${rows.length + 1}`, label: "New field", value: "", type: "text", source: "user" }]); setDirty(true); }}>
          <Plus className="h-4 w-4" />Add parameter</Button>}>
        <div className="overflow-x-auto">
          <table className="w-full min-w-[760px] text-sm">
            <thead>
              <tr className="border-b border-slate-800 text-left text-[11px] uppercase tracking-wide text-slate-400">
                <th scope="col" className="py-2 pr-2">Field</th><th className="py-2 pr-2">Key</th><th className="py-2 pr-2">Value</th>
                <th className="py-2 pr-2">Type</th><th className="py-2 pr-2">From</th><th />
              </tr>
            </thead>
            <tbody>
              {rows.map((p, i) => {
                const prob = problems.find((x) => x.key === p.key);
                return (
                  <tr key={i} className="border-b border-slate-800/60 align-top">
                    <td className="py-2 pr-2">
                      <input aria-label={`Label of ${p.key}`} className={cx(inputCls, "py-1.5")} value={p.label} onChange={(e) => update(i, { label: e.target.value })} />
                      {p.sop_dependent && <div className="mt-1"><Badge tone="violet">SOP</Badge></div>}
                    </td>
                    <td className="py-2 pr-2">
                      <input className={cx(inputCls, "py-1.5 font-mono text-xs")} value={p.key} disabled={usedKeys.has(p.key)}
                        onChange={(e) => update(i, { key: e.target.value.toLowerCase().replace(/[^a-z0-9_]/g, "_") })} />
                    </td>
                    <td className="py-2 pr-2">
                      {p.type === "boolean" ? (
                        <input type="checkbox" className="mt-2 accent-sky-500" checked={String(p.value) === "true"} onChange={(e) => update(i, { value: e.target.checked })} />
                      ) : p.type === "select" && p.constraints?.options?.length ? (
                        <select aria-label={`Value for ${p.label}`} aria-invalid={!!prob} className={cx(inputCls, "py-1.5", prob && "border-rose-500")}
                          value={String(p.value ?? "")} onChange={(e) => update(i, { value: e.target.value })}>
                          {!p.constraints.options.includes(String(p.value ?? "")) && <option value={String(p.value ?? "")}>{String(p.value ?? "") || "(choose)"}</option>}
                          {p.constraints.options.map((o) => <option key={o} value={o}>{o}</option>)}
                        </select>
                      ) : (
                        <input aria-label={`Value for ${p.label}`} aria-invalid={!!prob} data-needs-value={!String(p.value ?? "").trim() && p.source !== "user" && p.type !== "secret"}
                          className={cx(inputCls, "py-1.5", prob && "border-rose-500",
                            !String(p.value ?? "").trim() && p.source !== "user" && p.type !== "secret" && "border-amber-500 ring-1 ring-amber-500/60")}
                          type={p.type === "secret" ? "password" : p.type === "date" ? "date" : p.type === "number" ? "number" : "text"}
                          placeholder={p.type === "secret" ? (p.has_value ? "•••••• (saved)" : "kept secret, not exported") : ""}
                          value={String(p.value ?? "")} onChange={(e) => update(i, { value: e.target.value })} />
                      )}
                      {constraintText(p.constraints) && <div className="mt-1 text-[11px] text-slate-400" title="Read from the target page itself">Page: {constraintText(p.constraints)}</div>}
                      {prob && <div className="mt-1 text-[11px] text-rose-300">{prob.message}</div>}
                    </td>
                    <td className="py-2 pr-2">
                      <select aria-label={`Type of ${p.label}`} className={cx(inputCls, "py-1.5")} value={p.type} onChange={(e) => update(i, { type: e.target.value as Parameter["type"] })}>
                        {TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
                      </select>
                    </td>
                    <td className="py-2 pr-2"><Badge>{p.source || "user"}</Badge></td>
                    <td className="py-2">
                      <button aria-label={`Remove ${p.label}`} title={usedKeys.has(p.key) ? "Used by a recorded step" : "Remove"} disabled={usedKeys.has(p.key)}
                        onClick={() => { setRows(rows.filter((_, j) => j !== i)); setDirty(true); }}
                        className="mt-1.5 text-slate-400 hover:text-rose-400 disabled:opacity-30"><Trash2 className="h-4 w-4" /></button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        <div className="mt-4 flex flex-wrap items-center justify-end gap-2">
          {dirty && <span className="mr-auto text-xs text-amber-300">Unsaved changes</span>}
          <Button variant={dirty ? "primary" : "secondary"} disabled={!dirty} busy={busyKey === "params"}
            onClick={() => { void save(); }} data-testid="save-payload">Save values</Button>
        </div>
      </Card>

    </>
  );
}

export function BuildStep({ session: s, act, busyKey, goTo }: StepProps) {
  const [tab, setTab] = useState<string | null>(null);
  // 9.9: payload.json is shown next to the code - exactly what Understudy writes for a run and into the ZIP (the Step 2
  // values; secrets are never in it - they come from UNDERSTUDY_SECRET_<KEY> environment variables)
  const payloadJson = JSON.stringify(Object.fromEntries(s.parameters.filter((p) => p.type !== "secret").map((p) => [p.key, p.value ?? null])), null, 2) + "\n";
  const secretKeys = s.parameters.filter((p) => p.type === "secret").map((p) => p.key);
  const files = s.suite ? Array.from(new Set([...Object.keys(s.suite.files), "payload.json"])) : [];
  const current = tab && files.includes(tab) ? tab : s.suite?.entry_file || files[0];
  const content = (f: string | undefined) => (!f || !s.suite ? "" : f === "payload.json" && !s.suite.files[f] ? payloadJson : s.suite.files[f]);
  const rules = (s.rules || []).filter((r) => r.enabled !== false);
  const governed = s.pillars.GOVERN?.status === "PASS";
  return (
    <div className="space-y-5">
      <ToolRequirementBanner session={s} act={act} busyKey={busyKey} />
      <Help>
        <b>Step 3 - generate the code.</b> The SOP rules you confirmed in Step 2 are written into the script: it ships with
        <code className="mx-1 rounded bg-slate-900 px-1">governance.json</code> and checks <code className="mx-1 rounded bg-slate-900 px-1">payload.json</code>
        against them <i>before step 1</i> - every time it runs, here and wherever you run the ZIP. Data the SOP forbids stops it
        before it touches the application.
      </Help>
      {!governed && (
        <Alert tone="amber" title="Govern first" icon={<ShieldCheck className="h-4 w-4" />}>
          The code is written after your SOP rules are confirmed and the data passes them.
          <button type="button" className="ml-2 font-semibold underline" onClick={() => goTo(2)}>Go to Step 2</button>
        </Alert>
      )}
      <Card title="SOP rules in the script" icon={<ShieldCheck className="h-4 w-4 text-violet-400" />}
        right={<Button busy={busyKey === "build" || jobRunning(s, "build")} disabled={!governed || jobRunning(s)} onClick={() => act(() => wizardApi.build(s.session_id), "build")}
          data-testid="generate-code"><Code2 className="h-4 w-4" />{s.suite ? "Regenerate code" : "Generate automation code"}</Button>}>
        {rules.length === 0
          ? <p className="text-xs text-slate-400">Your SOP has no rules - the script still runs the check and prints <code>GOVERN OK: 0 SOP rule(s)</code>.</p>
          : <ul className="list-disc space-y-0.5 pl-5 text-xs text-slate-200">{rules.map((r) => <li key={r.rule_id}>{r.plain_english}</li>)}</ul>}
        <p className="mt-2 text-[11px] text-slate-400">Before step 1 the script prints <code>GOVERN OK</code> - or <code>GOVERN FAILED: why</code> and stops. Changing a rule in Step 2 means the code is generated again.</p>
      </Card>

      {s.suite && (
        <Card title={`Generated ${s.framework?.name} suite`} icon={<Code2 className="h-4 w-4 text-sky-400" />}
          right={<div className="flex gap-2"><Badge tone={s.suite.generator === "ai" ? "green" : s.suite.generator === "learned" || s.suite.generator === "agentic" ? "violet" : "sky"}>{s.suite.generator === "ai" ? "written by AI" : s.suite.generator === "learned" ? "learned earlier" : s.suite.generator === "agentic" ? "agentic script - Gemini acts at run time" : "vetted template"}</Badge><SourceBadge source={s.suite.agent_source} model={s.suite.model} /></div>}>
          {s.suite.notes.filter(Boolean).map((n) => <p key={n} className="mb-2 text-xs text-slate-400">{n}</p>)}
          <AgentTraceNote agent={s.suite.agent} />
          <div className="flex flex-wrap gap-1 border-b border-slate-800">
            {files.map((f) => (
              <button key={f} onClick={() => setTab(f)} data-testid={`suite-tab-${f}`}
                className={cx("rounded-t px-3 py-1.5 font-mono text-xs", f === current ? "bg-slate-800 text-white" : "text-slate-400 hover:text-white")}>{f}</button>
            ))}
          </div>
          {current === "payload.json" && (
            <p className="bg-slate-900/60 px-3 py-1.5 text-[11px] text-slate-400" data-testid="payload-tab-note">
              Your data from Step 2 - the script reads every value from this file and checks it against <code>governance.json</code> before step 1.
              Edit it to run with other data (or change the values in Step 2).
              {secretKeys.length > 0 && <> Secrets are not in this file: set {secretKeys.map((k) => `UNDERSTUDY_SECRET_${k.toUpperCase()}`).join(", ")} in the environment.</>}
            </p>
          )}
          <pre className="max-h-[420px] overflow-auto rounded-b bg-slate-950 p-3 text-[12px] leading-relaxed text-slate-200">{content(current)}</pre>
          <div className="mt-2 text-[11px] text-slate-400">
            Safety scan: {s.suite.findings.length === 0 ? "no problems - no hardcoded values, secrets or dangerous calls." : `${s.suite.findings.length} finding(s)`}
          </div>
        </Card>
      )}
      <div className="flex justify-between">
        <Button variant="ghost" onClick={() => goTo(2)}>Back</Button>
        <Button disabled={s.pillars.BUILD?.status !== "PASS" || jobRunning(s)} onClick={() => goTo(4)}>Continue to Execute <ArrowRight className="h-4 w-4" /></Button>
      </div>
    </div>
  );
}

/* ============================================================================================
 * STEP 2 - GOVERN (9.9: the data and the SOP rules - before the code is generated)
 * ========================================================================================== */
const RULE_TYPES: Rule["rule_type"][] = ["allowed_values", "denied_values", "max", "min", "date_range", "regex", "required", "role_access"];
const RULE_LABEL: Record<string, string> = {
  allowed_values: "must be one of", denied_values: "must NOT be", max: "maximum", min: "minimum", date_range: "date between",
  regex: "must match format", required: "is required", role_access: "role must be one of",
};

export function GovernStep(props: StepProps) {
  const { session: s, act, busyKey, goTo } = props;
  const [draftRules, setDraftRules] = useState<Rule[] | null>(null);
  const [agreeDraft, setAgree] = useState<boolean | null>(null);
  const [sopDraft, setSop] = useState<string | null>(null);
  const rules = draftRules ?? s.rules;
  const dirty = draftRules !== null;
  const agree = agreeDraft ?? s.rules_confirmed;
  const sop = sopDraft ?? s.sop_text;
  const setRules = (r: Rule[]) => setDraftRules(r);
  const setDirty = (d: boolean) => { if (!d) { setDraftRules(null); setAgree(null); } };
  const [editSop, setEditSop] = useState(false);
  const [fixes, setFixes] = useState<Record<string, string>>({});
  const sopRef = useRef<HTMLDivElement>(null);
  const keys = s.parameters.map((p) => p.key);
  const upd = (i: number, patch: Partial<Rule>) => { setRules(rules.map((r, j) => (j === i ? { ...r, ...patch } : r))); setDirty(true); setAgree(false); };

  const confirm = async () => {
    const res = await act(() => wizardApi.saveRules(s.session_id, rules, agree), "rules");
    if (res) setDirty(false);
  };
  const applyFix = async (field: string) => {
    const params = s.parameters.map((p) => (p.key === field ? { ...p, value: fixes[field] } : p));
    const res = await act(() => wizardApi.saveParameters(s.session_id, params), `fix-${field}`);
    if (res) await act(() => wizardApi.check(s.session_id), "check");
  };
  const c = s.compliance;
  return (
    <div className="space-y-5">
      <ToolRequirementBanner session={s} act={act} busyKey={busyKey} />
      <Help>
        <b>Step 2 - check the data and the rules.</b> Each row below is a value the automation types in; the code will read
        them from <code className="mx-1 rounded bg-slate-900 px-1">payload.json</code>. The AI turned your SOP text into the
        rules further down - read them like a checklist, fix what is wrong and confirm. Understudy tests the data against every
        rule, and in Step 3 the rules are written into the script, which checks them again before every run.
      </Help>
      <PayloadCard {...props} />
      <Card title="Rules found in your SOP" icon={<ShieldCheck className="h-4 w-4 text-sky-400" />}
        right={<SourceBadge source={s.rules_meta?.agent_source} model={s.rules_meta?.model} />}>
        {s.rules_meta?.recall && !jobRunning(s, "extract_rules") && <div className="mb-3"><RecallNote recall={s.rules_meta.recall} what="the rules" /></div>}
        {!jobRunning(s, "extract_rules") && (s.rules_meta?.notes || []).filter((n) => /SOP looks like|could not read/i.test(n)).map((n, i) => (
          <div key={i} className="mb-3"><Alert tone={s.rules_meta?.language_warning ? "red" : "amber"} title="SOP language" icon={<Info className="h-4 w-4" />}>{n}</Alert></div>
        ))}
        {jobRunning(s, "extract_rules") ? <div className="flex items-center gap-2 text-sm"><RotateCw className="h-4 w-4 animate-spin" />Reading your SOP...</div> : (
          <>
            {rules.length === 0 && <p className="text-sm text-slate-400">No rules were found. If your SOP has constraints, rephrase them or add a rule manually.</p>}
            <div className="space-y-3">
              {rules.map((r, i) => (
                <div key={r.rule_id} className={cx("rounded-lg border p-3", r.enabled ? "border-slate-800 bg-slate-950/50" : "border-slate-800/50 opacity-50")}>
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-sm font-medium text-white">{r.plain_english}</span>
                    <Badge tone={r.source === "ai" ? "green" : r.source === "learned" ? "violet" : r.source === "parser" ? "amber" : "slate"}>{r.agreement === "both" ? (r.source === "learned" ? "learned + parser agree" : "AI + parser agree") : r.source === "learned" ? "learned skill" : r.source}</Badge>
                    {r.bound === false && <Badge tone="red">not linked to a parameter</Badge>}
                    <label className="ml-auto flex items-center gap-1 text-[11px] text-slate-400">
                      <input type="checkbox" className="accent-sky-500" checked={r.enabled} onChange={(e) => upd(i, { enabled: e.target.checked })} />enforce
                    </label>
                    <button className="text-slate-400 hover:text-rose-400" onClick={() => { setRules(rules.filter((_, j) => j !== i)); setDirty(true); setAgree(false); }}><Trash2 className="h-4 w-4" /></button>
                  </div>
                  {r.source_text && <p className="mt-1 text-[11px] italic text-slate-400">&ldquo;{r.source_text}&rdquo;</p>}
                  <div className="mt-2 grid gap-2 sm:grid-cols-3">
                    <select className={cx(inputCls, "py-1.5 text-xs")} value={r.field} onChange={(e) => upd(i, { field: e.target.value })}>
                      {[...new Set([...keys, r.field])].map((k) => <option key={k} value={k}>{k}{keys.includes(k) ? "" : " (missing)"}</option>)}
                    </select>
                    <select className={cx(inputCls, "py-1.5 text-xs")} value={r.rule_type} onChange={(e) => upd(i, { rule_type: e.target.value as Rule["rule_type"] })}>
                      {RULE_TYPES.map((t) => <option key={t} value={t}>{RULE_LABEL[t]}</option>)}
                    </select>
                    {["allowed_values", "denied_values", "role_access"].includes(r.rule_type) && (
                      <input className={cx(inputCls, "py-1.5 text-xs")} value={r.values.join(", ")} placeholder="comma separated"
                        onChange={(e) => upd(i, { values: e.target.value.split(",").map((x) => x.trim()).filter(Boolean) })} />
                    )}
                    {["max", "min"].includes(r.rule_type) && (
                      <input type="number" className={cx(inputCls, "py-1.5 text-xs")} value={r.limit ?? ""} onChange={(e) => upd(i, { limit: e.target.value === "" ? null : Number(e.target.value) })} />
                    )}
                    {r.rule_type === "date_range" && (
                      <div className="flex gap-1">
                        <input type="date" className={cx(inputCls, "py-1.5 text-xs")} value={r.start_date || ""} onChange={(e) => upd(i, { start_date: e.target.value || null })} />
                        <input type="date" className={cx(inputCls, "py-1.5 text-xs")} value={r.end_date || ""} onChange={(e) => upd(i, { end_date: e.target.value || null })} />
                      </div>
                    )}
                    {r.rule_type === "regex" && (
                      <input className={cx(inputCls, "py-1.5 font-mono text-xs")} value={r.pattern || ""} onChange={(e) => upd(i, { pattern: e.target.value })} />
                    )}
                  </div>
                </div>
              ))}
            </div>
            <div className="mt-3 flex flex-wrap items-center gap-3">
              <Button variant="secondary" onClick={() => { setRules([...rules, { rule_id: `R-NEW${rules.length}`, field: keys[0] || "field", rule_type: "required", values: [], plain_english: "New rule", enabled: true, source: "user" }]); setDirty(true); setAgree(false); }}>
                <Plus className="h-4 w-4" />Add rule</Button>
              <Button variant="ghost" onClick={() => { setEditSop(true); setTimeout(() => sopRef.current?.scrollIntoView({ behavior: "smooth" }), 50); }}>Edit my SOP text</Button>
            </div>
            <div className="mt-4 flex flex-wrap items-center gap-3 rounded-lg border border-slate-800 bg-slate-950/60 p-3">
              <label className="flex items-center gap-2 text-sm text-slate-200">
                <input type="checkbox" className="h-4 w-4 accent-emerald-500" checked={agree} onChange={(e) => setAgree(e.target.checked)} />
                Yes - the AI understood my SOP correctly
              </label>
              <Button className="ml-auto" variant={s.rules_confirmed && !dirty ? "secondary" : "primary"} disabled={!agree && !dirty} busy={busyKey === "rules"} onClick={confirm}>
                {s.rules_confirmed && !dirty ? "Confirmed ✓" : "Save & confirm rules"}
              </Button>
            </div>
          </>
        )}
      </Card>

      {editSop && (
        <div ref={sopRef}>
          <Card title="Update the SOP" icon={<FileText className="h-4 w-4 text-sky-400" />}>
            <p className="mb-2 text-xs text-slate-400">Change the rule itself (any language). Saving re-reads the rules from the new text; then confirm
              them and run the compliance check again.</p>
            <textarea className={cx(inputCls, "h-32 font-mono text-xs")} value={sop} onChange={(e) => setSop(e.target.value)} />
            <div className="mt-2 flex justify-end gap-2">
              <Button variant="ghost" onClick={() => setEditSop(false)}>Cancel</Button>
              <Button busy={busyKey === "sop"} onClick={async () => { const r = await act(() => wizardApi.updateSop(s.session_id, sop), "sop"); if (r) { setEditSop(false); setDirty(false); setSop(null); } }}>
                Save SOP & re-read rules</Button>
            </div>
          </Card>
        </div>
      )}

      <Card title="Compliance check" icon={<ListChecks className="h-4 w-4 text-sky-400" />}
        right={<Button disabled={!s.rules_confirmed || dirty} busy={busyKey === "check"} onClick={() => act(() => wizardApi.check(s.session_id), "check")}>Run compliance check</Button>}>
        {!s.rules_confirmed && <p className="text-sm text-slate-400">Confirm the rules above first.</p>}
        {c && c.passed && (
          <Alert tone="green" title="Everything complies" icon={<CheckCircle2 className="h-4 w-4" />}>
            Your payload satisfies all {c.rules_checked} rule(s) and the generated code passed the safety scan.
          </Alert>
        )}
        {c && !c.passed && (
          <div className="space-y-3">
            <Alert tone="red" title={`${c.violations.length} problem(s) must be fixed before running`} icon={<XCircle className="h-4 w-4" />}>
              For each one, either change the payload value or update the SOP if the rule is wrong.
            </Alert>
            {c.violations.map((v, i) => {
              const isParam = s.parameters.some((p) => p.key === v.field);
              return (
                <div key={i} className="rounded-lg border border-rose-500/30 bg-rose-500/5 p-3">
                  <div className="flex items-start gap-2 text-sm text-rose-100"><AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-rose-400" />{v.message}</div>
                  <div className="ml-6 mt-1 text-[11px] text-slate-400">Rule: {v.rule}</div>
                  <div className="ml-6 mt-1 text-xs text-slate-300">💡 {v.fix_hint}</div>
                  <div className="ml-6 mt-2 flex flex-wrap gap-2">
                    {isParam && (
                      <>
                        <input className={cx(inputCls, "w-56 py-1.5 text-xs")} placeholder={`New value for ${v.field}`} value={fixes[v.field] ?? ""}
                          onChange={(e) => setFixes({ ...fixes, [v.field]: e.target.value })} />
                        <Button variant="secondary" disabled={!fixes[v.field]} busy={busyKey === `fix-${v.field}`} onClick={() => applyFix(v.field)}>Fix payload</Button>
                      </>
                    )}
                    {!isParam && v.kind === "MISSING_PARAMETER" && <Button variant="secondary" onClick={() => document.querySelector("[data-testid=save-payload]")?.scrollIntoView({ behavior: "smooth" })}>Add it to the payload above</Button>}
                    {v.rule_id === "SCRIPT-SAFETY" && <Button variant="secondary" onClick={() => goTo(3)}>Regenerate code</Button>}
                    {v.rule_id !== "SCRIPT-SAFETY" && <Button variant="ghost" onClick={() => { setEditSop(true); setTimeout(() => sopRef.current?.scrollIntoView({ behavior: "smooth" }), 50); }}>Update SOP instead</Button>}
                  </div>
                </div>
              );
            })}
          </div>
        )}
        {c?.warnings?.map((w, i) => <p key={i} className="mt-2 text-xs text-amber-300">⚠ {w.message}</p>)}
        {c?.ai_review?.warnings?.length ? (
          <div className="mt-3 space-y-1">
            <div className="flex items-center gap-2 text-xs text-slate-400">AI second opinion <SourceBadge source={c.ai_review.agent_source} /></div>
            {c.ai_review.warnings.map((w, i) => <p key={i} className="text-xs text-slate-300">• {w.message}</p>)}
          </div>
        ) : null}
      </Card>
      <div className="flex justify-between">
        <Button variant="ghost" onClick={() => goTo(1)}>Back</Button>
        <Button disabled={s.pillars.GOVERN?.status !== "PASS" || jobRunning(s)} onClick={() => goTo(3)}>Continue to Build <ArrowRight className="h-4 w-4" /></Button>
      </div>
    </div>
  );
}

/* ============================================================================================
 * STEP 4 - EXECUTE
 * ========================================================================================== */
export function LogConsole({ sid, active }: { sid: string; active: boolean }) {
  const [lines, setLines] = useState<string[]>([]);
  const next = useRef(0);
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    let stop = false;
    const tick = async () => {
      try {
        const r = await wizardApi.logs(sid, next.current);
        if (r.next < next.current) {                 // a new attempt started (self-healing re-run)
          next.current = 0;
          setLines((l) => [...l, "──────── new attempt ────────"].slice(-800));
          return;
        }
        if (r.lines.length) { next.current = r.next; setLines((l) => [...l, ...r.lines].slice(-800)); }
      } catch { /* ignore */ }
    };
    tick();
    const id = setInterval(() => { if (!stop) tick(); }, active ? 800 : 3000);
    return () => { stop = true; clearInterval(id); };
  }, [sid, active]);
  useEffect(() => { box.current?.scrollTo({ top: box.current.scrollHeight }); }, [lines]);
  return (
    <div ref={box} role="log" aria-label="Live run log" aria-live="polite" tabIndex={0} className="h-72 overflow-auto rounded-lg border border-slate-800 bg-black/60 p-3 font-mono text-[12px] leading-relaxed">
      {lines.length === 0 && <span className="text-slate-400">Logs will appear here...</span>}
      {lines.map((l, i) => (
        <div key={i} className={cx(l.startsWith("────") ? "my-1 text-sky-400" : /FAILED|Error|error/.test(l) ? "text-rose-300" : / OK:|COMPLETE/.test(l) ? "text-emerald-300" : "text-slate-300")}>{l}</div>
      ))}
    </div>
  );
}

export function RetryPrompt({ session: s, act, busyKey }: { session: WizardSession; act: Act; busyKey: string | null }) {
  const [fb, setFb] = useState("");
  if (!s.retry.awaiting_decision) return null;
  const suggestRef = !!s.retry.suggest_reference;
  const refOk = suggestRef && !!s.retry.reference_available;
  const dup = s.retry.duplicate_warning;
  const tool = s.framework?.name || "your tool";
  return (
    <Alert tone="amber" title={suggestRef ? `${tool} can't start on this machine yet` : dup ? "A record was saved, but not every value matched"
      : "Self-healing needs your help"} icon={<RotateCw className="h-4 w-4" />}>
      <p>{s.retry.prompt}</p>
      {dup && <p className="mt-1 text-xs text-amber-200" data-testid="duplicate-warning">⚠ {dup}</p>}
      {(
        <div className="mt-3 space-y-2" data-testid="retry-feedback">
          <label htmlFor="retry-fb" className="block text-xs font-semibold text-slate-200">
            {suggestRef ? "Tell the AI what's different on this machine (optional)" : "Tell the AI what went wrong (optional)"}</label>
          <textarea id="retry-fb" rows={2} maxLength={4000} value={fb} onChange={(e) => setFb(e.target.value)}
            placeholder={suggestRef ? "e.g. 'JDK 17 is installed in C:\\Program Files\\Java\\jdk-17' or 'Chrome is at /opt/google/chrome/chrome' or 'we use a proxy'"
              : "e.g. 'the invoice date was saved as 60923-02-20 instead of 2026-09-23' or 'there is no success message after submit - check the ledger row instead'"}
            className="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-xs text-white placeholder:text-slate-500 focus:border-sky-500 focus:outline-none" />
          <p className="text-xs text-slate-400">
            The AI uses your hint to prepare the next fix (you review it before it runs).
            {suggestRef ? " You can also fix the machine yourself (install what is named above) and just retry." : ""}
            {refOk && <> Or verify the recorded process with Understudy&apos;s built-in Playwright driver - your {tool} code
              stays exactly as generated and is still what goes in the ZIP.</>}
          </p>
        </div>
      )}
      <div className="mt-3 flex flex-wrap gap-2">
        <Button busy={busyKey === "retry"} onClick={() => act(() => wizardApi.retry(s.session_id, "heal", fb.trim() || undefined).then((r) => { setFb(""); return r; }), "retry")}>
          <Wand2 className="h-4 w-4" />{fb.trim() ? "Ask the AI to fix it" : suggestRef ? `Retry with ${tool}` : "Let the AI fix it"}</Button>
        {refOk && (
          <Button variant="ghost" busy={busyKey === "retry-ref"} onClick={() => act(() => wizardApi.retry(s.session_id, "reference"), "retry-ref")}>
            <ShieldCheck className="h-4 w-4" />Verify with built-in Playwright driver</Button>
        )}
        <Button variant="ghost" busy={busyKey === "decline"} onClick={() => act(() => wizardApi.declineRetry(s.session_id), "decline")}>Stop here</Button>
      </div>
    </Alert>
  );
}

/** 6.3: the AI's fix for a failed run, shown BEFORE the retry. The user approves it, corrects the AI (or answers its
 *  question) so it revises the fix, or stops. Nothing runs until the user approves. */
function DiffView({ d }: { d: { file: string; status: string; added: number; removed: number; diff: string; truncated: boolean } }) {
  return (
    <details className="rounded-lg border border-slate-800 bg-slate-950/70" open={d.added + d.removed <= 40}>
      <summary className="flex cursor-pointer items-center gap-2 px-3 py-2 text-xs">
        <Code2 className="h-3.5 w-3.5 text-sky-400" aria-hidden /><code className="text-slate-200">{d.file}</code>
        <span className="text-emerald-300">+{d.added}</span><span className="text-rose-300">-{d.removed}</span>
        {d.status !== "changed" && <Badge>{d.status}</Badge>}
      </summary>
      <pre className="max-h-72 overflow-auto border-t border-slate-800 p-2 text-[11.5px] leading-relaxed" aria-label={`Changes in ${d.file}`}>
        {d.diff.split("\n").map((l, i) => (
          <div key={i} className={cx(l.startsWith("+++") || l.startsWith("---") ? "text-slate-400"
            : l.startsWith("+") ? "bg-emerald-500/10 text-emerald-200" : l.startsWith("-") ? "bg-rose-500/10 text-rose-200"
            : l.startsWith("@@") ? "text-sky-300" : "text-slate-400")}>{l || " "}</div>
        ))}
        {d.truncated && <div className="text-amber-300">... diff truncated</div>}
      </pre>
    </details>
  );
}

export function HealProposalCard({ session: s, act, busyKey }: { session: WizardSession; act: Act; busyKey: string | null }) {
  const p = s.retry.proposal;
  const [text, setText] = useState("");
  if (!s.retry.awaiting_approval || !p) return null;
  const cannotRun = !!p.build_error || !!p.blocking?.length;
  const canRevise = p.revision < p.max_revisions;
  return (
    <section aria-labelledby="heal-proposal-h" data-testid="heal-proposal"
      className="rounded-xl border-2 border-amber-500/50 bg-amber-950/10 p-5 shadow-lg">
      <div className="mb-3 flex flex-wrap items-start justify-between gap-2">
        <h3 id="heal-proposal-h" className="flex items-center gap-2 text-sm font-semibold text-white">
          <Wand2 className="h-4 w-4 text-amber-300" aria-hidden />Review the fix before retry {p.attempt} of {p.max}
        </h3>
        <div className="flex flex-wrap gap-2">
          <Badge tone="violet">{p.category}</Badge>
          {p.revision > 0 && <Badge tone="sky">revision {p.revision}</Badge>}
          <SourceBadge source={p.agent_source} model={p.model} />
        </div>
      </div>
      <p className="mb-4 text-xs text-slate-300">Nothing runs until you approve. If the AI got something wrong, tell it below and it will revise the fix first.</p>

      <div className="space-y-4 text-sm">
        <div>
          {p.warning && <p className="mb-2 rounded-lg border border-amber-500/40 bg-amber-500/10 p-2 text-xs text-amber-100" data-testid="heal-warning">⚠ {p.warning}</p>}
          <h4 className="mb-1 text-xs font-bold uppercase tracking-wide text-rose-300">What went wrong</h4>
          <p className="rounded-lg bg-slate-950/60 p-2.5 font-mono text-xs text-rose-100">
            {p.failed_step ? `Step ${p.failed_step}: ` : ""}{p.failure_reason}</p>
          <p className="mt-2 text-slate-200">{p.diagnosis}</p>
        </div>

        <div>
          <h4 className="mb-1 text-xs font-bold uppercase tracking-wide text-emerald-300">What the AI changed</h4>
          {p.kind === "runtime" && p.repair_plan && (
            <div className="text-slate-200">
              <p>Runtime repair <code className="rounded bg-slate-800 px-1">{p.repair_plan.repair_id}</code>
                {p.repair_plan.source === "ai" && <Badge tone="violet">AI-advised</Badge>}: {p.repair_plan.explanation}</p>
              <ul className="mt-1 space-y-0.5 font-mono text-[11px] text-slate-300" aria-label="Runtime settings">
                {Object.entries(p.repair_plan.env || {}).map(([k, v]) => <li key={k}>{k}={v}</li>)}
                {(p.repair_plan.path_prepend || []).map((d) => <li key={d}>PATH first: {d}</li>)}
                {p.repair_plan.download_runtime && <li>download {p.repair_plan.download_runtime} (checksum verified, into Understudy&apos;s runtimes folder)</li>}
                {p.repair_plan.java_release ? <li>pom.xml: maven.compiler.release = {p.repair_plan.java_release}</li> : null}
                {p.repair_plan.switch_version && <li>switch this session to version {p.repair_plan.switch_version} (allowed by your admin) and reinstall</li>}
                {!!p.repair_plan.run_args?.length && <li>run command: add {p.repair_plan.run_args.join(" ")}</li>}
                {p.repair_plan.clean_project_files && <li>remove the extra project / solution files from the generated code</li>}
                {p.repair_plan.ask_admin && <li>send to your admin: {p.repair_plan.ask_admin.request}</li>}
              </ul>
              <span className="block text-xs text-slate-400">{p.repair_plan.java_release ? "Only pom.xml changes; the test code is not changed." : "The script itself is not changed."}</span>
            </div>
          )}
          {p.changes.length > 0 ? (
            <ul className="space-y-1 text-xs" aria-label="Step changes">
              {p.changes.map((c, i) => (
                <li key={i} className="font-mono text-slate-300">step {c.step}: <span className="text-rose-300 line-through">{c.from || "-"}</span> → <span className="text-emerald-300">{c.to}</span>{c.why ? <span className="ml-2 font-sans text-slate-400">({c.why})</span> : null}</li>
              ))}
            </ul>
          ) : p.kind === "script" ? <p className="text-xs text-slate-400">No step changes - only the instructions to the code generator below.</p> : null}
          {p.notes.length > 0 && (
            <ul className="mt-2 list-disc space-y-0.5 pl-5 text-xs text-slate-300" aria-label="Instructions for the code generator">
              {p.notes.map((n, i) => <li key={i}>{n}</li>)}
            </ul>
          )}
          {p.feedback_applied && <p className="mt-2 text-xs text-sky-200"><b>Your correction:</b> {p.feedback_applied}</p>}
        </div>

        {p.kind === "script" && (
          <div>
            <h4 className="mb-1 text-xs font-bold uppercase tracking-wide text-sky-300">Script changes</h4>
            {p.build_error && <Alert tone="red" title="The script could not be generated">{p.build_error}</Alert>}
            {!!p.blocking?.length && <Alert tone="red" title="The new script failed the safety scan">{p.blocking.map((b) => b.message).join("; ")}</Alert>}
            {p.script_diff.length === 0 && !p.build_error
              ? <p className="text-xs text-slate-400">The regenerated script is identical - the retry runs the same code (e.g. after a timing problem).</p>
              : <div className="space-y-2">{p.script_diff.map((d) => <DiffView key={d.file} d={d} />)}</div>}
            <AgentTraceNote agent={p.agent} title="Heal agent (code rewritten and re-checked on the live page)" />
          </div>
        )}

        {p.feedback.length > 0 && (
          <div>
            <h4 className="mb-1 text-xs font-bold uppercase tracking-wide text-slate-400">Your corrections so far</h4>
            <ol className="list-decimal space-y-0.5 pl-5 text-xs text-slate-300">{p.feedback.map((f, i) => <li key={i}>{f}</li>)}</ol>
          </div>
        )}

        {p.question && (
          <div role="status" className="rounded-lg border border-sky-500/50 bg-sky-500/10 p-3 text-sm text-sky-100" data-testid="heal-question">
            <MessageSquare className="mr-1 inline h-4 w-4" aria-hidden /><b>The AI asks:</b> {p.question}
          </div>
        )}

        <div className="border-t border-slate-800 pt-3">
          <label htmlFor="heal-feedback" className="mb-1 block text-xs font-semibold text-slate-200">
            {p.question ? "Your answer" : "Something wrong? Tell the AI what to fix"}</label>
          <textarea id="heal-feedback" rows={2} className={cx(inputCls, "text-sm")} value={text} maxLength={4000}
            onChange={(e) => setText(e.target.value)} disabled={!canRevise}
            placeholder={'e.g. "Step 2 should use #vendor-legal-name" or "wait 3 seconds after submit" or "don\'t click Save Draft, click Submit"'} />
          {!canRevise && <p className="mt-1 text-xs text-amber-300">The AI has revised this fix {p.max_revisions} times. Approve it or stop.</p>}
          <div className="mt-3 flex flex-wrap gap-2">
            {p.needs_admin && (
              <p className="w-full rounded-lg border border-amber-500/40 bg-amber-500/10 p-2 text-xs text-amber-100">
                This needs your admin. Approving sends the request to Mission Control (with the log); you&apos;ll get a
                notification when they answer, then run it again.</p>)}
            <Button variant="success" disabled={cannotRun} busy={busyKey === "heal-approve"}
              onClick={() => act(() => wizardApi.approveHeal(s.session_id, p.id), "heal-approve")}>
              <CheckCircle2 className="h-4 w-4" />{p.needs_admin ? "Approve & send to my admin" : "Approve & retry"}</Button>
            <Button variant="secondary" disabled={!text.trim() || !canRevise} busy={busyKey === "heal-revise"}
              onClick={async () => { if (await act(() => wizardApi.reviseHeal(s.session_id, p.id, text.trim()), "heal-revise")) setText(""); }}>
              <MessageSquare className="h-4 w-4" />{p.question ? "Send answer & revise" : "Ask the AI to revise"}</Button>
            <Button variant="ghost" busy={busyKey === "decline"} onClick={() => act(() => wizardApi.declineRetry(s.session_id), "decline")}>Stop here</Button>
          </div>
        </div>
      </div>
    </section>
  );
}

/** Self-healing status: automatic mode runs up to the admin's limit; in "ask" mode it prepares fixes for review. */
export function HealingStatus({ session: s, act, busyKey, showLog = false }: { session: WizardSession; act: Act; busyKey: string | null; showLog?: boolean }) {
  const healing = s.status === "healing" || jobRunning(s, "retry") || jobRunning(s, "heal_revise");
  const hist = s.retry.history || [];
  if (!healing && !hist.length) return null;
  const heal = s.retry.last_heal;
  const running = healing && s.job?.status === "running";
  return (
    <Card title={running ? (jobRunning(s, "heal_revise") ? "The AI is revising its fix with your correction" : `Self-healing - attempt ${Math.max(s.retry.count, 1)} of ${s.retry.max}`) : `Self-healing - ${hist.length} attempt${hist.length === 1 ? "" : "s"}`}
      icon={<Wand2 className={cx("h-4 w-4 text-sky-400", running && "animate-pulse")} />}
      right={running ? (
        <Button variant="danger" className="px-3 py-1 text-xs" busy={busyKey === "stop"} disabled={!!s.retry.stop_requested}
          onClick={() => act(() => wizardApi.declineRetry(s.session_id), "stop")}>
          <Square className="h-3 w-3" />{s.retry.stop_requested ? "Stopping after this attempt" : "Stop"}</Button>
      ) : heal ? <div className="flex gap-2"><Badge tone="violet">{heal.category}</Badge><SourceBadge source={heal.agent_source} /></div> : undefined}>
      {running && (
        <p className="mb-2 flex items-center gap-2 text-sm text-slate-300"><RotateCw className="h-4 w-4 animate-spin text-sky-400" />
          Understudy is diagnosing the failure and preparing a fix (steps, script or the tool&apos;s runtime). Depending on your
          admin&apos;s setting you review it before the retry, or it runs again by itself.</p>
      )}
      {heal && <p className="text-sm text-slate-300">{heal.diagnosis}</p>}
      {heal && heal.changes.length > 0 && (
        <ul className="mt-2 space-y-1 text-xs">
          {heal.changes.map((c, i) => (
            <li key={i} className="font-mono text-slate-400">step {c.step}: <span className="text-rose-300 line-through">{c.from || "-"}</span> → <span className="text-emerald-300">{c.to}</span>{c.why ? <span className="ml-2 font-sans text-slate-400">({c.why})</span> : null}</li>
          ))}
        </ul>
      )}
      {hist.length > 0 && (
        <ol className="mt-3 space-y-1 border-t border-slate-800 pt-2 text-[11px] text-slate-400">
          {hist.map((h, i) => (
            <li key={i}>Attempt {h.attempt}: <b className="text-slate-300">{h.category}</b>{h.approved_by === "user" ? <span className="text-slate-400"> · approved by you{h.revisions ? ` after ${h.revisions} correction${h.revisions === 1 ? "" : "s"}` : ""}</span> : null} → <span className={h.outcome === "PASS" ? "text-emerald-400" : h.outcome === "RUNNING" ? "text-sky-300" : "text-rose-300"}>{h.outcome}</span></li>
          ))}
        </ol>
      )}
      {running && showLog && <div className="mt-3"><LogConsole key={s.session_id + "-heal"} sid={s.session_id} active /></div>}
    </Card>
  );
}

function LearnedNotice({ session: s }: { session: WizardSession }) {
  if (!s.learned_skills?.length) return null;
  return (
    <Alert tone="green" title="Lesson saved as a learned skill" icon={<BookOpen className="h-4 w-4" />}>
      What fixed this run was saved on this machine ({s.learned_skills.join(", ")}). Next time you automate this target or
      use this tool, Understudy applies the fix before the first run - and it&apos;s in your ZIP under <code>skills/</code>.
    </Alert>
  );
}

function StopOutcome({ session: s }: { session: WizardSession }) {
  if (s.status === "needs_help") {
    return (
      <Alert tone="red" title="Self-healing couldn't fix it" icon={<XCircle className="h-4 w-4" />}>
        After {s.retry.count} automatic attempt{s.retry.count === 1 ? "" : "s"} (your admin&apos;s limit is {s.retry.max}) the
        automation still didn&apos;t work, so no ZIP was created. Every error was saved as a lesson for next time. Check the
        target URL, your values and the recorded steps (you can correct the AI&apos;s understanding in Step 1), then click
        <b> Run again</b>.
      </Alert>
    );
  }
  if (s.status === "stopped") return <Alert tone="amber" title="Stopped">You stopped self-healing. No ZIP was created because the result could not be verified.</Alert>;
  return null;
}

export function RuntimeRepairs({ session: s }: { session: WizardSession }) {
  const reps = s.runtime_repairs || [];
  if (!reps.length) return null;
  return (
    <Card title={`Runtime repairs for ${s.framework?.name || "the tool"}`} icon={<Wrench className="h-4 w-4 text-sky-400" />}
      right={<Badge tone="slate">same tool - no switch</Badge>}>
      <ol className="space-y-2 text-sm">
        {reps.map((r, i) => (
          <li key={i} className="rounded-lg border border-slate-800 bg-slate-950/50 p-2.5">
            <div className="flex flex-wrap items-center gap-2">
              <Badge tone={r.exhausted ? "red" : "sky"}>{r.cause}</Badge>
              <code className="text-[11px] text-slate-400">{r.repair_id || "no repair left"}</code>
              {r.result && <span className="text-[11px] text-slate-400">{r.result}</span>}
            </div>
            <p className="mt-1 text-slate-300">{r.explanation}</p>
            {Object.keys(r.env || {}).length > 0 && (
              <div className="mt-1 font-mono text-[11px] text-slate-400">
                {Object.entries(r.env).map(([k, v]) => <div key={k}>{k}={v}</div>)}
              </div>
            )}
          </li>
        ))}
      </ol>
    </Card>
  );
}

/** 8.4: the run is held - the tool needs another runtime version (Mission Control > Environments). The user sends the
 * one-click fix to the admin (who approves it) or runs anyway. */
function RuntimeCheckCard({ session: s, act, busyKey }: { session: WizardSession; act: Act; busyKey: string | null }) {
  const rc = s.runtime_check;
  if (!rc || rc.ok) return null;
  if (rc.ignored) {
    return (
      <p className="rounded-lg border border-amber-500/40 bg-amber-500/5 px-3 py-2 text-xs text-amber-100" data-testid="runtime-check-ignored">
        Ran despite a runtime-version warning: {rc.problems.map((p) => p.message).join(" ")}</p>
    );
  }
  return (
    <Card title="The run is on hold - the tool needs a different runtime version" icon={<AlertTriangle className="h-4 w-4 text-amber-300" />}
      collapsible={false} className="border-amber-500/40">
      <div className="space-y-3 text-sm" data-testid="runtime-check">
        {rc.problems.map((p) => {
          const sent = rc.sent?.[p.kind];
          return (
            <div key={p.kind} className="rounded-lg border border-slate-800 bg-slate-950/60 p-3">
              <p className="text-slate-100">{p.message}</p>
              {p.reason && <p className="mt-1 text-xs text-slate-400">Why: {p.reason}</p>}
              {p.fix ? <p className="mt-1 text-xs text-slate-300">Fix: <b>{p.fix.label}</b> - your admin approves it.</p>
                : <p className="mt-1 text-xs text-slate-300">No installed or downloadable version fits - your admin has to install one.</p>}
              <div className="mt-2">
                {sent ? <Badge tone="sky">Sent to your admin ({sent}) - you&apos;ll be notified</Badge> : (
                  <Button variant="primary" className="px-3 py-1.5 text-xs" busy={busyKey === `rtfix-${p.kind}`}
                    onClick={() => act(() => wizardApi.requestRuntimeFix(s.session_id, p.kind), `rtfix-${p.kind}`)}>
                    Ask my admin to {p.fix ? (p.fix.type === "switch" ? `switch to ${p.label} ${p.fix.version}` : "download it") : "install it"}
                  </Button>
                )}
              </div>
            </div>
          );
        })}
        <div className="flex flex-wrap items-center gap-2 border-t border-slate-800 pt-3 text-xs text-slate-400">
          <span>Know it works anyway?</span>
          <Button variant="secondary" className="px-3 py-1.5 text-xs" busy={busyKey === "execute-anyway"}
            onClick={() => act(() => wizardApi.execute(s.session_id, true), "execute-anyway")}>Run anyway</Button>
        </div>
      </div>
    </Card>
  );
}

export function ExecuteStep({ session: s, act, busyKey, goTo }: StepProps) {
  const ex = s.execution;
  const running = jobRunning(s, "execute") || jobRunning(s, "retry");
  const needsAi = !!s.agentic && s.agent_ai?.available === false;
  const expected = s.agentic && s.strategy_override !== "reference"
    ? `the ${s.framework?.name} agent - Gemini decides each action on the live page`
    : s.strategy_override === "reference"
    ? `Understudy's built-in Playwright driver (you chose it after the ${s.framework?.name} runtime couldn't be repaired; your code is still in the ZIP)`
    : ["DRY_RUN", "SKIPPED"].includes(s.dependencies?.status || "")
      ? "Understudy's built-in Playwright driver (dependency installation is switched off on this server)"
      : `your ${s.framework?.name} code with its own runtime`;
  return (
    <div className="space-y-5">
      <Help>
        <b>Step 4 - run it for real.</b> Understudy writes your checked values into <code className="rounded bg-slate-900 px-1">payload.json</code>,
        then runs the automation against <b>{s.target_url}</b>{(s.target_urls?.length ?? 0) > 1 ? <> and {s.target_urls!.length - 1} more page(s)</> : null}. Watch each step in the log below. If a run fails, Understudy
        fixes itself and runs again automatically (up to {s.retry.max} times, set by your admin).
      </Help>
      <Card title="Run the automation" icon={<Play className="h-4 w-4 text-sky-400" />}
        right={ex?.status && <Badge tone={ex.status === "PASS" ? "green" : ex.status === "FAIL" ? "red" : "sky"}>{ex.status}</Badge>}>
        <p className="mb-3 text-sm text-slate-300">It will run with {expected}.</p>
        <AgentAiNote session={s} />
        <Button busy={busyKey === "execute" || running} disabled={s.pillars.GOVERN?.status !== "PASS" || needsAi}
          onClick={() => act(() => wizardApi.execute(s.session_id), "execute")}>
          <Play className="h-4 w-4" />{ex ? "Run again" : "Run automation"}
        </Button>
        <div className="mt-4"><LogConsole key={s.session_id} sid={s.session_id} active={running} /></div>
        {ex && ex.status !== "RUNNING" && (
          <div className="mt-3 text-xs text-slate-400">
            Strategy: <b>{ex.strategy === "framework" ? (ex.agentic ? `${s.framework?.name} agent${ex.agent_model ? ` (${ex.agent_model})` : ""}` : `${s.framework?.name} runtime`) : "built-in Playwright driver"}</b>
            {ex.duration_ms ? ` · ${(ex.duration_ms / 1000).toFixed(1)} s` : ""}
          </div>
        )}
        {ex?.egress && ex.status !== "RUNNING" && (
          <div className="mt-2 rounded-lg border border-slate-800 bg-slate-950/60 p-2.5 text-xs text-slate-300" data-testid="egress-summary">
            <b>Network policy ({ex.egress.mode}):</b> reachable hosts: {ex.egress.allowed_hosts.join(", ") || "none"} ·{" "}
            {ex.egress.requests_denied === 0
              ? <span className="text-emerald-300">no request left the policy</span>
              : <span className="text-amber-300">{ex.egress.requests_denied} request(s) refused: {ex.egress.denied_hosts.map((d) => d.host).join(", ")}</span>}
          </div>
        )}
      </Card>
      <RuntimeCheckCard session={s} act={act} busyKey={busyKey} />
      <HealProposalCard session={s} act={act} busyKey={busyKey} />
      <HealingStatus session={s} act={act} busyKey={busyKey} />
      <RetryPrompt session={s} act={act} busyKey={busyKey} />
      <RuntimeRepairs session={s} />
      {s.export.ready && (s.retry.count > 0
        ? <Alert tone="green" title="Fixed and passed" icon={<CheckCircle2 className="h-4 w-4" />}>Self-healing made the automation work. Continue to Step 5 to download the ZIP.</Alert>
        : <Alert tone="green" title="Ran successfully" icon={<CheckCircle2 className="h-4 w-4" />}>The automation ran to completion. Continue to Step 5 to download the ZIP.</Alert>)}
      <StopOutcome session={s} />
      <div className="flex justify-between">
        <Button variant="ghost" onClick={() => goTo(3)}>Back</Button>
        <Button disabled={s.pillars["BUILD-EXECUTE"]?.status !== "PASS" || running} onClick={() => goTo(5)}>Continue to Export <ArrowRight className="h-4 w-4" /></Button>
      </div>
    </div>
  );
}


/* ============================================================================================
 * 9.0: "How complex is this automation?" - shown before the user picks a tool
 * ========================================================================================== */
const LEVEL_TONE: Record<string, "green" | "sky" | "amber" | "red" | "slate"> = { Simple: "green", Moderate: "sky", Complex: "amber", "Very complex": "red" };

export function ComplexityCard({ session: s }: { session: WizardSession }) {
  const c = s.complexity;
  if (!c) return null;
  const tone = LEVEL_TONE[c.level] || "slate";
  const pp = s.page_profile;
  // 9.8: with the difficulty score shown above, this box only explains what was inspected and how each challenge is handled
  const slim = !!s.difficulty;
  return (
    <div data-testid="complexity-card">
      <Card title={slim ? "What the inspection found - and how Understudy handles it" : "How complex is this automation?"} foldId="complexity"
        icon={<Gauge className="h-4 w-4 text-sky-400" />}
        summary={<span className="text-[11px] text-slate-400">{slim ? `${c.factors.length} challenge(s)` : `${c.level} · ${c.score}/10 - open to see why and which tool fits`}</span>}
        right={slim ? undefined : <Badge tone={tone}>{c.level} · {c.score}/10</Badge>}>
        {!slim && <div className="flex items-center gap-3">
          <div className="h-2 flex-1 overflow-hidden rounded-full bg-slate-800" aria-label={`Complexity ${c.score} of 10`}>
            <div className={cx("h-full rounded-full", tone === "green" ? "bg-emerald-400" : tone === "sky" ? "bg-sky-400" : tone === "amber" ? "bg-amber-400" : "bg-rose-400")}
              style={{ width: `${c.score * 10}%` }} />
          </div>
          <SourceBadge source={c.agent_source} />
        </div>}
        {!slim && <p className="mt-3 text-sm leading-relaxed text-slate-200">{c.summary}</p>}
        {pp && (pp.renderer === "chromium" ? <InspectionDetails pp={pp} /> : (
          <p className="mt-2 text-[11px] text-slate-400" data-testid="page-probe-summary">
            The live page{(pp.pages?.length ?? 1) > 1 ? "s" : ""} could not be checked{pp.error ? ` (${pp.error})` : ""} - this is based on the recording and the SOP.
          </p>
        ))}
        {c.factors.length > 0 && (
          <table className="mt-3 w-full text-xs">
            <thead><tr className="text-left text-[11px] text-slate-500"><th className="py-1 pr-2">What makes it harder</th><th className="py-1">How Understudy handles it</th></tr></thead>
            <tbody>
              {c.factors.map((f) => (
                <tr key={f.name} className="border-t border-slate-800/60 align-top">
                  <td className="py-1.5 pr-3">
                    <span className={cx("font-semibold", f.severity === "high" ? "text-amber-300" : "text-slate-200")}>{f.name}</span>
                    {f.detail ? <span className="block text-[11px] text-slate-400">{f.detail}</span> : null}
                  </td>
                  <td className="py-1.5 text-slate-300">{f.how_handled}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {!slim && c.tool_fit.length > 0 && (
          <div className="mt-3">
            <p className="text-[11px] font-semibold uppercase tracking-wide text-slate-500">Tool fit</p>
            <ul className="mt-1 space-y-1 text-xs">
              {c.tool_fit.map((t) => (
                <li key={t.tool_id}><Badge tone={t.fit === "good" ? "green" : t.fit === "ok" ? "sky" : "amber"}>{t.fit}</Badge>{" "}
                  <b className="text-slate-200">{t.name}</b> <span className="text-slate-400">- {t.why}</span></li>
              ))}
            </ul>
          </div>
        )}
        {c.tips.length > 0 && (
          <ul className="mt-3 list-disc space-y-0.5 pl-5 text-[11px] text-slate-400">{c.tips.map((x) => <li key={x}>{x}</li>)}</ul>
        )}
      </Card>
    </div>
  );
}

/** 9.4: what the automation-aware inspection checked - chosen from what the steps do, each page opened once */
function InspectionDetails({ pp }: { pp: NonNullable<WizardSession["page_profile"]> }) {
  const pages = pp.pages || [];
  const checks = pp.checks || [];
  const relevant = checks.filter((c) => c.relevant);
  const quiet = checks.filter((c) => !c.relevant);
  const reloaded = pages.filter((p) => (p.loads || 0) > 1);
  const multi = pages.length > 1;
  const tone = (r: string) => (r === "failed" ? "red" : r === "warning" ? "amber" : r === "skipped" ? "slate" : "green");
  const row = (c: (typeof checks)[number], i: number) => (
    <tr key={`${c.page}-${c.id}-${i}`} className="border-t border-slate-800/60 align-top">
      {multi && <td className="py-1.5 pr-2 text-slate-500">{c.page}</td>}
      <td className="py-1.5 pr-2"><Badge tone={tone(c.result)}>{c.title}</Badge></td>
      <td className="py-1.5 pr-2 text-slate-400">{c.why}</td>
      <td className={cx("py-1.5", c.relevant ? "text-amber-200" : "text-slate-300")}>{c.finding}</td>
    </tr>
  );
  const head = (
    <thead><tr className="text-left text-[11px] text-slate-500">{multi && <th className="py-1 pr-2">Page</th>}
      <th className="py-1 pr-2">Check</th><th className="py-1 pr-2">Why this automation needs it</th><th className="py-1">Found</th></tr></thead>
  );
  return (
    <div className="mt-3 rounded-lg border border-slate-800 bg-slate-950/50 p-3" data-testid="page-probe-summary">
      <div className="flex flex-wrap items-center gap-2 text-[11px] text-slate-400">
        <b className="text-slate-200">What Understudy checked</b>
        {pp.plan?.agent_source && <SourceBadge source={pp.plan.agent_source} />}
        <span>
          {pages.length || 1} page{pages.length > 1 ? "s" : ""}, {pp.loads} page load{pp.loads === 1 ? "" : "s"} in all
          {reloaded.length ? "" : " - no page needed a reload"}
        </span>
      </div>
      <ul className="mt-1.5 space-y-0.5 text-[11px] text-slate-400" data-testid="reload-decisions">
        {pages.filter((p) => !p.error).map((p) => {
          const ai = p.reload_decided_by && p.reload_decided_by !== "rules";
          return (
            <li key={p.index}>
              <Badge tone={ai ? "violet" : "slate"}>{ai ? "AI decided" : "Rules decided"}</Badge>{" "}
              {multi ? <b className="text-slate-300">Page {p.index}: </b> : null}
              {p.loads > 1 ? `opened ${p.loads} times` : "opened once"}
              {p.reload_why ? ` - ${p.reload_why}` : ""}
              {(p.verify_loads ?? 1) > 1 ? <span className="text-slate-500"> · the code&apos;s locators will be confirmed on {p.verify_loads} fresh loads{p.verify_why ? ` (${p.verify_why})` : ""}</span> : null}
            </li>
          );
        })}
      </ul>
      <p className="mt-1 text-[11px] text-slate-400">{pp.summary}</p>
      {multi && (
        <ol className="mt-2 space-y-0.5 text-[11px] text-slate-400" data-testid="inspected-pages">
          {pages.map((p) => (
            <li key={p.index}><b className="text-slate-300">Page {p.index}</b> <span className="break-all">{p.url}</span>
              {p.error ? <span className="text-amber-300"> - could not be opened directly ({p.error})</span>
                : <span>{p.title ? ` - ${p.title}` : ""}{p.steps?.length ? ` - steps ${p.steps.join(", ")}` : ""}</span>}</li>
          ))}
        </ol>
      )}
      {(pp.plan?.focus?.length ?? 0) > 0 && (
        <ul className="mt-2 list-disc space-y-0.5 pl-5 text-[11px] text-sky-200/80">{pp.plan!.focus!.map((f) => <li key={f}>{f}</li>)}</ul>
      )}
      {relevant.length > 0 && (
        <table className="mt-2 w-full text-xs" data-testid="inspection-relevant">{head}<tbody>{relevant.map(row)}</tbody></table>
      )}
      {quiet.length > 0 && (
        <details className="mt-2 text-xs">
          <summary className="cursor-pointer text-[11px] text-slate-400">
            {relevant.length ? `${quiet.length} other check(s) found nothing that makes this automation harder` : `All ${quiet.length} check(s) passed - nothing makes this automation harder`}
          </summary>
          <table className="mt-1 w-full">{head}<tbody>{quiet.map(row)}</tbody></table>
        </details>
      )}
    </div>
  );
}

/** 9.0: what the build / heal agent did - rounds, locators re-checked on fresh page loads */
export function AgentTraceNote({ agent, title = "Build agent" }: { agent?: BuildAgentTrace; title?: string }) {
  if (!agent || agent.status === "not run") return null;
  const tone = agent.status === "verified" ? "green" : agent.status === "unverified" ? "amber" : "slate";
  return (
    <div className="mt-3 rounded-lg border border-slate-800 bg-slate-950/60 p-3 text-xs" data-testid="agent-trace">
      <div className="flex flex-wrap items-center gap-2">
        <b className="text-slate-200">{title}</b>
        <Badge tone={tone}>{agent.status === "verified" ? `${agent.verified_locators} locator(s) confirmed on the live page (${agent.page_loads} page load${agent.page_loads === 1 ? "" : "s"})`
          : agent.status === "unverified" ? "some locators could not be confirmed" : "locators not checked on the live page"}</Badge>
        <span className="text-slate-500">{agent.rounds.length} round(s)</span>
        {agent.complexity ? <span className="text-slate-500">· written for a {agent.complexity.level.toLowerCase()} automation ({agent.complexity.score}/10)</span> : null}
      </div>
      {agent.profile_summary ? <p className="mt-1 text-[11px] text-slate-400">{agent.profile_summary}</p> : null}
      <ol className="mt-2 space-y-1">
        {agent.rounds.map((r) => (
          <li key={r.round} className="text-[11px] text-slate-300">
            <span className="text-slate-500">Round {r.round}:</span> {r.result}
            {r.checked !== undefined ? ` · ${r.checked} locator(s) checked` : ""}
            {(r.problems || []).slice(0, 2).map((p) => <span key={p} className="block pl-4 text-amber-300">{p}</span>)}
            {(r.failed || []).slice(0, 3).map((f) => <span key={f.value} className="block pl-4 text-amber-300">{f.strategy}=&apos;{f.value}&apos;: {f.problems[0]}</span>)}
          </li>
        ))}
      </ol>
      {agent.unresolved && agent.unresolved.length > 0 && agent.status === "unverified" && (
        <p className="mt-1 text-[11px] text-amber-300">Still open: {agent.unresolved.slice(0, 2).join("; ")}</p>
      )}
    </div>
  );
}

/* ============================================================================================
 * STEP 5 - EXPORT (9.0: no page re-check, no admin escalation - the run passed, the ZIP is built)
 * ========================================================================================== */
export function ExportStep({ session: s, act, busyKey, goTo }: StepProps) {
  const running = jobRunning(s, "export") || jobRunning(s, "retry");
  const ran = s.pillars["BUILD-EXECUTE"]?.status === "PASS";
  const auto = useRef(false);
  useEffect(() => {
    if (!auto.current && ran && !s.export.ready && !running && !s.pillars.EXPORT) {
      auto.current = true;
      act(() => wizardApi.export(s.session_id), "export");
    }
  }, [s, ran, running, act]);
  const kb = s.export.size_bytes ? ` (${Math.max(1, Math.round(s.export.size_bytes / 1024))} KB)` : "";
  return (
    <div className="space-y-5">
      <Help>
        <b>Step 5 - take the code.</b> The automation ran successfully in Step 4, so Understudy packages it as a ZIP you can run
        on your own machine or in CI.
      </Help>
      <LearnedNotice session={s} />
      <Card title="Download your automation" collapsible={false} icon={<Package className="h-4 w-4 text-sky-400" />}
        right={s.export.ready ? <Badge tone="green">ready</Badge> : <Badge tone="slate"><Lock className="h-3 w-3" />locked</Badge>}>
        {running && <div className="mb-3 flex items-center gap-2 text-sm text-slate-300"><RotateCw className="h-4 w-4 animate-spin text-sky-400" />Creating the ZIP...</div>}
        {s.export.ready ? (
          <div className="space-y-3" data-testid="export-ready">
            <p className="text-sm text-slate-300">Everything needed to run it on your own machine:</p>
            <ul className="grid gap-1 text-xs text-slate-400 sm:grid-cols-2">
              <li>• the {s.framework?.name} {s.agentic ? "agentic script and its runtime" : "code"}</li><li>• payload.json with your checked values</li>
              <li>• README.md - how to install, set the environment and run it</li>
              <li>• .env.example and config/environment.json</li><li>• dependency manifest + run.sh / run.bat</li>
              <li>• your SOP, confirmed rules and compliance report</li><li>• evidence: the run log and step results</li>
            </ul>
            <div className="flex flex-wrap gap-2">
              <a href={wizardApi.exportUrl(s.session_id)} className="inline-flex items-center gap-2 rounded-lg bg-emerald-500 px-4 py-2 text-sm font-semibold text-slate-950 hover:bg-emerald-400">
                <Download className="h-4 w-4" />Download {s.export.filename}{kb}
              </a>
              <Button variant="secondary" busy={busyKey === "export"} disabled={running}
                onClick={() => act(() => wizardApi.export(s.session_id), "export")}><RotateCw className="h-4 w-4" />Re-create ZIP</Button>
            </div>
          </div>
        ) : ran ? (
          <Button busy={busyKey === "export" || running} onClick={() => act(() => wizardApi.export(s.session_id), "export")}>
            <Package className="h-4 w-4" />Create ZIP</Button>
        ) : (
          <p className="text-sm text-slate-400">Run the automation successfully in Step 4 first - the ZIP is created right after.</p>
        )}
      </Card>
      {s.export.ready && s.export_readme?.markdown && <ReadmePreview readme={s.export_readme} />}
      <div className="flex justify-start"><Button variant="ghost" onClick={() => goTo(4)}>Back</Button></div>
    </div>
  );
}


/** 9.9: the ZIP's README - written by Gemini from the package's facts (checked by Understudy), or Understudy's own. */
function ReadmePreview({ readme }: { readme: NonNullable<WizardSession["export_readme"]> }) {
  return (
    <Card title="README.md in the ZIP" foldId="export-readme" icon={<FileText className="h-4 w-4 text-sky-400" />}
      right={<SourceBadge source={readme.source} model={readme.model} />}>
      <div data-testid="export-readme">
        {readme.source !== "LOCAL_HEURISTIC"
          ? <p className="mb-2 text-xs text-slate-400">Written by Gemini from this package&apos;s facts - how to install it, which environment variables to set and how to run it. Understudy checked it: every variable and the run command are in it, no secrets, no other sites{readme.added.length ? ` (it added: ${readme.added.join("; ")})` : ""}.</p>
          : <p className="mb-2 text-xs text-slate-400">Understudy&apos;s own README{readme.problems.length ? ` (Gemini's draft was not used: ${readme.problems.join("; ")})` : ""}.</p>}
        <pre className="max-h-96 overflow-auto whitespace-pre-wrap rounded-lg border border-slate-800 bg-slate-950 p-3 text-[11px] leading-relaxed text-slate-300">{readme.markdown}</pre>
      </div>
    </Card>
  );
}


/* ============================================================================================
 * 8.0 zero-trust tool registry: "Unsupported Framework" + the paused session
 * ========================================================================================== */
const LIFECYCLE = ["REQUESTED", "PROPOSED", "INSTALLING", "SMOKE_TESTING", "ACTIVE"] as const;
const LIFECYCLE_LABEL: Record<string, string> = {
  REQUESTED: "Requested", PROPOSED: "AI setup proposed", INSTALLING: "Installing in the sandbox",
  SMOKE_TESTING: "Smoke test", ACTIVE: "Active", NEEDS_ADMIN: "Waiting for your admin's answer", FAILED: "Setup stopped",
  REJECTED: "Rejected",
};

/** 8.0: the admin's decision on this session's enablement request (shown in Step 1 after the session continues). */
export function RequestOutcomeNotice({ session: s }: { session: WizardSession }) {
  const o = s.request_outcome;
  const [hidden, setHidden] = useState<string | null>(null);
  if (!o || hidden === o.id || s.pending_request) return null;
  const what = o.tool_name + (o.version ? ` ${o.version}` : "");
  if (o.status === "approved") {
    return (
      <div data-testid="request-outcome">
        <Alert tone="green" title={`Your admin enabled ${what} (${o.id})`} icon={<CheckCircle2 className="h-4 w-4" />}>
          <p>It passed its smoke test in the sandbox and is now selected for this session. Continue with Step 2.</p>
          <button type="button" onClick={() => setHidden(o.id)} className="mt-1 text-[11px] underline opacity-80">Dismiss</button>
        </Alert>
      </div>
    );
  }
  return (
    <div data-testid="request-outcome">
      <Alert tone="amber" title={`Your admin rejected the request for ${what} (${o.id})`} icon={<AlertTriangle className="h-4 w-4" />}>
        {o.note ? <p>Admin: &ldquo;{o.note}&rdquo;</p> : null}
        <p>The session continues - pick an approved tool below.</p>
        <button type="button" onClick={() => setHidden(o.id)} className="mt-1 text-[11px] underline opacity-80">Dismiss</button>
      </Alert>
    </div>
  );
}

export function PendingBanner({ session: s, act, busyKey }: { session: WizardSession; act: Act; busyKey: string | null }) {
  const p = s.pending_request;
  if (!p) return null;
  const rs = p.registry_status || "REQUESTED";
  const at = LIFECYCLE.indexOf(rs as typeof LIFECYCLE[number]);
  const stuck = rs === "NEEDS_ADMIN" || rs === "FAILED";
  return (
    <div data-testid="pending-banner">
      <Alert tone={stuck ? "amber" : "sky"} title={`Paused - waiting for your admin to enable ${p.tool_name}${p.version ? " " + p.version : ""} (${p.id})`}
        icon={<Hourglass className="h-4 w-4" />}>
        <p>This session is <b>PENDING ADMIN APPROVAL</b>. In Mission Control the AI is setting the tool up with your admin: it installs it in an
          isolated sandbox and runs a smoke test. When the test passes the tool becomes active, is selected here automatically and you
          continue {p.step >= 2 ? "where you left off" : "with Step 2"}. This page updates by itself.</p>
        <ol className="mt-2 flex flex-wrap items-center gap-1 text-[11px]" aria-label="Setup progress">
          {LIFECYCLE.map((k, i) => (
            <li key={k} aria-current={k === rs ? "step" : undefined}
              className={cx("rounded-full border px-2 py-0.5", k === rs ? "border-sky-400 bg-sky-500/15 text-sky-100"
                : at >= 0 && i < at ? "border-emerald-500/40 text-emerald-300" : "border-slate-700 text-slate-400")}>
              {i < at ? "✓ " : ""}{LIFECYCLE_LABEL[k]}</li>
          ))}
          {at < 0 && <li className="rounded-full border border-amber-500/60 bg-amber-500/10 px-2 py-0.5 text-amber-200">{LIFECYCLE_LABEL[rs] || rs}</li>}
        </ol>
        {p.admin_note && <p className="mt-1 text-xs">Admin: {p.admin_note}</p>}
        <div className="mt-2">
          <Button variant="ghost" busy={busyKey === "cancel-pending"} onClick={() => act(() => wizardApi.cancelPending(s.session_id), "cancel-pending")}>
            Stop waiting and pick an approved tool</Button>
        </div>
      </Alert>
    </div>
  );
}

export function ToolRequirementBanner({ session: s, act, busyKey }: { session: WizardSession; act: Act; busyKey: string | null }) {
  const req = s.tool_requirement;
  const [reason, setReason] = useState(req ? `My SOP requires it: "${req.quote}"` : "");
  if (!req || s.pending_request) return null;
  const what = req.name + (req.version ? ` ${req.version}` : "");
  const send = (version?: string) => act(() => wizardApi.requestTool(s.session_id, req.known
    ? { framework_id: req.tool_id, reason, ...(version ? { version } : {}) }
    : { custom_name: req.name, reason }), "enable-request");
  if (req.status === "active") {
    if (s.framework?.id === req.tool_id) return null;
    return (
      <Alert tone="green" title={`Your SOP requires ${what} - it is approved`} icon={<CheckCircle2 className="h-4 w-4" />}>
        <p>&ldquo;{req.quote}&rdquo;</p>
        {!s.framework && <div className="mt-2"><Button busy={busyKey === "framework"}
          onClick={() => act(() => wizardApi.chooseFramework(s.session_id, { framework_id: req.tool_id }), "framework")}>Use {req.name}</Button></div>}
      </Alert>
    );
  }
  if (req.status === "blocked") {
    return (
      <Alert tone="red" title={`Your SOP requires ${what}, but your admin has blocked it`} icon={<XCircle className="h-4 w-4" />}>
        <p>&ldquo;{req.quote}&rdquo;{req.blocked?.note ? <> Admin: {req.blocked.note}</> : null} Ask your admin, or update the SOP.</p>
      </Alert>
    );
  }
  const versionCase = req.status === "version_not_allowed";
  const pendingElsewhere = req.status === "pending";
  return (
    <div data-testid="unsupported-framework">
      <Alert tone="amber" title={versionCase ? `Version not allowed: ${what}` : pendingElsewhere ? `${what} is being set up for another session`
        : `Unsupported Framework: ${what}`} icon={<AlertTriangle className="h-4 w-4" />}>
        <p>Your SOP says &ldquo;{req.quote.replace(/[.\s]+$/, "")}&rdquo;.{" "}
          {versionCase ? <>Your admin allows {req.name} {req.allowed_versions?.join(", ")} - not {req.version}.</>
            : pendingElsewhere ? <>A request ({req.request?.id}) is already with your admin.</>
            : <>{req.name} isn&apos;t enabled on this installation (only tools your admin has activated can run).</>}
          {" "}Submit an enablement request: this session pauses, your admin&apos;s AI setup installs and smoke-tests the tool, and the
          session continues automatically once it is active.</p>
        <textarea aria-label="Why you need it" className={cx(inputCls, "mt-2 h-14 text-xs")} value={reason} maxLength={500}
          onChange={(e) => setReason(e.target.value)} />
        <div className="mt-2 flex flex-wrap gap-2">
          <Button busy={busyKey === "enable-request"} onClick={() => { void send(versionCase ? req.version || undefined : undefined); }}>
            <Send className="h-4 w-4" />{pendingElsewhere ? "Wait for it too" : "Submit enablement request"}</Button>
        </div>
        <p className="mt-2 text-[11px] opacity-80">Or pick an approved tool below - you&apos;ll be asked to confirm, and the choice is recorded.</p>
      </Alert>
    </div>
  );
}
