"use client";

/* 9.8 / 9.9: Step 1 - the automation difficulty assessment (ten parameters Gemini chose from the recording, grouped by
 * the enterprise categories; the user can tune values / weights or mark one as not applicable), the decision -
 * traditional or agentic automation, nothing else - with the tools of each kind, and Step 4's Gemini key for agentic runs. */
import React, { useMemo, useState } from "react";
import { Bot, Calculator, Gauge, RotateCcw, Sparkles, Undo2, Wrench } from "lucide-react";
import { wizardApi, type DifficultyParam, type FrameworkOption, type RoutingTool, type WizardSession } from "../../lib/wizard";
import { Alert, Badge, Button, Card, Fold, SourceBadge, cx, inputCls } from "./ui";
import type { Act } from "./steps";

const LEVEL_TONE: Record<string, "green" | "sky" | "amber" | "red" | "slate"> = { Simple: "green", Moderate: "sky", Complex: "amber", "Very complex": "red" };
const BAR: Record<string, string> = { green: "bg-emerald-400", sky: "bg-sky-400", amber: "bg-amber-400", red: "bg-rose-400", slate: "bg-slate-400" };
const valueTone = (v: number) => (v >= 7 ? "text-rose-300" : v >= 4 ? "text-amber-300" : "text-emerald-300");

type Draft = Record<string, { value: number; weight: number; applicable: boolean }>;

function draftOf(params: DifficultyParam[]): Draft {
  return Object.fromEntries(params.map((p) => [p.key, { value: p.effective_value, weight: p.effective_weight, applicable: p.effective_applicable }]));
}

/** The score the server will compute - shown live while the user edits (the server's number is the one that counts).
 * Parameters that don't apply drop out and their weight is redistributed. */
function preview(params: DifficultyParam[], d: Draft): number | null {
  const live = params.filter((p) => d[p.key].applicable && d[p.key].weight > 0);
  const tw = live.reduce((a, p) => a + d[p.key].weight, 0);
  if (tw <= 0) return null;
  let score = Math.cbrt(live.reduce((a, p) => a + d[p.key].weight * d[p.key].value ** 3, 0) / tw);
  for (const p of live) if (p.blocker && d[p.key].value >= 7) score = Math.max(score, 0.8 * d[p.key].value);
  return Math.round(Math.min(10, score) * 10) / 10;
}

const isDirty = (p: DifficultyParam, c?: Draft[string]) =>
  !!c && (c.value !== p.effective_value || c.weight !== p.effective_weight || c.applicable !== p.effective_applicable);

