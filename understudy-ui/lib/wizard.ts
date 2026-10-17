/**
 * Understudy (v7.0) - typed API client for /api/wizard. Every call carries the Studio user's token.
 */
import { getApiBase } from "./api";
import { signalLoginRequired, userHeaders, withAccessToken, type HistoryRef } from "./studio";

export type PillarName = "WATCH" | "BUILD" | "GOVERN" | "BUILD-EXECUTE" | "EXPORT" | "VERIFY";
export type AgentSource = "ACTUAL_AI_API" | "LEARNED_SKILL" | "LOCAL_HEURISTIC";

/** Set when an answer was re-used offline from what Understudy learned in an earlier AI session. */
export interface RecallInfo {
  memory_id: string;
  score: number;
  label: string;
  learned_with: string;
  learned_at: string;
  exact: boolean;
  step?: string;
  why_no_ai?: string;
  detail?: Record<string, number | boolean | string[][]>;
  threshold?: number;
}

export interface ScriptDiff { file: string; status: "added" | "removed" | "changed"; added: number; removed: number; diff: string; truncated: boolean }
export interface HealProposal {
  id: string; attempt: number; max: number; revision: number; max_revisions: number;
  /** 8.2: e.g. "A record was already saved by this run ..." */
  warning?: string;
  failure: string; failure_reason: string; failed_step?: number | null; category: string; diagnosis: string;
  changes: { step: number; from: string | null; to: string; why?: string }[];
  notes: string[]; question: string; feedback: string[]; feedback_applied: string;
  agent_source: AgentSource; model?: string | null; kind: "script" | "runtime";
  repair_plan: {
    cause: string; repair_id: string; explanation: string; env: Record<string, string>;
    /** 8.2 */ path_prepend?: string[]; download_runtime?: string | null; java_release?: number | null; source?: "rules" | "ai";
    /** 8.3 */ switch_version?: string | null; run_args?: string[]; clean_project_files?: boolean;
    ask_admin?: { action: "allow_version" | "server_change"; version?: string; request: string } | null; ai_diagnosis?: string;
  } | null;
  /** 8.3: approving this sends a request to the admin instead of re-running */
  needs_admin?: { action: string; version?: string; request: string } | null;
  script_diff: ScriptDiff[]; build_error?: string; blocking?: { message?: string; severity: string }[];
  /** 9.0: the heal agent's trace (code rewritten and re-checked on the live page) */
  agent?: BuildAgentTrace;
}

export interface Pillar {
  pillar: PillarName;
  status: "PASS" | "FAIL";
  reason: string;
  timestamp: string;
  evidence?: Record<string, unknown>;
}

export interface Step {
  order: number;
  action: string;
  selector?: string;
  fallback_selectors?: string[];
  label?: string;
  param_key?: string;
  value?: string;
  description?: string;
  /** mm:ss in the recording where the action happens (moment-by-moment WATCH) */
  video_time?: string;
  /** what was seen/heard at that moment */
  evidence?: string;
  /** true: found on the target page; false: not on the start page (later page / appears after a step) */
  grounded?: boolean;
}

export interface Constraints {
  required?: boolean; options?: string[]; min?: string; max?: string; minlength?: string; maxlength?: string;
  pattern?: string; step?: string; format?: string; multiple?: boolean;
}

export interface Grounding {
  page: string; renderer: string; checked: number; on_page: number; constraints: number; skipped?: string;
  repaired: { step: number; from: string | null; to: string; why: string }[];
  not_found: { step: number; selector: string; why: string }[];
  type_fixes: string[]; value_fixes: string[];
}

export interface HistoryContext {
  refs: { type: "session" | "project"; id: string; name: string; project_name?: string | null; sessions?: number }[];
  sessions: { session_id: string; name: string; project_name: string | null; status: string; target_url: string }[];
  skipped: { session_id: string; name: string; why: string }[];
  blocked: { session_id: string; name: string; message: string }[];
  chars: number; applied?: string[]; used_by?: AgentSource;
}

export interface Parameter {
  key: string;
  label: string;
  value: string | number | boolean;
  type: "text" | "number" | "date" | "email" | "select" | "boolean" | "secret";
  source?: string;
  required?: boolean;
  sop_dependent?: boolean;
  description?: string;
  verify?: boolean;
  has_value?: boolean;
  /** what the target page itself accepts for this field */
  constraints?: Constraints;
}

