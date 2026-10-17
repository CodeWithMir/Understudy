"use client";

/* Mission Control > Users (8.1).
 * Accounts have a username, an email (one email = one account) and a password the admin never sees again: an admin
 * can create an account with or without a password (without = the user sets one with an emailed code), ask a user
 * to reset, add the email of an older account, Block / Unblock or Delete - Block and Delete need a reason that is
 * mailed to the user together with the admin contact. Password resets always go through an emailed code (OTP). */
import React, { useCallback, useEffect, useMemo, useState } from "react";
import { Ban, CheckCircle2, Mail, MailWarning, RefreshCw, Search, Send, ShieldOff, Trash2, Unlock, UserPlus, Users } from "lucide-react";
import { adminApi, type MailStatus, type SignupStatus, type StudioUser } from "../lib/admin";
import { Modal } from "./wizard/explorer";
import { Badge, Button, Card, cx, PasswordInput } from "./wizard/ui";

const input = "w-full text-xs bg-slate-950 border border-slate-700 rounded-lg px-3 py-2 text-white placeholder:text-slate-400 focus:outline-none focus:border-sky-500";
type Msg = { ok: boolean; text: string } | null;

function mailNote(r: { mail_sent?: boolean; mail_error?: string | null }, to?: string | null): string {
  if (r.mail_sent) return to ? ` An email was sent to ${to}.` : " The user was emailed.";
  return r.mail_error ? ` The email could NOT be sent: ${r.mail_error}` : "";
}

/** Block or Delete: the reason is required and goes to the user by email. */
function ReasonDialog({ user, kind, onClose, onDone }: {
  user: StudioUser; kind: "block" | "delete"; onClose: () => void; onDone: (m: Msg) => void;
}) {
  const [reason, setReason] = useState("");
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const del = kind === "delete";
  // Forgiving match: surrounding spaces and letter case don't matter (usernames are unique case-insensitively).
  const typedOk = typed.trim().toLowerCase() === user.username.trim().toLowerCase();
  const ok = reason.trim().length >= 5 && (!del || typedOk);
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true); setErr(null);
    try {
      if (del) {
        const r = await adminApi.deleteUser(user.username, reason.trim());
        onDone({ ok: true, text: `Deleted '${user.username}'. ${r.projects_archived} project(s) and ${r.sessions_archived} session(s) are kept for audit.${mailNote(r, user.email)}` });
      } else {
        const r = await adminApi.blockUser(user.username, reason.trim());
        onDone({ ok: r.mail_sent, text: `Blocked '${user.username}'; their open sessions ended.${mailNote(r, user.email)}` });
      }
      onClose();
    } catch (e2) { setErr((e2 as Error).message); } finally { setBusy(false); }
  };
  return (
    <Modal title={del ? `Delete the account '${user.username}'?` : `Block '${user.username}'?`} onClose={onClose}>
      <form onSubmit={submit} className="space-y-3 text-xs text-slate-300" data-testid={`${kind}-dialog`}>
        <p>{del
          ? "The account is removed and nobody can log in as this user. Their projects and sessions are kept for audit. The email address becomes free for a new account."
          : "The user can't log in or reset their password until you unblock them. Their open sessions end immediately."}</p>
        <label className="block space-y-1">
          <span className="font-semibold text-slate-200">Reason (emailed to {user.email || "the user"})</span>
          <textarea className={cx(input, "h-20")} value={reason} onChange={(e) => setReason(e.target.value)} maxLength={500} required
            placeholder={del ? "e.g. Contract ended on 30 September" : "e.g. Repeated SOP violations - please contact IT"} />
        </label>
        {!user.email && <p className="text-amber-300">This account has no email address, so the user won&apos;t get the mail.</p>}
        <p className="text-slate-400">The mail also asks the user to contact the admin to have the account {del ? "reviewed" : "unblocked"}.</p>
        {del && (
          <label className="block space-y-1">
            <span>Type <b className="select-all font-mono text-white">{user.username}</b> to confirm</span>
            <input className={cx(input, typed && (typedOk ? "border-emerald-500" : "border-rose-500"))} value={typed} onChange={(e) => setTyped(e.target.value)}
              aria-label="Type the username to confirm" autoComplete="off" autoCorrect="off" autoCapitalize="none" spellCheck={false}
              name="confirm-username" data-lpignore="true" data-1p-ignore placeholder={user.username} />
            <span className={cx("block text-[11px]", typedOk ? "text-emerald-300" : "text-slate-400")} aria-live="polite">
              {typedOk ? "Username matches." : typed ? "Doesn't match yet." : "Paste or type the username exactly as shown."}</span>
          </label>
        )}
        {reason.trim().length > 0 && reason.trim().length < 5 && <p className="text-amber-300">The reason needs at least 5 characters.</p>}
        {err && <p className="text-rose-300" role="alert">{err}</p>}
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
          <Button type="submit" busy={busy} disabled={!ok} className={del ? "bg-rose-600 hover:bg-rose-500" : "bg-amber-500 text-slate-950 hover:bg-amber-400"}>
            {del ? <Trash2 className="h-4 w-4" /> : <Ban className="h-4 w-4" />}{del ? "Delete and email the user" : "Block and email the user"}
          </Button>
        </div>
      </form>
    </Modal>
  );
}

