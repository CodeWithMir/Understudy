/**
 * Understudy admin API client (Mission Control): URL exception list, tool whitelist / access requests,
 * tool versions, settings and the detailed activity telemetry.
 */
import { getApiBase } from "./api";
import type { FrameworkOption } from "./wizard";

export interface UrlException { id: string; url: string; host: string; note: string; added_by: string; added_at: string }
export type RequestStatus = "pending" | "suggesting" | "stack_ready" | "planning" | "plan_ready" | "installing"
  | "smoke_review" | "smoke_testing" | "awaiting_admin" | "setup_failed" | "approved" | "rejected";
/** 8.0 zero-trust registry lifecycle (8.2 adds CHOOSE_STACK and SMOKE_REVIEW) */
export type RegistryStatus = "ACTIVE" | "REQUESTED" | "CHOOSE_STACK" | "PROPOSED" | "INSTALLING" | "SMOKE_REVIEW"
  | "SMOKE_TESTING" | "NEEDS_ADMIN" | "FAILED" | "REJECTED";

/** 8.2 AI install assistant: the stack the AI suggests for a tool. */
export interface StackLanguage { id: string; name: string; recommended: boolean; notes: string; package_managers: string[] }
export interface StackPackageManager {
  id: string; name: string; language: string; ecosystem: string; supported: boolean; available: boolean; server: string; notes: string;
  /** 8.2: Understudy can install with it, but the admin switched it off */
  disabled_by_admin?: boolean;
}
export interface StackComponent {
  id: string; name: string; role: string; purpose: string; required: boolean; default_selected: boolean; conflicts_with: string[];
  packages: { language: string; package_manager: string; name: string; recommended_version: string }[];
}
export interface StackSuggestion {
  tool_family: string; summary: string; category: string; languages: StackLanguage[]; package_managers: StackPackageManager[];
  components: StackComponent[]; run_profiles: { language: string; package_manager: string; requires: string[]; run_command: string[]; entry_file: string }[];
  os_requirements: string[]; questions: string[]; agent_source: string; model?: string; suggested_at: string; notes: string[];
  toolchains: Record<string, string>; note?: string;
}
export interface StackSelection {
  language: string; package_manager: string; ecosystem: string; components: string[]; versions: Record<string, string>;
  used: { id: string; name: string; role: string; package: string; version: string }[]; notes?: string[]; by?: string; at?: string;
  package_managers_available?: string[];
}
export interface StackVersions {
  language: string; package_manager: string; ecosystem: string;
  components: Record<string, { available: boolean; package?: string; versions?: string[]; latest?: string | null; recommended?: string; source?: string; error?: string | null }>;
}
/** 8.2: package managers / build tools / runtimes the admin controls. */
export interface PackageManagerRow {
  id: string; name: string; kind: "package_manager" | "build_tool"; languages: string[]; ecosystem: string; note: string;
  enabled: boolean; changed_by?: string | null; changed_at?: string | null; ready: boolean; missing: string[];
  needs: { name: string; found: boolean; path: string | null; version: string }[];
  downloads: { key: string; label: string; installed: boolean; registered: boolean }[];
}
export interface ToolchainRow { key: string; kind: string; home: string; bin: string; version?: string; source: "download" | "path"; by?: string; at?: string; exists: boolean; env_name?: string }
/** 8.3: the AI's choice of download for missing programs (or its diagnosis of a failed download). */
export interface RuntimeAdvice {
  id?: string; programs?: string[]; recommended: string; alternatives: string[]; expected_programs: string[];
  summary: string; diagnosis?: string; steps: string[]; manual?: string; agent_source: string; model?: string;
  labels?: Record<string, string>; failed_key?: string; os?: string; at?: string;
}
export interface ToolchainJob {
  key: string; label?: string; status: "running" | "done" | "failed"; log: string[]; error?: string | null; home?: string; version?: string;
  verified?: Record<string, { found: boolean; version: string; path: string | null }>; diagnosis?: RuntimeAdvice; advice?: string | null;
}
export interface PackageManagersState {
  package_managers: PackageManagerRow[]; toolchains: ToolchainRow[];
  java_for_builds: { home: string; major: number; source: string } | null;
  jobs: Record<string, ToolchainJob>;
  downloadable: RuntimeLine[];
  advice?: Record<string, RuntimeAdvice>;
}
/** 8.3: a run that passed step 4 but failed the AI's verification, sent to the admin by the user. */
export interface EscalationAttachment { id: string; name: string; size: number; content_type: string; ext: string; by: string; at: string }
export type EscalationScope = "everyone" | "user";
/** 9.3: a tool change the admin approved for one user only */
export interface UserOverride { user: string; tool_id: string; kind: "package" | "version" | "env" | "path"; key: string; value: string; by?: string; at?: string }