export interface FrameworkOption {
  id: string;
  name: string;
  language: string;
  category: string;
  builtin?: boolean;
  ecosystem: string;
  beginner_summary: string;
  pros: string[];
  cons: string[];
  difficulty: string;
  recommended: boolean;
  packages: { name: string; version: string }[];
  run_command: string[];
  entry_file: string;
  template_available: boolean;
  trusted: boolean;
  requires_approval: boolean;
  untrusted_packages: string[];
  source: string;
  allowed?: boolean;
  /** 9.0: approved, but the admin opened it to selected users only (not this user) - or 9.9: to no user */
  restricted?: boolean;
  access_mode?: "all" | "selected" | "none";
  access_request?: { id: string; status: "pending" | "planning" | "plan_ready" | "installing" | "setup_failed" | "approved" | "rejected"; admin_note: string } | null;
  versions?: { allowed: string[]; default: string | null; set_by?: string | null; set_at?: string | null } | null;
  /** 7.2: package source set by the admin's AI tool setup (private index / directory on the server) */
  sources?: { index_url?: string; local_dir?: string };
  /** 7.2: the admin blocked this tool - it can't be used or requested */
  blocked?: { name: string; note: string } | null;
  /** 7.2: requests for versions the admin hasn't allowed yet */
  version_requests?: { id: string; version: string; status: string; admin_note: string }[];
}

export interface Rule {
  rule_id: string;
  field: string;
  bound?: boolean;
  rule_type: "allowed_values" | "denied_values" | "max" | "min" | "date_range" | "regex" | "required" | "role_access";
  values: string[];
  limit?: number | null;
  exclusive?: boolean;
  start_date?: string | null;
  end_date?: string | null;
  pattern?: string | null;
  source_text?: string;
  plain_english: string;
  source?: string;
  enabled: boolean;
  agreement?: string;
}

export interface Violation {
  rule_id: string;
  field: string;
  kind: string;
  value: unknown;
  message: string;
  fix_hint: string;
  rule: string;
  source_text?: string;
}

/** 8.0: the tool the SOP requires, with its live registry status. */
export interface ToolRequirement {
  tool_id: string; name: string; known: boolean; version: string | null; quote: string; source: "sop" | "ai";
  status: "active" | "version_not_allowed" | "pending" | "blocked" | "unsupported";
  allowed_versions?: string[]; request?: { id: string; status: string; admin_note: string; version?: string | null } | null;
  blocked?: { name: string; note: string }; overridden?: { by: string; tool: string; at: string };
}
/** 8.0: the session is paused until the admin enables a tool (PENDING_ADMIN_APPROVAL). */
export interface PendingRequest {
  id: string; tool_id: string; tool_name: string; version: string | null; kind: "tool" | "version"; requested_at: string;
  step: number; reason: string; status?: string; registry_status?: string; admin_note?: string;
}
export interface ExecutionLogs {
  session_id: string; session_name: string | null; label: string; status: string | null; attempt?: number;
  strategy?: string; started_at?: string; lines: string[]; dependency_lines: string[]; dependency_status: string | null;
  egress?: { mode: string; allow: string[]; denied: { host: string; reason: string }[] } | null;
  retries: { attempt: number; category?: string; diagnosis?: string; outcome?: string }[];
}
export interface LiveEvent { id: string; type: string; at: number; data: Record<string, unknown> }

export interface RuntimeCheckProblem {
  kind: string; label: string; required: string; required_text: string; reason: string; source: string;
  current: string; current_path: string; message: string;
  fix: { type: "switch"; install_id: string; version: string; path: string; label: string } | { type: "download"; key: string; label: string } | null;
}
export interface RuntimeCheck {
  ok: boolean; tool_id: string; tool_name?: string; problems: RuntimeCheckProblem[]; checked_at: string;
  sent?: Record<string, string>; ignored?: boolean; skipped?: string;
}

