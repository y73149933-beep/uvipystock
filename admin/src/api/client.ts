/**
 * JWT-authenticated fetch wrapper for admin API.
 *
 * Stores the JWT in localStorage; attaches it as a Bearer token.
 * Throws AdminApiError on non-2xx with the backend's error shape.
 */

const API_BASE = import.meta.env.VITE_ADMIN_API_BASE || "";

export class AdminApiError extends Error {
  status: number;
  code: string;
  details: Record<string, unknown>;

  constructor(status: number, code: string, message: string, details: Record<string, unknown> = {}) {
    super(message);
    this.name = "AdminApiError";
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

export function getStoredToken(): string | null {
  return localStorage.getItem("admin_jwt");
}

export function setStoredToken(token: string): void {
  localStorage.setItem("admin_jwt", token);
}

export function clearStoredToken(): void {
  localStorage.removeItem("admin_jwt");
}

/**
 * Admin login: POST email + password to get a JWT.
 * For the sandbox, we use a simple login endpoint (or the admin can
 * generate a JWT directly via the CLI). Here we POST to /api/admin/login.
 */
export async function loginAdmin(email: string, password: string): Promise<string> {
  const resp = await fetch(`${API_BASE}/api/admin/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });

  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}));
    throw new AdminApiError(resp.status, err.error?.code || "login_failed", err.error?.message || "Login failed");
  }

  const data = await resp.json();
  const token = data.token || data.access_token;
  if (!token) throw new AdminApiError(500, "no_token", "Server did not return a token");
  setStoredToken(token);
  return token;
}

async function request<T>(
  method: string,
  path: string,
  body?: unknown,
): Promise<T> {
  const token = getStoredToken();
  if (!token) {
    throw new AdminApiError(401, "not_authenticated", "No admin token stored. Please log in.");
  }

  const headers: Record<string, string> = {
    Authorization: `Bearer ${token}`,
  };
  let bodyStr: string | undefined;
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    bodyStr = JSON.stringify(body);
  }

  const resp = await fetch(`${API_BASE}${path}`, {
    method,
    headers,
    body: bodyStr,
  });

  if (!resp.ok) {
    let errorBody: { error?: { code?: string; message?: string; details?: Record<string, unknown> } };
    try {
      errorBody = await resp.json();
    } catch {
      errorBody = {};
    }
    const err = errorBody.error || {};
    throw new AdminApiError(
      resp.status,
      err.code || "http_error",
      err.message || resp.statusText,
      err.details || {},
    );
  }

  if (resp.status === 204) return undefined as T;
  return resp.json() as Promise<T>;
}

export const adminApi = {
  get: <T>(path: string) => request<T>("GET", path),
  post: <T>(path: string, body?: unknown) => request<T>("POST", path, body),
  put: <T>(path: string, body?: unknown) => request<T>("PUT", path, body),
  patch: <T>(path: string, body?: unknown) => request<T>("PATCH", path, body),
  delete: <T>(path: string, body?: unknown) => request<T>("DELETE", path, body),
};
