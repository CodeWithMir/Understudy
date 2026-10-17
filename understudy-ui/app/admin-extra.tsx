"use client";

/**
 * Mission Control: login screen, account (change password), learned skills and code health panels.
 */
import React, { useCallback, useEffect, useState } from "react";
import {
  AlertTriangle, BookOpen, Bot, CheckCircle2, ChevronDown, ChevronRight, Eye, EyeOff, FileCode2, KeyRound, Lock, LogIn, RefreshCw,
  RotateCcw, ShieldCheck, Trash2, UserCog, Wrench, XCircle,
} from "lucide-react";
import {
  AdminApiError, adminApi, setAdminToken, type AuthStatus, type CodeFinding, type CodeHealthState, type SkillDetail,
  type EnvFinding, type SkillSummary, type RecallList, type CodeChange, type LineChange,
} from "../lib/admin";
import { Badge, Button, Card, cx } from "./wizard/ui";
import { ThemeToggle } from "./theme-toggle";

/** Password field with a show/hide (eye) toggle. */
function PasswordInput({ value, onChange, placeholder, autoComplete, id, className, invalid }: {
  value: string; onChange: (v: string) => void; placeholder?: string; autoComplete?: string; id?: string; className?: string; invalid?: boolean;
}) {
  const [show, setShow] = useState(false);
  return (
    <div className={cx("relative", className)}>
      <input id={id} className={cx(input, "pr-10", invalid && "border-rose-500")} type={show ? "text" : "password"} value={value}
        placeholder={placeholder} autoComplete={autoComplete} onChange={(e) => onChange(e.target.value)} />
      <button type="button" onClick={() => setShow(!show)} aria-label={show ? "Hide password" : "Show password"}
        title={show ? "Hide password" : "Show password"}
        className="absolute right-2 top-1/2 -translate-y-1/2 rounded p-1 text-slate-400 hover:text-slate-100">
        {show ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
      </button>
    </div>
  );
}

const input = "w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 placeholder-slate-400 focus:border-sky-500 focus:outline-none";

/* ------------------------------------------------------------------------------------------------ */
export function LoginScreen({ onLoggedIn }: { onLoggedIn: (defaultPassword: boolean) => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setErr(null);
    try {
      const r = await adminApi.login(username, password);
      setAdminToken(r.token);
      onLoggedIn(r.default_password);
    } catch (ex) {
      setErr((ex as AdminApiError).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <main id="main" className="relative flex min-h-screen items-center justify-center bg-slate-950 px-4 text-slate-100">
      <div className="absolute right-4 top-4"><ThemeToggle /></div>
      <form onSubmit={submit} aria-label="Mission Control sign in" className="w-full max-w-sm space-y-4 rounded-2xl border border-slate-800 bg-slate-900/80 p-7 shadow-2xl">
        <div className="flex items-center gap-3">
          <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-gradient-to-br from-sky-400 to-emerald-400 font-black text-slate-950">U</div>
          <div>
            <h1 className="text-lg font-bold text-white">Mission Control</h1>
            <p className="text-xs text-slate-400">Understudy admin console - please sign in</p>
          </div>
        </div>
        <label className="block space-y-1.5">
          <span className="text-xs font-semibold uppercase tracking-wide text-slate-400">Username</span>
          <input className={input} autoComplete="username" value={username} onChange={(e) => setUsername(e.target.value)} autoFocus />
        </label>
        <div className="space-y-1.5">
          <label htmlFor="login-password" className="block text-xs font-semibold uppercase tracking-wide text-slate-400">Password</label>
          <PasswordInput id="login-password" autoComplete="current-password" value={password} onChange={setPassword} />
        </div>
        {err && <div className="rounded-lg border border-rose-500/40 bg-rose-950/30 px-3 py-2 text-xs text-rose-200">{err}</div>}
        <Button type="submit" className="w-full" busy={busy} disabled={!username || !password}><LogIn className="h-4 w-4" />Sign in</Button>
        <p className="text-[11px] leading-relaxed text-slate-400">
          User <code>admin</code>. The password is generated on the first start and shown once; show it again with{" "}
          <code>python scripts/admin_password.py show</code> (Cloud Run: Secret Manager <code>understudy-admin-password</code>).
          A password changed here lasts until the backend restarts.
        </p>
      </form>
    </main>
  );
}

/* ------------------------------------------------------------------------------------------------ */
export function AccountPanel({ auth, onChanged }: { auth: AuthStatus | null; onChanged: (a: AuthStatus) => void }) {
  const [cur, setCur] = useState("");
  const [next, setNext] = useState("");
  const [again, setAgain] = useState("");
  const [user, setUser] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const mismatch = !!again && next !== again;
  const save = async () => {
    setBusy(true);
    setMsg(null);
    try {
      const a = await adminApi.changePassword(cur, next, user.trim() || undefined);
      onChanged(a);
      setCur(""); setNext(""); setAgain(""); setUser("");
      setMsg({ ok: true, text: "Saved. Other admin sessions were signed out. This lasts until the backend restarts." });
    } catch (e) {
      setMsg({ ok: false, text: (e as Error).message });
    } finally {
      setBusy(false);
    }
  };
  return (
    <Card title="Admin account" icon={<UserCog className="h-4 w-4 text-sky-400" />}
      right={auth?.default_password ? <Badge tone="amber">default password</Badge> : <Badge tone="green">custom password</Badge>}>
      <p className="mb-3 text-xs text-slate-400">
        Signed in as <b className="text-slate-200">{auth?.username}</b>. Set your own username and password here. For safety
        it is kept in memory only: when Understudy is stopped and started again (any start script, Docker or a new Cloud Run
        revision), it falls back to the saved password (<code>.env</code> locally, Secret Manager on Cloud Run).
      </p>
      <div className="grid gap-2 sm:grid-cols-2">
        <PasswordInput placeholder="Current password" autoComplete="current-password" value={cur} onChange={setCur} />
        <input className={input} placeholder={`New username (optional, now "${auth?.username || "admin"}")`} value={user} onChange={(e) => setUser(e.target.value)} />
        <PasswordInput placeholder="New password (8+ chars, letters and numbers)" autoComplete="new-password" value={next} onChange={setNext} />
        <PasswordInput placeholder="Repeat new password" autoComplete="new-password" value={again} onChange={setAgain} invalid={mismatch} />
      </div>
      {msg && <p className={cx("mt-2 text-xs", msg.ok ? "text-emerald-400" : "text-rose-400")}>{msg.text}</p>}
      <div className="mt-3 flex justify-end">
        <Button busy={busy} disabled={!cur || !next || next !== again} onClick={() => { void save(); }}><KeyRound className="h-4 w-4" />Change password</Button>
      </div>
    </Card>
  );
}

/* ------------------------------------------------------------------------------------------------ */
export function SkillsPanel() {
  const [data, setData] = useState<{ dir: string; skills: SkillSummary[] } | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [detail, setDetail] = useState<SkillDetail | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const load = useCallback(async () => {
    try { setData(await adminApi.skills()); setErr(null); } catch (e) { setErr((e as Error).message); }
  }, []);
  useEffect(() => {
    const t = setTimeout(() => { void load(); }, 0);
    const id = setInterval(() => { void load(); }, 8000);
    return () => { clearTimeout(t); clearInterval(id); };
  }, [load]);
  const show = async (id: string) => {
    if (open === id) { setOpen(null); return; }
    setOpen(id);
    setDetail(null);
    try { setDetail(await adminApi.skill(id)); } catch (e) { setErr((e as Error).message); }
  };
  return (
    <Card title="Learned skills" icon={<BookOpen className="h-4 w-4 text-emerald-400" />}
      right={<Badge tone="green">{data?.skills.length ?? 0} skill{data?.skills.length === 1 ? "" : "s"}</Badge>}>
      <p className="mb-3 text-xs text-slate-400">
        Every error Understudy hits is written down as a lesson; when self-healing fixes it, the fix is saved too and applied
        automatically before the next run with that tool or target. Skills are plain <code>SKILL.md</code> files on this
        machine{data?.dir ? <> in <code className="text-slate-300">{data.dir}</code></> : null}, and ship in each ZIP.
      </p>
      {err && <p className="mb-2 text-xs text-rose-400">{err}</p>}
      {!data?.skills.length ? (
        <div className="rounded-lg border border-slate-800 bg-slate-950/60 p-4 text-center text-xs text-slate-400">No lessons yet - they appear after the first failed run.</div>
      ) : (
        <div className="space-y-2">
          {data.skills.map((sk) => (
            <div key={sk.id} className="rounded-lg border border-slate-800 bg-slate-950/60">
              <div className="flex items-center gap-2 p-2.5 text-xs">
                <button type="button" onClick={() => { void show(sk.id); }} className="flex flex-1 items-center gap-2 text-left">
                  {open === sk.id ? <ChevronDown className="h-3.5 w-3.5 text-slate-400" /> : <ChevronRight className="h-3.5 w-3.5 text-slate-400" />}
                  <span className="font-medium text-slate-100">{sk.framework_name}</span>
                  <span className="text-slate-400">· {sk.scope_label}</span>
                </button>
                <Badge tone="green">{sk.learned} learned</Badge>
                {sk.lessons - sk.learned > 0 && <Badge tone="amber">{sk.lessons - sk.learned} unresolved</Badge>}
                <button type="button" title="Delete this skill" onClick={async () => {
                  if (!window.confirm(`Delete the learned skill ${sk.id}? Understudy will have to learn these lessons again.`)) return;
                  try { setData(await adminApi.deleteSkill(sk.id)); } catch (e) { setErr((e as Error).message); }
                }} className="rounded p-1 text-slate-400 hover:bg-slate-800 hover:text-rose-300"><Trash2 className="h-3.5 w-3.5" /></button>
              </div>
              {open === sk.id && (
                <div className="border-t border-slate-800 p-3">
                  {!detail ? <p className="text-xs text-slate-400">Loading...</p> : (
                    <pre className="max-h-96 overflow-auto whitespace-pre-wrap rounded-lg bg-slate-950 p-3 text-[11px] leading-relaxed text-slate-300">{detail.markdown}</pre>
                  )}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </Card>
  );
}

/* ------------------------------------------------------------------------------------------------ */
const PROMPT_NAMES: Record<string, string> = {
  WATCH_ANALYZE: "Process from a recording", WATCH_URLS: "Page addresses from a recording", WATCH_REFINE: "Correction applied", FRAMEWORK_RESOLVE: "Tool set-up",
  BUILD_PARAMETERIZE: "Payload parameters", BUILD_GENERATE_SUITE: "Generated code", GOVERN_EXTRACT_RULES: "SOP rules",
  GOVERN_COMPLIANCE_REVIEW: "Compliance second opinion", MODEL_ARMOR_CLASSIFIER: "Model Armor block",
  VERIFY_ASSESS: "Verification explanation", AUTOMATION_COMPLEXITY: "Automation complexity", VERIFY_SELF_HEAL: "Self-healing fix", CODE_HEALTH_FIX: "Code health fix",
  ENV_UPGRADE_ADVICE: "Upgrade advice",
};

export function RecallPanel() {
  const [data, setData] = useState<RecallList | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [detail, setDetail] = useState<Record<string, unknown> | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const load = useCallback(async () => {
    try { setData(await adminApi.recall()); setErr(null); } catch (e) { setErr((e as Error).message); }
  }, []);
  useEffect(() => {
    const t = setTimeout(() => { void load(); }, 0);
    const id = setInterval(() => { void load(); }, 8000);
    return () => { clearTimeout(t); clearInterval(id); };
  }, [load]);
  const show = async (id: string) => {
    if (open === id) { setOpen(null); return; }
    setOpen(id);
    setDetail(null);
    try { setDetail(await adminApi.recallItem(id)); } catch (e) { setErr((e as Error).message); }
  };
  const st = data?.stats;
  return (
    <Card title="Learned AI answers (used offline)" icon={<BookOpen className="h-4 w-4 text-violet-400" />}
      right={<div className="flex items-center gap-2">
        <Badge tone="violet">{st?.total ?? 0} learned</Badge>
        <Badge tone="slate">{st?.reuses ?? 0} re-used offline</Badge>
        {!!st?.total && <button type="button" className="rounded px-2 py-0.5 text-[11px] text-slate-400 hover:bg-slate-800 hover:text-rose-300"
          onClick={async () => {
            if (!window.confirm("Forget every learned AI answer? Offline, Understudy will use only its built-in rules until AI teaches it again.")) return;
            try { setData(await adminApi.clearRecall()); } catch (e) { setErr((e as Error).message); }
          }}>Forget all</button>}
      </div>}>
      <p className="mb-3 text-xs text-slate-400">
        Every answer the AI gives that passes validation is remembered here: the step that asked, a fingerprint of the question
        (normalised words, form fields, step shapes, numbers, the recording&apos;s hash) and the answer, without secret values.
        When there is no key, the quota is used up or Google can&apos;t be reached, Understudy compares the new problem with these
        and re-uses the closest one when it is similar enough, adapted to the new inputs and re-checked by every safety gate.
        Value-dependent answers are only re-used for the identical question, a different SOP limit or date is never re-used, and
        Model Armor only re-uses <i>block</i> verdicts.{st && !st.enabled ? <b className="text-amber-300"> Recall is switched off (UNDERSTUDY_RECALL=off).</b> : null}
      </p>
      {err && <p className="mb-2 text-xs text-rose-400">{err}</p>}
      {!data?.memories.length ? (
        <div className="rounded-lg border border-slate-800 bg-slate-950/60 p-4 text-center text-xs text-slate-400">Nothing learned yet - run a session with AI switched on.</div>
      ) : (
        <div className="space-y-2" data-testid="recall-list">
          {data.memories.map((m) => (
            <div key={m.id} className="rounded-lg border border-slate-800 bg-slate-950/60">
              <div className="flex items-center gap-2 p-2.5 text-xs">
                <button type="button" onClick={() => { void show(m.id); }} className="flex min-w-0 flex-1 items-center gap-2 text-left">
                  {open === m.id ? <ChevronDown className="h-3.5 w-3.5 shrink-0 text-slate-400" /> : <ChevronRight className="h-3.5 w-3.5 shrink-0 text-slate-400" />}
                  <Badge tone="sky">{m.step}</Badge>
                  <span className="font-medium text-slate-100">{PROMPT_NAMES[m.prompt_id] || m.prompt_id}</span>
                  <span className="truncate text-slate-400">· {m.label}</span>
                </button>
                <span className="hidden text-[11px] text-slate-400 sm:inline">{m.learned_with} · {String(m.created_at).slice(0, 10)}</span>
                <Badge tone={m.hits ? "violet" : "slate"}>{m.hits} re-use{m.hits === 1 ? "" : "s"}</Badge>
                <button type="button" title="Forget this answer" onClick={async () => {
                  try { setData(await adminApi.deleteRecall(m.id)); } catch (e) { setErr((e as Error).message); }
                }} className="rounded p-1 text-slate-400 hover:bg-slate-800 hover:text-rose-300"><Trash2 className="h-3.5 w-3.5" /></button>
              </div>
              {open === m.id && (
                <div className="border-t border-slate-800 p-3">
                  {!detail ? <p className="text-xs text-slate-400">Loading...</p> : (
                    <pre className="max-h-96 overflow-auto whitespace-pre-wrap rounded-lg bg-slate-950 p-3 text-[11px] leading-relaxed text-slate-300">{JSON.stringify({ question_fingerprint: detail.features, answer: detail.answer }, null, 2)}</pre>
                  )}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </Card>
  );
}

/* ------------------------------------------------------------------------------------------------ */
const STATUS_TONE: Record<string, "slate" | "green" | "amber" | "red" | "sky" | "violet"> = {
  open: "amber", proposed: "sky", applied: "green", resolved: "green", failed: "red", no_fix: "slate", protected: "violet",
  third_party: "slate", not_patchable: "slate", ignored: "slate",
};

function FindingRow({ f, act, ai }: { f: CodeFinding; act: (fn: () => Promise<CodeHealthState>) => void; ai: boolean }) {
  const [open, setOpen] = useState(f.status === "proposed");
  const p = f.proposal;
  const v = p?.verification;
  return (
    <div className="rounded-lg border border-slate-800 bg-slate-950/60 p-3 text-xs">
      <div className="flex flex-wrap items-center gap-2">
        <button type="button" onClick={() => setOpen(!open)} className="text-slate-400">{open ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}</button>
        <Badge tone={STATUS_TONE[f.status] || "slate"}>{f.status.replace("_", " ")}</Badge>
        <Badge tone="slate">{f.category}</Badge>
        <code className="text-slate-300">{f.file ? `${f.file}:${f.line}` : (f.external_file || "third-party").split(/[\\/]/).slice(-3).join("/")}</code>
        <span className="ml-auto flex gap-1.5">
          {["open", "failed", "no_fix"].includes(f.status) && ai && (
            <Button variant="secondary" className="px-2.5 py-1 text-xs" onClick={() => act(() => adminApi.codeHealthPropose(f.id))}><Bot className="h-3.5 w-3.5" />Ask AI to fix</Button>
          )}
          {f.status === "proposed" && (
            <Button variant="success" className="px-2.5 py-1 text-xs" onClick={() => act(() => adminApi.codeHealthApply(f.id))}><CheckCircle2 className="h-3.5 w-3.5" />Apply verified fix</Button>
          )}
          {["open", "failed", "no_fix", "proposed"].includes(f.status) && (
            <Button variant="ghost" className="px-2.5 py-1 text-xs" onClick={() => act(() => adminApi.codeHealthIgnore(f.id))}>Ignore</Button>
          )}
        </span>
      </div>
      <p className="mt-1.5 whitespace-pre-wrap text-slate-400">{f.message.trim().slice(0, 400)}</p>
      {f.status === "protected" && <p className="mt-1 text-violet-300">Protected file (orchestration layer) - reported, never changed.</p>}
      {open && p && (
        <div className="mt-2 space-y-2 border-t border-slate-800 pt-2">
          {p.error && <p className="text-rose-300">AI error: {p.error}</p>}
          {p.diagnosis && <p className="text-slate-300"><b>Diagnosis:</b> {p.diagnosis}</p>}
          {p.strategy && <p className="text-slate-400">Strategy: <b>{p.strategy.replace(/_/g, " ")}</b>{p.risk ? ` · risk ${p.risk}` : ""}{p.model ? ` · ${p.model}` : ""}</p>}
          {p.explanation && <p className="text-slate-400">{p.explanation}</p>}
          {v && (
            <div className="flex flex-wrap gap-1.5">
              {[["compiles", v.compiled], ["no unsafe calls", v.safe], ["warning gone", v.warning_gone], ["backend imports", v.import_ok]].map(([label, ok]) => (
                <Badge key={String(label)} tone={ok ? "green" : "red"}>{ok ? <CheckCircle2 className="h-3 w-3" /> : <XCircle className="h-3 w-3" />}{String(label)}</Badge>
              ))}
            </div>
          )}
          {v?.log?.map((l, i) => <p key={i} className="text-amber-300">{l}</p>)}
          {p.changes && p.changes.length > 0 && (
            <div className="text-slate-300"><b>{f.status === "applied" ? "What the AI changed" : "What would change"}</b> in <code>{f.file}</code>:
              <ChangeList changes={p.changes} /></div>
          )}
          {p.diff ? <DiffBlock diff={p.diff} /> : p.edits?.map((e, i) => (
            <div key={i} className="grid gap-1 md:grid-cols-2">
              <pre className="overflow-auto rounded bg-rose-950/30 p-2 text-[11px] text-rose-200">{e.find}</pre>
              <pre className="overflow-auto rounded bg-emerald-950/30 p-2 text-[11px] text-emerald-200">{e.replace}</pre>
            </div>
          ))}
          {f.status === "proposed" && <p className="text-sky-300">Nothing has been changed yet - this is only a proposal until you click <b>Apply verified fix</b>.</p>}
        </div>
      )}
    </div>
  );
}

function EnvRow({ f, act, ai }: { f: EnvFinding; act: (fn: () => Promise<CodeHealthState>) => void; ai: boolean }) {
  const [open, setOpen] = useState(!!f.advice && f.status !== "ignored");
  const a = f.advice;
  const tone = f.status === "resolved" ? "green" : f.severity === "error" ? "red" : f.severity === "warn" ? "amber" : "slate";
  return (
    <div className="rounded-lg border border-slate-800 bg-slate-950/60 p-3 text-xs">
      <div className="flex flex-wrap items-center gap-2">
        <button type="button" onClick={() => setOpen(!open)} className="text-slate-400">{open ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}</button>
        <Badge tone={tone}>{f.status === "resolved" ? "resolved" : f.severity}</Badge>
        <Badge tone="slate">{f.category}</Badge>
        <span className="font-medium text-slate-200">{f.title}</span>
        <span className="ml-auto flex gap-1.5">
          {f.status !== "resolved" && (
            <Button variant="secondary" className="px-2.5 py-1 text-xs" onClick={() => { setOpen(true); act(() => adminApi.envAdvise(f.id)); }}>
              <Bot className="h-3.5 w-3.5" />{a ? "Refresh advice" : ai ? "Get AI upgrade advice" : "Show upgrade steps"}</Button>
          )}
          {["open", "advised"].includes(f.status) && <Button variant="ghost" className="px-2.5 py-1 text-xs" onClick={() => act(() => adminApi.envIgnore(f.id))}>Ignore</Button>}
        </span>
      </div>
      <p className="mt-1.5 whitespace-pre-wrap font-mono text-[11px] text-slate-400">{f.message}</p>
      {open && a && (
        <div className="mt-2 space-y-2 border-t border-slate-800 pt-2">
          <div className="flex items-center gap-2 text-slate-400">
            <Badge tone={a.agent_source === "ACTUAL_AI_API" ? "green" : "amber"}>{a.agent_source === "ACTUAL_AI_API" ? `AI${a.model ? " · " + a.model : ""}` : "built-in steps"}</Badge>
            {a.risk && <span>risk {a.risk}</span>}
            {a.ai_error && <span className="text-amber-300">AI unavailable: {a.ai_error}</span>}
          </div>
          {a.summary && <p className="text-slate-300">{a.summary}</p>}
          <ol className="list-decimal space-y-1.5 pl-5 text-slate-300">
            {a.steps.map((st, i) => (
              <li key={i}><b>{st.title}</b>{st.detail ? <span className="block text-slate-400">{st.detail}</span> : null}
                {st.command && <code className="mt-1 block overflow-x-auto rounded bg-black/60 px-2 py-1 text-[11px] text-emerald-200">{st.command}</code>}</li>
            ))}
          </ol>
          {!!a.file_changes?.length && (
            <ul className="space-y-1 text-slate-400">{a.file_changes.map((c, i) => <li key={i}><code className="text-slate-200">{c.file}</code>: {c.change}</li>)}</ul>
          )}
          <p className="text-[11px] text-slate-400">Advice only - Understudy never upgrades your Python, Node.js or packages by itself.</p>
        </div>
      )}
    </div>
  );
}

export function CodeHealthPanel() {
  const [st, setSt] = useState<CodeHealthState | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [showAll, setShowAll] = useState(false);
  const load = useCallback(async () => {
    try { setSt(await adminApi.codeHealth()); setErr(null); } catch (e) { setErr((e as Error).message); }
  }, []);
  useEffect(() => {
    const t = setTimeout(() => { void load(); }, 0);
    const id = setInterval(() => { void load(); }, 4000);
    return () => { clearTimeout(t); clearInterval(id); };
  }, [load]);
  const act = (fn: () => Promise<CodeHealthState>) => {
    fn().then(setSt).catch((e) => setErr((e as Error).message));
  };
  const own = (st?.findings || []).filter((f) => f.file);
  const other = (st?.findings || []).filter((f) => !f.file);
  const actionable = own.filter((f) => !["resolved", "ignored"].includes(f.status));
  const envOpen = (st?.environment || []).filter((f) => !["resolved", "ignored"].includes(f.status));
  return (
    <Card title="Code health - AI self-heal for start-up and install warnings" icon={<FileCode2 className="h-4 w-4 text-sky-400" />}
      right={<div className="flex items-center gap-2">
        {st?.busy && <Badge tone="sky"><RefreshCw className="h-3 w-3 animate-spin" />{st.task}</Badge>}
        <Button variant="secondary" className="px-3 py-1 text-xs" disabled={st?.busy} onClick={() => act(adminApi.codeHealthRun)}><RefreshCw className="h-3.5 w-3.5" />Scan &amp; heal now</Button>
      </div>}>
      <p className="mb-3 text-xs text-slate-400">
        Understudy imports its backend with deprecation and future warnings switched on and lists every warning from its own
        code (a fresh install has none; they appear when a dependency upgrade deprecates something). With a Gemini key set, the AI proposes the
        smallest fix for each warning in Understudy&apos;s own code. Each fix is tested on a copy of the code first: it must
        compile, add no unsafe calls, make the warning disappear and keep the backend starting. The original file is backed
        up and every fix can be rolled back. <b>Restart the backend</b> after applying to load the fix.
      </p>
      {st && <ChangesBanner st={st} />}
      <div className="mb-3 flex flex-wrap items-center gap-3 rounded-lg border border-slate-800 bg-slate-950/50 px-3 py-2 text-xs">
        <label className="flex items-center gap-2 text-slate-200">
          <input type="checkbox" className="accent-emerald-500" checked={!!st?.auto_apply} onChange={(e) => act(() => adminApi.codeHealthSettings(e.target.checked))} />
          Apply verified fixes automatically
        </label>
        <span className="text-slate-400">{st?.ai_available ? <><Bot className="inline h-3 w-3" /> AI available</> : <><AlertTriangle className="inline h-3 w-3 text-amber-400" /> No AI key - scanning only</>}</span>
        {st?.last_scan && <span className="text-slate-400">Last scan {new Date(st.last_scan.at).toLocaleString()} · {st.last_scan.count} warning(s){st.last_scan.import_ok ? "" : " · backend import FAILED"}</span>}
        <span className="text-slate-400"><Lock className="inline h-3 w-3" /> Protected: {st?.protected_files.join(", ")}</span>
      </div>
      {err && <p className="mb-2 text-xs text-rose-400">{err}</p>}
      <div className="mb-2 mt-1 text-[11px] font-bold uppercase text-slate-400">Runtimes &amp; dependencies on this machine</div>
      <p className="mb-2 text-[11px] text-slate-400">
        Python / Node.js end-of-life (dates from endoflife.date), npm <code>EBADENGINE</code> and <code>deprecated</code> warnings
        of the web UI, and <code>pip check</code>. These need an upgrade on the machine, so you get step-by-step advice for
        this OS (from the AI when a Gemini key is set) - nothing is changed automatically.
      </p>
      <div className="mb-4 space-y-2">
        {envOpen.length === 0 ? (
          <div className="rounded-lg border border-slate-800 bg-slate-950/60 p-3 text-center text-xs text-slate-400">No runtime or dependency warnings.</div>
        ) : envOpen.map((f) => <EnvRow key={f.id} f={f} act={act} ai={!!st?.ai_available} />)}
      </div>
      <div className="mb-2 text-[11px] font-bold uppercase text-slate-400">Warnings in Understudy&apos;s own code</div>
      <div className="space-y-2">
        {actionable.length === 0 ? (
          <div className="rounded-lg border border-slate-800 bg-slate-950/60 p-4 text-center text-xs text-slate-400">
            <ShieldCheck className="mx-auto mb-1 h-5 w-5 text-emerald-500/60" />No open warnings in Understudy&apos;s own code.
          </div>
        ) : actionable.map((f) => <FindingRow key={f.id} f={f} act={act} ai={!!st?.ai_available} />)}
      </div>
      {(own.length > actionable.length || other.length > 0) && (
        <button type="button" onClick={() => setShowAll(!showAll)} className="mt-3 text-[11px] text-sky-400 hover:underline">
          {showAll ? "Hide" : "Show"} resolved / ignored ({own.length - actionable.length}) and third-party warnings ({other.length})
        </button>
      )}
      {showAll && (
        <div className="mt-2 space-y-2 opacity-80">
          {own.filter((f) => ["resolved", "ignored"].includes(f.status)).map((f) => <FindingRow key={f.id} f={f} act={act} ai={false} />)}
          {other.map((f) => <FindingRow key={f.id} f={f} act={act} ai={false} />)}
        </div>
      )}
      <AiChanges st={st} act={act} setErr={setErr} />
    </Card>
  );
}

/* ============================================================================================
 * AI code changes: did the AI change code, which lines, why, where the backup is, and how to revert.
 * ========================================================================================== */
const CHANGE_STATUS: Record<string, { label: string; tone: "green" | "amber" | "red" | "slate" | "sky"; help: string }> = {
  in_place: { label: "AI change in place", tone: "amber", help: "The file is exactly as the AI left it." },
  rolled_back: { label: "reverted", tone: "slate", help: "The original file was restored from the backup." },
  modified_since: { label: "edited since", tone: "red", help: "Someone changed the file after the AI fix. Reverting needs 'Revert anyway' (the current file is saved first)." },
  file_missing: { label: "file missing", tone: "red", help: "The file no longer exists." },
};

function rangeText(r: [number, number]) {
  return r[1] < r[0] ? "-" : r[0] === r[1] ? `line ${r[0]}` : `lines ${r[0]}-${r[1]}`;
}

export function ChangeList({ changes }: { changes: LineChange[] }) {
  return (
    <ul className="mt-1 space-y-1.5">
      {changes.map((c, i) => (
        <li key={i} className="rounded border border-slate-800 bg-slate-950/70 p-2">
          <div className="mb-1 text-[11px] text-slate-300">
            {c.kind === "added" ? <>added new {rangeText(c.new_lines)} (after original line {c.old_lines[0] - 1})</>
              : c.kind === "removed" ? <>removed original {rangeText(c.old_lines)}</>
              : <>original {rangeText(c.old_lines)} → new {rangeText(c.new_lines)}</>}
          </div>
          {c.before.length > 0 && <pre className="overflow-auto whitespace-pre rounded bg-rose-950/30 px-2 py-1 text-[11px] text-rose-200">{c.before.map((l, j) => `${String(c.old_lines[0] + j).padStart(4)} - ${l}`).join("\n")}</pre>}
          {c.after.length > 0 && <pre className="mt-1 overflow-auto whitespace-pre rounded bg-emerald-950/30 px-2 py-1 text-[11px] text-emerald-200">{c.after.map((l, j) => `${String(c.new_lines[0] + j).padStart(4)} + ${l}`).join("\n")}</pre>}
        </li>
      ))}
    </ul>
  );
}

export function DiffBlock({ diff }: { diff: string }) {
  return (
    <details className="text-[11px]">
      <summary className="cursor-pointer text-sky-300">Unified diff</summary>
      <pre className="mt-1 max-h-80 overflow-auto rounded bg-slate-950 p-2 font-mono leading-relaxed">
        {diff.split("\n").map((l, i) => (
          <div key={i} className={l.startsWith("+") && !l.startsWith("+++") ? "text-emerald-300" : l.startsWith("-") && !l.startsWith("---") ? "text-rose-300" : l.startsWith("@@") ? "text-sky-300" : "text-slate-400"}>{l || " "}</div>
        ))}
      </pre>
    </details>
  );
}

function ChangesBanner({ st }: { st: CodeHealthState }) {
  const c = st.changes;
  if (!c) return null;
  if (!c.total) {
    return <div className="mb-3 rounded-lg border border-slate-800 bg-slate-950/50 px-3 py-2 text-xs text-slate-300" data-testid="ai-changes-banner">
      <CheckCircle2 className="mr-1 inline h-3.5 w-3.5 text-emerald-400" />The AI has <b>not changed any code</b> so far.
      {st.auto_apply ? " Auto-apply is ON: verified fixes will be applied without asking." : " Auto-apply is off: nothing is changed until you click Apply."}</div>;
  }
  return (
    <div className="mb-3 space-y-2" data-testid="ai-changes-banner">
      <div className="rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs text-amber-100">
        <Wrench className="mr-1 inline h-3.5 w-3.5" />The AI changed code <b>{c.total}</b> time{c.total === 1 ? "" : "s"}
        {c.by_ai_alone ? <> (<b>{c.by_ai_alone}</b> by auto-apply, without an admin click)</> : null}:
        {" "}<b>{c.in_place}</b> still in place{c.files_in_place.length ? ` (${c.files_in_place.join(", ")})` : ""},
        {" "}<b>{c.rolled_back}</b> reverted{c.modified_since ? <>, <b>{c.modified_since}</b> edited since</> : null}.
        {" "}Details, backups and Revert are under <b>AI code changes</b> below.
      </div>
      {c.restart_needed && (
        <div className="rounded-lg border border-sky-500/40 bg-sky-500/10 px-3 py-2 text-xs text-sky-100" role="status">
          <RefreshCw className="mr-1 inline h-3.5 w-3.5" />Restart the backend to load the latest change{c.restart_files.length > 1 ? "s" : ""} ({c.restart_files.join(", ")}) - the running process still uses the code it started with.
        </div>
      )}
    </div>
  );
}

function AiChanges({ st, act, setErr }: { st: CodeHealthState | null; act: (fn: () => Promise<CodeHealthState>) => void; setErr: (e: string | null) => void }) {
  const [open, setOpen] = useState<string | null>(null);
  if (!st) return null;
  const revert = async (h: CodeChange, force = false) => {
    const msg = force
      ? `${h.file} was edited after this AI fix. Revert anyway?\n\nThe current file is saved to ${st.backups_dir}/${h.id}/before-revert/ first, then the original is restored from ${h.backup}.`
      : `Revert this AI change?\n\n${h.file} will be restored from the backup ${h.backup}. Restart the backend afterwards.`;
    if (!window.confirm(msg)) return;
    try {
      setErr(null);
      act(() => adminApi.codeHealthRollback(h.id, force));
    } catch (e) { setErr((e as Error).message); }
  };
  return (
    <div className="mt-4 border-t border-slate-800 pt-3" data-testid="ai-code-changes">
      <div className="mb-1 flex items-center gap-1 text-[11px] font-bold uppercase text-slate-400"><Wrench className="h-3 w-3" />AI code changes</div>
      <p className="mb-2 text-[11px] text-slate-400">
        Every fix the AI applied, newest first. Backups are in <code>{st.backups_dir}/&lt;change id&gt;/</code>: the original file at its
        path, plus <code>CHANGE.md</code>, <code>change.json</code> and <code>fix.diff</code>. If the backend can&apos;t start, revert from a terminal:
        <code> python scripts/code_health.py list</code> then <code>revert &lt;id&gt;</code>.
      </p>
      {st.history.length === 0 ? (
        <div className="rounded-lg border border-slate-800 bg-slate-950/60 p-3 text-center text-xs text-slate-400">No AI code changes yet.</div>
      ) : (
        <div className="space-y-2">
          {st.history.map((h) => {
            const meta = CHANGE_STATUS[h.status] || CHANGE_STATUS.in_place;
            const isOpen = open === h.id;
            return (
              <div key={h.id} className="rounded-lg border border-slate-800 bg-slate-950/60 p-3 text-xs" data-testid="ai-change">
                <div className="flex flex-wrap items-center gap-2">
                  <button type="button" onClick={() => setOpen(isOpen ? null : h.id)} aria-expanded={isOpen} className="text-slate-400">
                    {isOpen ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}</button>
                  <span title={meta.help}><Badge tone={meta.tone}>{meta.label}</Badge></span>
                  <code className="text-slate-200">{h.file}</code>
                  <span className="text-slate-400">{new Date(h.at).toLocaleString()} · applied by <b className={h.by === "auto-apply" ? "text-amber-300" : "text-slate-200"}>{h.applied_by}</b></span>
                  {h.summary && h.summary.length > 0 && <span className="text-slate-400">· {h.summary.length} change{h.summary.length === 1 ? "" : "s"}</span>}
                  <span className="ml-auto flex gap-1.5">
                    {h.backup_exists && <Button variant="ghost" className="px-2 py-0.5 text-[11px]" onClick={() => { adminApi.codeHealthDownloadBackup(h.id).catch((e) => setErr((e as Error).message)); }}>
                      <FileCode2 className="h-3 w-3" />Original file</Button>}
                    {h.status === "in_place" && <Button variant="secondary" className="px-2 py-0.5 text-[11px]" onClick={() => revert(h)}><RotateCcw className="h-3 w-3" />Revert</Button>}
                    {h.status === "modified_since" && <Button variant="danger" className="px-2 py-0.5 text-[11px]" onClick={() => revert(h, true)}><RotateCcw className="h-3 w-3" />Revert anyway</Button>}
                  </span>
                </div>
                <div className="mt-1 text-slate-400"><code className="text-[10px]">{h.id}</code> · backup <code className="text-[10px]">{h.backup}</code>{h.backup_exists ? "" : " (missing)"}</div>
                {h.rolled_back && <p className="mt-1 text-slate-300">Reverted {h.rolled_back_at ? new Date(h.rolled_back_at).toLocaleString() : ""} by {h.rolled_back_by || "admin"}
                  {h.saved_before_revert ? <> - the edited file was kept at <code>{h.saved_before_revert}</code></> : null}.</p>}
                {isOpen && (
                  <div className="mt-2 space-y-2 border-t border-slate-800 pt-2">
                    {h.warning && <p className="text-slate-300"><b>Triggered by:</b> {h.warning.category} at line {h.warning.line}: <span className="text-slate-400">{h.warning.message.slice(0, 300)}</span></p>}
                    {h.diagnosis && <p className="text-slate-300"><b>Why (AI diagnosis):</b> {h.diagnosis}</p>}
                    {h.explanation && <p className="text-slate-400"><b>What the AI says it did:</b> {h.explanation}</p>}
                    <p className="text-slate-400">{h.model ? `Model ${h.model}` : ""}{h.strategy ? ` · strategy ${h.strategy.replace(/_/g, " ")}` : ""}{h.risk ? ` · risk ${h.risk}` : ""}</p>
                    {h.verification && (
                      <div className="flex flex-wrap gap-1.5">
                        {[["compiled", h.verification.compiled], ["no unsafe calls", h.verification.safe], ["warning gone", h.verification.warning_gone], ["backend imported", h.verification.import_ok]].map(([l, ok]) => (
                          <Badge key={String(l)} tone={ok ? "green" : "red"}>{ok ? <CheckCircle2 className="h-3 w-3" /> : <XCircle className="h-3 w-3" />}{String(l)}</Badge>
                        ))}
                      </div>
                    )}
                    {h.changes && h.changes.length > 0 ? <div className="text-slate-300"><b>Lines changed:</b><ChangeList changes={h.changes} /></div>
                      : <p className="text-slate-400">No line-level record (changes applied before 7.1 that are no longer in place).</p>}
                    {h.diff && <DiffBlock diff={h.diff} />}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

export { UsersPanel } from "./users-panel";