/** 9.0: the build / heal agent's trace - each round, what it checked on fresh page loads */
export interface BuildAgentTrace {
  rounds: { round: number; result: string; locators?: number; checked?: number; page_loads?: number; problems?: string[];
    failed?: { strategy: string; value: string; problems: string[] }[]; note?: string }[];
  verified_locators: number; page_loads: number; status: "verified" | "unverified" | "not checked" | "not run";
  unresolved?: string[]; profile_summary?: string;
  /** 9.4: the complexity the code was written for, and whether known-unstable ids / positions were checked statically */
  complexity?: { score: number; level: string } | null; static_checks?: boolean;
}
export interface ComplexityFactor { name: string; points: number; severity: "low" | "medium" | "high" | string; detail: string; how_handled: string }
export interface Complexity {
  score: number; level: "Simple" | "Moderate" | "Complex" | "Very complex" | string; summary: string; probed: boolean;
  factors: ComplexityFactor[]; tool_fit: { tool_id: string; name: string; fit: "good" | "ok" | "poor"; why: string }[];
  tips: string[]; agent_source: AgentSource; model: string;
}
/** 9.9: one of the ten parameters Gemini chose for this process (or the rules, offline) - its assessed value and
 * weight, the user's overrides, and its share of the score */
export interface DifficultyParam {
  key: string; name: string; category: string; measures: string; why: string; evidence: string[]; video_time: string;
  source: "ai" | "rules"; library_key: string | null; measured_value: number | null; ai_value: number | null;
  value: number; weight: number; default_weight: number; blocker: boolean; applicable: boolean;
  user_value: number | null; user_weight: number | null; user_applicable: boolean | null;
  effective_value: number; effective_weight: number; effective_applicable: boolean;
  weight_pct: number; share_pct: number; contribution: number;
}
export interface RoutingTool { tool_id: string; name: string; kind: "traditional" | "agentic"; language: string; fit: "good" | "ok" | "poor"; why: string }
export interface Routing {
  approach: "traditional" | "agentic"; label: string; meaning: string;
  confidence: "high" | "medium" | "low"; summary: string;
  why: string[]; against: string[]; notes: string[]; source: AgentSource; model: string;
  rules_approach: "traditional" | "agentic";
  traditional: { strengths: string[]; tools: RoutingTool[] };
  agentic: { strengths: string[]; costs: string[]; tools: RoutingTool[] };
}
export interface Difficulty {
  score: number; level: string; summary: string; probed: boolean;
  adjusted: boolean; assessed_score: number; chosen_by: "ai" | "rules";
  formula: string; floor: { key: string; name: string; value: number; why: string } | null;
  categories: { name: string; weight_pct: number }[];
  parameters: DifficultyParam[]; recommendation: Routing; agent_source: AgentSource; model: string;
  solution_fit: { status: "fit" | "mismatch" | "not chosen"; kind?: string; recommended?: "traditional" | "agentic"; message: string };
  history: { by: string; changes: string[]; score_before: number; score_after: number }[];
}
/** 9.9: an agentic run uses the organisation's Gemini (Vertex AI or the admin's key) - never a key from the user */
export interface AgentAi { available: boolean; platform?: string; model?: string; reason?: string }
/** 9.4: one check of the automation-aware inspection - what was checked, why THIS automation needed it, what it found */
export interface InspectionCheck {
  id: string; title: string; why: string; result: "ok" | "warning" | "failed" | "skipped" | string; finding: string;
  relevant: boolean; page: number;
}
export interface InspectedPage {
  index: number; url: string; final_url?: string; title?: string; loads: number; reload_why?: string; ready_ms?: number;
  /** who decided how many times this page was reloaded (the INSPECTION_RELOAD agent, or "rules" without AI) */
  reload_decided_by?: AgentSource | "rules" | string; verify_loads?: number; verify_why?: string;
  steps?: number[]; summary?: string; field_count?: number; error?: string | null;
}
export interface InspectionPlanView {
  stability?: "always" | "if_suspicious" | "never" | string; stability_why?: string; focus?: string[];
  agent_source?: AgentSource; model?: string;
  pages: { index: number; url: string; steps: number[]; checks: { id: string; title: string; why: string }[] }[];
}
export interface PageEntry {
  url: string; source: "user" | "video"; seen_in_video?: boolean; video_time?: string; evidence?: string; purpose?: string;
}
export interface PageProfileView {
  renderer: string; loads?: number; title?: string; error?: string | null; summary: string;
  signals: Record<string, number | boolean | string | null>;
  fields: { label: string; kind: string; page?: number; recommended?: { strategy: string; value: string } | null; unstable?: string[]; moved_between_loads?: boolean }[];
  pages?: InspectedPage[]; checks?: InspectionCheck[]; plan?: InspectionPlanView | null;
}

