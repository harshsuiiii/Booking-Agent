/**
 * API access for the web app. Authentication is the httpOnly session cookie set by
 * /v1/auth/login, sent with `credentials: "include"`; the browser never sees a key.
 */
/**
 * Where the API lives. Deployed, requests go to this site's own /v1/..., which
 * next.config.ts forwards to the API (API_PROXY_URL), so they are same-origin and the
 * session cookie just works. Locally, `next dev` talks to uvicorn on :8000.
 * NEXT_PUBLIC_API_URL overrides both; don't set it in production.
 */
export const API_BASE =
  process.env.NEXT_PUBLIC_API_URL ??
  (process.env.NODE_ENV === "development" ? "http://localhost:8000" : "");

/** Error carrying the HTTP status, FastAPI `detail` (string or object) and auth `code`. */
export class ApiError extends Error {
  constructor(
    public status: number,
    public detail: unknown,
    public code?: string,
  ) {
    super(detailMessage(detail) ?? `HTTP ${status}`);
  }
}

function detailMessage(detail: unknown): string | null {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail.map((d: { msg?: string }) => d.msg ?? String(d)).join(", ");
  }
  if (detail && typeof detail === "object" && "message" in detail) {
    return String((detail as { message: unknown }).message);
  }
  return null;
}

type UnauthorizedHandler = (code: string | undefined) => void;
let onUnauthorized: UnauthorizedHandler | null = null;

/** The signed-in shell registers this to send the user to /login on 401. */
export function setUnauthorizedHandler(handler: UnauthorizedHandler | null) {
  onUnauthorized = handler;
}

/** fetch() against the API with the session cookie. Never retried automatically. */
export async function apiFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const headers = new Headers(init.headers);
  if (init.body !== undefined && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const res = await fetch(`${API_BASE}${path}`, { ...init, headers, credentials: "include" });
  if (res.status === 401 && !path.startsWith("/v1/auth/")) {
    const body = await res.clone().json().catch(() => ({}));
    onUnauthorized?.(body.code);
  }
  return res;
}

async function parse<T>(res: Response): Promise<T> {
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new ApiError(res.status, data.detail, data.code);
  return data as T;
}

export async function getJson<T>(path: string, init?: RequestInit): Promise<T> {
  return parse<T>(await apiFetch(path, init));
}

export async function postJson<T>(path: string, body?: unknown): Promise<T> {
  return parse<T>(
    await apiFetch(path, {
      method: "POST",
      body: body === undefined ? undefined : JSON.stringify(body),
    }),
  );
}
