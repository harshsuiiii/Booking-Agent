"use client";

import { airportDate, airportTime, isRoundTripOffer, sliceLabel } from "@/lib/offerUtils";

import { Fragment, useEffect } from "react";
import type { SerializedGroup, SerializedOffer, TaxLine } from "@/types/sse";
import {
  formatINR,
  formatMinutes,
  baggageLabel,
  policyLabel,
  isUnknown,
  layoverMinutes,
  sliceJourneyMinutes,
} from "@/lib/offerUtils";

interface Props {
  group: SerializedGroup | null;
  onClose: () => void;
}

function Row({ label, value, highlight }: { label: string; value: string; highlight?: boolean }) {
  return (
    <div className={`flex justify-between items-center py-1.5 ${highlight ? "font-semibold" : ""}`}>
      <span className="text-sm text-zinc-600">{label}</span>
      <span className={`text-sm ${highlight ? "text-zinc-900" : "text-zinc-700"}`}>{value}</span>
    </div>
  );
}

function PolicyRow({ label, policy }: { label: string; policy: SerializedOffer["fare_rules"]["change"] }) {
  const allowed = policyLabel(policy.allowed as boolean | "UNKNOWN");
  const fee = policy.fee;
  let feeStr = "";
  if (isUnknown(fee)) feeStr = " (fee: unknown)";
  else if (fee === null) feeStr = " (no fee)";
  else if (fee) feeStr = ` (fee: ${formatINR(fee.amount)})`;

  return (
    <div className="flex justify-between items-center py-1.5">
      <span className="text-sm text-zinc-600">{label}</span>
      <span
        className={`text-sm font-medium ${
          allowed === "unknown"
            ? "text-amber-600"
            : allowed === "yes"
            ? "text-green-600"
            : "text-zinc-500"
        }`}
        title={allowed === "unknown" ? "Supplier did not disclose this information" : undefined}
      >
        {allowed}{feeStr}
      </span>
    </div>
  );
}

