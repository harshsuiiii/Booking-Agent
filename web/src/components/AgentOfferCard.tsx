"use client";

import type { AgentOffer } from "@/types/agent";
import { airportDate, airportTime, formatINR, formatMinutes } from "@/lib/offerUtils";
import SimulatedBadge from "@/components/SimulatedBadge";
import { TestDataBadge } from "@/components/ui";

interface Props {
  offer: AgentOffer;
  onBook: (offer: AgentOffer) => void;
}

function Badge({ label, value }: { label: string; value: string }) {
  const tone =
    value === "unknown"
      ? "bg-amber-50 text-amber-700 border-amber-200"
      : value === "no" || value === "none"
        ? "bg-zinc-50 text-zinc-500 border-zinc-200"
        : "bg-green-50 text-green-700 border-green-200";
  return (
    <span
      className={`text-xs px-2 py-0.5 rounded-full font-medium border ${tone}`}
      title={value === "unknown" ? "Supplier did not disclose this information" : undefined}
    >
      {label}: {value}
    </span>
  );
}

export default function AgentOfferCard({ offer, onBook }: Props) {
  return (
    <div
      className="bg-white rounded-xl border border-zinc-200 shadow-sm p-4 flex flex-col gap-3"
      data-testid="agent-offer-card"
    >
      <div className="flex items-start justify-between gap-4">
        <div className="flex flex-col gap-0.5">
          <span className="text-xs text-zinc-400 font-mono">
            #{offer.rank} · {offer.airline} {offer.flight_number}
          </span>
          <span className="text-lg font-bold text-zinc-900">
            {formatINR(offer.customer_price_inr)}
          </span>
          <span className="text-xs text-zinc-500">
            {offer.pax_count} passenger{offer.pax_count !== 1 ? "s" : ""} · incl. taxes
            {offer.supplier_label && <> · via {offer.supplier_label}</>}
          </span>
        </div>
        <div className="flex flex-col items-end gap-1 text-right">
          <span className="text-sm font-medium text-zinc-700">{offer.route}</span>
          <span className="text-xs text-zinc-500">
            {formatMinutes(offer.duration_minutes)} ·{" "}
            {offer.stops === 0
              ? "Direct"
              : `${offer.stops} stop${offer.stops > 1 ? "s" : ""}${offer.via?.length ? ` via ${offer.via.join(", ")}` : ""}`}
          </span>
          <span className="text-xs text-zinc-400 font-mono">
            {airportDate(offer.departure)} · {airportTime(offer.departure)} → {airportTime(offer.arrival)}
          </span>
        </div>
      </div>

      <div className="flex items-center justify-between gap-2 flex-wrap">
        <div className="flex gap-1.5 flex-wrap">
          {offer.source === "simulated" && <SimulatedBadge />}
          {offer.is_test_data && <TestDataBadge />}
          <Badge label="Bags" value={offer.checked_baggage} />
          <Badge label="Refund" value={offer.refundable} />
          <Badge label="Change" value={offer.changeable} />
        </div>
        <button
          type="button"
          onClick={() => onBook(offer)}
          className="text-sm font-medium bg-blue-600 text-white rounded-lg px-4 py-1.5 hover:bg-blue-700"
        >
          Book
        </button>
      </div>
    </div>
  );
}