export interface Escalation {
  /** 9.3: who the confirmed fix applied to */
  scope?: EscalationScope;
  /** 8.3: "runtime_help" = self-healing needs an admin change (allow a version, install build tools ...) */
  kind?: "runtime_help";
  admin_action?: { action: "allow_version" | "server_change" | "use_version" | "add_package"; version?: string; request?: string; tool_id?: string; tool_name?: string;
    /** 8.5.3: the package the request asks for (e.g. robotframework-jsonlibrary) */
    package?: string;
    /** 8.4: the runtime switch / download the pre-run check proposed */
    kind?: string; fix?: RuntimeFix };
  id: string; session_id: string; owner: string; project_name?: string | null; session_name?: string | null;
  status: "open" | "info_requested" | "confirmed" | "rejected" | "superseded" | "withdrawn";
  created_at: string; updated_at?: string; message: string; attachments: EscalationAttachment[];
  question?: string | null; admin_note?: string | null; decided_by?: string | null; decided_at?: string | null;
  conversation: { role: "user" | "admin"; by: string; text: string; at: string; attachments?: string[]; decision?: string }[];
  evidence?: {
    target_url?: string; tool?: string; tool_version?: string | null; retries?: number;
    verify: { summary?: string; method?: string; checks?: { check?: string; name?: string; passed?: boolean }[]; explanation?: string;
      visual_diff_ratio?: number | null; has_before?: boolean; has_after?: boolean };
    execute?: { status?: string; strategy?: string; duration_ms?: number; log_tail?: string[] };
    parameters?: { key: string; label?: string; value: string }[];
  };
}
export interface EscalationRow {
  id: string; status: Escalation["status"]; owner: string; project_name?: string | null; session_name?: string | null; session_id: string;
  created_at: string; updated_at?: string; decided_by?: string | null; message: string; question?: string | null; attachments: number;
  tool?: string | null; verify_summary?: string | null; kind?: "runtime_help" | null;
}
/** 8.2: the AI-written smoke test waiting for the admin's review. */
export interface SmokeReview {
  files: Record<string, string>; source: string; notes: string[]; ai_notes?: string; written_at: string; approved: boolean;
  revision: number; feedback: string[]; approved_by?: string; rewrite_error?: string | null; previous_failure?: string | null;
}
/** Phase A: what the AI will set up (OS dependencies, package commands, environment, runner wrapper). */
export interface SetupManifest {
  os_dependencies: { name: string; check: "binary" | "env"; value: string; why?: string }[];
  package_commands: string[]; env_vars: Record<string, string>; path_prepend?: string[]; interpreter?: string;
  runner_notes?: string; sandbox: string; smoke_test: string;
  runner_wrapper?: Record<string, string>;
}
/** One line of the setup chat between the AI synthesizer and the admin. */
export interface SetupMessage {
  id: string; role: "ai" | "admin" | "system" | "user"; kind: string; text: string; at: string;
  data?: { options?: ClarifyOption[]; output?: string; command?: string; exit_code?: number | null; diagnosis?: string;
    category?: string; agent_source?: string; [k: string]: unknown };
}
export interface ClarifyOption { id: string; label: string; effect?: string; changes?: Record<string, unknown> }
/** Phase C: the setup halted; the AI asks the admin how to continue. */
export interface Clarification {
  question: string; options: ClarifyOption[]; diagnosis?: string; category?: string; agent_source?: string;
  round?: number; stage?: string; asked_at?: string; model?: string | null;
  failure?: { stage: string; message: string; command?: string; exit_code?: number | null; output?: string; runner?: string };
  /** 8.5: the setup repair agent's turn - what it checked, what it proposes */
  agent?: AgentTurn;
}
export interface AgentCheck { check: string; label: string; ok: boolean; exit_code: number | null; output: string }
export interface AgentStep {
  kind: "checks" | "proposal" | "error"; thought?: string; checks?: AgentCheck[]; summary?: string; problems?: string[];
  precheck?: AgentCheck[]; text?: string;
}
export interface AgentTurn {
  n: number; steps: AgentStep[]; repeated?: boolean; model?: string | null; action?: string; error_line?: string;
  /** 8.5.1: the stronger model offered to the admin (never switched automatically) / the one the admin chose */
  offer_model?: string | null; requested_model?: string | null;
  diagnosis?: string;
  fix?: { summary: string; confidence?: string; describe: string; key: string;
    changes: Record<string, unknown> & { smoke_files?: Record<string, string> } } | null;
}
export interface SmokeResult {
  status: "PASSED" | "FAILED" | "SKIPPED" | "SEEDED" | string; exit_code?: number; source?: string; notes?: string[];
  duration_ms?: number; output?: string; url?: string; runner?: string; at?: string; detail?: string;
  files?: Record<string, string>;
}
export interface RegistryEntry {
  tool_id: string; name: string; status: RegistryStatus; versions?: VersionPolicy; source?: string; approved_by?: string;
  approved_at?: string; smoke?: SmokeResult | null; verified?: boolean; baseline?: boolean; request?: string | null;
  sandbox?: { kind?: string } | null; kind?: string; version?: string | null; sessions?: number;
  requested_by?: string[]; created_at?: string;
}
/** 9.9.1: always the local process sandbox (Understudy runs on Cloud Run, which has no Docker). */
export interface SandboxSettings { mode: "local"; memory_mb: number; timeout_s: number; install_timeout_s: number;
  source?: string; updated_by?: string; updated_at?: string }