export interface WizardSession {
  session_id: string;
  current_step: number;
  status: string;
  tool_requirement?: ToolRequirement | null;
  pending_request?: PendingRequest | null;
  /** 8.0: what the admin decided on this session's last enablement request */
  request_outcome?: { id: string; status: "approved" | "rejected"; tool_id: string; tool_name: string; version?: string | null; note: string; at: string } | null;
  owner?: string | null;
  project_id?: string | null;
  project_name?: string | null;
  session_name?: string | null;
  /** 9.1: still the automatic name - the AI names the session once Step 1 understood the recording */
  name_auto?: boolean;
  name_source?: AgentSource | "USER" | null;
  history_context?: HistoryContext | null;
  dependency_lock?: { extras: { name: string; version?: string; why?: string }[]; primary: string | null;
    chosen_version: string | null; manifests: string[]; notes: string[] } | null;
  target_url: string;
  /** 9.4: every page the process visits, in order (target_urls[0] === target_url) */
  target_urls?: string[];
  sop_text: string;
  media: { filename: string; kind: string; size_bytes: number; sha256: string; sample: boolean } | null;
  analysis: {
    process_name: string;
    summary: string;
    app_type: string;
    steps: Step[];
    verification: { method: string; url: string; api_url?: string; expect_param_keys: string[] };
    api?: { method: string; url: string } | null;
    media_interpreted: boolean;
    security_flags: string[];
    grounding?: Grounding;
    history_applied?: string[];
  } | null;
  analysis_meta?: { agent_source: AgentSource; model: string; ai_error?: string | null; ai_configured?: boolean; recall?: RecallInfo | null };
  parameters_meta?: { agent_source: AgentSource; model: string; recall?: RecallInfo | null };
  inventory: { url: string; title: string; fields: number; buttons: number; links?: number; forms?: number; error?: string; renderer?: string } | null;
  framework: FrameworkOption | null;
  framework_options: FrameworkOption[];
  dependencies: { status: string; message: string; logs: string[]; plan?: { commands: string[][]; manifest_files: Record<string, string> };
    versions?: { name: string; requested: string; installed: string | null; source: string }[] } | null;
  parameters: Parameter[];
  parameter_problems: { key: string; message: string }[];
  suite: {
    files: Record<string, string>;
    entry_file: string;
    generator: "ai" | "learned" | "template" | "agentic";
    recall?: RecallInfo | null;
    agent_source: AgentSource;
    model: string;
    notes: string[];
    findings: { file: string; kind: string; message: string; severity: string }[];
    agent?: BuildAgentTrace;
    diagnosis?: string;
  } | null;
  /** 9.0: how complex this automation is (shown before the user picks a tool) */
  complexity?: Complexity | null;
  /** 9.8: the Automation Difficulty Score (ten parameters) and the traditional / agentic recommendation */
  difficulty?: Difficulty | null;
  /** 9.9: the Gemini an agentic run uses (set up by the admin) */
  agent_ai?: AgentAi;
  agentic?: boolean;
  page_profile?: PageProfileView | null;
  rules: Rule[];
  rules_confirmed: boolean;
  rules_meta: { agent_source?: AgentSource; model?: string; notes?: string[]; security_flags?: string[]; recall?: RecallInfo | null; sop_language?: string; language_warning?: boolean };
  compliance: {
    passed: boolean;
    violations: Violation[];
    warnings: { field: string; message: string }[];
    rules_checked: number;
    ai_review?: { agent_source: AgentSource; warnings: { field: string; message: string; severity: string }[]; note?: string };
  } | null;
  execution: {
    status: string;
    strategy?: "framework" | "reference";
    /** 9.8: an agentic run and the Gemini model its agent used */
    agentic?: boolean; agent_model?: string; needs_ai?: boolean;
    exit_code?: number;
    failed_step?: number;
    failure_reason?: string;
    duration_ms?: number;
    egress?: { mode: string; allowed_hosts: string[]; requests_allowed: number; requests_denied: number; denied_hosts: { host: string; reason: string }[] };
    logs: string[];
    log_count?: number;
    attempt?: number;
  } | null;
  verification: {
    status: "PASS" | "FAIL";
    method: string;
    summary: string;
    explanation?: string;
    explanation_source?: AgentSource;
    likely_cause?: string;
    visual_diff_ratio?: number | null;
    checks: { check: string; passed: boolean; value?: unknown; before_count?: number; after_count?: number; note?: string }[];
    evidence?: { before_screenshot?: string; after_screenshot?: string; renderer?: string };
  } | null;
  retry: {
    count: number;
    max: number;
    awaiting_decision: boolean;
    /** 6.3: a self-healing fix is waiting for the user's approval (admin setting "ask"). */
    awaiting_approval?: boolean;
    proposal?: HealProposal | null;
    prompt: string | null;
    suggest_reference?: boolean;
    /** 8.2: the built-in Playwright driver is offered only when the admin activated Playwright. */
    reference_available?: boolean;
    /** 8.2: the run already saved a record - the fixed run will save it once more */
    duplicate_warning?: string;
    stop_requested?: boolean;
    history: { attempt: number; category: string; diagnosis: string; changes: { step: number; from: string | null; to: string; why?: string }[]; outcome: string; agent_source: AgentSource | "USER"; repair?: RuntimeRepair | null; approved_by?: string; revisions?: number; feedback?: string[]; files_changed?: string[] }[];
    last_heal: { category: string; diagnosis: string; changes: { step: number; from: string | null; to: string; why?: string }[]; agent_source: AgentSource | "USER"; repair?: RuntimeRepair | null } | null;
  };
  runtime_repairs?: RuntimeRepair[];
  runtime_env?: Record<string, string>;
  clarifications?: Clarification[];
  understanding_confirmed?: boolean;
  /** 9.4: the pages the process visits - read from the recording by the AI and/or typed by the user */
  pages?: PageEntry[];
  /** addresses the AI saw in the recording but could not use (blocked, or only partly readable) */
  pages_rejected?: { url: string; video_time?: string; reason: string }[];
  /** false while pages the AI found still wait for the user's check (Build waits too) */
  pages_confirmed?: boolean;
  pages_meta?: { agent_source?: AgentSource; model?: string | null; notes?: string; error?: string | null; at?: string } | null;
  applied_skills?: { skill_id: string; lesson_id: string; category: string; signature: string; applied: string[] }[];
  learned_skills?: string[];
  pillars: Partial<Record<PillarName, Pillar>>;
  security_events: { channel: string; pattern: string | null; message: string; at: string }[];
  job: { name?: string; status: "idle" | "running" | "done" | "error"; error?: string | null; details?: unknown; security?: boolean };
  export: { ready: boolean; filename?: string; size_bytes?: number; download_url?: string };
  /** 9.9: the ZIP's README - written by Gemini from the package's facts and checked, or Understudy's own */
  export_readme?: { markdown: string; source: AgentSource; model: string; added: string[]; problems: string[] } | null;
  /** 8.4: the run was held because the tool's runtime versions don't meet its requirements (or ran anyway: ignored) */
  runtime_check?: RuntimeCheck | null;
  agent_trace: { stage: string; agent_source: AgentSource; agent?: string; action: string; at: string }[];
  notes: string[];
  strategy_override?: "reference" | "framework" | null;
  ai_mode: AgentSource;
  ai_model: string | null;
}