export default function OfferDrawer({ group, onClose }: Props) {
  useEffect(() => {
    if (!group) return;
    function handleKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    document.addEventListener("keydown", handleKey);
    return () => document.removeEventListener("keydown", handleKey);
  }, [group, onClose]);

  if (!group) return null;
  const offer = group.primary;
  const bd = offer.price;
  // Admins get the supplier breakdown; agents get sell price and taxes only.
  const isBreakdown = bd.base_fare !== undefined;
  const taxesTotal = bd.taxes
    ? bd.taxes.reduce((sum, t: TaxLine) => sum + parseFloat(t.amount), 0)
    : parseFloat(bd.taxes_total ?? "0");
  const money = (v: string | undefined) => `${bd.currency} ${parseFloat(v ?? "0").toLocaleString("en-IN")}`;

  return (
    <>
      {/* Backdrop */}
      <div
        className="fixed inset-0 bg-black/30 z-40"
        onClick={onClose}
        aria-hidden="true"
      />

      {/* Drawer */}
      <aside
        className="fixed right-0 top-0 h-full w-full max-w-lg bg-white shadow-2xl z-50 flex flex-col overflow-hidden"
        role="dialog"
        aria-modal="true"
        aria-label="Offer details"
      >
        {/* Header */}
        <div className="flex items-center justify-between px-5 py-4 border-b border-zinc-200">
          <div>
            <h2 className="text-base font-semibold text-zinc-900">Offer details</h2>
            <p className="text-xs text-zinc-400 font-mono">{offer.supplier_label ?? offer.supplier_id} · {offer.fare_basis}</p>
          </div>
          <button
            onClick={onClose}
            className="text-zinc-400 hover:text-zinc-600 p-1 rounded"
            aria-label="Close"
          >
            ✕
          </button>
        </div>

        <div className="flex-1 overflow-y-auto px-5 py-4 flex flex-col gap-5">
          {/* Itinerary */}
          {offer.slices.map((slice, si) => (
            <section key={si}>
              <h3 className="text-xs font-semibold text-zinc-400 uppercase tracking-wide mb-2 flex justify-between">
                <span>
                  {sliceLabel(si, offer.slices.length, isRoundTripOffer(offer))}
                  {slice.segments[0] && (
                    <span className="normal-case tracking-normal font-normal"> · {airportDate(slice.segments[0].departure_at)}</span>
                  )}
                </span>
                <span className="normal-case tracking-normal" data-testid="slice-journey-time">
                  {formatMinutes(sliceJourneyMinutes(slice))} total
                </span>
              </h3>
              <div className="flex flex-col gap-2">
                {slice.segments.map((seg, i) => (
                  <Fragment key={i}>
                    {i > 0 && (
                      <div className="text-xs text-zinc-500 px-3" data-testid="layover">
                        Layover at {seg.origin} · {formatMinutes(layoverMinutes(slice, i - 1))}
                      </div>
                    )}
                    <div className="border border-zinc-100 rounded-lg p-3 text-sm">
                      <div className="flex items-center justify-between font-medium text-zinc-800">
                        <span className="font-mono">{seg.origin} → {seg.destination}</span>
                        <span className="text-xs text-zinc-400 font-mono">
                          {seg.marketing_carrier.iata_code}{seg.flight_number}
                        </span>
                      </div>
                      <div className="text-xs text-zinc-500 mt-1 flex gap-4">
                        <span>
                          {airportTime(seg.departure_at)}
                          {" – "}
                          {airportTime(seg.arrival_at)}
                        </span>
                        <span>{formatMinutes(seg.duration_minutes)} flight</span>
                        {seg.stops > 0 && <span>{seg.stops} tech stop{seg.stops > 1 ? "s" : ""}</span>}
                      </div>
                    </div>
                  </Fragment>
                ))}
              </div>
            </section>
          ))}

          {/* Baggage */}
          <section>
            <h3 className="text-xs font-semibold text-zinc-400 uppercase tracking-wide mb-2">Baggage</h3>
            <div className="border border-zinc-100 rounded-lg divide-y divide-zinc-100">
              <Row label="Checked bags" value={baggageLabel(offer.baggage.checked_pieces)} />
              <Row
                label="Checked kg"
                value={isUnknown(offer.baggage.checked_kg) ? "unknown" : offer.baggage.checked_kg ? `${offer.baggage.checked_kg} kg` : "—"}
              />
              <Row label="Cabin bags" value={baggageLabel(offer.baggage.cabin_pieces)} />
            </div>
          </section>

          {/* Fare rules */}
          <section>
            <h3 className="text-xs font-semibold text-zinc-400 uppercase tracking-wide mb-2">Fare rules</h3>
            <div className="border border-zinc-100 rounded-lg divide-y divide-zinc-100">
              <PolicyRow label="Change" policy={offer.fare_rules.change} />
              <PolicyRow label="Cancel" policy={offer.fare_rules.cancel} />
              <PolicyRow label="Refund" policy={offer.fare_rules.refund} />
            </div>
            <p className="text-[10px] text-amber-600 mt-1">
              &ldquo;unknown&rdquo; means the supplier did not disclose this — not that it is
              unavailable.
            </p>
          </section>

          {/* Price breakdown */}
          <section>
            <h3 className="text-xs font-semibold text-zinc-400 uppercase tracking-wide mb-2">Price breakdown</h3>
            <div className="border border-zinc-100 rounded-lg divide-y divide-zinc-100">
              {isBreakdown && <Row label="Base fare" value={money(bd.base_fare)} />}
              {bd.taxes?.map((t: TaxLine) => (
                <Row key={t.code} label={`Tax ${t.code}${t.description ? ` (${t.description})` : ""}`} value={money(t.amount)} />
              ))}
              <Row label="Taxes total" value={`${bd.currency} ${taxesTotal.toLocaleString("en-IN")}`} />
              {isBreakdown && parseFloat(bd.fuel_surcharge ?? "0") > 0 && (
                <Row label="Fuel surcharge" value={money(bd.fuel_surcharge)} />
              )}
              {isBreakdown && parseFloat(bd.supplier_fee ?? "0") > 0 && (
                <Row label="Supplier fee" value={money(bd.supplier_fee)} />
              )}
              {offer.net_inr !== undefined && <Row label="Net fare (admin)" value={formatINR(offer.net_inr)} />}
              {offer.markup_inr !== undefined && <Row label="Agency markup (admin)" value={formatINR(offer.markup_inr)} />}
              <Row label="Total to customer (INR)" value={formatINR(offer.sell_inr)} highlight />
            </div>
            <p className="text-[10px] text-zinc-400 mt-1">
              This price is a display estimate. Live re-price is required before booking.
            </p>
          </section>

          {/* Alternates */}
          {group.alternates.length > 0 && (
            <section>
              <h3 className="text-xs font-semibold text-zinc-400 uppercase tracking-wide mb-2">
                Also available via
              </h3>
              <div className="flex flex-col gap-1">
                {group.alternates.map((a) => (
                  <div key={a.offer_id} className="flex justify-between text-sm text-zinc-600 border border-zinc-100 rounded-lg px-3 py-2">
                    <span>{a.supplier_label ?? a.supplier_id}</span>
                    <span className="font-medium">{formatINR(a.sell_inr)}</span>
                  </div>
                ))}
              </div>
            </section>
          )}
        </div>
      </aside>
    </>
  );
}
