"use client";

import { useState } from "react";
import type { SerializedGroup } from "@/types/sse";
import {
  formatINR,
  formatMinutes,
  sliceJourneyMinutes,
  sliceLabel,
  isRoundTripOffer,
  airportDate,
  baggageLabel,
  policyLabel,
  airportTime,
} from "@/lib/offerUtils";
import SimulatedBadge from "@/components/SimulatedBadge";
import { TestDataBadge } from "@/components/ui";

import { apiFetch } from "@/lib/api";

interface PriceChangedInfo {
  old_price_inr: string;
  new_price_inr: string;
}

interface Props {
  group: SerializedGroup;
  onViewDetails: (group: SerializedGroup) => void;
  rank: number;
}

function PolicyBadge({
  label,
  value,
}: {
  label: string;
  value: string;
}) {
  const isUnknownVal = value === "unknown";
  const isYes = value === "yes";
  return (
    <span
      className={`text-xs px-2 py-0.5 rounded-full font-medium ${
        isUnknownVal
          ? "bg-amber-50 text-amber-700 border border-amber-200"
          : isYes
          ? "bg-green-50 text-green-700 border border-green-200"
          : "bg-zinc-50 text-zinc-500 border border-zinc-200"
      }`}
      title={isUnknownVal ? "Supplier did not disclose this information" : undefined}
    >
      {label}: {value}
    </span>
  );
}

function BaggageBadge({ pieces }: { pieces: ReturnType<typeof baggageLabel> }) {
  const isUnknownVal = pieces === "unknown";
  return (
    <span
      className={`text-xs px-2 py-0.5 rounded-full font-medium ${
        isUnknownVal
          ? "bg-amber-50 text-amber-700 border border-amber-200"
          : pieces === "none"
          ? "bg-zinc-50 text-zinc-500 border border-zinc-200"
          : "bg-green-50 text-green-700 border border-green-200"
      }`}
      title={isUnknownVal ? "Supplier did not disclose baggage information" : undefined}
    >
      Bags: {pieces}
    </span>
  );
}

export default function OfferCard({ group, onViewDetails, rank }: Props) {
  const offer = group.primary;
  const [repricing, setRepricing] = useState(false);
  const [priceChanged, setPriceChanged] = useState<PriceChangedInfo | null>(null);
  const [repriceError, setRepriceError] = useState<string | null>(null);

  const isRoundTrip = isRoundTripOffer(offer);

  async function handleReprice() {
    setRepricing(true);
    setPriceChanged(null);
    setRepriceError(null);
    try {
      const res = await apiFetch(`/v1/offers/${offer.offer_id}/price`, { method: "POST" });
      const data = await res.json();
      if (!res.ok) {
        setRepriceError(data.detail ?? `Error ${res.status}`);
      } else if (data.type === "price_changed") {
        setPriceChanged({ old_price_inr: data.old_price_inr, new_price_inr: data.new_price_inr });
      }
    } catch (e) {
      setRepriceError(e instanceof Error ? e.message : "Reprice failed");
    } finally {
      setRepricing(false);
    }
  }

  return (
    <div
      className="bg-white rounded-xl border border-zinc-200 shadow-sm p-4 flex flex-col gap-3 hover:shadow-md transition-shadow"
      data-testid="offer-card"
    >
      {/* Header row */}
      <div className="flex items-start justify-between gap-4">
        <div className="flex flex-col gap-0.5">
          <span className="text-xs text-zinc-400 font-mono flex items-center gap-2">
            #{rank} · {offer.supplier_label ?? offer.supplier_id}
            {offer.source === "simulated" && <SimulatedBadge />}
            {offer.is_test_data && <TestDataBadge />}
          </span>
          <span className="text-lg font-bold text-zinc-900">{formatINR(offer.sell_inr)}</span>
          {offer.fare_family && (
            <span className="text-xs text-zinc-500">{offer.fare_family}</span>
          )}
        </div>

        <ul className="flex flex-col items-end gap-1.5 text-right min-w-0">
          {offer.slices.map((slice, i) => {
            const first = slice.segments[0];
            const last = slice.segments[slice.segments.length - 1];
            if (!first || !last) return null;
            const stops = slice.segments.length - 1;
            return (
              <li key={i} className="flex flex-col items-end gap-0.5" data-testid="offer-slice">
                <div className="flex items-center gap-2 text-sm font-medium text-zinc-700">
                  {offer.slices.length > 1 && (
                    <span className="text-xs font-normal text-zinc-400">{sliceLabel(i, offer.slices.length, isRoundTrip)}</span>
                  )}
                  <span>{first.origin}</span>
                  <span className="text-zinc-400">→</span>
                  <span>{last.destination}</span>
                </div>
                <span className="text-xs text-zinc-500">
                  {formatMinutes(sliceJourneyMinutes(slice))} · {stops === 0 ? "Direct" : `${stops} stop${stops > 1 ? "s" : ""}`}
                </span>
                <span className="text-xs text-zinc-400 font-mono">
                  {offer.slices.length > 1 && <>{airportDate(first.departure_at)} · </>}
                  {airportTime(first.departure_at)} → {airportTime(last.arrival_at)}
                </span>
              </li>
            );
          })}
        </ul>
      </div>

      {/* Policy badges */}
      <div className="flex flex-wrap gap-1.5">
        <BaggageBadge pieces={baggageLabel(offer.baggage.checked_pieces)} />
        <PolicyBadge label="Refund" value={policyLabel(offer.fare_rules.refund.allowed)} />
        <PolicyBadge label="Change" value={policyLabel(offer.fare_rules.change.allowed)} />
      </div>

      {/* Alternates */}
      {group.alternates.length > 0 && (
        <div className="text-xs text-zinc-500 border-t border-zinc-100 pt-2">
          Also via:{" "}
          {group.alternates.map((a) => (
            <span key={a.offer_id} className="font-mono mr-2">
              {a.supplier_label ?? a.supplier_id} ({formatINR(a.sell_inr)})
            </span>
          ))}
        </div>
      )}

      {/* Price changed banner */}
      {priceChanged && (
        <div className="bg-amber-50 border border-amber-200 rounded-lg p-2 text-xs text-amber-800">
          Price changed:{" "}
          <span className="line-through">{formatINR(priceChanged.old_price_inr)}</span>
          {" → "}
          <span className="font-bold">{formatINR(priceChanged.new_price_inr)}</span>
        </div>
      )}
      {repriceError && (
        <div className="bg-red-50 border border-red-200 rounded-lg p-2 text-xs text-red-700">
          {repriceError}
        </div>
      )}

      {/* Actions */}
      <div className="flex gap-2 pt-1">
        <button
          onClick={() => onViewDetails(group)}
          className="flex-1 text-sm text-blue-600 border border-blue-200 rounded-lg py-1.5 hover:bg-blue-50 transition-colors font-medium"
        >
          View details
        </button>
        <button
          onClick={handleReprice}
          disabled={repricing}
          className="text-sm text-zinc-600 border border-zinc-200 rounded-lg px-3 py-1.5 hover:bg-zinc-50 transition-colors disabled:opacity-50"
          title="Get live price from supplier"
        >
          {repricing ? "Checking…" : "Re-price live"}
        </button>
      </div>
    </div>
  );
}