export interface Clarification {
  round: number;
  text: string;
  at: string;
  status: "pending" | "applied" | "not_applied" | "blocked";
  agent_source?: AgentSource;
  notes?: string[];
  summary?: string;
}

export interface UserVersions {
  tool_id: string; package: string | null; restricted: boolean; versions: string[]; default: string | null;
  source: string; latest: string | null; installed?: string | null; constraint?: string; error?: string | null;
}

export interface RuntimeRepair {
  at: string;
  cause: string;
  repair_id: string | null;
  explanation: string;
  env: Record<string, string>;
  exhausted: boolean;
  result: string | null;
  log?: string[];
}

export class WizardApiError extends Error {
  details: unknown;
  status: number;
  constructor(message: string, status: number, details?: unknown) {
    super(message);
    this.status = status;
    this.details = details;
  }
}

async function call(path: string, init?: RequestInit): Promise<WizardSession> {
  const res = await fetch(`${getApiBase()}/api/wizard${path}`, { ...init, headers: userHeaders(init?.headers) });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const d = body?.detail;
    if (res.status === 401 && d?.login_required) signalLoginRequired();
    const msg = typeof d === "string" ? d : d?.message || (res.status === 413
      ? "The upload is too large for the server to receive (Google Cloud Run accepts at most 32 MB per request)."
      : `Request failed (${res.status})`);
    throw new WizardApiError(msg, res.status, d?.details);
  }
  return body.session as WizardSession;
}

