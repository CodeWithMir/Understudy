/**
 * Understudy - backend base URL and the Mission Control calls that live on server.py
 * (Gemini key / model configuration and Model Armor interceptions).
 *
 * The wizard (lib/wizard.ts), the Studio account / projects (lib/studio.ts) and the rest of Mission Control
 * (lib/admin.ts) build their URLs with getApiBase().
 */

const DEFAULT_BACKEND = "http://localhost:8000";

/**
 * NEXT_PUBLIC_UNDERSTUDY_API_BASE:
 *   (unset)          the backend at http://localhost:8000 (local / Docker Compose)
 *   same-origin      single-origin deployments (Cloud Run: one container, one public URL); next.config.ts proxies
 *                    /api and the demo portals to the backend inside the container (no CORS, no second public port)
 *   https://host...  an explicit backend URL
 */
const API_BASE_MODE = process.env.NEXT_PUBLIC_UNDERSTUDY_API_BASE || "";

export function getApiBase(): string {
  if (API_BASE_MODE === "same-origin" && typeof window !== "undefined") return window.location.origin;
  if (/^https?:\/\//.test(API_BASE_MODE)) return API_BASE_MODE.replace(/\/+$/, "");
  return DEFAULT_BACKEND;
}

/** Mission Control session token (set by the admin login) for admin-only backend endpoints. */
const ADMIN_TOKEN_KEY = "understudy_admin_token";
function adminHeaders(extra: Record<string, string> = {}): Record<string, string> {
  let token = "";
  try { token = window.sessionStorage.getItem(ADMIN_TOKEN_KEY) || ""; } catch { /* storage unavailable */ }
  return token ? { ...extra, "X-Admin-Token": token } : extra;
}

export interface SecurityAlert {
  alert_id: string;
  run_id?: string;
  status: string;
  prompt_injection: string;
  channel: string;
  portal: string;
  portal_url?: string;
  raw_text?: string;
  model_name?: string;
  timestamp: string;
}

export async function getSecurityAlerts(): Promise<{ status: string; alerts: SecurityAlert[]; count: number; active_model: string }> {
  try {
    const res = await fetch(`${getApiBase()}/api/security/alerts`, { headers: adminHeaders() });
    if (!res.ok) return { status: "ERROR", alerts: [], count: 0, active_model: "" };
    return await res.json();
  } catch {
    return { status: "ERROR", alerts: [], count: 0, active_model: "" };
  }
}

export async function clearSecurityAlerts(): Promise<unknown> {
  const res = await fetch(`${getApiBase()}/api/security/alerts`, { method: "DELETE", headers: adminHeaders() });
  return await res.json();
}

/** Where Understudy's AI runs and where its state lives (no secrets). */
export interface PlatformInfo {
  mode: "vertex" | "studio" | null;
  sdk: string | null;
  platform: string;
  project?: string | null;
  location?: string | null;
  media_cache?: { uploads: number; reused: number };
  gcs_bucket?: boolean;
  store?: "local" | "firestore";
  /** Firestore was switched on but is unavailable (reason); local files are used meanwhile */
  store_degraded?: string | null;
  cloud_run?: boolean;
}

export interface ApiKeyStatus {
  platform?: PlatformInfo;
  has_key: boolean;
  standin?: string | null;
  masked_key: string | null;
  source: string;
  persisted: boolean;
  provider: string;
  selected_model?: string;
  available_models?: DiscoveredModel[];
  model_ids?: string[];
  total_models_count?: number;
  models_source?: string;
  models_error?: string;
  capabilities: string[];
}

export async function getApiKeyStatus(): Promise<ApiKeyStatus> {
  const res = await fetch(`${getApiBase()}/api/config/api-key`, { headers: adminHeaders() });
  if (res.status === 401) throw new Error("login required");
  if (!res.ok) {
    return {
      has_key: false,
      masked_key: null,
      source: "none",
      persisted: false,
      selected_model: "gemini-3.6-flash",
      available_models: [],
      model_ids: [],
      total_models_count: 0,
      provider: "Google Generative AI (Gemini 3.6 Flash)",
      capabilities: [],
    };
  }
  return await res.json();
}

export async function setGoogleApiKey(
  apiKey: string,
  selectedModel?: string,
  validateOnSave: boolean = false,
  persist: boolean = true
): Promise<unknown> {
  const res = await fetch(`${getApiBase()}/api/config/api-key`, {
    method: "POST",
    headers: adminHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({
      api_key: apiKey,
      selected_model: selectedModel || null,
      validate_on_save: validateOnSave,
      persist,
    }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: "Failed to configure API key" }));
    throw new Error(err.detail || "Failed to configure API key");
  }
  return await res.json();
}

export async function setSelectedModel(modelName: string): Promise<unknown> {
  const res = await fetch(`${getApiBase()}/api/config/model`, {
    method: "POST",
    headers: adminHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({ selected_model: modelName }),
  });
  return await res.json();
}

export interface DiscoveredModel {
  id: string;
  displayName: string;
  description: string;
  isRecommended: boolean;
  category: string;
}

/** 9.9: ``source`` - where the list came from: "vertex" (Vertex AI's list), "studio" (the key's list) or "" (built-in suggestions) */
export async function getAvailableModels(apiKey?: string): Promise<{ models: DiscoveredModel[]; total_count: number; selected_model: string; recommended: string[]; source?: string; error?: string }> {
  const q = apiKey ? `?api_key=${encodeURIComponent(apiKey)}` : "";
  const res = await fetch(`${getApiBase()}/api/config/models${q}`);
  if (!res.ok) {
    throw new Error(`Failed to fetch models: HTTP ${res.status}`);
  }
  return await res.json();
}

export async function validateGoogleApiKey(apiKey?: string, selectedModel?: string): Promise<{ valid: boolean; message: string; model?: string }> {
  const res = await fetch(`${getApiBase()}/api/config/api-key/validate`, {
    method: "POST",
    headers: adminHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({ api_key: apiKey || null, selected_model: selectedModel || null }),
  });
  if (!res.ok) {
    throw new Error(`Backend server error (${res.status}). Please check your backend connection.`);
  }
  return await res.json();
}

export async function clearGoogleApiKey(): Promise<unknown> {
  const res = await fetch(`${getApiBase()}/api/config/api-key`, {
    method: "DELETE",
    headers: adminHeaders(),
  });
  return await res.json();
}
