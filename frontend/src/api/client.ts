import { buildAuthHeaders } from "@/lib/hmac";

/**
 * Typed fetch wrapper that adds HMAC-SHA256 auth headers automatically.
 *
 * Reads credentials from localStorage (set by the login page / settings).
 * Throws ApiError on non-2xx responses with the backend's error shape.
 */

const API_BASE = import.meta.env.VITE_API_BASE || "";

export class ApiError extends Error {
  status: number;
  code: string;
  details: Record<string, unknown>;

  constructor(status: number, code: string, message: string, details: Record<string, unknown> = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

interface StoredCredentials {
  apiKey: string;
  apiSecret: string;
}

export function getStoredCredentials(): StoredCredentials | null {
  const raw = localStorage.getItem("exchange_credentials");
  if (!raw) return null;
  try {
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

export function setStoredCredentials(apiKey: string, apiSecret: string): void {
  localStorage.setItem("exchange_credentials", JSON.stringify({ apiKey, apiSecret }));
}

export function clearStoredCredentials(): void {
  localStorage.removeItem("exchange_credentials");
}

async function request<T>(
  method: string,
  path: string,
  body?: unknown,
  params?: Record<string, string | number | undefined>,
): Promise<T> {
  const creds = getStoredCredentials();
  if (!creds) {
    throw new ApiError(401, "not_authenticated", "No API credentials stored. Please log in.");
  }

  // Build full path with query string
  let fullPath = path;
  if (params) {
    const searchParams = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) {
      if (v !== undefined && v !== null) searchParams.set(k, String(v));
    }
    const qs = searchParams.toString();
    if (qs) fullPath = `${path}?${qs}`;
  }

  const bodyStr = body ? JSON.stringify(body) : "";
  const headers = await buildAuthHeaders(creds.apiKey, creds.apiSecret, method, fullPath, bodyStr);

  const resp = await fetch(`${API_BASE}${fullPath}`, {
    method,
    headers,
    body: bodyStr || undefined,
  });

  if (!resp.ok) {
    let errorBody: { error?: { code?: string; message?: string; details?: Record<string, unknown> } };
    try {
      errorBody = await resp.json();
    } catch {
      errorBody = {};
    }
    const err = errorBody.error || {};
    throw new ApiError(
      resp.status,
      err.code || "http_error",
      err.message || resp.statusText,
      err.details || {},
    );
  }

  // 204 No Content
  if (resp.status === 204) return undefined as T;
  return resp.json() as Promise<T>;
}

export const api = {
  get: <T>(path: string, params?: Record<string, string | number | undefined>) =>
    request<T>("GET", path, undefined, params),
  post: <T>(path: string, body?: unknown) => request<T>("POST", path, body),
  put: <T>(path: string, body?: unknown) => request<T>("PUT", path, body),
  delete: <T>(path: string, body?: unknown) => request<T>("DELETE", path, body),
};