async function getJson<T>(path: string): Promise<T> {
  const res = await fetch(`${getApiBase()}${path}`, { headers: userHeaders() });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    if (res.status === 401 && data?.detail?.login_required) signalLoginRequired();
    throw new WizardApiError(data?.detail?.message || `Request failed (${res.status})`, res.status, data?.detail);
  }
  return data as T;
}

/** 9.9: Cloud Run refuses request bodies over 32 MiB, so a recording goes straight to Cloud Storage when the server
 * offers it (a resumable upload session bound to this page's origin), and Step 1 only names the upload. */
interface UploadPlan { mode: "gcs" | "direct"; upload_id?: string; upload_url?: string; content_type?: string; message?: string }

async function uploadRecording(media: File, onProgress?: (pct: number) => void): Promise<string | null> {
  const res = await fetch(`${getApiBase()}/api/wizard/uploads`, {
    method: "POST", headers: userHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({ filename: media.name, size: media.size, origin: window.location.origin }),
  });
  const plan = (await res.json().catch(() => ({}))) as UploadPlan & { detail?: { message?: string } };
  if (!res.ok) throw new WizardApiError(plan?.detail?.message || `Could not start the upload (${res.status})`, res.status);
  if (plan.mode !== "gcs" || !plan.upload_url || !plan.upload_id) {
    if (plan.message) throw new WizardApiError(plan.message, 413);
    return null;                                              // send it with Step 1 as before
  }
  await new Promise<void>((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", plan.upload_url!);
    xhr.setRequestHeader("Content-Type", plan.content_type || media.type || "application/octet-stream");
    xhr.upload.onprogress = (e) => { if (e.lengthComputable && onProgress) onProgress(Math.round((e.loaded / e.total) * 100)); };
    xhr.onload = () => (xhr.status >= 200 && xhr.status < 300 ? resolve()
      : reject(new WizardApiError(`Cloud Storage refused the recording (${xhr.status})`, xhr.status)));
    xhr.onerror = () => reject(new WizardApiError("The upload to Cloud Storage was interrupted - check your connection and try again.", 0));
    xhr.send(media);
  });
  return plan.upload_id;
}

const refsField = (refs?: HistoryRef[]) => JSON.stringify((refs || []).map((r) => ({ type: r.type, id: r.id })));

const json = (method: string, data?: unknown): RequestInit => ({
  method,
  headers: { "Content-Type": "application/json" },
  body: data === undefined ? undefined : JSON.stringify(data),
});