export interface SandboxInfo {
  settings: SandboxSettings; effective: "local"; problem: string; modes: string[]; bounds: Record<string, [number, number]>;
}
export interface InboxItem {
  id: string; kind?: string; tool_id: string; tool_name: string; version?: string | null; status: RequestStatus;
  requested_by?: string[]; sessions?: string[]; created_at: string; reason?: string; question?: string | null;
  registry_status?: RegistryStatus;
}
export interface InboxPage { open: InboxItem[]; needs_admin: number; escalations_open?: number; events: { id: string; type: string; at: number; data?: Record<string, unknown> }[]; now: number; live_sockets: number }
export interface SetupPlan {
  kind: "tool" | "version"; tool_id: string; tool_name: string; summary: string; spec: FrameworkOption | null;
  sources?: { index_url?: string; local_dir?: string }; download_steps?: string[]; commands?: string[];
  manifests?: Record<string, string>; already_satisfied?: string[]; toolchain_missing?: string[]; requirements?: string[];
  versions?: { allowed: string[]; default: string | null; notes: string; blocked: string[];
    registry: { source: string; versions: string[]; latest: string | null; error: string | null; package?: string } };
  code_generation?: string; template?: string | null; policy_diff?: string; risks?: string[];
  instructions_applied?: string[]; notes: string[]; agent_source: string; model?: string; prepared_at: string;
  manifest?: SetupManifest;
}
export interface ToolRequest {
  id: string; kind?: "tool" | "version"; tool_id: string; tool_name: string; version?: string | null; spec: FrameworkOption;
  reason: string; source: string; requested_by?: string[]; sessions: string[]; status: RequestStatus; created_at: string;
  decided_at: string | null; decided_by?: string | null; admin_note: string;
  plan?: SetupPlan | null; plan_history?: SetupPlan[]; instructions?: { by: string; at: string; text: string }[];
  setup?: { status: string; logs: string[]; message?: string; result?: string; allowed: string[]; default: string | null;
    forced?: boolean; started_at?: string; finished_at?: string; round?: number; install_version?: string | null;
    sandbox?: { kind?: string; [k: string]: unknown }; install?: { status?: string; [k: string]: unknown };
    smoke?: SmokeResult; failure?: Clarification["failure"]; runtimes?: string[];
    smoke_review?: SmokeReview | null; rewriting?: boolean; java?: { home?: string; major?: number; source?: string; release?: number } } | null;
  conversation?: SetupMessage[]; clarification?: Clarification | null;
  stack?: { suggestion?: StackSuggestion | null; selection?: StackSelection | null; error?: string; notes?: string[]; original_tool_id?: string } | null;
}
export interface BlockedTool { id: string; key: string; name: string; versions: string[]; note: string; added_by: string; added_at: string }
export interface VersionPolicy { allowed: string[]; default: string | null; set_by?: string | null; set_at?: string | null }
export interface WhitelistedTool extends FrameworkOption {
  approved_by: string; approved_at: string; whitelist_source: string; builtin?: boolean; versions: VersionPolicy; toolchain_missing: string[];
  /** 8.3: run settings every run of the tool gets (editable by the admin) */
  runtime?: { env_vars: Record<string, string>; path_prepend: string[]; java_home?: string; java_release?: number | null };
  /** 9.0: who may use the tool */
  access?: ToolAccess;
}
export type AccessMode = "all" | "selected" | "none";
export interface ToolAccess { mode: AccessMode; users: string[]; by?: string | null; at?: string | null }
/** 8.3: changes the admin makes themselves when answering a runtime-help request */
export type AdminFixAction = { type: "allow_version"; version: string } | { type: "set_env"; name: string; value: string }
  | { type: "path_prepend"; path: string } | { type: "proxy_bypass"; host: string } | { type: "network_mode"; mode: "restricted" | "public" }
  | { type: "use_version"; kind: string; install_id?: string; key?: string }
  | { type: "add_package"; name: string; version?: string };

/** 8.4: Mission Control > Environments */
export type RuntimeKind = "java" | "python" | "node" | "ruby" | "go" | "dotnet" | "maven" | "gradle";
export interface RuntimeInstall {
  id: string; kind: RuntimeKind; path: string; home: string; bin: string; version: string; sources: string[];
  source_labels?: string[]; download_key: string; default: boolean;
}
export type RuntimeFix = { type: "switch"; install_id: string; version: string; path: string; label: string }
  | { type: "download"; key: string; label: string; installed?: boolean };
export interface ToolRuntimeRow {
  kind: RuntimeKind; label: string;
  current: { path: string; version: string; install_id: string; source: string; picked: boolean; found: boolean };
  requirement: { spec: string; source: string; reason?: string; by?: string; at?: string } | null;
  requirement_text: string; ok: boolean; tested_with: string; fix: RuntimeFix | null;
}
export interface ToolEnvironment {
  tool_id: string; name: string; ecosystem: string; language: string; runtimes: ToolRuntimeRow[]; ok: boolean;
  verify: { status?: "running" | "passed" | "failed"; detail?: string; output?: string; started_at?: string; finished_at?: string };
  tested_at?: string | null; error?: string;
}
export interface EnvironmentsState {
  kinds: { kind: RuntimeKind; label: string; installs: RuntimeInstall[] }[];
  browsers: { name: string; path: string; version: string }[];
  tools: ToolEnvironment[];
  downloadable: (RuntimeLine & { kind: RuntimeKind; version: string; line: string })[];
  jobs: Record<string, ToolchainJob & { side_by_side?: boolean; switched?: { tool_id: string; version: string } }>;
  sandbox: string; backend_python: string; os: string; scanned_at: string;
}
export interface AvailableVersions {
  tool_id: string; ecosystem: string; package: string | null; catalog_constraint: string; versions: string[];
  latest: string | null; installed: string | null; source: "registry" | "offline" | "none"; error: string | null;
  fetched_at: number | null; policy: VersionPolicy;
}
export type ActivityKind = "USER_ACTION" | "AI_REQUEST" | "AI_RESPONSE" | "AI_REPAIR" | "AI_ERROR" | "RECALL" | "FALLBACK" | "STEP" | "EXECUTION"
  | "RUNTIME" | "SECURITY" | "ADMIN";