function EmailDialog({ user, onClose, onDone }: { user: StudioUser; onClose: () => void; onDone: (m: Msg) => void }) {
  const [email, setEmail] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  return (
    <Modal title={`Add the email of '${user.username}'`} onClose={onClose}>
      <form className="space-y-3 text-xs text-slate-300" onSubmit={async (e) => {
        e.preventDefault(); setBusy(true); setErr(null);
        try { await adminApi.setUserEmail(user.username, email.trim()); onDone({ ok: true, text: `Email added for '${user.username}'.` }); onClose(); }
        catch (e2) { setErr((e2 as Error).message); } finally { setBusy(false); }
      }}>
        <p>This account was created before email was required. Once set, the email can&apos;t be changed from Mission Control
          (that would let an admin take the account over with a password reset).</p>
        <input className={input} type="email" value={email} onChange={(e) => setEmail(e.target.value)} placeholder="name@company.com" aria-label="Email" required />
        {err && <p className="text-rose-300" role="alert">{err}</p>}
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
          <Button type="submit" busy={busy} disabled={!email.trim()}><Mail className="h-4 w-4" />Save email</Button>
        </div>
      </form>
    </Modal>
  );
}

export function MailPanel({ status, onChanged }: { status: MailStatus | null; onChanged: (s: MailStatus) => void }) {
  const [to, setTo] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<Msg>(null);
  const s = status;
  return (
    <Card title="Email (OTP codes and account notices)" icon={<Mail className="h-4 w-4 text-sky-400" />}
      right={s ? <Badge tone={s.delivering ? "green" : s.mode === "outbox" ? "amber" : "red"}>{s.delivering ? "sending" : s.mode}</Badge> : null}>
      <div className="space-y-3 text-xs text-slate-300" data-testid="mail-panel">
        {s && (
          <dl className="grid grid-cols-2 gap-2 rounded-lg border border-slate-800 bg-slate-950/60 p-2.5 sm:grid-cols-4">
            <div><dt className="text-slate-400">Mode</dt><dd className="font-medium">{s.mode}</dd></div>
            <div><dt className="text-slate-400">SMTP server</dt><dd className="font-mono">{s.host ? `${s.host}:${s.port} (${s.security})` : "-"}</dd></div>
            <div><dt className="text-slate-400">From</dt><dd className="break-all font-mono">{s.from}</dd></div>
            <div><dt className="text-slate-400">Sent / failed</dt><dd>{s.stats.sent} / {s.stats.failed}</dd></div>
          </dl>
        )}
        {s?.problem && <p className={cx("rounded-lg border p-2", s.mode === "outbox" ? "border-amber-500/40 bg-amber-500/10 text-amber-100" : "border-rose-500/40 bg-rose-500/10 text-rose-100")}>
          <MailWarning className="mr-1 inline h-3.5 w-3.5" />{s.problem}</p>}
        {s?.stats.last_error && <p className="text-rose-300">Last error: {s.stats.last_error}</p>}
        <p className="text-slate-400">Configured on the server in <code>.env</code> (UNDERSTUDY_SMTP_HOST, _PORT, _USER, _PASSWORD,
          UNDERSTUDY_MAIL_FROM) - free with a Gmail App Password or Brevo&apos;s free plan; see README 2.0.2. The SMTP password is
          never shown here.</p>
        <div className="flex gap-2">
          <input className={input} type="email" value={to} onChange={(e) => setTo(e.target.value)} placeholder="Send a test email to..." aria-label="Test email address" />
          <Button busy={busy} disabled={!to.trim()} onClick={async () => {
            setBusy(true); setMsg(null);
            try { const r = await adminApi.testMail(to.trim()); onChanged(r.mail); setMsg({ ok: true, text: `Test email ${r.mail.mode === "smtp" ? "sent" : "written to the outbox"} (${r.to}).` }); }
            catch (e) { setMsg({ ok: false, text: (e as Error).message }); adminApi.mail().then(onChanged).catch(() => null); }
            finally { setBusy(false); }
          }}><Send className="h-4 w-4" />Send test</Button>
        </div>
        {msg && <p className={msg.ok ? "text-emerald-300" : "text-rose-300"} role={msg.ok ? "status" : "alert"}>{msg.text}</p>}
      </div>
    </Card>
  );
}

