/**
 * API access for the web app. There is no login: the API treats every visitor as an
 * Agent of the demo agency (sell prices only).
 *
 * Where the API lives: deployed, requests go to this site's own /v1/..., which
 * next.config.ts forwards to the API (API_PROXY_URL), so they are same-origin. Locally,
 * `next dev` talks to uvicorn on :8000. NEXT_PUBLIC_API_URL overrides both.
 */
export const API_BASE = (
  process.env.NEXT_PUBLIC_API_URL ??
  (process.env.NODE_ENV === "development" ? "http://localhost:8000" : "")
).replace(/\/+$/, "");

/** Error carrying the HTTP status, FastAPI `detail` (string or object) and error `code`. */
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

/** fetch() against the API. Never retried automatically. */
export async function apiFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const headers = new Headers(init.headers);
  if (init.body !== undefined && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  return fetch(`${API_BASE}${path}`, { ...init, headers });
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