export interface ActivityEvent {
  seq: number; ts: string; kind: ActivityKind; session_id: string | null; stage: string | null; title: string;
  detail: string; source: string | null; level: "info" | "warn" | "error"; data: Record<string, unknown>;
  /** "[User] | [Project Name] | [Session Name]" for every event of a run */
  context_label?: string;
  context?: { user: string; project: string; project_id: string | null; session_name: string };
}
export interface ActivityPage {
  events: ActivityEvent[]; last_seq: number; more: boolean;
  stats: Record<ActivityKind, number> & { sessions: string[]; users?: string[]; projects?: string[]; ai_calls: number; ai_failures: number; fallbacks: number };
}
export interface AdminPolicy {
  url_exceptions: UrlException[];
  url_blocklist: UrlException[];
  always_blocked: string[];
  tools: {
    whitelist: WhitelistedTool[];
    catalog: (FrameworkOption & { allowed: boolean })[];
    requests: ToolRequest[];
    pending_count: number;
    blocklist: BlockedTool[];
    default_tools: string[];
    registry?: RegistryEntry[];
  };
  settings: { max_self_heal_retries: number; code_health_auto_apply?: boolean; network_mode?: "restricted" | "public"; self_heal_approval?: "ask" | "automatic"; setup_fix_approval?: "ask" | "automatic"; proxy_bypass?: string[];
    setup_agent?: { enabled: boolean; escalation_model: string; by?: string | null; at?: string | null }; setup_lessons?: number };
  auth?: AuthStatus;
  token_required: boolean;
}

/** 9.9: where one runtime source is fetched from - the official site or the admin's mirror */
export interface RuntimeMirror { id: string; label: string; official: string; mirror: string; testable: boolean }

/** 9.9: a release line the official sources offer (Temurin JDK 11, Node.js 18 ...) and whether it is end of life */
export interface RuntimeLine { key: string; label: string; kind?: string; installed?: boolean; eol?: boolean; eol_date?: string; lts?: boolean; note?: string }

/** 9.9: runs a download; an end-of-life line asks the admin first (the server answers 409 with details.eol). */
export async function withEolConfirm<T>(run: (confirmEol: boolean) => Promise<T>): Promise<T | null> {
  try {
    return await run(false);
  } catch (e) {
    const d = (e as AdminApiError).details as { details?: { eol?: boolean } } | undefined;
    if ((e as AdminApiError).status === 409 && d?.details?.eol) {
      if (!window.confirm(`${(e as Error).message}\n\nIt is downloaded and checksum-verified like any other version.`)) return null;
      return run(true);
    }
    throw e;
  }
}

export class AdminApiError extends Error {
  status: number;
  details: unknown;
  constructor(message: string, status: number, details?: unknown) { super(message); this.status = status; this.details = details; }
}

const TOKEN_KEY = "understudy_admin_token";
export function getAdminToken(): string {
  try { return window.sessionStorage.getItem(TOKEN_KEY) || ""; } catch { return ""; }
}
export function setAdminToken(t: string) {
  try { window.sessionStorage.setItem(TOKEN_KEY, t); } catch { /* storage unavailable */ }
}
export function clearAdminToken() {
  try { window.sessionStorage.removeItem(TOKEN_KEY); } catch { /* storage unavailable */ }
}

export interface StudioUser {
  username: string; display_name: string; email: string | null; email_verified?: boolean; active: boolean;
  status: "active" | "blocked" | "disabled"; blocked: { reason: string; by: string; at: string } | null;
  password_set: boolean; source?: "admin" | "signup" | null;
  /** 9.6: two-step sign-in (emailed code after the password) is on */
  two_factor?: boolean;
  created_by: string; created_at: string; updated_at: string;
  last_login: string | null; password_set_at: string | null; password_set_by: string | null;
  projects?: number; sessions?: number; completed?: number;
}
/** 8.1 outgoing email (SMTP / outbox) - the SMTP password is never returned */
export interface MailStatus {
  mode: "smtp" | "outbox" | "off"; host: string; port: number; security: string; user: string; from: string; from_name: string;
  admin_contact: string; password_set: boolean; delivering: boolean; problem: string;
  stats: { sent: number; failed: number; last_error: string | null; last_error_at: number | null; last_sent_at: number | null };
}
export interface SignupStatus {
  enabled: boolean; source: "admin" | "env"; env_default: boolean; set_by: string | null; set_at: string | null;
  /** 8.2: email domains allowed to self-sign-up ([] = any) */
  domains?: string[]; domains_source?: "admin" | "env"; domains_set_by?: string | null; domains_set_at?: string | null;
}
export interface UserActionResult { user: StudioUser; mail_sent: boolean; mail_error: string | null }
export interface StudioUserResult { user: StudioUser; password: null; mail_sent: boolean; mail_error: string | null }

