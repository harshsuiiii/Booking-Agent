"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import type { BookingPage, BookingState } from "@/types/dashboard";
import { getJson } from "@/lib/api";
import { ALL_STATES, STATE_LABEL, day, inr } from "@/lib/format";
import { DemoTag, EmptyState, ErrorState, Skeleton, StatusBadge } from "@/components/ui";

const input =
  "rounded-lg border border-zinc-300 bg-white px-3 py-2 text-sm focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500";

/**
 * Bookings with filters (search, status, travel dates) and paging.
 * Net and markup columns appear only when the API sends them (admins).
 */
export default function BookingsTable({ pageSize = 20, compact = false }: { pageSize?: number; compact?: boolean }) {
  const [q, setQ] = useState("");
  const [debouncedQ, setDebouncedQ] = useState("");
  const [status, setStatus] = useState<BookingState | "">("");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [page, setPage] = useState(1);
  const [data, setData] = useState<BookingPage | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [reload, setReload] = useState(0);

  useEffect(() => {
    const t = setTimeout(() => {
      setDebouncedQ(q.trim());
      setPage(1);
    }, 300);
    return () => clearTimeout(t);
  }, [q]);

  const load = useCallback(async () => {
    const params = new URLSearchParams({ page: String(page), page_size: String(pageSize) });
    if (debouncedQ) params.set("q", debouncedQ);
    if (status) params.set("status", status);
    if (from) params.set("from", from);
    if (to) params.set("to", to);
    return getJson<BookingPage>(`/v1/bookings?${params}`);
  }, [page, pageSize, debouncedQ, status, from, to]);

  useEffect(() => {
    let cancelled = false;
    load()
      .then((d) => {
        if (!cancelled) {
          setData(d);
          setError(null);
        }
      })
      .catch((e: Error) => {
        if (!cancelled) setError(e.message || "Couldn't load bookings.");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [load, reload]);

  // Net/markup columns only when the API sends them (admins), and not in the compact view.
  const showAdmin = !compact && (data?.items.some((i) => i.price.net_inr !== undefined) ?? false);
  const filtered = Boolean(debouncedQ || status || from || to);
  const first = data && data.total > 0 ? (data.page - 1) * data.page_size + 1 : 0;
  const last = data ? Math.min(data.total, data.page * data.page_size) : 0;

  function resetFilters() {
    setQ("");
    setDebouncedQ("");
    setStatus("");
    setFrom("");
    setTo("");
    setPage(1);
  }

  return (
    <div className="flex min-w-0 flex-col gap-3">
      <form
        className="flex flex-wrap items-end gap-2"
        role="search"
        aria-label="Filter bookings"
        onSubmit={(e) => e.preventDefault()}
      >
        <label className="flex min-w-[12rem] flex-1 flex-col gap-1 text-xs font-medium text-zinc-500">
          Search
          <input
            type="search"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="Reference, PNR, passenger, airport"
            className={input}
          />
        </label>
        <label className="flex flex-col gap-1 text-xs font-medium text-zinc-500">
          Status
          <select
            value={status}
            onChange={(e) => {
              setStatus(e.target.value as BookingState | "");
              setPage(1);
            }}
            className={input}
          >
            <option value="">All statuses</option>
            {ALL_STATES.map((s) => (
              <option key={s} value={s}>
                {STATE_LABEL[s]}
              </option>
            ))}
          </select>
        </label>
        {!compact && (
          <>
            <label className="flex flex-col gap-1 text-xs font-medium text-zinc-500">
              Travel from
              <input type="date" value={from} max={to || undefined} onChange={(e) => { setFrom(e.target.value); setPage(1); }} className={input} />
            </label>
            <label className="flex flex-col gap-1 text-xs font-medium text-zinc-500">
              Travel to
              <input type="date" value={to} min={from || undefined} onChange={(e) => { setTo(e.target.value); setPage(1); }} className={input} />
            </label>
          </>
        )}
        {filtered && (
          <button
            type="button"
            onClick={resetFilters}
            className="rounded-lg px-3 py-2 text-sm font-medium text-zinc-600 hover:bg-zinc-100 focus-visible:outline-2 focus-visible:outline-blue-600"
          >
            Clear filters
          </button>
        )}
      </form>

      {error ? (
        <ErrorState message={error} onRetry={() => { setLoading(true); setReload((n) => n + 1); }} />
      ) : (
        <div className="overflow-x-auto rounded-lg border border-zinc-200">
          <table className={`w-full text-sm ${compact ? "min-w-[560px]" : "min-w-[760px]"}`} aria-busy={loading}>
            <caption className="sr-only">Bookings</caption>
            <thead className="bg-zinc-50 text-left text-xs uppercase tracking-wide text-zinc-500">
              <tr>
                <th scope="col" className="px-3 py-2 font-medium">Reference</th>
                <th scope="col" className="px-3 py-2 font-medium">Passenger</th>
                <th scope="col" className="px-3 py-2 font-medium">Route</th>
                <th scope="col" className="px-3 py-2 font-medium">Travel</th>
                {!compact && <th scope="col" className="px-3 py-2 font-medium">Supplier</th>}
                <th scope="col" className="px-3 py-2 font-medium">Status</th>
                {showAdmin && <th scope="col" className="px-3 py-2 text-right font-medium">Net</th>}
                {showAdmin && <th scope="col" className="px-3 py-2 text-right font-medium">Markup</th>}
                <th scope="col" className="px-3 py-2 text-right font-medium">Sell</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-zinc-100 bg-white">
              {loading && !data
                ? Array.from({ length: Math.min(pageSize, 6) }, (_, i) => (
                    <tr key={i}>
                      <td colSpan={9} className="px-3 py-2.5">
                        <Skeleton className="h-5 w-full" />
                      </td>
                    </tr>
                  ))
                : data?.items.map((b) => (
                    <tr key={b.booking_id} className="hover:bg-zinc-50">
                      <td className="px-3 py-2.5">
                        <div className="flex items-center gap-2 whitespace-nowrap">
                          <Link
                            href={`/bookings/${b.booking_id}`}
                            className="font-mono font-medium text-blue-700 underline-offset-2 hover:underline focus-visible:outline-2 focus-visible:outline-blue-600"
                          >
                            {b.reference}
                          </Link>
                          {b.is_demo && <DemoTag />}
                        </div>
                      </td>
                      <td className="px-3 py-2.5 whitespace-nowrap text-zinc-800">{b.passenger}</td>
                      <td className="px-3 py-2.5 whitespace-nowrap font-mono text-zinc-800">
                        {b.origin} → {b.destination}
                      </td>
                      <td className="px-3 py-2.5 whitespace-nowrap text-zinc-700">{day(b.travel_date)}</td>
                      {!compact && <td className="px-3 py-2.5 whitespace-nowrap text-xs text-zinc-600">{b.supplier_label}</td>}
                      <td className="px-3 py-2.5"><StatusBadge state={b.state} /></td>
                      {showAdmin && <td className="px-3 py-2.5 text-right tabular-nums text-zinc-600">{inr(b.price.net_inr ?? 0)}</td>}
                      {showAdmin && <td className="px-3 py-2.5 text-right tabular-nums text-zinc-600">{inr(b.price.markup_inr ?? 0)}</td>}
                      <td className="px-3 py-2.5 whitespace-nowrap text-right font-medium tabular-nums text-zinc-900">{inr(b.price.sell_inr)}</td>
                    </tr>
                  ))}
            </tbody>
          </table>
          {data && data.items.length === 0 && (
            <EmptyState
              title={filtered ? "No bookings match these filters" : "No bookings yet"}
              hint={filtered ? "Try clearing the filters." : <Link className="text-blue-700 underline" href="/search">Search for flights</Link>}
            />
          )}
        </div>
      )}

      {data && data.total > 0 && (
        <nav aria-label="Pagination" className="flex items-center justify-between gap-3 text-sm text-zinc-600">
          <span aria-live="polite">
            Showing {first}–{last} of {data.total}
          </span>
          <div className="flex gap-2">
            <button
              type="button"
              disabled={page <= 1 || loading}
              onClick={() => setPage((p) => p - 1)}
              className="rounded-lg border border-zinc-300 bg-white px-3 py-1.5 font-medium hover:bg-zinc-50 focus-visible:outline-2 focus-visible:outline-blue-600 disabled:opacity-40"
            >
              Previous
            </button>
            <button
              type="button"
              disabled={last >= data.total || loading}
              onClick={() => setPage((p) => p + 1)}
              className="rounded-lg border border-zinc-300 bg-white px-3 py-1.5 font-medium hover:bg-zinc-50 focus-visible:outline-2 focus-visible:outline-blue-600 disabled:opacity-40"
            >
              Next
            </button>
          </div>
        </nav>
      )}
    </div>
  );
}
