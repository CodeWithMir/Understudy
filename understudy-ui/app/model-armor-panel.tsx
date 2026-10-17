"use client";

/* Mission Control > AI, security & account > Google Cloud Model Armor (7.2).
 * The admin turns the managed Model Armor screening on or off, points it at a template (project / location / id),
 * chooses fail-open or fail-closed, tests the connection with a harmless sentence and a textbook prompt injection,
 * and can create a template in their Google Cloud project. The built-in Model Armor stages always run as well. */
import React, { useCallback, useEffect, useState } from "react";
import { CheckCircle2, Cloud, Loader2, Plus, ShieldCheck, XCircle } from "lucide-react";
import { AdminApiError, adminApi, type ArmorTarget, type ArmorTest, type ModelArmorStatus } from "../lib/admin";
import { Badge, Card, cx } from "./wizard/ui";

const FILTER_LABELS: Record<string, { name: string; hint: string }> = {
  pi_and_jailbreak: { name: "Prompt injection & jailbreak", hint: "the main protection for SOPs, payloads and recordings" },
  malicious_uri: { name: "Malicious URLs", hint: "known phishing / malware links in any text" },
  rai: { name: "Responsible AI", hint: "hate speech, harassment, dangerous and sexually explicit content" },
  sdp: { name: "Sensitive data (basic)", hint: "credit-card, bank-account and similar numbers - off by default: it also blocks payloads that legitimately contain them" },
};
const input = "w-full rounded-lg border border-slate-700 bg-slate-950 px-2.5 py-1.5 font-mono text-xs text-slate-100 placeholder:text-slate-400 focus:border-sky-500 focus:outline-none";

function TestResult({ t }: { t: ArmorTest }) {
  return (
    <div className={cx("rounded-lg border p-3 text-[11px]", t.ok ? "border-emerald-500/40 bg-emerald-500/5" : "border-rose-500/40 bg-rose-500/5")}
      role="status" data-testid="armor-test">
      <div className="flex items-center gap-2 font-semibold text-slate-100">
        {t.ok ? <CheckCircle2 className="h-4 w-4 text-emerald-400" /> : <XCircle className="h-4 w-4 text-rose-400" />}
        {t.ok ? "Connection test passed" : t.reachable ? "Reached Model Armor, but the verdicts are not as expected" : "Model Armor could not be reached"}
      </div>
      {t.clean && (
        <p className="mt-1.5 text-slate-300">Harmless business sentence: <b className={t.clean.blocked ? "text-rose-300" : "text-emerald-300"}>
          {t.clean.blocked ? `blocked (${t.clean.filters.join(", ")})` : "allowed"}</b> · {t.clean.ms} ms</p>
      )}
      {t.attack && (
        <p className="text-slate-300">Textbook prompt injection: <b className={t.attack.blocked ? "text-emerald-300" : "text-rose-300"}>
          {t.attack.blocked ? `blocked by ${t.attack.filters.join(", ")}` : "NOT blocked"}</b> · {t.attack.ms} ms</p>
      )}
      {t.error && <p className="mt-1 break-words text-rose-200">{t.error}</p>}
      {t.fix && <p className="mt-1 text-slate-300">Fix: <code className="break-all rounded bg-slate-900 px-1 text-sky-200">{t.fix}</code></p>}
    </div>
  );
}