export interface GcpCheck { id: string; name: string; status: "PASS" | "FAIL" | "WARN" | "SKIP"; detail: string; fix: string }
export interface GcpCheckReport { status: "PASS" | "FAIL" | "WARN"; network: boolean; checks: GcpCheck[]; checked_at: number }

export interface AuthStatus { username: string; default_password: boolean; changed_at: number | null; resets_on_restart: boolean; active_sessions: number; user?: string; shared?: boolean }
export interface LoginResult { token: string; username: string; expires_at: number; default_password: boolean }
export interface SkillSummary { id: string; framework_id: string; framework_name: string; scope: string; scope_label: string; updated_at: string; lessons: number; learned: number; path: string }
export interface RecallMemory { id: string; prompt_id: string; step: string; label: string; learned_with: string; platform: string; created_at: string; updated_at: string; hits: number; last_used: string | null }
export interface RecallStats { enabled: boolean; total: number; by_step: Record<string, number>; reuses: number }
export interface RecallList { dir: string; stats: RecallStats; memories: RecallMemory[] }
export interface SkillLesson { id: string; category: string; signature: string; example?: string; status: "learned" | "unresolved"; occurrences: number; successes: number; fix: Record<string, unknown>; last_seen?: string }
export interface SkillDetail { skill: { id: string; framework_name: string; scope_label: string; lessons: SkillLesson[] }; markdown: string }
export interface CodeFinding {
  id: string; file: string | null; line: number; category: string; message: string; status: string; external_file?: string | null;
  first_seen?: string; last_seen?: string;
  proposal?: { ok: boolean; error?: string; diagnosis?: string; strategy?: string; explanation?: string; risk?: string;
    edits?: { find: string; replace: string }[]; agent_source?: string; model?: string;
    diff?: string; changes?: LineChange[];
    verification?: { passed: boolean; compiled?: boolean; safe?: boolean; warning_gone?: boolean; import_ok?: boolean; new_warnings?: string[]; log: string[] } };
  history_id?: string;
}
/** One changed block: 1-based inclusive line ranges in the original (old) and the changed (new) file. */
export interface LineChange { kind: "changed" | "added" | "removed"; old_lines: [number, number]; new_lines: [number, number]; before: string[]; after: string[] }
export type ChangeStatus = "in_place" | "rolled_back" | "modified_since" | "file_missing";
/** An AI code change that was applied: who, why, what (diff), where the backup is, and its live status. */
export interface CodeChange {
  id: string; finding: string; file: string; at: string; by: string; applied_by: string; status: ChangeStatus;
  warning?: { category: string; message: string; line: number }; diagnosis?: string; explanation?: string; strategy?: string;
  risk?: string; agent_source?: string; model?: string; diff?: string; changes?: LineChange[]; summary?: string[];
  verification?: { compiled?: boolean; safe?: boolean; warning_gone?: boolean; import_ok?: boolean; new_warnings?: string[]; log?: string[] };
  backup: string; backup_dir?: string; backup_exists: boolean; rolled_back: boolean; rolled_back_at?: string; rolled_back_by?: string;
  rolled_back_forced?: boolean; saved_before_revert?: string;
}
export interface ChangesSummary {
  total: number; in_place: number; rolled_back: number; modified_since: number; file_missing: number; by_ai_alone: number;
  files_in_place: string[]; last_change_at: string | null; restart_needed: boolean; restart_files: string[];
}
export interface EnvAdvice {
  summary: string; risk?: string; steps: { title: string; detail?: string; command?: string }[];
  file_changes?: { file: string; change: string }[]; agent_source: string; model?: string; ai_error?: string | null; at: string;
}
export interface EnvFinding {
  id: string; kind: "environment"; category: string; severity: "error" | "warn" | "info"; title: string; message: string;
  status: "open" | "advised" | "ignored" | "resolved"; facts: Record<string, unknown>; advice?: EnvAdvice; last_seen?: string;
}
export interface CodeHealthState {
  environment: EnvFinding[];
  findings: CodeFinding[]; history: CodeChange[]; changes: ChangesSummary; backups_dir: string;
  last_scan: { at: string; import_ok: boolean; error: string | null; count: number; duration_s?: number } | null;
  busy: boolean; task: string | null; ai_available: boolean; auto_apply: boolean; enabled: boolean; protected_files: string[];
}

async function call<T = AdminPolicy>(path: string, method = "GET", body?: unknown): Promise<T> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  const token = getAdminToken();
  if (token) headers["X-Admin-Token"] = token;
  const res = await fetch(`${getApiBase()}/api/admin${path}`, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const d = data?.detail;
    throw new AdminApiError(typeof d === "string" ? d : d?.message || `Request failed (${res.status})`, res.status, d);
  }
  return data as T;
}