export function DifficultyCard({ session: s, act, busyKey, onPick }: {
  session: WizardSession; act: Act; busyKey: string | null; onPick: (f: FrameworkOption) => void;
}) {
  const d = s.difficulty;
  const [draft, setDraft] = useState<Draft>(() => (d ? draftOf(d.parameters) : {}));
  const serverKey = d ? d.parameters.map((p) => `${p.key}:${p.effective_value}:${p.effective_weight}:${p.effective_applicable}`).join("|") : "";
  const [seen, setSeen] = useState(serverKey);
  if (seen !== serverKey) {                 // the server recalculated: start editing from its numbers
    setSeen(serverKey);
    if (d) setDraft(draftOf(d.parameters));
  }
  const changed = useMemo(() => (d ? d.parameters.filter((p) => isDirty(p, draft[p.key])) : []), [d, draft]);
  if (!d) return null;
  const tone = LEVEL_TONE[d.level] || "slate";
  const live = changed.length ? preview(d.parameters, draft) : d.score;
  const allZero = live === null;
  const set = (key: string, patch: Partial<Draft[string]>) => setDraft((x) => ({ ...x, [key]: { ...x[key], ...patch } }));
  const recalc = () => act(() => wizardApi.recalculateDifficulty(s.session_id,
    changed.map((p) => ({ key: p.key, value: draft[p.key].value, weight: draft[p.key].weight, applicable: draft[p.key].applicable }))), "difficulty");
  const reset = () => act(() => wizardApi.recalculateDifficulty(s.session_id, [], true), "difficulty-reset");
  const byCat = d.categories.map((c) => ({ ...c, params: d.parameters.filter((p) => p.category === c.name) }));
  const other = d.parameters.filter((p) => !d.categories.some((c) => c.name === p.category));
  if (other.length) byCat.push({ name: "Other", weight_pct: 0, params: other });
  return (
    <div data-testid="difficulty-card" className="space-y-5">
      <Card title="Automation difficulty assessment" foldId="difficulty" icon={<Gauge className="h-4 w-4 text-sky-400" />} defaultOpen
        summary={<span className="text-[11px] text-slate-400">{d.score}/10 · {d.level} · recommends {d.recommendation.approach} automation</span>}
        right={<div className="flex flex-wrap items-center justify-end gap-1.5">
          {d.adjusted && <Badge tone="violet">tuned by you</Badge>}
          <Badge tone={tone}>{d.level}</Badge>
        </div>}>
        <div className="flex flex-wrap items-end gap-x-6 gap-y-3">
          <div>
            <div className="text-[11px] uppercase tracking-wide text-slate-500">Difficulty</div>
            <div className="text-4xl font-bold tabular-nums text-white" data-testid="difficulty-score">{d.score}<span className="text-lg font-semibold text-slate-500">/10</span></div>
          </div>
          <div className="min-w-[12rem] flex-1">
            <div className="h-2.5 overflow-hidden rounded-full bg-slate-800" role="meter" aria-valuemin={0} aria-valuemax={10} aria-valuenow={d.score} aria-label="Automation difficulty">
              <div className={cx("h-full rounded-full", BAR[tone])} style={{ width: `${d.score * 10}%` }} />
            </div>
            <div className="mt-1 flex justify-between text-[10px] text-slate-500"><span>0 easy</span><span>5</span><span>10 very hard</span></div>
          </div>
          <SourceBadge source={d.agent_source} model={d.model} />
        </div>
        <p className="mt-3 text-sm leading-relaxed text-slate-200">{d.summary}</p>
        <p className="mt-1 text-[11px] text-slate-400" data-testid="difficulty-chosen-by">
          {d.chosen_by === "ai"
            ? <><Sparkles className="mr-1 inline h-3 w-3 text-sky-400" />Gemini watched your recording and chose these ten parameters for this process, with their weights - like an automation CoE assessing a use case.</>
            : <>No AI for this assessment: Understudy chose the ten most relevant parameters from what it measured, weighted by the enterprise categories.</>}
        </p>
        {d.adjusted && d.assessed_score !== d.score && (
          <p className="mt-1 text-[11px] text-slate-400">Assessed score before your changes: {d.assessed_score}/10.</p>
        )}
        {d.floor && <p className="mt-2 text-xs text-amber-300">{d.floor.why}</p>}

        <div className="mt-4 overflow-x-auto">
          <table className="w-full min-w-[720px] text-xs" data-testid="difficulty-params">
            <thead>
              <tr className="text-left text-[11px] text-slate-500">
                <th className="py-1.5 pr-3 font-medium">Parameter - and why it matters here</th>
                <th className="py-1.5 pr-3 font-medium">Applies</th>
                <th className="py-1.5 pr-3 font-medium">Value (0-10)</th>
                <th className="py-1.5 pr-3 font-medium">Weight</th>
                <th className="py-1.5 font-medium">Contribution</th>
              </tr>
            </thead>
            {byCat.map((c) => (
              <tbody key={c.name}>
                <tr className="border-t border-slate-700">
                  <td colSpan={5} className="pt-3 pb-1 text-[11px] font-semibold uppercase tracking-wide text-slate-400">
                    {c.name} <span className="font-normal normal-case text-slate-500">· {c.weight_pct}% of the weight</span>
                  </td>
                </tr>
                {c.params.map((p) => {
                  const cur = draft[p.key] || { value: p.effective_value, weight: p.effective_weight, applicable: p.effective_applicable };
                  const tuned = p.user_value !== null || p.user_weight !== null || p.user_applicable !== null;
                  const dirty = isDirty(p, cur);
                  return (
                    <tr key={p.key} className={cx("border-t border-slate-800/70 align-top", dirty && "bg-sky-500/5", !cur.applicable && "opacity-50")} data-testid={`param-${p.key}`}>
                      <td className="py-2 pr-3">
                        <div className="flex flex-wrap items-center gap-1.5">
                          <span className="font-semibold text-slate-100" title={p.measures}>{p.name}</span>
                          {p.source === "ai" && !p.library_key && <Badge tone="sky" title="Gemini defined this parameter for this process">process-specific</Badge>}
                          {p.video_time && <Badge tone="slate" title="Where the recording shows it">▶ {p.video_time}</Badge>}
                          {p.blocker && <Badge tone="red" title="A value of 7+ is a blocker: it lifts the overall score">blocker</Badge>}
                          {tuned && <Badge tone="violet">tuned</Badge>}
                        </div>
                        {p.why && <div className="mt-0.5 text-[11px] text-slate-300">{p.why}</div>}
                        <div className="mt-0.5 text-[11px] text-slate-500">{p.evidence.join(" · ")}</div>
                        <div className="mt-0.5 text-[10px] text-slate-500">
                          {p.measured_value !== null ? `measured ${p.measured_value} · ` : ""}{p.ai_value !== null ? `AI ${p.ai_value} · ` : ""}assessed {p.value}
                        </div>
                      </td>
                      <td className="py-2 pr-3">
                        <input type="checkbox" checked={cur.applicable} aria-label={`${p.name} applies`} className="h-4 w-4 accent-sky-400"
                          title="Untick if it doesn't apply - its weight goes to the others"
                          onChange={(e) => set(p.key, { applicable: e.target.checked })} />
                      </td>
                      <td className="py-2 pr-3">
                        <div className="flex items-center gap-2">
                          <input type="range" min={0} max={10} step={1} value={cur.value} aria-label={`${p.name} value`} disabled={!cur.applicable}
                            onChange={(e) => set(p.key, { value: Number(e.target.value) })} className="w-24 accent-sky-400" />
                          <span className={cx("w-6 text-right font-semibold tabular-nums", valueTone(cur.value))}>{cur.value}</span>
                        </div>
                      </td>
                      <td className="py-2 pr-3">
                        <div className="flex items-center gap-1.5">
                          <input type="number" min={0} max={100} step={1} value={cur.weight} aria-label={`${p.name} weight`} disabled={!cur.applicable}
                            onChange={(e) => set(p.key, { weight: Math.max(0, Math.min(100, Math.round(Number(e.target.value) || 0))) })}
                            className={cx(inputCls, "w-16 min-w-[3.75rem] shrink-0 px-2 py-1 text-xs tabular-nums")} />
                          <span className="text-[10px] text-slate-500">{p.weight_pct}%</span>
                        </div>
                      </td>
                      <td className="py-2">
                        <div className="flex items-center gap-2">
                          <div className="h-1.5 w-20 overflow-hidden rounded-full bg-slate-800">
                            <div className="h-full rounded-full bg-sky-400" style={{ width: `${Math.min(100, p.share_pct)}%` }} />
                          </div>
                          <span className="tabular-nums text-slate-300">{p.contribution.toFixed(2)}</span>
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            ))}
          </table>
        </div>
        <p className="mt-2 text-[10px] text-slate-500">{d.formula}. The contributions add up to the score.</p>

        <div className="mt-3 flex flex-wrap items-center gap-2 rounded-lg border border-slate-800 bg-slate-950/50 p-3" aria-live="polite">
          <Calculator className="h-4 w-4 text-sky-400" aria-hidden />
          <span className="text-xs text-slate-300">
            {changed.length
              ? allZero ? "Keep at least one parameter that applies, with a weight above 0."
                : <>You changed {changed.length} parameter{changed.length === 1 ? "" : "s"} - new score about <b className="text-white">{live}/10</b>. Recalculate to update the traditional / agentic recommendation too.</>
              : "The AI may misjudge your process: move a value or weight, or untick a parameter that doesn't apply, then recalculate."}
          </span>
          <div className="ml-auto flex flex-wrap gap-2">
            {changed.length > 0 && <Button variant="ghost" onClick={() => setDraft(draftOf(d.parameters))}><Undo2 className="h-4 w-4" />Undo</Button>}
            {d.adjusted && !changed.length && (
              <Button variant="secondary" busy={busyKey === "difficulty-reset"} disabled={busyKey !== null} onClick={reset} data-testid="difficulty-reset">
                <RotateCcw className="h-4 w-4" />Back to assessed values</Button>
            )}
            <Button busy={busyKey === "difficulty"} disabled={!changed.length || allZero || busyKey !== null} onClick={recalc} data-testid="difficulty-recalc">
              <Calculator className="h-4 w-4" />Recalculate</Button>
          </div>
        </div>
        {d.history.length > 0 && (
          <Fold title={`Your changes (${d.history.length})`} foldId="difficulty-history" className="mt-3">
            <ul className="space-y-1 text-[11px] text-slate-400">
              {d.history.slice().reverse().map((h, i) => (
                <li key={i}><b className="text-slate-300">{h.score_before} → {h.score_after}</b> · {h.changes.join("; ") || "no change"}</li>
              ))}
            </ul>
          </Fold>
        )}
      </Card>
      <RoutingCard session={s} busyKey={busyKey} onPick={onPick} />
    </div>
  );
}

const FIT_TONE: Record<string, "green" | "amber" | "red" | "slate"> = { fit: "green", mismatch: "amber", "not chosen": "slate" };

function RoutingCard({ session: s, busyKey, onPick }: { session: WizardSession; busyKey: string | null; onPick: (f: FrameworkOption) => void }) {
  const r = s.difficulty!.recommendation;
  const agentic = r.approach === "agentic";
  const opt = (id: string) => s.framework_options.find((o) => o.id === id);
  return (
    <Card title="Traditional or agentic automation?" foldId="routing" icon={<Bot className="h-4 w-4 text-violet-400" />} defaultOpen
      summary={<span className="text-[11px] text-slate-400">recommends {r.approach}</span>}
      right={<SourceBadge source={r.source} model={r.model} />}>
      <div className={cx("rounded-xl border p-4", agentic ? "border-violet-500/50 bg-violet-500/10" : "border-sky-500/50 bg-sky-500/10")} data-testid="routing-recommendation">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-[11px] font-semibold uppercase tracking-wide text-slate-400">Recommended</span>
          <span className={cx("text-lg font-bold", agentic ? "text-violet-200" : "text-sky-200")} data-testid="routing-approach">
            {agentic ? "Agentic automation" : "Traditional automation"}</span>
          <Badge tone={r.confidence === "high" ? "green" : r.confidence === "medium" ? "sky" : "amber"}>{r.confidence} confidence</Badge>
        </div>
        <p className="mt-1 text-xs text-slate-400" data-testid="routing-meaning">{r.meaning}</p>
        <p className="mt-2 text-sm text-slate-200">{r.summary}</p>
        {r.why.length > 0 && (
          <div className="mt-2">
            <div className="text-[11px] font-semibold text-slate-400">Why {agentic ? "agentic" : "traditional"} for this workflow</div>
            <ul className="mt-1 list-disc space-y-0.5 pl-5 text-xs text-slate-200">{r.why.map((w) => <li key={w}>{w}</li>)}</ul>
          </div>
        )}
        {r.against.length > 0 && (
          <div className="mt-2">
            <div className="text-[11px] font-semibold text-slate-400">What speaks for {agentic ? "traditional" : "agentic"}</div>
            <ul className="mt-1 list-disc space-y-0.5 pl-5 text-xs text-slate-400">{r.against.map((w) => <li key={w}>{w}</li>)}</ul>
          </div>
        )}
        {r.notes.map((n) => <p key={n} className="mt-2 text-xs text-amber-300">{n}</p>)}
        {s.difficulty!.solution_fit && s.difficulty!.solution_fit.status !== "not chosen" && (
          <div className="mt-3" data-testid="solution-fit">
            <Badge tone={FIT_TONE[s.difficulty!.solution_fit.status] || "slate"}>{s.difficulty!.solution_fit.status === "fit" ? "your tool matches" : "your tool is the other kind"}</Badge>
            <span className="ml-2 text-xs text-slate-300">{s.difficulty!.solution_fit.message}</span>
          </div>
        )}
        {r.source !== "LOCAL_HEURISTIC" && r.rules_approach !== r.approach && (
          <p className="mt-2 text-[11px] text-slate-400">Understudy&apos;s rule check alone would pick {r.rules_approach} - the AI weighed the workflow differently. You decide.</p>
        )}
      </div>
      <div className="mt-4 grid gap-4 lg:grid-cols-2">
        <ToolColumn kind="traditional" title="Traditional tools" recommended={!agentic} strengths={r.traditional.strengths}
          tools={r.traditional.tools} opt={opt} session={s} busyKey={busyKey} onPick={onPick} />
        <ToolColumn kind="agentic" title="Agentic tools" recommended={agentic} strengths={r.agentic.strengths} costs={r.agentic.costs}
          tools={r.agentic.tools} opt={opt} session={s} busyKey={busyKey} onPick={onPick} />
      </div>
    </Card>
  );
}

function ToolColumn({ kind, title, recommended, strengths, costs, tools, opt, session: s, busyKey, onPick }: {
  kind: "traditional" | "agentic"; title: string; recommended: boolean; strengths: string[]; costs?: string[]; tools: RoutingTool[];
  opt: (id: string) => FrameworkOption | undefined; session: WizardSession; busyKey: string | null; onPick: (f: FrameworkOption) => void;
}) {
  return (
    <div className={cx("rounded-xl border p-4", recommended ? (kind === "agentic" ? "border-violet-500/50" : "border-sky-500/50") : "border-slate-800")}
      data-testid={`routing-${kind}`}>
      <div className="flex items-center gap-2">
        {kind === "agentic" ? <Bot className="h-4 w-4 text-violet-400" /> : <Wrench className="h-4 w-4 text-sky-400" />}
        <span className="text-sm font-semibold text-white">{title}</span>
        {recommended && <Badge tone={kind === "agentic" ? "violet" : "sky"}>recommended</Badge>}
      </div>
      <ul className="mt-2 space-y-0.5 text-[11px] text-emerald-300">{strengths.map((x) => <li key={x}>+ {x}</li>)}</ul>
      {costs && <ul className="mt-1 space-y-0.5 text-[11px] text-amber-300">{costs.map((x) => <li key={x}>- {x}</li>)}</ul>}
      <div className="mt-3 space-y-2">
        {tools.length === 0 && <p className="text-xs text-slate-500">No {kind} tool is available for this session.</p>}
        {tools.map((t) => {
          const o = opt(t.tool_id);
          const selected = s.framework?.id === t.tool_id;
          const locked = o?.allowed === false;
          const unusable = !o || o.blocked || o.restricted;
          return (
            <div key={t.tool_id} className={cx("flex items-start gap-3 rounded-lg border p-2.5", selected ? "border-sky-400 bg-sky-500/10" : "border-slate-800 bg-slate-950/50")}
              data-testid={`route-tool-${t.tool_id}`}>
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-1.5">
                  <span className="text-xs font-semibold text-slate-100">{t.name}</span>
                  <Badge tone={t.fit === "good" ? "green" : t.fit === "ok" ? "sky" : "amber"}>fit: {t.fit}</Badge>
                  {o && !locked && !unusable && <Badge tone={o.builtin ? "violet" : "green"}>{o.builtin ? "built in - no setup" : "approved"}</Badge>}
                  {locked && !unusable && <Badge tone="amber">{o?.access_request ? "requested" : "needs admin approval"}</Badge>}
                </div>
                <p className="mt-0.5 text-[11px] text-slate-400">{t.why}</p>
              </div>
              <Button variant={selected ? "success" : "secondary"} className="shrink-0 px-3 py-1 text-xs"
                disabled={busyKey !== null || !!unusable || selected} onClick={() => o && onPick(o)}
                data-testid={`use-${t.tool_id}`}>{selected ? "Selected" : locked ? "Request" : "Use"}</Button>
            </div>
          );
        })}
      </div>
    </div>
  );
}

/* Step 4 (9.9): an agentic run uses the Gemini your admin set up - nothing for the user to enter. */
export function AgentAiNote({ session: s }: { session: WizardSession }) {
  const ai = s.agent_ai;
  if (!s.agentic || !ai) return null;
  return ai.available ? (
    <p className="mb-3 flex flex-wrap items-center gap-1.5 text-xs text-slate-400" data-testid="agent-ai-note">
      <Bot className="h-3.5 w-3.5 text-violet-400" aria-hidden />
      The agent asks your organisation&apos;s Gemini at each step
      <Badge tone="violet">{ai.platform}{ai.model ? ` · ${ai.model}` : ""}</Badge>
      - set up by your admin; the run holds no API key.
    </p>
  ) : (
    <div className="mb-3" data-testid="agent-ai-note"><Alert tone="amber" title="Gemini isn't set up yet">{ai.reason}</Alert></div>
  );
}