export function ModelArmorPanel() {
  const [st, setSt] = useState<ModelArmorStatus | null>(null);
  const [form, setForm] = useState<ArmorTarget>({ project: "", location: "", template_id: "" });
  const [enabled, setEnabled] = useState(false);
  const [failClosed, setFailClosed] = useState(false);
  const [filters, setFilters] = useState<string[]>([]);
  const [confidence, setConfidence] = useState("MEDIUM_AND_ABOVE");
  const [test, setTest] = useState<ArmorTest | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [msg, setMsg] = useState<{ ok: boolean; text: string; fix?: string | null } | null>(null);
  const [needsForce, setNeedsForce] = useState(false);
  const [showCreate, setShowCreate] = useState(false);

  const adopt = useCallback((s: ModelArmorStatus, resetForm: boolean) => {
    setSt(s);
    if (resetForm) {
      setForm({ project: s.project || s.suggested.project, location: s.location || s.suggested.location,
        template_id: s.template_id || s.suggested.template_id });
      setEnabled(s.enabled);
      setFailClosed(s.fail_closed);
      setFilters(s.default_filters);
    }
    if (s.test) setTest(s.test);
  }, []);
  useEffect(() => {
    adminApi.modelArmor().then((s) => adopt(s, true)).catch((e) => setMsg({ ok: false, text: (e as Error).message }));
  }, [adopt]);

  const run = async (key: string, fn: () => Promise<ModelArmorStatus>, ok: (s: ModelArmorStatus) => string, resetForm = false) => {
    setBusy(key); setMsg(null);
    try {
      const s = await fn();
      adopt(s, resetForm);
      setNeedsForce(false);
      setMsg({ ok: true, text: ok(s) });
    } catch (e) {
      const err = e as AdminApiError;
      const d = err.details as { needs_force?: boolean; test?: ArmorTest } | undefined;
      if (d?.needs_force) { setNeedsForce(true); if (d.test) setTest(d.test); }
      setMsg({ ok: false, text: err.message });
    } finally { setBusy(null); }
  };
  const save = (force = false) => run("save", () => adminApi.saveModelArmor({ ...form, enabled, fail_closed: failClosed, force }),
    (s) => s.enabled ? `Saved: every input is now screened by Google Cloud Model Armor (${s.fail_closed ? "fail-closed" : "fail-open"}).`
      : "Saved: Google Cloud Model Armor is off - the built-in Model Armor stages still screen every input.", true);
  const filled = form.project.trim() && form.location.trim() && form.template_id.trim();
  const dirty = !!st && (enabled !== st.enabled || failClosed !== st.fail_closed || (st.template
    ? form.project !== st.project || form.location !== st.location || form.template_id !== st.template_id : enabled));
  const resource = filled ? `projects/${form.project.trim()}/locations/${form.location.trim()}/templates/${form.template_id.trim()}` : "";
  const toggleFilter = (f: string) => setFilters(filters.includes(f) ? filters.filter((x) => x !== f) : [...filters, f]);

  return (
    <Card title="Google Cloud Model Armor" icon={<Cloud className="h-4 w-4 text-sky-400" />}
      right={st ? <Badge tone={st.enabled ? "green" : "slate"}>{st.enabled ? `ON · ${st.fail_closed ? "fail-closed" : "fail-open"}` : "OFF · built-in only"}</Badge> : null}>
      <div className="space-y-4" data-testid="model-armor-panel">
        <p className="text-xs text-slate-400">
          Understudy&apos;s built-in Model Armor (clean-up, attack patterns, Gemini classifier) always screens every input: SOP text,
          payload values, URLs, tool names, video transcripts and previous-session context. Turn this on to <b className="text-slate-200">also</b> send
          each of those texts to Google Cloud Model Armor (<code>sanitizeUserPrompt</code>) first. Only the text being checked is sent; the server
          signs in with its Google Cloud identity{st?.service_account ? <> (<code>{st.service_account}</code>)</> : null}, no key is stored.
        </p>
        {st && st.source === "env" && (
          <p className="rounded-lg border border-sky-500/30 bg-sky-500/5 p-2 text-[11px] text-sky-100">
            Currently set by the environment (<code>UNDERSTUDY_MODEL_ARMOR_TEMPLATE</code>). Saving here overrides it for every instance.</p>
        )}

        <label className="flex items-center gap-2 text-sm text-slate-100">
          <input type="checkbox" className="h-4 w-4 accent-emerald-500" checked={enabled} onChange={(e) => setEnabled(e.target.checked)}
            aria-label="Screen every input with Google Cloud Model Armor" />
          Screen every input with Google Cloud Model Armor
        </label>

        <div className="grid gap-2 sm:grid-cols-3">
          <label className="space-y-1 text-[11px] text-slate-400">Project id
            <input className={input} value={form.project} onChange={(e) => setForm({ ...form, project: e.target.value })} placeholder="my-gcp-project" aria-label="Model Armor project id" />
          </label>
          <label className="space-y-1 text-[11px] text-slate-400">Location
            <input className={input} value={form.location} onChange={(e) => setForm({ ...form, location: e.target.value })} placeholder="us-central1" aria-label="Model Armor location" />
          </label>
          <label className="space-y-1 text-[11px] text-slate-400">Template id
            <input className={input} value={form.template_id} onChange={(e) => setForm({ ...form, template_id: e.target.value })} placeholder="understudy-inputs" aria-label="Model Armor template id" />
          </label>
        </div>
        {resource && <p className="break-all text-[11px] text-slate-400">Template: <code className="text-slate-200">{resource}</code></p>}

        <div role="radiogroup" aria-label="When Model Armor can't be reached" className="space-y-1.5 text-xs">
          <div className="text-[11px] font-semibold uppercase tracking-wide text-slate-400">When Google Cloud Model Armor can&apos;t be reached</div>
          {[
            { v: false, t: "Fail open (recommended)", d: "the built-in stages still protect the input and a warning is recorded" },
            { v: true, t: "Fail closed", d: "block every user input until Model Armor answers again" },
          ].map((o) => (
            <label key={String(o.v)} className={cx("flex cursor-pointer items-start gap-2 rounded-lg border p-2",
              failClosed === o.v ? "border-sky-500/50 bg-sky-500/5" : "border-slate-800")}>
              <input type="radio" name="armor-fail" className="mt-0.5 accent-sky-500" checked={failClosed === o.v} onChange={() => setFailClosed(o.v)} />
              <span><b className="text-slate-100">{o.t}</b> <span className="text-slate-400">- {o.d}</span></span>
            </label>
          ))}
        </div>

        <div className="flex flex-wrap gap-2">
          <button type="button" disabled={!filled || busy !== null}
            onClick={() => { void run("test", () => adminApi.testModelArmor(form), (s) => s.test?.ok ? "Connection test passed." : "Connection test finished - see the result."); }}
            className="inline-flex items-center gap-1.5 rounded-lg border border-slate-700 bg-slate-800 px-3 py-1.5 text-xs text-slate-100 hover:text-white disabled:opacity-50">
            {busy === "test" ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <ShieldCheck className="h-3.5 w-3.5" />}Test connection</button>
          <button type="button" disabled={(enabled && !filled) || busy !== null || !dirty} onClick={() => { void save(false); }}
            className="inline-flex items-center gap-1.5 rounded-lg bg-sky-500 px-3 py-1.5 text-xs font-bold text-slate-950 hover:bg-sky-400 disabled:opacity-50">
            {busy === "save" && <Loader2 className="h-3.5 w-3.5 animate-spin" />}Save</button>
          {needsForce && (
            <button type="button" disabled={busy !== null} onClick={() => { void save(true); }}
              className="rounded-lg border border-rose-500/50 px-3 py-1.5 text-xs text-rose-200 hover:bg-rose-500/10">Save anyway (blocks inputs until it&apos;s reachable)</button>
          )}
          {st?.source === "admin" && st.env_template && (
            <button type="button" disabled={busy !== null}
              onClick={() => { void run("reset", () => adminApi.resetModelArmor(), () => "Reset: the environment setting applies again.", true); }}
              className="rounded-lg px-3 py-1.5 text-xs text-slate-400 hover:bg-slate-800">Use the environment setting</button>
          )}
          <button type="button" onClick={() => setShowCreate(!showCreate)}
            className="ml-auto inline-flex items-center gap-1 text-xs text-sky-400 hover:underline"><Plus className="h-3.5 w-3.5" />Create template in Google Cloud</button>
        </div>
        {enabled && !st?.enabled && <p className="text-[11px] text-slate-400">Saving runs the connection test first.</p>}
        {msg && <p className={cx("text-[11px]", msg.ok ? "text-emerald-300" : "text-rose-300")} role={msg.ok ? "status" : "alert"}>{msg.text}</p>}
        {test && <TestResult t={test} />}

        {showCreate && (
          <div className="space-y-2 rounded-lg border border-slate-800 bg-slate-950/60 p-3 text-xs" data-testid="armor-create">
            <p className="text-[11px] text-slate-400">Creates <code className="text-slate-200">{resource || "the template above"}</code> with these filters.
              The server&apos;s identity needs <code>roles/modelarmor.admin</code> for this (and <code>roles/modelarmor.user</code> to screen), and the
              project needs the Model Armor API (<code>modelarmor.googleapis.com</code>) enabled - <code>scripts/gcp_setup.sh --model-armor</code> does all of it.</p>
            {(st?.filters_available || []).map((f) => (
              <label key={f} className="flex items-start gap-2">
                <input type="checkbox" className="mt-0.5 accent-sky-500" checked={filters.includes(f)} onChange={() => toggleFilter(f)} />
                <span><b className="text-slate-100">{FILTER_LABELS[f]?.name || f}</b> <span className="text-slate-400">- {FILTER_LABELS[f]?.hint}</span></span>
              </label>
            ))}
            <label className="flex items-center gap-2 text-[11px] text-slate-400">Confidence
              <select value={confidence} onChange={(e) => setConfidence(e.target.value)} aria-label="Filter confidence"
                className="rounded border border-slate-700 bg-slate-900 px-2 py-1 text-xs text-slate-100">
                <option value="LOW_AND_ABOVE">low and above (strictest)</option>
                <option value="MEDIUM_AND_ABOVE">medium and above (recommended)</option>
                <option value="HIGH">high only (fewest false alarms)</option>
              </select>
            </label>
            <button type="button" disabled={!filled || !filters.length || busy !== null}
              onClick={() => {
                void run("create", () => adminApi.createModelArmorTemplate({ ...form, filters, confidence }),
                  (s) => s.create?.ok ? (s.create.already_existed ? "That template already exists - test it, then turn screening on and save."
                    : "Template created. Test it, then turn screening on and save.") : `Not created: ${s.create?.fix || s.create?.error}`);
              }}
              className="inline-flex items-center gap-1.5 rounded-lg bg-emerald-500 px-3 py-1.5 text-xs font-bold text-slate-950 hover:bg-emerald-400 disabled:opacity-50">
              {busy === "create" && <Loader2 className="h-3.5 w-3.5 animate-spin" />}Create template</button>
          </div>
        )}

        {st?.enabled && (
          <dl className="grid grid-cols-3 gap-2 rounded-lg border border-slate-800 bg-slate-950/60 p-2 text-[11px]" aria-label="Model Armor activity since start">
            <div><dt className="text-slate-400">Screened</dt><dd className="font-semibold text-slate-100">{st.stats.screened}</dd></div>
            <div><dt className="text-slate-400">Blocked</dt><dd className="font-semibold text-rose-300">{st.stats.blocked}</dd></div>
            <div><dt className="text-slate-400">Unreachable</dt><dd className="font-semibold text-amber-300" title={st.stats.last_error || undefined}>{st.stats.unavailable}</dd></div>
          </dl>
        )}
        {st?.source === "admin" && st.updated_by && (
          <p className="text-[11px] text-slate-400">Last changed by {st.updated_by} · {String(st.updated_at || "").slice(0, 16).replace("T", " ")} UTC</p>
        )}
      </div>
    </Card>
  );
}
