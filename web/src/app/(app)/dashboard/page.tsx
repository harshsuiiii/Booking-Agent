"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import type { Charts, Summary } from "@/types/dashboard";
import { getJson } from "@/lib/api";
import { day, inr, inrCompact, when } from "@/lib/format";
import BookingsTable from "@/components/BookingsTable";
import { BarList, ColumnChart } from "@/components/charts";
import { ArrowRightIcon } from "@/components/Icons";
import { Card, DemoTag, EmptyState, ErrorState, PageHeader, Skeleton } from "@/components/ui";

function StatCard({ label, value, note, adminOnly }: { label: string; value: string; note?: string; adminOnly?: boolean }) {
  return (
    <div className="rounded-xl border border-zinc-200 bg-white p-4 shadow-sm">
      <p className="flex items-center gap-2 text-xs font-medium uppercase tracking-wide text-zinc-500">
        {label}
        {adminOnly && (
          <span className="rounded bg-zinc-100 px-1.5 py-px text-[10px] font-semibold normal-case tracking-normal text-zinc-600">
            Admin
          </span>
        )}
      </p>
      <p className="mt-2 text-2xl font-semibold tabular-nums tracking-tight text-zinc-900">{value}</p>
      {note && <p className="mt-1 text-xs text-zinc-500">{note}</p>}
    </div>
  );
}

