import type { AgentOffer } from "@/types/agent";
import { ApiError, apiFetch } from "@/lib/api";

export type AgentEvent =
  | { type: "session"; session_id: string }
  | { type: "status"; stage: string; text: string; tool?: string }
  | { type: "tool_done"; tool: string; ms: number; text: string }
  | { type: "reply"; text: string }
  | { type: "offers"; offers: AgentOffer[] }
  | { type: "error"; status: number; message: string }
  | { type: "done" };

/** Abort if the server sends nothing for this long (it streams a status at every step). */
export const IDLE_TIMEOUT_MS = 60_000;

/**
 * POST a chat message and call onEvent for each streamed event.
 * Resolves after the "done" event; rejects on network errors, HTTP errors before the
 * stream starts, or IDLE_TIMEOUT_MS of silence.
 */
export async function streamAgentChat(
  body: { message: string; session_id: string | null },
  onEvent: (e: AgentEvent) => void,
): Promise<void> {
  const controller = new AbortController();
  let idleTimer: ReturnType<typeof setTimeout> | undefined;
  let timedOut = false;
  const resetIdle = () => {
    clearTimeout(idleTimer);
    idleTimer = setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, IDLE_TIMEOUT_MS);
  };

  resetIdle();
  try {
    const res = await apiFetch("/v1/agent/chat/stream", {
      method: "POST",
      body: JSON.stringify(body),
      signal: controller.signal,
    });
    if (!res.ok || !res.body) {
      const data = await res.json().catch(() => ({}));
      throw new ApiError(res.status, data.detail, data.code);
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      resetIdle();
      buffer += decoder.decode(value, { stream: true });
      const frames = buffer.split("\n\n");
      buffer = frames.pop() ?? "";
      for (const frame of frames) {
        const line = frame.split("\n").find((l) => l.startsWith("data: "));
        if (!line) continue;
        const event = JSON.parse(line.slice(6)) as AgentEvent;
        onEvent(event);
        if (event.type === "done") return;
      }
    }
  } catch (err) {
    if (timedOut) {
      throw new Error(
        `No response from the server for ${IDLE_TIMEOUT_MS / 1000}s. Check that the API is running.`,
      );
    }
    throw err;
  } finally {
    clearTimeout(idleTimer);
  }
}