/** Google Cloud Model Armor (7.2) */
export interface ArmorVerdict { blocked: boolean; filters: string[]; ms: number }
export interface ArmorTest {
  template: string; ok: boolean; reachable: boolean; clean: ArmorVerdict | null; attack: ArmorVerdict | null;
  error: string | null; fix: string | null; checked_at: number;
}
export interface ModelArmorStatus {
  enabled: boolean; template: string; fail_closed: boolean; source: "admin" | "env" | "none";
  updated_by?: string; updated_at?: string; project?: string; location?: string; template_id?: string;
  suggested: { project: string; location: string; template_id: string };
  env_template: string; env_fail_closed: boolean;
  stats: { screened: number; blocked: number; unavailable: number; last_error: string | null; last_error_at: number | null; last_block_at: number | null };
  filters_available: string[]; default_filters: string[]; service_account: string;
  samples: { clean: string; attack: string };
  test: ArmorTest | null;
  create?: { ok: boolean; already_existed: boolean; template: string; error: string | null; fix: string | null };
}
export interface ArmorTarget { project: string; location: string; template_id: string }

type WithRequest = AdminPolicy & { request: ToolRequest };
/** 9.9: the browser stacks Understudy's agent can run on (Mission Control's agentic tool builder) */
export interface AgenticToolOptions {
  language: string; genai_package: string;
  drivers: { id: string; label: string; package: string; default_version: string; browsers: { id: string; label: string }[] }[];
}
export interface AgenticToolBody {
  driver: string; browser: string; name?: string; driver_version?: string; genai_version?: string;
  extra_packages?: string[]; model?: string;
}

