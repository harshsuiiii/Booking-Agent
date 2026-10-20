/**
 * Shared Playwright helpers. The API is mocked with page.route(); there is no login, and
 * role differences come only from the payloads the mocked API sends.
 */
export type Role = "agent" | "admin";

export function json(body: unknown, status = 200) {
  return { status, contentType: "application/json", body: JSON.stringify(body) };
}
