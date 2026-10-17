/**
 * Understudy Studio (v7.0) - user login, projects and the previous-session context search.
 * Accounts are created by the Mission Control admin; the token lives in sessionStorage for this tab only.
 */
import { getApiBase } from "./api";
import type { WizardSession } from "./wizard";

const TOKEN_KEY = "understudy_user_token";
const USER_KEY = "understudy_user_name";
export const LOGIN_EVENT = "understudy:login-required";

export function getUserToken(): string {
  try { return window.sessionStorage.getItem(TOKEN_KEY) || ""; } catch { return ""; }
}
export function getUserName(): string {
  try { return window.sessionStorage.getItem(USER_KEY) || ""; } catch { return ""; }
}
export function setUserToken(token: string, username: string) {
  try { window.sessionStorage.setItem(TOKEN_KEY, token); window.sessionStorage.setItem(USER_KEY, username); } catch { /* storage unavailable */ }
}
export function clearUserToken() {
  try { window.sessionStorage.removeItem(TOKEN_KEY); window.sessionStorage.removeItem(USER_KEY); } catch { /* ignore */ }
}

/** Headers every Studio request carries. */
export function userHeaders(extra?: HeadersInit): Headers {
  const h = new Headers(extra);
  const t = getUserToken();
  if (t) h.set("X-User-Token", t);
  return h;
}

/** For <video src> / <a href> downloads, which cannot send headers. */
export function withAccessToken(url: string): string {
  const t = getUserToken();
  return t ? `${url}${url.includes("?") ? "&" : "?"}access_token=${encodeURIComponent(t)}` : url;
}

export class StudioApiError extends Error {
  status: number;
  details: unknown;
  constructor(message: string, status: number, details?: unknown) { super(message); this.status = status; this.details = details; }
}

