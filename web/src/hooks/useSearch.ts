"use client";

import { useState, useCallback, useRef } from "react";
import type { SerializedGroup, SSEEvent, SupplierStatus } from "@/types/sse";

import { apiFetch } from "@/lib/api";
import { toRequestBody, type SearchParams } from "@/lib/searchQuery";

export type { SearchParams };

export interface SearchState {
  searchId: string | null;
  groups: SerializedGroup[];
  isComplete: boolean;
  isLoading: boolean;
  supplierStatuses: Record<string, SupplierStatus>;
  error: string | null;
  elapsedSeconds: number | null;
}

const INITIAL_STATE: SearchState = {
  searchId: null,
  groups: [],
  isComplete: false,
  isLoading: false,
  supplierStatuses: {},
  error: null,
  elapsedSeconds: null,
};

export function useSearch() {
  const [state, setState] = useState<SearchState>(INITIAL_STATE);
  const abortRef = useRef<AbortController | null>(null);

  const search = useCallback(async (params: SearchParams) => {
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;

    setState({ ...INITIAL_STATE, isLoading: true });

    try {
      const startRes = await apiFetch("/v1/search", {
        method: "POST",
        body: JSON.stringify(toRequestBody(params)),
        signal: controller.signal,
      });

      if (!startRes.ok) {
        const err = await startRes.json().catch(() => ({}));
        const msg = Array.isArray(err.detail)
          ? err.detail.map((d: { msg: string }) => d.msg).join(", ")
          : (err.detail ?? `HTTP ${startRes.status}`);
        throw new Error(msg);
      }

      const { search_id } = (await startRes.json()) as { search_id: string };
      setState((s) => ({ ...s, searchId: search_id }));

      const streamRes = await apiFetch(`/v1/search/${search_id}/stream`, {
        signal: controller.signal,
      });

      if (!streamRes.ok) throw new Error(`Stream error: ${streamRes.status}`);
      if (!streamRes.body) throw new Error("No response body");

      const reader = streamRes.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        const lines = buf.split("\n");
        buf = lines.pop() ?? "";

        for (const line of lines) {
          if (!line.startsWith("data: ")) continue;
          const payload = line.slice(6).trim();
          if (payload === "[DONE]") {
            setState((s) => ({ ...s, isLoading: false }));
            return;
          }
          try {
            applyEvent(JSON.parse(payload) as SSEEvent);
          } catch {
            // skip malformed lines
          }
        }
      }

      setState((s) => ({ ...s, isLoading: false }));
    } catch (err: unknown) {
      if (err instanceof Error && err.name === "AbortError") return;
      setState((s) => ({
        ...s,
        isLoading: false,
        error: err instanceof Error ? err.message : "Unknown error",
      }));
    }

    function applyEvent(event: SSEEvent) {
      setState((s) => {
        switch (event.type) {
          case "source_started":
            return {
              ...s,
              supplierStatuses: { ...s.supplierStatuses, [event.supplier_id]: "loading" },
            };
          case "cache_hit":
            return {
              ...s,
              supplierStatuses: { ...s.supplierStatuses, cache: "cache" },
            };
          case "offers_partial":
            return {
              ...s,
              groups: event.groups,
              supplierStatuses: { ...s.supplierStatuses, [event.supplier_id]: "done" },
            };
          case "source_failed":
            return {
              ...s,
              supplierStatuses: { ...s.supplierStatuses, [event.supplier_id]: "failed" },
            };
          case "source_timed_out":
            return {
              ...s,
              supplierStatuses: { ...s.supplierStatuses, [event.supplier_id]: "timed_out" },
            };
          case "source_skipped":
            return {
              ...s,
              supplierStatuses: { ...s.supplierStatuses, [event.supplier_id]: "skipped" },
            };
          case "search_complete": {
            const statuses: Record<string, SupplierStatus> = { ...s.supplierStatuses };
            event.suppliers_succeeded.forEach((id) => (statuses[id] = "done"));
            event.suppliers_failed.forEach((id) => (statuses[id] = "failed"));
            event.suppliers_timed_out.forEach((id) => (statuses[id] = "timed_out"));
            event.suppliers_skipped.forEach((id) => (statuses[id] = "skipped"));
            return {
              ...s,
              groups: event.groups,
              isComplete: true,
              elapsedSeconds: event.elapsed_seconds,
              supplierStatuses: statuses,
            };
          }
          default:
            return s;
        }
      });
    }
  }, []);

  const reset = useCallback(() => {
    abortRef.current?.abort();
    setState(INITIAL_STATE);
  }, []);

  return { state, search, reset };
}
