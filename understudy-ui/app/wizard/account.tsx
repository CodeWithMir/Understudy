"use client";

/* Understudy Studio accounts (8.1): Log in | Sign up | Forgot password, and the Profile tab with an OTP password reset
 * and (9.6) two-step sign-in: a code mailed after the right password.
 * Codes are 6 digits, mailed to the account's email, valid for 10 minutes and single use; the API never returns them. */
import React, { useEffect, useState } from "react";
import { ArrowLeft, CheckCircle2, KeyRound, LogIn, Mail, RefreshCw, ShieldCheck, User, UserPlus } from "lucide-react";
import { domainLabel, emailDomainAllowed, setUserToken, studioApi, type AuthOptions, type OtpChallenge, type UserProfile } from "../../lib/studio";
import { Alert, Button, Card, Field, PasswordInput, cx, inputCls } from "./ui";

type Mode = "login" | "signup" | "forgot";

function useCountdown(until: number | null): number {
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    if (!until) return;
    const id = setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => clearInterval(id);
  }, [until]);
  return until ? Math.max(0, Math.round(until - now)) : 0;
}

function fmt(s: number): string {
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

/** Step 2 of every flow: enter the 6-digit code (with expiry countdown and "send a new code"). */
export function CodeStep({ challenge, onVerify, onResend, busy, error, onBack }: {
  challenge: OtpChallenge; onVerify: (code: string) => void; onResend: () => Promise<OtpChallenge | null>;
  busy: boolean; error: string | null; onBack?: () => void;
}) {
  const [code, setCode] = useState("");
  const [ch, setCh] = useState(challenge);
  const [resendAt, setResendAt] = useState(() => Date.now() / 1000 + challenge.resend_in);
  const [info, setInfo] = useState<string | null>(null);
  const left = useCountdown(ch.expires_at);
  const resendIn = useCountdown(resendAt);
  return (
    <form className="space-y-3" data-testid="otp-step" onSubmit={(e) => { e.preventDefault(); onVerify(code); }}>
      <p className="text-xs text-slate-300">We sent a 6-digit code to <b className="text-white">{ch.email}</b>.{" "}
        {left > 0 ? <>It expires in <b className="tabular-nums text-white" aria-live="off">{fmt(left)}</b>.</> : <b className="text-rose-300">It has expired - send a new one.</b>}</p>
      <Field label="Code from the email">
        <input className={cx(inputCls, "text-center font-mono text-lg tracking-[0.5em]")} value={code} autoFocus
          onChange={(e) => setCode(e.target.value.replace(/\D/g, "").slice(0, 6))} inputMode="numeric" autoComplete="one-time-code"
          pattern="\d{6}" maxLength={6} aria-label="6-digit code" placeholder="••••••" required />
      </Field>
      {error && <Alert tone="red">{error}</Alert>}
      {info && <Alert tone="green">{info}</Alert>}
      <Button type="submit" busy={busy} disabled={code.length !== 6 || left === 0} className="w-full"><ShieldCheck className="h-4 w-4" />Verify code</Button>
      <div className="flex items-center justify-between text-xs">
        {onBack ? <button type="button" className="inline-flex items-center gap-1 text-slate-400 hover:text-white" onClick={onBack}><ArrowLeft className="h-3 w-3" />Back</button> : <span />}
        <button type="button" disabled={resendIn > 0} className="inline-flex items-center gap-1 text-sky-300 hover:underline disabled:text-slate-500 disabled:no-underline"
          onClick={async () => {
            setInfo(null);
            const n = await onResend();
            if (n) { setCh(n); setCode(""); setResendAt(Date.now() / 1000 + n.resend_in); setInfo("A new code is on its way."); }
          }}><RefreshCw className="h-3 w-3" />{resendIn > 0 ? `Send a new code in ${resendIn}s` : "Send a new code"}</button>
      </div>
    </form>
  );
}

/** Step 3: new password + confirmation. */
export function NewPasswordStep({ onSubmit, busy, error }: { onSubmit: (pw: string, confirm: string) => void; busy: boolean; error: string | null }) {
  const [pw, setPw] = useState("");
  const [confirm, setConfirm] = useState("");
  const mismatch = confirm.length > 0 && pw !== confirm;
  const weak = pw.length > 0 && (pw.length < 8 || !/[A-Za-z]/.test(pw) || !/\d/.test(pw));
  return (
    <form className="space-y-3" data-testid="new-password-step" onSubmit={(e) => { e.preventDefault(); onSubmit(pw, confirm); }}>
      <p className="text-xs text-emerald-300"><CheckCircle2 className="mr-1 inline h-3.5 w-3.5" />Code verified. Choose your new password.</p>
      <Field label="New password" hint="At least 8 characters with letters and numbers.">
        <PasswordInput className={inputCls} value={pw} onChange={(e) => setPw(e.target.value)} autoComplete="new-password" autoFocus required maxLength={128} />
      </Field>
      <Field label="Confirm new password">
        <PasswordInput className={inputCls} value={confirm} onChange={(e) => setConfirm(e.target.value)} autoComplete="new-password" required maxLength={128}
          aria-invalid={mismatch || undefined} />
      </Field>
      {weak && <p className="text-xs text-amber-300">Use at least 8 characters with letters and numbers.</p>}
      {mismatch && <p className="text-xs text-rose-300" role="alert">The passwords don&apos;t match.</p>}
      {error && <Alert tone="red">{error}</Alert>}
      <Button type="submit" busy={busy} disabled={!pw || pw !== confirm || weak} className="w-full"><KeyRound className="h-4 w-4" />Save new password</Button>
    </form>
  );
}

function LoginForm({ onLoggedIn, notice }: { onLoggedIn: (u: string) => void; notice: string | null }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // 9.6: two-step sign-in - the password was right, now the code from the email
  const [code, setCode] = useState<OtpChallenge | null>(null);
  if (code) {
    return (
      <div className="space-y-3" data-testid="login-code">
        <Alert tone="green" title="Password accepted">Two-step sign-in is on for this account. Enter the code we just emailed you.</Alert>
        <CodeStep challenge={code} busy={busy} error={error} onBack={() => { setCode(null); setError(null); setPassword(""); }}
          onResend={async () => { try { return (await studioApi.resendCode(code.challenge_id)).challenge; } catch (e) { setError((e as Error).message); return null; } }}
          onVerify={async (c) => {
            setBusy(true); setError(null);
            try { const r = await studioApi.loginVerify(code.challenge_id, c); setUserToken(r.token, r.username); onLoggedIn(r.username); }
            catch (err) { setError((err as Error).message); } finally { setBusy(false); }
          }} />
      </div>
    );
  }
  return (
    <form className="space-y-4" data-testid="login-form" onSubmit={async (e) => {
      e.preventDefault(); setBusy(true); setError(null);
      try {
        const r = await studioApi.login(username.trim(), password);
        if (r.two_factor_required) { setCode(r.challenge); return; }
        setUserToken(r.token, r.username); onLoggedIn(r.username);
      }
      catch (err) { setError((err as Error).message); } finally { setBusy(false); }
    }}>
      {notice && <Alert tone="green">{notice}</Alert>}
      <Field label="Username or email">
        <input className={inputCls} value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" autoFocus required maxLength={254} />
      </Field>
      <Field label="Password">
        <PasswordInput className={inputCls} value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" required maxLength={128} />
      </Field>
      {error && <Alert tone="red">{error}</Alert>}
      <Button type="submit" busy={busy} disabled={!username.trim() || !password} className="w-full"><KeyRound className="h-4 w-4" />Log in</Button>
    </form>
  );
}

function SignupFlow({ onLoggedIn, options }: { onLoggedIn: (u: string) => void; options: AuthOptions | null }) {
  const [f, setF] = useState({ username: "", email: "", password: "", confirm: "" });
  const [ch, setCh] = useState<OtpChallenge | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  if (options && !options.signup_enabled) {
    return <Alert tone="amber">Self-signup is switched off on this server. Ask your Mission Control admin for an account.</Alert>;
  }
  if (ch) {
    return <CodeStep challenge={ch} busy={busy} error={error} onBack={() => { setCh(null); setError(null); }}
      onResend={async () => { try { return (await studioApi.resendCode(ch.challenge_id)).challenge; } catch (e) { setError((e as Error).message); return null; } }}
      onVerify={async (code) => {
        setBusy(true); setError(null);
        try { const r = await studioApi.signupVerify(ch.challenge_id, code); setUserToken(r.token, r.username); onLoggedIn(r.username); }
        catch (e) { setError((e as Error).message); } finally { setBusy(false); }
      }} />;
  }
  const mismatch = f.confirm.length > 0 && f.password !== f.confirm;
  const domains = options?.signup_domains || [];
  const badDomain = f.email.includes("@") && f.email.split("@").pop()!.includes(".") && !emailDomainAllowed(f.email, domains);
  return (
    <form className="space-y-3" data-testid="signup-form" onSubmit={async (e) => {
      e.preventDefault(); setBusy(true); setError(null);
      try { setCh((await studioApi.signup(f.username.trim(), f.email.trim(), f.password, f.confirm)).challenge); }
      catch (err) { setError((err as Error).message); } finally { setBusy(false); }
    }}>
      <Field label="Username" hint="3-40 letters, digits or _ . @ -">
        <input className={inputCls} value={f.username} onChange={(e) => setF({ ...f, username: e.target.value })} autoComplete="username" required maxLength={40} autoFocus />
      </Field>
      <Field label="Email" hint={domains.length ? `Use your company email (${domains.map(domainLabel).join(", ")}). We send a code to confirm it.`
        : "We send a code to confirm it. One email can have only one account."}>
        <input className={inputCls} type="email" value={f.email} onChange={(e) => setF({ ...f, email: e.target.value })} autoComplete="email" required maxLength={254}
          aria-invalid={badDomain || undefined} placeholder={domains.length && !domains[0].startsWith("*.") ? `name@${domains[0]}` : undefined} />
      </Field>
      {badDomain && <p className="text-xs text-rose-300" role="alert" data-testid="signup-domain-error">
        Sign-up is limited to {domains.map(domainLabel).join(", ")} addresses. Use your company email, or ask your admin for an account.</p>}
      <Field label="Password" hint="At least 8 characters with letters and numbers.">
        <PasswordInput className={inputCls} value={f.password} onChange={(e) => setF({ ...f, password: e.target.value })} autoComplete="new-password" required maxLength={128} />
      </Field>
      <Field label="Confirm password">
        <PasswordInput className={inputCls} value={f.confirm} onChange={(e) => setF({ ...f, confirm: e.target.value })} autoComplete="new-password" required maxLength={128} />
      </Field>
      {mismatch && <p className="text-xs text-rose-300" role="alert">The passwords don&apos;t match.</p>}
      {error && <Alert tone="red">{error}</Alert>}
      <Button type="submit" busy={busy} disabled={!f.username.trim() || !f.email.trim() || !f.password || f.password !== f.confirm || badDomain} className="w-full">
        <UserPlus className="h-4 w-4" />Sign up - email me a code</Button>
    </form>
  );
}

/** Forgot password: email -> code -> new password. Also used by Profile (with ``profile`` = the logged-in flow). */
export function ResetFlow({ profile, onDone }: { profile?: UserProfile; onDone: (message: string) => void }) {
  const [email, setEmail] = useState("");
  const [ch, setCh] = useState<OtpChallenge | null>(null);
  const [ticket, setTicket] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const run = async (fn: () => Promise<void>) => { setBusy(true); setError(null); try { await fn(); } catch (e) { setError((e as Error).message); } finally { setBusy(false); } };
  if (ticket) {
    return <NewPasswordStep busy={busy} error={error} onSubmit={(pw, confirm) => run(async () => {
      const r = profile ? await studioApi.profileReset(ticket, pw, confirm) : await studioApi.resetPassword(ticket, pw, confirm);
      onDone(r.message);
    })} />;
  }
  if (ch) {
    return <CodeStep challenge={ch} busy={busy} error={error} onBack={() => { setCh(null); setError(null); }}
      onResend={async () => { try { return (await studioApi.resendCode(ch.challenge_id)).challenge; } catch (e) { setError((e as Error).message); return null; } }}
      onVerify={(code) => run(async () => {
        const r = profile ? await studioApi.profileResetVerify(ch.challenge_id, code) : await studioApi.verifyCode(ch.challenge_id, code);
        setTicket(r.ticket);
      })} />;
  }
  if (profile) {
    return (
      <div className="space-y-3" data-testid="profile-reset-start">
        <p className="text-xs text-slate-300">We&apos;ll send a 6-digit code to <b className="text-white">{profile.email_masked || "your email"}</b>. After you
          enter it you choose a new password; you&apos;ll be signed out everywhere and log in again.</p>
        {error && <Alert tone="red">{error}</Alert>}
        <Button busy={busy} disabled={!profile.email} onClick={() => run(async () => setCh((await studioApi.profileResetStart()).challenge))}>
          <Mail className="h-4 w-4" />Send the code to my email</Button>
        {!profile.email && <p className="text-xs text-amber-300">Your account has no email address yet - ask your admin to add it.</p>}
      </div>
    );
  }
  return (
    <form className="space-y-3" data-testid="forgot-form" onSubmit={(e) => { e.preventDefault(); void run(async () => setCh((await studioApi.forgot(email.trim())).challenge)); }}>
      <p className="text-xs text-slate-300">Enter the email address of your account. If it is registered, we send a code that is valid for 10 minutes.</p>
      <Field label="Registered email">
        <input className={inputCls} type="email" value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="email" required maxLength={254} autoFocus />
      </Field>
      {error && <Alert tone="red">{error}</Alert>}
      <Button type="submit" busy={busy} disabled={!email.trim()} className="w-full"><Mail className="h-4 w-4" />Email me a code</Button>
    </form>
  );
}

/** The signed-out screen: Log in | Sign up, with "Forgot password?" next to them. */
export function AuthScreen({ onLoggedIn, initialNotice = null }: { onLoggedIn: (username: string) => void; initialNotice?: string | null }) {
  const [mode, setMode] = useState<Mode>("login");
  const [notice, setNotice] = useState<string | null>(initialNotice);
  const [options, setOptions] = useState<AuthOptions | null>(null);
  useEffect(() => { studioApi.options().then(setOptions).catch(() => null); }, []);
  const tabs: { id: Mode; label: string; icon: React.ReactNode }[] = [
    { id: "login", label: "Log in", icon: <LogIn className="h-3.5 w-3.5" /> },
    ...(options?.signup_enabled === false ? [] : [{ id: "signup" as Mode, label: "Sign up", icon: <UserPlus className="h-3.5 w-3.5" /> }]),
  ];
  const title = mode === "forgot" ? "Forgot your password?" : mode === "signup" ? "Create your Understudy account" : "Log in to Understudy Studio";
  return (
    <div className="mx-auto mt-10 max-w-md px-4">
      <Card collapsible={false} title={title} icon={mode === "forgot" ? <KeyRound className="h-4 w-4 text-amber-400" /> : <LogIn className="h-4 w-4 text-sky-400" />}>
        {mode !== "forgot" && (
          <div className="mb-4 flex items-center gap-1 border-b border-slate-800" role="tablist" aria-label="Account">
            {tabs.map((t) => (
              <button key={t.id} type="button" role="tab" aria-selected={mode === t.id} onClick={() => setMode(t.id)} data-testid={`auth-tab-${t.id}`}
                className={cx("-mb-px inline-flex items-center gap-1.5 border-b-2 px-3 py-2 text-sm", mode === t.id ? "border-sky-400 text-white" : "border-transparent text-slate-400 hover:text-white")}>
                {t.icon}{t.label}</button>
            ))}
            <button type="button" onClick={() => { setMode("forgot"); setNotice(null); }} data-testid="auth-forgot"
              className="ml-auto pb-1 text-xs text-sky-300 hover:underline">Forgot password?</button>
          </div>
        )}
        {mode === "login" && <LoginForm onLoggedIn={onLoggedIn} notice={notice} />}
        {mode === "signup" && <SignupFlow onLoggedIn={onLoggedIn} options={options} />}
        {mode === "forgot" && (
          <div className="space-y-3">
            <ResetFlow onDone={(m) => { setNotice(m); setMode("login"); }} />
            <button type="button" className="inline-flex items-center gap-1 text-xs text-slate-400 hover:text-white" onClick={() => setMode("login")}>
              <ArrowLeft className="h-3 w-3" />Back to log in</button>
          </div>
        )}
        {options && options.email_mode !== "smtp" && mode !== "login" && (
          <p className="mt-3 text-[11px] text-amber-300">This server isn&apos;t sending real email yet ({options.email_mode} mode) - ask your admin.</p>
        )}
      </Card>
    </div>
  );
}

/** Profile tab: username, email, and the OTP password reset. */
export function ProfileView({ onBack, onSignedOut }: { onBack: () => void; onSignedOut: (message: string) => void }) {
  const [p, setP] = useState<UserProfile | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [resetting, setResetting] = useState(false);
  useEffect(() => { studioApi.profile().then((r) => setP(r.profile)).catch((e) => setErr((e as Error).message)); }, []);
  return (
    <div className="mx-auto max-w-2xl space-y-5 p-6" data-testid="profile-view">
      <button type="button" onClick={onBack} className="inline-flex items-center gap-1 text-xs text-slate-400 hover:text-white"><ArrowLeft className="h-3.5 w-3.5" />Back to projects</button>
      <Card title="Profile" icon={<User className="h-4 w-4 text-sky-400" />}>
        {err && <Alert tone="red">{err}</Alert>}
        {p && (
          <dl className="grid gap-3 text-sm sm:grid-cols-2">
            <div><dt className="text-[11px] uppercase tracking-wide text-slate-400">Username</dt><dd className="font-semibold text-white" data-testid="profile-username">{p.username}</dd></div>
            <div><dt className="text-[11px] uppercase tracking-wide text-slate-400">Email</dt>
              <dd className="break-all text-white" data-testid="profile-email">{p.email || <span className="text-amber-300">not set - ask your admin</span>}
                {p.email && (p.email_verified ? <span className="ml-2 text-[11px] text-emerald-300">verified</span> : <span className="ml-2 text-[11px] text-slate-400">not verified yet</span>)}</dd></div>
            <div><dt className="text-[11px] uppercase tracking-wide text-slate-400">Account</dt>
              <dd className="text-slate-300">{p.source === "signup" ? "Signed up" : `Created by ${p.created_by || "admin"}`} on {String(p.created_at).slice(0, 10)}</dd></div>
            <div><dt className="text-[11px] uppercase tracking-wide text-slate-400">Password last changed</dt>
              <dd className="text-slate-300">{p.password_set_at ? String(p.password_set_at).slice(0, 16).replace("T", " ") : "not set"}</dd></div>
          </dl>
        )}
      </Card>
      {p && <TwoFactorCard profile={p} onChanged={setP} />}
      {p && (
        <Card title="Reset password" icon={<KeyRound className="h-4 w-4 text-amber-400" />}>
          {!resetting
            ? <Button onClick={() => setResetting(true)} data-testid="profile-reset"><KeyRound className="h-4 w-4" />Reset password</Button>
            : <ResetFlow profile={p} onDone={onSignedOut} />}
        </Card>
      )}
    </div>
  );
}

/** 9.6: Profile > Two-step sign-in - after the right password, a 6-digit code is emailed and must be entered. */
function TwoFactorCard({ profile: p, onChanged }: { profile: UserProfile; onChanged: (p: UserProfile) => void }) {
  const [ch, setCh] = useState<OtpChallenge | null>(null);
  const [offing, setOffing] = useState(false);
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);
  const run = async (fn: () => Promise<void>) => {
    setBusy(true); setError(null); setInfo(null);
    try { await fn(); } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  };
  const on = !!p.two_factor;
  return (
    <Card title="Two-step sign-in" icon={<ShieldCheck className="h-4 w-4 text-emerald-400" />}
      right={<span data-testid="two-factor-state" className={cx("rounded-full border px-2 py-0.5 text-[11px] font-semibold",
        on ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-300" : "border-slate-600 text-slate-400")}>{on ? "On" : "Off"}</span>}>
      <p className="mb-3 text-xs text-slate-300">
        {on ? <>After your password, we email a 6-digit code to <b className="text-white">{p.email_masked || "your email"}</b> and you enter it to finish
                signing in.{p.two_factor_since ? <> On since {String(p.two_factor_since).slice(0, 10)}.</> : null}</>
            : <>Add a second step to signing in: after your password, we email a 6-digit code to <b className="text-white">{p.email_masked || "your email"}</b>.
                Someone who knows your password still can&apos;t get in without your mailbox.</>}
      </p>
      {info && <div className="mb-3"><Alert tone="green">{info}</Alert></div>}
      {!on && !ch && (
        <>
          <Button busy={busy} disabled={!p.email} data-testid="two-factor-on"
            onClick={() => run(async () => setCh((await studioApi.twoFactorStart()).challenge))}>
            <Mail className="h-4 w-4" />Turn on - email me a code</Button>
          {!p.email && <p className="mt-2 text-xs text-amber-300">Your account has no email address yet - ask your admin to add it.</p>}
          {error && <div className="mt-3"><Alert tone="red">{error}</Alert></div>}
        </>
      )}
      {!on && ch && (
        <CodeStep challenge={ch} busy={busy} error={error} onBack={() => { setCh(null); setError(null); }}
          onResend={async () => { try { return (await studioApi.resendCode(ch.challenge_id)).challenge; } catch (e) { setError((e as Error).message); return null; } }}
          onVerify={(code) => run(async () => { const r = await studioApi.twoFactorVerify(ch.challenge_id, code); setCh(null); onChanged(r.profile); setInfo(r.message); })} />
      )}
      {on && !offing && (
        <Button variant="secondary" onClick={() => { setOffing(true); setError(null); setInfo(null); }} data-testid="two-factor-off">Turn off</Button>
      )}
      {on && offing && (
        <form className="space-y-3" onSubmit={(e) => { e.preventDefault(); void run(async () => {
          const r = await studioApi.twoFactorDisable(password); setOffing(false); setPassword(""); onChanged(r.profile); setInfo(r.message); }); }}>
          <Field label="Your password" hint="Needed to turn two-step sign-in off.">
            <PasswordInput className={inputCls} value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" autoFocus required maxLength={128} />
          </Field>
          {error && <Alert tone="red">{error}</Alert>}
          <div className="flex gap-2">
            <Button type="submit" variant="secondary" busy={busy} disabled={!password}>Turn off two-step sign-in</Button>
            <Button type="button" variant="ghost" onClick={() => { setOffing(false); setPassword(""); setError(null); }}>Cancel</Button>
          </div>
        </form>
      )}
    </Card>
  );
}
