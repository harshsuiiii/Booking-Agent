"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useState, type ReactNode } from "react";
import type { BookingDetail } from "@/types/dashboard";
import type { FareRulePolicy } from "@/types/sse";
import { ApiError, getJson } from "@/lib/api";
import { STATE_LABEL, inr, when } from "@/lib/format";
import { airportDate, airportTime, formatMinutes, sliceJourneyMinutes } from "@/lib/offerUtils";
import { ChevronLeftIcon } from "@/components/Icons";
import { Card, DemoTag, ErrorState, Skeleton, StatusBadge, TestDataBadge } from "@/components/ui";
import SimulatedBadge from "@/components/SimulatedBadge";

function Row({ label, value, strong }: { label: string; value: ReactNode; strong?: boolean }) {
  return (
    <div className={`flex items-center justify-between gap-3 py-1.5 text-sm ${strong ? "font-semibold text-zinc-900" : "text-zinc-700"}`}>
      <dt>{label}</dt>
      <dd className="tabular-nums">{value}</dd>
    </div>
  );
}

function policyText(p: FareRulePolicy): string {
  if (p.allowed === "UNKNOWN") return "Unknown (not disclosed)";
  if (!p.allowed) return "Not allowed";
  if (p.fee === "UNKNOWN") return "Allowed · fee unknown";
  if (p.fee === null || p.fee === undefined) return "Allowed · free";
  return `Allowed · fee ${inr(p.fee.amount)}`;
}

function bagText(pieces: number | null | "UNKNOWN" | undefined, kg: string | null | "UNKNOWN" | undefined): string {
  if (pieces === "UNKNOWN" || pieces === undefined) return "Unknown (not disclosed)";
  if (pieces === null || pieces === 0) return "Not included";
  return `${pieces} pc${kg && kg !== "UNKNOWN" ? ` × ${parseFloat(kg)} kg` : ""}`;
}