/** Mission Control switch: may people create their own Studio account? (saved in the admin policy) */
function SignupSwitch({ status, onChanged, onError }: { status: SignupStatus | null; onChanged: (s: SignupStatus) => void; onError: (m: string) => void }) {
  const [busy, setBusy] = useState(false);
  const on = !!status?.enabled;
  const toggle = async () => {
    setBusy(true);
    try { onChanged(await adminApi.setSignup(!on)); } catch (e) { onError((e as Error).message); } finally { setBusy(false); }
  };
  return (
    <label className="inline-flex cursor-pointer items-center gap-2 text-[11px] text-slate-300" data-testid="signup-switch"
      title={status ? (status.source === "admin" ? `Set by ${status.set_by} in Mission Control` : `From UNDERSTUDY_SIGNUP (default ${status.env_default ? "on" : "off"})`) : undefined}>
      <span>Allow self-signup</span>
      <button type="button" role="switch" aria-checked={on} aria-label="Allow self-signup" disabled={busy || !status} onClick={() => { void toggle(); }}
        className={cx("relative h-5 w-9 rounded-full transition-colors disabled:opacity-50", on ? "bg-emerald-500" : "bg-slate-600")}>
        <span className={cx("absolute top-0.5 h-4 w-4 rounded-full bg-white shadow transition-all", on ? "left-[18px]" : "left-0.5")} />
      </button>
      <span className={on ? "text-emerald-300" : "text-slate-400"}>{on ? "on" : "off"}</span>
    </label>
  );
}