export default function DashboardPage() {
  const [summary, setSummary] = useState<Summary | null>(null);
  const [charts, setCharts] = useState<Charts | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    Promise.all([getJson<Summary>("/v1/dashboard/summary"), getJson<Charts>("/v1/dashboard/charts")])
      .then(([s, c]) => {
        setSummary(s);
        setCharts(c);
      })
      .catch((e: Error) => setError(e.message || "Couldn't load the dashboard."));
  }, []);

  useEffect(load, [load]);
  const retry = () => {
    setError(null);
    load();
  };

  const isAdmin = summary?.markup_this_month_inr !== undefined;
  const winRates = charts?.by_supplier.filter((s) => s.supplier_performance?.win_rate != null) ?? [];

  return (
    <div className="flex flex-col gap-6">
      <PageHeader
        title="Dashboard"
        subtitle={summary ? `Agency overview · ${summary.month}` : "Agency overview"}
        action={
          <Link
            href="/search"
            className="inline-flex items-center gap-2 rounded-lg bg-blue-600 px-4 py-2 text-sm font-semibold text-white hover:bg-blue-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-600"
          >
            New search <ArrowRightIcon size={16} />
          </Link>
        }
      />

      {error && <ErrorState message={error} onRetry={retry} />}

      <section aria-label="Summary" className={`grid gap-4 ${isAdmin ? "sm:grid-cols-2 lg:grid-cols-4" : "sm:grid-cols-3"}`}>
        {summary ? (
          <>
            <StatCard label="Bookings this month" value={String(summary.bookings_this_month)} note="Excludes drafts" />
            <StatCard label="Sales this month" value={inr(summary.sales_this_month_inr)} note="Paid and ticketed, sell price" />
            {isAdmin && (
              <StatCard label="Markup earned" value={inr(summary.markup_this_month_inr ?? 0)} note="This month, paid and ticketed" adminOnly />
            )}
            <StatCard label="Upcoming trips" value={String(summary.upcoming_trips)} note="Paid or ticketed, not yet flown" />
          </>
        ) : (
          !error && Array.from({ length: 3 }, (_, i) => <Skeleton key={i} className="h-28" />)
        )}
      </section>

      <div className="grid gap-4 lg:grid-cols-5">
        <Card title="Bookings, last 30 days" className="lg:col-span-3">
          {charts ? (
            <ColumnChart
              label="Bookings per day, last 30 days"
              data={charts.bookings_over_time.map((d) => ({
                label: day(d.date).replace(/ \d{4}$/, ""),
                value: d.bookings,
                detail: parseFloat(d.sales_inr) > 0 ? `${inr(d.sales_inr)} sold` : undefined,
              }))}
            />
          ) : (
            <Skeleton className="h-44" />
          )}
        </Card>
        <Card title="Sales by supplier" className="lg:col-span-2">
          {charts ? (
            charts.by_supplier.length ? (
              <BarList
                label="Sales by supplier (paid and ticketed)"
                format={inrCompact}
                data={charts.by_supplier.map((s) => ({
                  label: s.supplier_label ?? s.supplier_id,
                  value: parseFloat(s.sales_inr),
                  detail: `${s.bookings} bkg`,
                }))}
              />
            ) : (
              <EmptyState title="No supplier activity yet" />
            )
          ) : (
            <Skeleton className="h-44" />
          )}
        </Card>
      </div>

      {isAdmin && charts && (
        <Card
          title="Supplier win rate"
          action={<span className="text-xs text-zinc-500">Share of searches each supplier won · since {charts.metrics_since}</span>}
        >
          {winRates.length ? (
            <BarList
              label="Supplier win rate"
              format={(v) => `${Math.round(v * 100)}%`}
              data={winRates.map((s) => ({
                label: s.supplier_label ?? s.supplier_id,
                value: s.supplier_performance?.win_rate ?? 0,
                detail: `${s.supplier_performance?.wins} of ${winRates.reduce((n, w) => n + (w.supplier_performance?.wins ?? 0), 0)} · avg ${s.supplier_performance?.avg_latency_ms ?? "–"} ms`,
              }))}
            />
          ) : (
            <EmptyState title="No searches since the API started" hint="Run a search and come back." />
          )}
        </Card>
      )}

      <div className="grid gap-4 lg:grid-cols-3">
        <Card
          title="Recent bookings"
          className="lg:col-span-2"
          action={
            <Link href="/bookings" className="text-sm font-medium text-blue-700 hover:underline focus-visible:outline-2 focus-visible:outline-blue-600">
              View all
            </Link>
          }
        >
          <BookingsTable pageSize={6} compact />
        </Card>

        <div className="flex min-w-0 flex-col gap-4">
          <Card title="Upcoming trips">
            {!summary ? (
              <Skeleton className="h-24" />
            ) : summary.next_trips.length === 0 ? (
              <EmptyState title="No upcoming trips" />
            ) : (
              <ul className="flex flex-col divide-y divide-zinc-100">
                {summary.next_trips.map((t) => (
                  <li key={t.booking_id} className="flex items-center justify-between gap-2 py-2 text-sm">
                    <Link href={`/bookings/${t.booking_id}`} className="min-w-0 focus-visible:outline-2 focus-visible:outline-blue-600">
                      <span className="font-mono font-medium text-zinc-900">{t.origin} → {t.destination}</span>
                      <span className="block truncate text-xs text-zinc-500">{t.passenger} · {t.reference}</span>
                    </Link>
                    <span className="flex shrink-0 flex-col items-end gap-1 text-xs text-zinc-600">
                      {day(t.travel_date)}
                      {t.is_demo && <DemoTag />}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </Card>

          <Card title="Recent searches">
            {!summary ? (
              <Skeleton className="h-24" />
            ) : summary.recent_searches.length === 0 ? (
              <EmptyState title="No searches yet" hint={<Link className="text-blue-700 underline" href="/search">Start a search</Link>} />
            ) : (
              <ul className="flex flex-col divide-y divide-zinc-100">
                {summary.recent_searches.map((s) => (
                  <li key={s.searched_at} className="py-2 text-sm">
                    <Link
                      href={`/search?from=${s.origin}&to=${s.destination}&date=${s.departure_date}`}
                      className="flex items-center justify-between gap-2 rounded focus-visible:outline-2 focus-visible:outline-blue-600"
                    >
                      <span>
                        <span className="font-mono font-medium text-zinc-900">{s.origin} → {s.destination}</span>
                        <span className="block text-xs text-zinc-500">
                          {day(s.departure_date)} · {s.passengers} pax · {s.offer_count} offers
                          {s.source === "assistant" ? " · via assistant" : ""}
                        </span>
                      </span>
                      <span className="shrink-0 text-xs text-zinc-400">{when(s.searched_at)}</span>
                    </Link>
                  </li>
                ))}
              </ul>
            )}
          </Card>
        </div>
      </div>
    </div>
  );
}
