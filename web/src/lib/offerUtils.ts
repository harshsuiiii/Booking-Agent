import type { SerializedOffer, SliceData, Unknown } from "@/types/sse";

export const UNKNOWN: Unknown = "UNKNOWN";

export function isUnknown(v: unknown): v is Unknown {
  return v === "UNKNOWN";
}

/** Whole minutes between two ISO timestamps. Both carry UTC offsets, so this is real elapsed time. */
function minutesBetween(fromIso: string, toIso: string): number {
  return Math.round((Date.parse(toIso) - Date.parse(fromIso)) / 60_000);
}

/** Elapsed time of a slice, first departure to last arrival, layovers included. */
export function sliceJourneyMinutes(slice: SliceData): number {
  const first = slice.segments[0];
  const last = slice.segments[slice.segments.length - 1];
  if (!first || !last) return 0;
  return minutesBetween(first.departure_at, last.arrival_at);
}

/** Elapsed journey time of the first slice (outbound), layovers included. */
export function outboundJourneyMinutes(offer: SerializedOffer): number {
  const slice: SliceData | undefined = offer.slices[0];
  return slice ? sliceJourneyMinutes(slice) : 0;
}

/** Elapsed time of every slice added up (each slice's layovers included, not the days between). */
export function totalJourneyMinutes(offer: SerializedOffer): number {
  return offer.slices.reduce((sum, s) => sum + sliceJourneyMinutes(s), 0);
}

/** Two slices that go there and back. */
export function isRoundTripOffer(offer: SerializedOffer): boolean {
  const [a, b] = offer.slices;
  if (offer.slices.length !== 2 || !a?.segments.length || !b?.segments.length) return false;
  return a.segments[0].origin === b.segments[b.segments.length - 1].destination &&
    a.segments[a.segments.length - 1].destination === b.segments[0].origin;
}

/** "Outbound" / "Return" for a round trip, else "Flight 2". */
export function sliceLabel(i: number, count: number, roundTrip: boolean): string {
  if (roundTrip && count === 2) return i === 0 ? "Outbound" : "Return";
  return `Flight ${i + 1}`;
}

/** Ground time between segment i and segment i + 1 of a slice. */
export function layoverMinutes(slice: SliceData, i: number): number {
  return minutesBetween(slice.segments[i].arrival_at, slice.segments[i + 1].departure_at);
}

/** Number of connections in the first slice (segments - 1). */
export function outboundStops(offer: SerializedOffer): number {
  const slice: SliceData | undefined = offer.slices[0];
  if (!slice) return 0;
  return Math.max(0, slice.segments.length - 1);
}

/** Effective price in INR: adds penalties for undisclosed features used in "best value" sort. */
export function effectivePrice(offer: SerializedOffer): number {
  const base = parseFloat(offer.sell_inr);
  let penalty = 0;

  const bags = offer.baggage.checked_pieces;
  if (isUnknown(bags) || bags === null || bags === 0) penalty += 1500;

  const refund = offer.fare_rules.refund.allowed;
  if (isUnknown(refund) || refund === false) penalty += 250;

  const change = offer.fare_rules.change.allowed;
  if (isUnknown(change) || change === false) penalty += 150;

  return base + penalty;
}

export function formatMinutes(minutes: number): string {
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  return m === 0 ? `${h}h` : `${h}h ${m}m`;
}

/**
 * Wall-clock time as written in an ISO timestamp, i.e. the airport's local time.
 * Avoids Date, which would convert to the browser's timezone.
 */
export function airportTime(iso: string): string {
  const m = /T(\d{2}):(\d{2})/.exec(iso);
  return m ? `${m[1]}:${m[2]}` : iso;
}

/** Calendar date as written in an ISO timestamp (airport local), e.g. "20 Nov 2026". */
export function airportDate(iso: string): string {
  const [y, mo, d] = iso.slice(0, 10).split("-").map(Number);
  return new Date(Date.UTC(y, mo - 1, d)).toLocaleDateString("en-IN", {
    day: "numeric",
    month: "short",
    year: "numeric",
    timeZone: "UTC",
  });
}

export function formatINR(value: string | number): string {
  const n = typeof value === "string" ? parseFloat(value) : value;
  return new Intl.NumberFormat("en-IN", {
    style: "currency",
    currency: "INR",
    maximumFractionDigits: 0,
  }).format(n);
}

/** Display a boolean | Unknown value for refundability / changeability. */
export function policyLabel(value: boolean | Unknown | null | undefined): string {
  if (isUnknown(value)) return "unknown";
  if (value === true) return "yes";
  return "no";
}

/** Display a checked baggage value. */
export function baggageLabel(pieces: number | null | Unknown): string {
  if (isUnknown(pieces)) return "unknown";
  if (pieces === null || pieces === 0) return "none";
  return `${pieces} pc`;
}