/** 8.2: self-signup can be limited to company email domains, e.g. only @acme.com. */
function SignupDomains({ status, onChanged, onError }: { status: SignupStatus | null; onChanged: (s: SignupStatus) => void; onError: (m: string) => void }) {
  const saved = status?.domains || [];
  const [draft, setDraft] = useState<string[] | null>(null);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const list = draft ?? saved;
  const dirty = draft !== null && JSON.stringify(draft) !== JSON.stringify(saved);
  const add = () => {
    const parts = text.split(/[\s,;]+/).map((x) => x.trim().toLowerCase().replace(/^@/, "")).filter(Boolean);
    if (!parts.length) return;
    setDraft(Array.from(new Set([...list, ...parts])));
    setText("");
  };
  const save = async (domains: string[] | null) => {
    setBusy(true);
    try { onChanged(await adminApi.setSignupDomains(domains)); setDraft(null); } catch (e) { onError((e as Error).message); } finally { setBusy(false); }
  };
  return (
    <div className="mb-3 space-y-2 rounded-lg border border-slate-800 bg-slate-950/60 px-3 py-2" data-testid="signup-domains">
      <div className="flex flex-wrap items-center gap-2 text-[11px] text-slate-300">
        <Mail className="h-3.5 w-3.5 text-sky-400" />
        <b>Self-signup email domains</b>
        {saved.length ? <Badge tone="amber">restricted</Badge> : <Badge tone="slate">any domain</Badge>}
        {status?.domains_source === "env" && saved.length > 0 && <span className="text-slate-400">(from UNDERSTUDY_SIGNUP_DOMAINS)</span>}
      </div>
      <p className="text-[11px] text-slate-400">Only people whose email ends with one of these domains can sign up themselves (accounts you create
        here are not limited). Use <code>*.acme.com</code> to include subdomains. Leave empty to allow any domain.</p>
      <div className="flex flex-wrap gap-1.5">
        {list.map((d) => (
          <span key={d} className="inline-flex items-center gap-1 rounded-md border border-sky-500/50 bg-sky-500/10 px-2 py-0.5 font-mono text-[11px] text-sky-100">
            @{d}
            <button type="button" aria-label={`Remove ${d}`} onClick={() => setDraft(list.filter((x) => x !== d))} className="text-slate-400 hover:text-rose-300">×</button>
          </span>
        ))}
        {list.length === 0 && <span className="text-[11px] text-slate-500">No restriction.</span>}
      </div>
      <div className="flex flex-wrap gap-2">
        <input className={cx(input, "max-w-xs")} value={text} onChange={(e) => setText(e.target.value)} maxLength={260}
          onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); add(); } }}
          aria-label="Email domain allowed to sign up" placeholder="companyname.com" />
        <Button variant="ghost" onClick={add} disabled={!text.trim()}>Add</Button>
        <Button busy={busy} disabled={!dirty} onClick={() => { void save(list); }}>Save domains</Button>
        {status?.domains_source === "admin" && <Button variant="ghost" busy={busy} onClick={() => { void save(null); }}>Use server default</Button>}
      </div>
    </div>
  );
}