export default function BookingDetailPage() {
  const { id } = useParams<{ id: string }>();
  const [b, setB] = useState<BookingDetail | null>(null);
  const [error, setError] = useState<{ status: number; message: string } | null>(null);

  const load = useCallback(() => {
    getJson<BookingDetail>(`/v1/bookings/${id}`)
      .then(setB)
      .catch((e: unknown) =>
        setError({
          status: e instanceof ApiError ? e.status : 0,
          message: e instanceof Error ? e.message : "Couldn't load this booking.",
        }),
      );
  }, [id]);

  useEffect(load, [load]);
  const retry = () => {
    setError(null);
    load();
  };

  const back = (
    <Link href="/bookings" className="inline-flex items-center gap-1 text-sm font-medium text-zinc-600 hover:text-zinc-900 focus-visible:outline-2 focus-visible:outline-blue-600">
      <ChevronLeftIcon size={16} /> All bookings
    </Link>
  );

  if (error) {
    return (
      <div className="flex flex-col gap-4">
        {back}
        {error.status === 404 ? (
          <Card><p className="text-sm text-zinc-700">This booking doesn&apos;t exist or belongs to another agency.</p></Card>
        ) : (
          <ErrorState message={error.message} onRetry={retry} />
        )}
      </div>
    );
  }
  if (!b) {
    return (
      <div className="flex flex-col gap-4" aria-busy="true">
        {back}
        <Skeleton className="h-16" />
        <div className="grid gap-4 lg:grid-cols-3"><Skeleton className="h-64 lg:col-span-2" /><Skeleton className="h-64" /></div>
      </div>
    );
  }

  const o = b.offer;
  const isAdmin = b.price.net_inr !== undefined;
  const timeline = [
    { label: "Created", at: b.created_at, actor: "" },
    ...b.transitions.map((t) => ({ label: STATE_LABEL[t.to], at: t.occurred_at, actor: t.actor })),
  ];

  return (
    <div className="flex flex-col gap-6">
      {back}
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-xs font-medium uppercase tracking-wide text-zinc-500">Booking</p>
          <h1 className="flex flex-wrap items-center gap-2 text-2xl font-semibold tracking-tight text-zinc-900">
            <span className="font-mono">{b.reference}</span>
            <StatusBadge state={b.state} />
            {b.is_demo && <DemoTag />}
            {o.source === "simulated" && <SimulatedBadge />}
            {o.is_test_data && <TestDataBadge />}
          </h1>
          <p className="mt-1 text-sm text-zinc-500">
            {o.slices[0].segments[0].origin} → {o.slices[0].segments.at(-1)?.destination} · {b.passenger_count} passenger
            {b.passenger_count === 1 ? "" : "s"} · via {b.supplier_label}
            {b.pnr && <> · PNR <span className="font-mono font-medium text-zinc-700">{b.pnr}</span></>}
          </p>
        </div>
        <div className="text-right">
          <p className="text-xs text-zinc-500">Total to customer</p>
          <p className="text-2xl font-semibold tabular-nums text-zinc-900">{inr(b.price.sell_inr)}</p>
        </div>
      </header>

      {b.error_detail && <ErrorState message={b.error_detail} />}

      <div className="grid gap-4 lg:grid-cols-3">
        <div className="flex flex-col gap-4 lg:col-span-2">
          <Card title="Itinerary">
            <ol className="flex flex-col gap-5">
              {o.slices.map((sl, si) => (
                <li key={si}>
                  <p className="mb-2 text-xs font-semibold uppercase tracking-wide text-zinc-500">
                    {si === 0 ? "Outbound" : "Return"} · {airportDate(sl.segments[0].departure_at)} · {formatMinutes(sliceJourneyMinutes(sl))} total
                  </p>
                  <ol className="flex flex-col gap-2">
                    {sl.segments.map((seg, i) => (
                      <li key={i}>
                        {i > 0 && (
                          <p className="my-1 ml-3 border-l-2 border-dashed border-zinc-200 pl-3 text-xs text-amber-700">
                            Layover at {seg.origin}
                          </p>
                        )}
                        <div className="grid grid-cols-[auto_1fr_auto] items-center gap-3 rounded-lg border border-zinc-100 bg-zinc-50 px-3 py-2 text-sm">
                          <span className="font-mono text-xs text-zinc-500">
                            {seg.marketing_carrier.iata_code} {seg.flight_number}
                          </span>
                          <span className="font-medium text-zinc-900">
                            {seg.origin} {airportTime(seg.departure_at)} → {seg.destination} {airportTime(seg.arrival_at)}
                          </span>
                          <span className="text-xs text-zinc-500">{formatMinutes(seg.duration_minutes)}</span>
                        </div>
                      </li>
                    ))}
                  </ol>
                </li>
              ))}
            </ol>
            <p className="mt-3 text-xs text-zinc-500">Times are local to each airport.</p>
          </Card>

          <Card title="Fare rules">
            <dl className="divide-y divide-zinc-100">
              <Row label="Fare" value={`${o.fare_family ?? "—"} · ${o.cabin_class.replace("_", " ").toLowerCase()} · ${o.fare_basis}`} />
              <Row label="Checked baggage" value={bagText(o.baggage.checked_pieces, o.baggage.checked_kg)} />
              <Row label="Cabin baggage" value={bagText(o.baggage.cabin_pieces, o.baggage.cabin_kg)} />
              <Row label="Change" value={policyText(o.fare_rules.change)} />
              <Row label="Cancel" value={policyText(o.fare_rules.cancel)} />
              <Row label="Refund" value={policyText(o.fare_rules.refund)} />
            </dl>
            <p className="mt-2 text-xs text-amber-700">&ldquo;Unknown&rdquo; means the supplier didn&apos;t disclose it, not that it isn&apos;t allowed.</p>
          </Card>

          <Card title="Passengers">
            <ul className="flex flex-col gap-1 text-sm">
              {b.passengers.map((p, i) => (
                <li key={i} className="flex justify-between">
                  <span className="text-zinc-900">{p.name}</span>
                  <span className="text-xs capitalize text-zinc-500">{p.type.toLowerCase()}</span>
                </li>
              ))}
            </ul>
          </Card>
        </div>

        <div className="flex flex-col gap-4">
          <Card title="Price breakdown" action={isAdmin ? <span className="text-xs text-zinc-500">Admin view</span> : undefined}>
            <dl className="divide-y divide-zinc-100">
              {isAdmin && (
                <>
                  <Row label="Base fare" value={inr(b.price.base_fare ?? 0)} />
                  {b.price.taxes?.map((t) => (
                    <Row key={t.code} label={`Tax ${t.code}${t.description ? ` · ${t.description}` : ""}`} value={inr(t.amount)} />
                  ))}
                  <Row label="Fuel surcharge" value={inr(b.price.fuel_surcharge ?? 0)} />
                  <Row label="Supplier fee" value={inr(b.price.supplier_fee ?? 0)} />
                  <Row label="Net fare" value={inr(b.price.net_inr ?? 0)} strong />
                  <Row label="Agency markup" value={inr(b.price.markup_inr ?? 0)} />
                </>
              )}
              {!isAdmin && <Row label="Taxes and fees (included)" value={inr(b.price.taxes_total)} />}
              <Row label="Total to customer" value={inr(b.price.sell_inr)} strong />
            </dl>
          </Card>

          <Card title="Status history">
            <ol className="relative flex flex-col gap-3 border-l border-zinc-200 pl-4">
              {timeline.map((t, i) => (
                <li key={i} className="relative text-sm">
                  <span
                    aria-hidden="true"
                    className={`absolute -left-[21px] top-1.5 h-2.5 w-2.5 rounded-full ring-2 ring-white ${
                      i === timeline.length - 1 ? "bg-blue-600" : "bg-zinc-300"
                    }`}
                  />
                  <p className="font-medium text-zinc-900">{t.label}</p>
                  <p className="text-xs text-zinc-500">
                    {when(t.at)}
                    {t.actor && ` · ${t.actor}`}
                  </p>
                </li>
              ))}
            </ol>
          </Card>
        </div>
      </div>
    </div>
  );
}