export const wizardApi = {
  info: async () => (await fetch(`${getApiBase()}/api/wizard/info`)).json(),
  /** Step 1 of a named session (created in a project). */
  /** 9.4: ``targetUrls`` - every page the process visits, in order (the first is where it starts). */
  start: async (sid: string, form: { targetUrls: string[]; sopText: string; media: File | null; useSample: boolean; refs?: HistoryRef[] },
    onProgress?: (pct: number) => void) => {
    const uploadId = form.media && !form.useSample ? await uploadRecording(form.media, onProgress) : null;
    const fd = new FormData();
    fd.append("target_url", form.targetUrls[0] || "");
    fd.append("target_urls", JSON.stringify(form.targetUrls));
    fd.append("sop_text", form.sopText);
    fd.append("use_sample_media", String(form.useSample));
    fd.append("context_refs", refsField(form.refs));
    if (uploadId) fd.append("upload_id", uploadId);
    else if (form.media && !form.useSample) fd.append("media", form.media);
    return call(`/sessions/${sid}/start`, { method: "POST", body: fd });
  },
  get: (sid: string) => call(`/sessions/${sid}`),
  logs: (sid: string, since: number) =>
    getJson<{ next: number; lines: string[]; execution_status?: string }>(`/api/wizard/sessions/${sid}/logs?since=${since}`),
  chooseFramework: (sid: string, choice: { framework_id?: string; custom_name?: string; version?: string; override_sop_tool?: boolean }) =>
    call(`/sessions/${sid}/framework`, json("POST", choice)),
  cancelPending: (sid: string) => call(`/sessions/${sid}/pending/cancel`, json("POST")),
  duplicate: (sid: string) => call(`/sessions/${sid}/duplicate`, json("POST")),
  executionLogs: (sid: string) => getJson<ExecutionLogs>(`/api/wizard/sessions/${sid}/execution-logs`),
  events: (after: number) => getJson<{ events: LiveEvent[]; now: number }>(`/api/wizard/events?after=${after}`),
  clarify: (sid: string, feedback: string) => call(`/sessions/${sid}/watch/clarify`, json("POST", { feedback })),
  confirmWatch: (sid: string) => call(`/sessions/${sid}/watch/confirm`, json("POST")),
  /** 9.4: save the checked / edited page list; ``rewatch`` asks the AI to watch the recording again with it */
  setPages: (sid: string, body: { urls: string[]; rewatch?: boolean; note?: string }) =>
    call(`/sessions/${sid}/pages`, json("PUT", body)),
  confirmPages: (sid: string) => call(`/sessions/${sid}/pages/confirm`, json("POST")),
  frameworkVersions: (id: string) => getJson<UserVersions>(`/api/wizard/frameworks/${encodeURIComponent(id)}/versions`),
  requestTool: (sid: string, req: { framework_id?: string; custom_name?: string; reason?: string; version?: string }) =>
    call(`/sessions/${sid}/tool-request`, json("POST", req)),
  targets: () => getJson<{ targets: { url: string; note: string }[] }>("/api/wizard/targets"),
  saveParameters: (sid: string, parameters: Parameter[]) => call(`/sessions/${sid}/parameters`, json("PUT", { parameters })),
  build: (sid: string) => call(`/sessions/${sid}/build`, json("POST")),
  saveRules: (sid: string, rules: Rule[], confirmed: boolean) => call(`/sessions/${sid}/govern/rules`, json("PUT", { rules, confirmed })),
  updateSop: (sid: string, sop_text: string) => call(`/sessions/${sid}/govern/sop`, json("POST", { sop_text })),
  check: (sid: string) => call(`/sessions/${sid}/govern/check`, json("POST")),
  /** 9.8: the user's values / weights for the difficulty parameters -> new score and recommendation */
  recalculateDifficulty: (sid: string, parameters: { key: string; value?: number; weight?: number; applicable?: boolean }[], reset = false) =>
    call(`/sessions/${sid}/difficulty/recalculate`, json("POST", { parameters, reset })),
  execute: (sid: string, ignoreRequirements = false) =>
    call(`/sessions/${sid}/execute${ignoreRequirements ? "?ignore_requirements=true" : ""}`, json("POST")),
  requestRuntimeFix: (sid: string, kind: string) => call(`/sessions/${sid}/runtime-fix`, json("POST", { kind })),
  /** 9.1: rename the session (the AI never renames it afterwards) */
  rename: (sid: string, name: string) => call(`/sessions/${sid}/name`, json("PUT", { name })),
  /** 9.0 Step 5: (re-)create the ZIP */
  export: (sid: string) => call(`/sessions/${sid}/export`, json("POST")),
  retry: (sid: string, mode: "heal" | "reference" = "heal", feedback?: string) =>
    call(`/sessions/${sid}/retry?mode=${mode}`, json("POST", feedback ? { feedback } : undefined)),
  declineRetry: (sid: string) => call(`/sessions/${sid}/retry/decline`, json("POST")),
  approveHeal: (sid: string, proposal_id: string) => call(`/sessions/${sid}/heal/approve`, json("POST", { proposal_id })),
  reviseHeal: (sid: string, proposal_id: string, feedback: string) =>
    call(`/sessions/${sid}/heal/revise`, json("POST", { proposal_id, feedback })),
  exportUrl: (sid: string) => withAccessToken(`${getApiBase()}/api/wizard/sessions/${sid}/export`),
  mediaUrl: (sid: string) => withAccessToken(`${getApiBase()}/api/wizard/sessions/${sid}/media`),
};

export const SAMPLE_SOP = `Approved vendors: Acme Industrial Supplies, Globex Corp, Initech LLC.
The spend limit per invoice is $5,000.
Invoices must be dated between 2026-01-01 and 2026-12-31.
Never pay the following vendors: Umbrella Corporation.
Vendor Name: Acme Industrial Supplies`;