export const adminApi = {
  agenticToolOptions: () => call<AgenticToolOptions>("/agentic-tools/options"),
  createAgenticTool: (body: AgenticToolBody) => call<WithRequest>("/agentic-tools", "POST", body),
  addToolWithAi: (name: string, version?: string, instruction?: string, assistant = true) =>
    call<WithRequest>("/tool-setup", "POST", { name, version: version || null, instruction: instruction || null, assistant }),
  suggestStack: (id: string, note?: string) => call<WithRequest>(`/tool-requests/${id}/stack/suggest`, "POST", { note: note || null }),
  stackVersions: (id: string, language: string, pm: string, refresh = false) =>
    call<StackVersions>(`/tool-requests/${id}/stack/versions?language=${encodeURIComponent(language)}&package_manager=${encodeURIComponent(pm)}${refresh ? "&refresh=true" : ""}`),
  selectStack: (id: string, sel: { language: string; package_manager: string; package_managers_available: string[]; components: string[]; versions: Record<string, string> }) =>
    call<WithRequest>(`/tool-requests/${id}/stack/select`, "POST", sel),
  environments: (refresh = false) => call<EnvironmentsState>(`/environments${refresh ? "?refresh=true" : ""}`),
  selectToolRuntime: (toolId: string, kind: string, installId: string | null, verify = true) =>
    call<{ tool: ToolEnvironment }>(`/environments/tools/${encodeURIComponent(toolId)}/select`, "POST", { kind, install_id: installId, verify }),
  setToolRequirements: (toolId: string, requirements: Record<string, string>) =>
    call<{ tool: ToolEnvironment }>(`/environments/tools/${encodeURIComponent(toolId)}/requirements`, "PUT", { requirements }),
  /** 9.9: mirrors for runtime downloads (internal Artifactory / Nexus) */
  runtimeMirrors: () => call<{ sources: RuntimeMirror[] }>("/runtime-mirrors"),
  setRuntimeMirror: (source: string, url: string) => call<{ sources: RuntimeMirror[] }>(`/runtime-mirrors/${encodeURIComponent(source)}`, "PUT", { url }),
  testRuntimeMirror: (source: string, url?: string) =>
    call<{ ok: boolean | null; url: string; detail: string; bytes?: number }>(`/runtime-mirrors/${encodeURIComponent(source)}/test`, "POST", { url: url || "" }),
  downloadRuntime: (key: string, toolId?: string, confirmEol = false) =>
    call("/environments/download", "POST", { key, tool_id: toolId || null, confirm_eol: confirmEol }),
  packageManagers: () => call<PackageManagersState>("/package-managers"),
  setPackageManager: (id: string, enabled: boolean) => call<PackageManagersState>(`/package-managers/${id}`, "PUT", { enabled }),
  downloadToolchain: (key: string, expect?: string[], adviceId?: string, confirmEol = false) =>
    call<PackageManagersState>("/toolchains/download", "POST", { key, expect: expect || [], advice_id: adviceId || null, confirm_eol: confirmEol }),
  adviseToolchain: (programs: string[], context = "") => call<{ advice: RuntimeAdvice }>("/toolchains/advise", "POST", { programs, context }),
  escalations: (status?: string) => call<{ escalations: EscalationRow[]; open: number }>(`/escalations${status ? `?status=${status}` : ""}`),
  escalation: (id: string) => call<{ escalation: Escalation }>(`/escalations/${id}`),
  /** 9.3: scope "user" = only the user who asked gets packages / versions / run variables / PATH folders */
  decideEscalation: (id: string, decision: "confirm" | "reject" | "ask", note: string, actions: AdminFixAction[] = [], rerun = false,
    scope: EscalationScope = "everyone") =>
    call<{ escalation: Escalation }>(`/escalations/${id}/decide`, "POST", { decision, note, actions, rerun, scope }),
  userOverrides: () => call<{ overrides: UserOverride[] }>("/user-overrides"),
  removeUserOverride: (o: Pick<UserOverride, "user" | "tool_id" | "kind" | "key">) =>
    call<{ overrides: UserOverride[] }>("/user-overrides/remove", "POST", o),
  setProxyBypass: (hosts: string[]) => call("/proxy-bypass", "PUT", { hosts }),
  setToolRuntime: (toolId: string, envVars: Record<string, string>, pathPrepend: string[]) =>
    call(`/tools/${encodeURIComponent(toolId)}/runtime`, "PUT", { env_vars: envVars, path_prepend: pathPrepend }),
  registerToolchain: (kind: string, path: string) => call<PackageManagersState>("/toolchains", "PUT", { kind, path }),
  removeToolchain: (kind: string) => call<PackageManagersState>(`/toolchains/${kind}`, "DELETE"),
  approveSmoke: (id: string) => call<WithRequest>(`/tool-requests/${id}/smoke/approve`, "POST"),
  reviseSmoke: (id: string, feedback: string) => call<WithRequest>(`/tool-requests/${id}/smoke/revise`, "POST", { feedback }),
  planRequest: (id: string, instruction?: string) =>
    call<WithRequest>(`/tool-requests/${id}/plan`, "POST", { instruction: instruction || null }),
  setupRequest: (id: string, allowed: string[], def: string | null, force = false) =>
    call<WithRequest>(`/tool-requests/${id}/setup`, "POST", { allowed, default: def, force }),
  answerRequest: (id: string, text: string, option?: string) =>
    call<WithRequest>(`/tool-requests/${id}/answer`, "POST", { text, option: option || "" }),
  rerunSmoke: (toolId: string) => call<AdminPolicy & { smoke: SmokeResult }>(`/tools/${encodeURIComponent(toolId)}/smoke`, "POST"),
  inbox: (after = 0) => call<InboxPage>(`/inbox?after=${after}`),
  sandbox: () => call<SandboxInfo>("/sandbox"),
  saveSandbox: (b: Partial<SandboxSettings> & { mode: SandboxSettings["mode"] }) => call<SandboxInfo>("/sandbox", "PUT", b),
  blockTool: (name: string, versions: string[], note: string) => call("/tool-blocklist", "POST", { name, versions, note }),
  unblockTool: (id: string) => call(`/tool-blocklist/${id}`, "DELETE"),
  modelArmor: () => call<ModelArmorStatus>("/model-armor"),
  saveModelArmor: (b: ArmorTarget & { enabled: boolean; fail_closed: boolean; force?: boolean }) =>
    call<ModelArmorStatus>("/model-armor", "PUT", b),
  resetModelArmor: () => call<ModelArmorStatus>("/model-armor", "DELETE"),
  testModelArmor: (t: ArmorTarget) => call<ModelArmorStatus>("/model-armor/test", "POST", t),
  createModelArmorTemplate: (t: ArmorTarget & { filters: string[]; confidence: string }) =>
    call<ModelArmorStatus>("/model-armor/template", "POST", t),
  policy: () => call("/policy"),
  addUrl: (url: string, note: string) => call("/url-exceptions", "POST", { url, note }),
  removeUrl: (id: string) => call(`/url-exceptions/${id}`, "DELETE"),
  addBlocked: (url: string, note: string) => call("/url-blocklist", "POST", { url, note }),
  removeBlocked: (id: string) => call(`/url-blocklist/${id}`, "DELETE"),
  setNetworkMode: (mode: "restricted" | "public") => call("/network-mode", "PUT", { mode }),
  setHealApproval: (mode: "ask" | "automatic") => call("/self-heal-approval", "PUT", { mode }),
  setSetupFixApproval: (mode: "ask" | "automatic") => call("/setup-fix-approval", "PUT", { mode }),
  setSetupAgent: (enabled?: boolean, escalationModel?: string) =>
    call("/setup-agent", "PUT", { enabled: enabled ?? null, escalation_model: escalationModel ?? null }),
  revokeTool: (id: string) => call(`/tools/${encodeURIComponent(id)}`, "DELETE"),
  setToolAccess: (id: string, mode: AccessMode, users: string[]) =>
    call(`/tools/${encodeURIComponent(id)}/access`, "PUT", { mode, users }),
  reject: (id: string, note: string) => call(`/tool-requests/${id}/reject`, "POST", { note }),
  saveSettings: (max_self_heal_retries: number) => call("/settings", "PUT", { max_self_heal_retries }),
  versions: (id: string, refresh = false) =>
    call<AvailableVersions>(`/tools/${encodeURIComponent(id)}/versions${refresh ? "?refresh=true" : ""}`),
  saveVersions: (id: string, allowed: string[], def: string | null) =>
    call(`/tools/${encodeURIComponent(id)}/versions`, "PUT", { allowed, default: def }),
  telemetry: (after: number, limit = 1000) => call<ActivityPage>(`/telemetry?after=${after}&limit=${limit}`),
  users: () => call<{ users: StudioUser[]; login_required: boolean; signup_enabled: boolean; signup: SignupStatus; mail: MailStatus }>("/users"),
  setSignup: (enabled: boolean | null) => call<SignupStatus>("/signup", "PUT", { enabled }),
  setSignupDomains: (domains: string[] | null) => call<SignupStatus>("/signup/domains", "PUT", { domains }),
  createUser: (username: string, email: string, password: string | null, display_name?: string) =>
    call<StudioUserResult>("/users", "POST", { username, email, password: password || null, display_name: display_name || null }),
  askPasswordReset: (username: string) => call<{ user: StudioUser; mail_sent: boolean }>(`/users/${encodeURIComponent(username)}/password-reset`, "POST"),
  setUserEmail: (username: string, email: string) => call<{ user: StudioUser }>(`/users/${encodeURIComponent(username)}/email`, "PUT", { email }),
  blockUser: (username: string, reason: string) => call<UserActionResult>(`/users/${encodeURIComponent(username)}/block`, "POST", { reason }),
  unblockUser: (username: string) => call<UserActionResult>(`/users/${encodeURIComponent(username)}/unblock`, "POST"),
  /** 9.6: turn a user's two-step sign-in off (they lost access to their mailbox) */
  disableTwoFactor: (username: string) => call<{ user: StudioUser }>(`/users/${encodeURIComponent(username)}/two-factor/disable`, "POST"),
  deleteUser: (username: string, reason: string) =>
    call<{ deleted: string; mail_sent: boolean; mail_error: string | null; projects_archived: number; sessions_archived: number }>(
      `/users/${encodeURIComponent(username)}`, "DELETE", { reason }),
  mail: () => call<MailStatus>("/mail"),
  testMail: (to: string) => call<{ ok: boolean; to: string; mail: MailStatus }>("/mail/test", "POST", { to }),
  login: (username: string, password: string) => call<LoginResult>("/login", "POST", { username, password }),
  logout: () => call<{ ok: boolean }>("/logout", "POST"),
  me: () => call<AuthStatus>("/me"),
  changePassword: (current_password: string, new_password: string, new_username?: string) =>
    call<AuthStatus>("/password", "PUT", { current_password, new_password, ...(new_username ? { new_username } : {}) }),
  skills: () => call<{ dir: string; skills: SkillSummary[] }>("/skills"),
  skill: (id: string) => call<SkillDetail>(`/skills/${encodeURIComponent(id)}`),
  deleteSkill: (id: string) => call<{ dir: string; skills: SkillSummary[] }>(`/skills/${encodeURIComponent(id)}`, "DELETE"),
  recall: () => call<RecallList>("/recall"),
  recallItem: (id: string) => call<Record<string, unknown>>(`/recall/${encodeURIComponent(id)}`),
  deleteRecall: (id: string) => call<RecallList>(`/recall/${encodeURIComponent(id)}`, "DELETE"),
  clearRecall: () => call<RecallList>("/recall", "DELETE"),
  codeHealth: () => call<CodeHealthState>("/code-health"),
  gcpCheck: (network = true) => call<GcpCheckReport>(`/gcp/check?network=${network}`, "POST"),
  codeHealthRun: () => call<CodeHealthState>("/code-health/run", "POST"),
  envAdvise: (id: string) => call<CodeHealthState>(`/code-health/env/${id}/advise`, "POST"),
  envIgnore: (id: string) => call<CodeHealthState>(`/code-health/env/${id}/ignore`, "POST"),
  codeHealthPropose: (id: string) => call<CodeHealthState>(`/code-health/${id}/propose`, "POST"),
  codeHealthApply: (id: string) => call<CodeHealthState>(`/code-health/${id}/apply`, "POST"),
  codeHealthIgnore: (id: string) => call<CodeHealthState>(`/code-health/${id}/ignore`, "POST"),
  codeHealthRollback: (hid: string, force = false) =>
    call<CodeHealthState>(`/code-health/history/${hid}/rollback${force ? "?force=true" : ""}`, "POST"),
  /** Downloads the original file (before the AI changed it). */
  codeHealthDownloadBackup: async (hid: string) => {
    const token = getAdminToken();
    const res = await fetch(`${getApiBase()}/api/admin/code-health/history/${encodeURIComponent(hid)}/backup`,
      { headers: token ? { "X-Admin-Token": token } : {} });
    if (!res.ok) throw new AdminApiError((await res.json().catch(() => ({})))?.detail?.message || `Request failed (${res.status})`, res.status);
    const name = /filename="([^"]+)"/.exec(res.headers.get("Content-Disposition") || "")?.[1] || `${hid}.txt`;
    const url = URL.createObjectURL(await res.blob());
    const a = document.createElement("a");
    a.href = url; a.download = name; a.click();
    setTimeout(() => URL.revokeObjectURL(url), 2000);
  },
  codeHealthSettings: (auto_apply: boolean) => call<CodeHealthState>("/code-health/settings", "PUT", { auto_apply }),
  clearTelemetry: () => call<{ ok: boolean }>("/telemetry", "DELETE"),
};

/** 8.3: an escalation file / evidence image, fetched with the admin token (opened in a new tab or downloaded). */
export async function openEscalationFile(path: string, filename: string, inline: boolean): Promise<void> {
  const res = await fetch(`${getApiBase()}/api/admin${path}`, { headers: { "X-Admin-Token": getAdminToken() } });
  if (!res.ok) throw new AdminApiError(`Could not open ${filename} (${res.status})`, res.status, null);
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  if (inline) { window.open(url, "_blank", "noopener"); }
  else { const a = document.createElement("a"); a.href = url; a.download = filename; document.body.appendChild(a); a.click(); a.remove(); }
  setTimeout(() => URL.revokeObjectURL(url), 60_000);
}