/** Signals the page to show the login screen (token expired, password reset by the admin, account disabled...). */
export function signalLoginRequired() {
  clearUserToken();
  try { window.dispatchEvent(new Event(LOGIN_EVENT)); } catch { /* not in a browser */ }
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${getApiBase()}${path}`, { ...init, headers: userHeaders(init?.headers) });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const d = body?.detail;
    if (res.status === 401 && d?.login_required) signalLoginRequired();
    throw new StudioApiError(typeof d === "string" ? d : d?.message || `Request failed (${res.status})`, res.status, d?.details);
  }
  return body as T;
}

const json = (method: string, data?: unknown): RequestInit => ({
  method, headers: { "Content-Type": "application/json" }, body: data === undefined ? undefined : JSON.stringify(data),
});

export interface SessionSummary {
  session_id: string; owner: string | null; project_id: string | null; project_name: string | null; name: string;
  status: string; current_step: number; pillars_passed: number; target_url: string; process_name: string | null;
  framework: string | null; steps: number; has_analysis: boolean; created_at: string; updated_at: string; usable?: boolean;
  /** 9.1 */ name_auto?: boolean; name_source?: string | null;
  /** 8.0 explorer micro-badge */
  badge?: "DRAFT" | "ANALYSED" | "BUILT" | "GOVERNED" | "RUNNING" | "PENDING" | "FAILED" | "VERIFIED";
}
export interface Project {
  project_id: string; owner: string | null; name: string; description: string; created_at: string; updated_at: string;
  sessions: SessionSummary[]; session_count: number; completed: number;
}
export interface HistoryRef { type: "session" | "project"; id: string; name: string; project_name?: string | null; sessions?: number }
export interface HistorySearch {
  query: string;
  sessions: SessionSummary[];
  projects: { project_id: string; name: string; description: string; session_count: number; usable_sessions: number }[];
}

/** 8.1 accounts: email + one-time codes */
export interface AuthOptions {
  signup_enabled: boolean; email_mode: "smtp" | "outbox" | "off"; email_delivering: boolean; otp_minutes: number;
  /** 8.2: only these email domains may self-sign-up ([] = any). "*.acme.com" = any subdomain of acme.com. */
  signup_domains?: string[];
}

/** Does an email address match the admin's signup domains? (the server checks again) */
export function emailDomainAllowed(email: string, domains: string[] | undefined): boolean {
  if (!domains || domains.length === 0) return true;
  const host = (email.split("@").pop() || "").trim().toLowerCase().replace(/\.$/, "");
  return domains.some((d) => (d.startsWith("*.") ? host === d.slice(2) || host.endsWith("." + d.slice(2)) : host === d));
}

export function domainLabel(d: string): string {
  return d.startsWith("*.") ? `@<any>.${d.slice(2)}` : `@${d}`;
}
export interface OtpChallenge {
  challenge_id: string; purpose: "signup" | "reset" | "forgot" | "login" | "2fa_on"; email: string; expires_at: number; expires_in: number;
  attempts_left: number; resend_in: number;
}
export interface UserProfile {
  username: string; display_name: string; email: string | null; email_masked: string | null; email_verified: boolean;
  created_at: string; created_by: string | null; source: "admin" | "signup" | null; last_login: string | null; password_set_at: string | null;
  /** 9.6: two-step sign-in (a code mailed after the password) */
  two_factor?: boolean; two_factor_since?: string | null;
}
/** 9.6: /api/auth/login answers with a token, or - with two-step sign-in on - with a mailed code to enter first. */
export type LoginResult =
  | { token: string; username: string; display_name: string; expires_at: number; two_factor_required?: false }
  | { two_factor_required: true; username: string; challenge: OtpChallenge; message: string };

export const studioApi = {
  login: (username: string, password: string) => req<LoginResult>("/api/auth/login", json("POST", { username, password })),
  loginVerify: (challenge_id: string, code: string) =>
    req<{ token: string; username: string; display_name: string; expires_at: number }>("/api/auth/login/verify", json("POST", { challenge_id, code })),
  twoFactorStart: () => req<{ challenge: OtpChallenge; message: string }>("/api/auth/profile/two-factor/start", json("POST")),
  twoFactorVerify: (challenge_id: string, code: string) =>
    req<{ profile: UserProfile; message: string }>("/api/auth/profile/two-factor/verify", json("POST", { challenge_id, code })),
  twoFactorDisable: (password: string) =>
    req<{ profile: UserProfile; message: string }>("/api/auth/profile/two-factor/disable", json("POST", { password })),
  logout: () => req<{ ok: boolean }>("/api/auth/logout", json("POST")),
  me: () => req<{ username: string | null; login_required: boolean }>("/api/auth/me"),
  options: () => req<AuthOptions>("/api/auth/options"),
  signup: (username: string, email: string, password: string, confirm_password: string) =>
    req<{ challenge: OtpChallenge; message: string }>("/api/auth/signup", json("POST", { username, email, password, confirm_password })),
  signupVerify: (challenge_id: string, code: string) =>
    req<{ token: string; username: string; display_name: string; expires_at: number }>("/api/auth/signup/verify", json("POST", { challenge_id, code })),
  forgot: (email: string) => req<{ challenge: OtpChallenge; message: string }>("/api/auth/password/forgot", json("POST", { email })),
  verifyCode: (challenge_id: string, code: string) =>
    req<{ ticket: string; expires_in: number; username: string }>("/api/auth/code/verify", json("POST", { challenge_id, code })),
  resendCode: (challenge_id: string) => req<{ challenge: OtpChallenge; message: string }>("/api/auth/code/resend", json("POST", { challenge_id })),
  resetPassword: (ticket: string, new_password: string, confirm_password: string) =>
    req<{ ok: boolean; message: string }>("/api/auth/password/reset", json("POST", { ticket, new_password, confirm_password })),
  profile: () => req<{ profile: UserProfile }>("/api/auth/profile"),
  profileResetStart: () => req<{ challenge: OtpChallenge; message: string }>("/api/auth/profile/password/start", json("POST")),
  profileResetVerify: (challenge_id: string, code: string) =>
    req<{ ticket: string; expires_in: number; username: string }>("/api/auth/profile/password/verify", json("POST", { challenge_id, code })),
  profileReset: (ticket: string, new_password: string, confirm_password: string) =>
    req<{ ok: boolean; message: string }>("/api/auth/profile/password/reset", json("POST", { ticket, new_password, confirm_password })),
  projects: () => req<{ projects: Project[]; user: string | null }>("/api/projects"),
  project: (id: string) => req<{ project: Project }>(`/api/projects/${encodeURIComponent(id)}`),
  createProject: (name: string, description: string) => req<{ project: Project }>("/api/projects", json("POST", { name, description })),
  deleteProject: (id: string) => req<{ deleted: string; sessions_deleted: number }>(`/api/projects/${encodeURIComponent(id)}`, { method: "DELETE" }),
  /** 9.1: no name needed - the AI names the session after Step 1 (pass one only to choose it yourself) */
  createSession: (projectId: string, name = "") =>
    req<{ session: WizardSession }>(`/api/projects/${encodeURIComponent(projectId)}/sessions`, json("POST", { name })),
  deleteSession: (sid: string) => req<{ deleted: string }>(`/api/wizard/sessions/${encodeURIComponent(sid)}`, { method: "DELETE" }),
  searchHistory: (q: string, exclude = "") =>
    req<HistorySearch>(`/api/history/search?q=${encodeURIComponent(q)}&exclude=${encodeURIComponent(exclude)}`),
  // 8.3: notifications (the Studio bell)
  notifications: (limit = 50) => req<{ notifications: StudioNotification[]; unread: number }>(`/api/wizard/notifications?limit=${limit}`),
  readNotifications: (ids?: string[]) =>
    req<{ changed: number; unread: number; notifications: StudioNotification[] }>("/api/wizard/notifications/read", json("POST", { ids: ids || null })),
};

/** 8.3: a Studio notification (bell). */
export interface StudioNotification {
  id: string; kind: string; title: string; body: string; level: "info" | "success" | "warning" | "error"; at: string; read: boolean;
  link: { session_id?: string; escalation_id?: string };
}