export function UsersPanel() {
  const [users, setUsers] = useState<StudioUser[] | null>(null);
  const [loginRequired, setLoginRequired] = useState(true);
  const [signup, setSignup] = useState<SignupStatus | null>(null);
  const [mail, setMail] = useState<MailStatus | null>(null);
  const [username, setUsername] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [q, setQ] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [msg, setMsg] = useState<Msg>(null);
  const [dialog, setDialog] = useState<{ kind: "block" | "delete" | "email"; user: StudioUser } | null>(null);
  const load = useCallback(() => {
    adminApi.users().then((r) => { setUsers(r.users); setLoginRequired(r.login_required); setSignup(r.signup); setMail(r.mail); })
      .catch((e) => setMsg({ ok: false, text: (e as Error).message }));
  }, []);
  useEffect(() => { const t = setTimeout(load, 0); return () => clearTimeout(t); }, [load]);
  const shown = useMemo(() => {
    const t = q.trim().toLowerCase();
    return (users || []).filter((u) => !t || u.username.toLowerCase().includes(t) || (u.email || "").includes(t));
  }, [users, q]);
  const create = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy("create"); setMsg(null);
    try {
      const r = await adminApi.createUser(username.trim(), email.trim(), password || null);
      setMsg({ ok: r.mail_sent, text: `Created '${r.user.username}' (${r.user.email}).` + (password ? " The password is not shown again." :
        " No password was set: the welcome email tells the user to set one with Forgot password.") + mailNote(r) });
      setUsername(""); setEmail(""); setPassword("");
      load();
    } catch (err) { setMsg({ ok: false, text: (err as Error).message }); } finally { setBusy(null); }
  };
  const act = async (key: string, fn: () => Promise<Msg>) => {
    setBusy(key); setMsg(null);
    try { setMsg(await fn()); load(); } catch (err) { setMsg({ ok: false, text: (err as Error).message }); } finally { setBusy(null); }
  };
  return (
    <div className="grid items-start gap-5 xl:grid-cols-3">
      <div className="xl:col-span-2">
        <Card title="Studio users" icon={<Users className="h-4 w-4 text-sky-400" />}
          right={<div className="flex gap-1.5">
            {loginRequired ? <Badge tone="green">login required</Badge> : <Badge tone="amber">login off</Badge>}
            </div>}>
          <div className="mb-3 flex flex-wrap items-center gap-x-3 gap-y-1 rounded-lg border border-slate-800 bg-slate-950/60 px-3 py-2">
            <SignupSwitch status={signup} onChanged={(s) => { setSignup(s); setMsg({ ok: true, text: s.enabled ? "Self-signup is on: people can create their own account (email verified by a code)." : "Self-signup is off: only you can create accounts. The Sign up tab disappears from the Studio." }); }}
              onError={(m) => setMsg({ ok: false, text: m })} />
            <span className="text-[11px] text-slate-400">{signup?.enabled ? (signup.domains?.length ? `Only ${signup.domains.map((d) => "@" + d).join(", ")} addresses can register.` : "Anyone who can open the Studio can register.") : "Only accounts you create can log in."}</span>
          </div>
          <SignupDomains status={signup} onChanged={(s) => { setSignup(s); setMsg({ ok: true, text: s.domains?.length ? `Self-signup is limited to ${s.domains.map((d) => "@" + d).join(", ")}.` : "Self-signup accepts any email domain." }); }}
            onError={(m) => setMsg({ ok: false, text: m })} />
          <p className="mb-3 text-xs text-slate-400">
            Every account has a username and an email address (one email = one account). When self-signup is on, people can also register
            themselves (their email is verified with a code). <b className="text-slate-200">Passwords are never shown or set here</b>: resets
            always go through a 10-minute code sent to the user&apos;s email. Block and Delete need a reason, which is emailed to the user.
          </p>
          <form onSubmit={create} className="grid gap-2 sm:grid-cols-[1fr_1.3fr_1fr_auto]" data-testid="create-user">
            <input className={input} placeholder="username" value={username} onChange={(e) => setUsername(e.target.value)} aria-label="New username"
              autoComplete="off" maxLength={40} required />
            <input className={input} type="email" placeholder="email (required)" value={email} onChange={(e) => setEmail(e.target.value)}
              aria-label="Email of the new user" autoComplete="off" maxLength={254} required />
            <PasswordInput className={input} placeholder="password (optional)" value={password} onChange={(e) => setPassword(e.target.value)}
              aria-label="Password of the new user (optional)" autoComplete="new-password" maxLength={128} />
            <Button type="submit" busy={busy === "create"} disabled={!username.trim() || !email.trim()}><UserPlus className="h-4 w-4" />Create user</Button>
          </form>
          <p className="mt-1 text-[11px] text-slate-400">Leave the password empty to let the user choose their own (recommended): the welcome email explains how.</p>
          {msg && <p className={cx("mt-2 text-xs", msg.ok ? "text-emerald-300" : "text-rose-300")} role={msg.ok ? "status" : "alert"}>{msg.text}</p>}
          <div className="mt-4 flex items-center gap-2">
            <Search className="h-3.5 w-3.5 text-slate-400" />
            <input className={cx(input, "max-w-xs")} value={q} onChange={(e) => setQ(e.target.value)} placeholder="Find by username or email" aria-label="Find users" />
            <button type="button" onClick={load} aria-label="Refresh" className="rounded p-1.5 text-slate-400 hover:bg-slate-800 hover:text-white"><RefreshCw className="h-3.5 w-3.5" /></button>
          </div>
          <div className="mt-2 overflow-x-auto">
            <table className="w-full min-w-[760px] text-sm" data-testid="users-table">
              <thead>
                <tr className="border-b border-slate-800 text-left text-[11px] uppercase tracking-wide text-slate-400">
                  <th scope="col" className="py-2 pr-2">User</th><th scope="col" className="py-2 pr-2">Email</th>
                  <th scope="col" className="py-2 pr-2">Projects / sessions</th><th scope="col" className="py-2 pr-2">Last login</th>
                  <th scope="col" className="py-2 pr-2">Status</th><th scope="col"><span className="sr-only">Actions</span></th>
                </tr>
              </thead>
              <tbody>
                {users && shown.length === 0 && <tr><td colSpan={6} className="py-3 text-xs text-slate-400">{users.length ? "No match." : "No users yet."}</td></tr>}
                {shown.map((u) => (
                  <tr key={u.username} className="border-b border-slate-800/60 align-top">
                    <td className="py-2 pr-2"><b className="text-slate-100">{u.username}</b>
                      <div className="text-[11px] text-slate-400">{u.source === "signup" ? "signed up" : `created by ${u.created_by}`} {String(u.created_at).slice(0, 10)}</div></td>
                    <td className="py-2 pr-2 text-xs">
                      {u.email ? <span className="break-all text-slate-200">{u.email}</span> : <span className="text-amber-300">no email</span>}
                      {u.email && (u.email_verified ? <CheckCircle2 className="ml-1 inline h-3 w-3 text-emerald-400" aria-label="verified" /> : null)}
                      {!u.password_set && <div className="text-[11px] text-amber-300">password not set yet</div>}
                      {u.two_factor && <div className="mt-0.5"><Badge tone="green">2-step sign-in</Badge></div>}
                    </td>
                    <td className="py-2 pr-2 text-xs text-slate-300">{u.projects ?? 0} / {u.sessions ?? 0}{u.completed ? ` (${u.completed} verified)` : ""}</td>
                    <td className="py-2 pr-2 text-xs text-slate-400">{u.last_login ? String(u.last_login).slice(0, 16).replace("T", " ") : "never"}</td>
                    <td className="py-2 pr-2">
                      {u.status === "blocked" ? <span title={`Blocked by ${u.blocked?.by} on ${String(u.blocked?.at).slice(0, 16).replace("T", " ")}`}>
                        <Badge tone="red">blocked</Badge><div className="mt-0.5 max-w-[14rem] text-[11px] text-rose-200">{u.blocked?.reason}</div></span>
                        : u.status === "disabled" ? <Badge tone="amber">disabled</Badge> : <Badge tone="green">active</Badge>}
                    </td>
                    <td className="whitespace-nowrap py-2 text-right">
                      {!u.email && <Button variant="ghost" className="px-2 py-1 text-xs" onClick={() => setDialog({ kind: "email", user: u })}><Mail className="h-3.5 w-3.5" />Add email</Button>}
                      {u.email && u.status !== "blocked" && (
                        <Button variant="ghost" className="px-2 py-1 text-xs" busy={busy === `reset-${u.username}`} title="Emails the user to reset their password with a code - you never see or set it"
                          onClick={() => act(`reset-${u.username}`, async () => { await adminApi.askPasswordReset(u.username); return { ok: true, text: `Asked '${u.username}' by email to reset their password.` }; })}>
                          <Send className="h-3.5 w-3.5" />Ask to reset</Button>)}
                      {u.two_factor && (
                        <Button variant="ghost" className="px-2 py-1 text-xs" busy={busy === `2fa-${u.username}`}
                          title="Turns the user's two-step sign-in off, e.g. when they lost access to their mailbox. They are told by email."
                          onClick={() => act(`2fa-${u.username}`, async () => { await adminApi.disableTwoFactor(u.username); return { ok: true, text: `Two-step sign-in is off for '${u.username}'.` }; })}>
                          <ShieldOff className="h-3.5 w-3.5" />2-step off</Button>)}
                      {u.status === "blocked" || u.status === "disabled"
                        ? <Button variant="ghost" className="px-2 py-1 text-xs text-emerald-300" busy={busy === `unblock-${u.username}`}
                            onClick={() => act(`unblock-${u.username}`, async () => { const r = await adminApi.unblockUser(u.username); return { ok: true, text: `Unblocked '${u.username}'.${mailNote(r, u.email)}` }; })}>
                            <Unlock className="h-3.5 w-3.5" />Unblock</Button>
                        : <Button variant="ghost" className="px-2 py-1 text-xs text-amber-300" onClick={() => setDialog({ kind: "block", user: u })}><Ban className="h-3.5 w-3.5" />Block</Button>}
                      <Button variant="ghost" className="px-2 py-1 text-xs text-rose-300" aria-label={`Delete ${u.username}`} onClick={() => setDialog({ kind: "delete", user: u })}>
                        <Trash2 className="h-3.5 w-3.5" /></Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      </div>
      <MailPanel status={mail} onChanged={setMail} />
      {dialog && (dialog.kind === "email"
        ? <EmailDialog user={dialog.user} onClose={() => setDialog(null)} onDone={(m) => { setMsg(m); load(); }} />
        : <ReasonDialog user={dialog.user} kind={dialog.kind} onClose={() => setDialog(null)} onDone={(m) => { setMsg(m); load(); }} />)}
    </div>
  );
}
