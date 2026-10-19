/**
 * Shared Playwright helpers. The API is mocked with page.route(); proxy.ts only checks
 * that a session cookie exists, so tests add a placeholder cookie and mock /v1/auth/me.
 */
import type { BrowserContext, Page } from "@playwright/test";

export type Role = "agent" | "admin";

export const USERS = {
  agent: { email: "agent@demo.test", name: "Demo Agent", role: "agent", tenant_id: "demo-agency" },
  admin: { email: "admin@demo.test", name: "Demo Admin", role: "admin", tenant_id: "demo-agency" },
} as const;

export async function addSessionCookie(context: BrowserContext) {
  await context.addCookies([
    { name: "ba_session", value: "test-session", domain: "localhost", path: "/", httpOnly: true, sameSite: "Lax" },
  ]);
}

/** Signed in as `role` for both proxy.ts (cookie) and the app (mocked /v1/auth/me). */
export async function signIn(page: Page, role: Role = "agent") {
  await addSessionCookie(page.context());
  await page.route("**/v1/auth/me", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(USERS[role]) }),
  );
}

export function json(body: unknown, status = 200) {
  return { status, contentType: "application/json", body: JSON.stringify(body) };
}
