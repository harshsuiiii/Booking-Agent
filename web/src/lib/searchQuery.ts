/**
 * The flight search the form builds, its API body, and its URL form.
 *
 * URL (shareable, reload-safe; prefills the form, never auto-runs a search):
 *   ?trip=multi_city&legs=DEL.BOM.2026-11-01.morning~BOM.GOI.2026-11-03.any
 *    &adt=1&chd=0&inf=0&cabin=ECONOMY&stops=0&air=AI,6E&bags=1&refund=1&noovn=1
 * The older ?from=DEL&to=BOM&date=2026-11-01 form (dashboard links) still prefills.
 */

export type JourneyType = "one_way" | "return" | "multi_city";
export type TimeWindow = "any" | "early_morning" | "morning" | "afternoon" | "evening";

export const TIME_WINDOWS: { value: TimeWindow; label: string }[] = [
  { value: "any", label: "At any time" },
  { value: "early_morning", label: "Early morning (00–06)" },
  { value: "morning", label: "Morning (06–12)" },
  { value: "afternoon", label: "Afternoon (12–18)" },
  { value: "evening", label: "Evening (18–24)" },
];

export const CABINS = [
  { value: "ECONOMY", label: "Economy" },
  { value: "PREMIUM_ECONOMY", label: "Premium economy" },
  { value: "BUSINESS", label: "Business" },
  { value: "FIRST", label: "First" },
] as const;

export const MAX_LEGS = 6;
export const MAX_PAX = 9;

export interface SearchLeg {
  origin: string;
  destination: string;
  departure_date: string; // YYYY-MM-DD
  time_window: TimeWindow;
}

export interface Passengers {
  adults: number;
  children: number;
  infants: number;
}

export interface AdvancedFilters {
  max_stops: number | null; // per flight; null = any
  preferred_airlines: string[];
  bags_included: boolean;
  refundable_only: boolean;
  no_overnight_layover: boolean;
}

export interface SearchParams {
  journey_type: JourneyType;
  legs: SearchLeg[];
  passengers: Passengers;
  cabin: string;
  filters: AdvancedFilters;
}

export const DEFAULT_FILTERS: AdvancedFilters = {
  max_stops: null,
  preferred_airlines: [],
  bags_included: false,
  refundable_only: false,
  no_overnight_layover: false,
};

/** Body for POST /v1/search. */
export function toRequestBody(p: SearchParams) {
  return {
    journey_type: p.journey_type,
    legs: p.legs.map((l) => ({ ...l, origin: l.origin.toUpperCase(), destination: l.destination.toUpperCase() })),
    cabin: p.cabin,
    pax_counts: p.passengers,
    filters: {
      max_stops: p.filters.max_stops,
      preferred_airlines: p.filters.preferred_airlines,
      bags_included: p.filters.bags_included,
      refundable_only: p.filters.refundable_only,
      no_overnight_layover: p.filters.no_overnight_layover,
    },
  };
}

export function passengerSummary({ adults, children, infants }: Passengers): string {
  const part = (n: number, one: string, many: string) => (n ? `${n} ${n === 1 ? one : many}` : "");
  return [part(adults, "adult", "adults"), part(children, "child", "children"), part(infants, "infant", "infants")]
    .filter(Boolean)
    .join(", ");
}

// ── dates ─────────────────────────────────────────────────────────────────────

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

/** YYYY-MM-DD in the browser's timezone (toISOString would use UTC and can be a day off in IST). */
export function toLocalIsoDate(d: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

export function addDays(isoDate: string, days: number): string {
  const d = new Date(`${isoDate}T12:00:00`);
  d.setDate(d.getDate() + days);
  return toLocalIsoDate(d);
}

/** A real calendar date as YYYY-MM-DD, or null (e.g. 31/02/2027). */
export function isoFromParts(day: string, month: string, year: string): string | null {
  if (!/^\d{1,2}$/.test(day) || !/^\d{1,2}$/.test(month) || !/^\d{4}$/.test(year)) return null;
  const d = Number(day), m = Number(month), y = Number(year);
  const date = new Date(y, m - 1, d);
  if (date.getFullYear() !== y || date.getMonth() !== m - 1 || date.getDate() !== d) return null;
  return toLocalIsoDate(date);
}

// ── URL ───────────────────────────────────────────────────────────────────────

const IATA = /^[A-Z]{3}$/;
const JOURNEYS: JourneyType[] = ["one_way", "return", "multi_city"];
const WINDOWS = new Set<string>(TIME_WINDOWS.map((w) => w.value));
const CABIN_VALUES = new Set<string>(CABINS.map((c) => c.value));

export function toUrlQuery(p: SearchParams): string {
  const q = new URLSearchParams();
  q.set("trip", p.journey_type);
  q.set("legs", p.legs.map((l) => [l.origin, l.destination, l.departure_date, l.time_window].join(".")).join("~"));
  q.set("adt", String(p.passengers.adults));
  if (p.passengers.children) q.set("chd", String(p.passengers.children));
  if (p.passengers.infants) q.set("inf", String(p.passengers.infants));
  q.set("cabin", p.cabin);
  if (p.filters.max_stops !== null) q.set("stops", String(p.filters.max_stops));
  if (p.filters.preferred_airlines.length) q.set("air", p.filters.preferred_airlines.join(","));
  if (p.filters.bags_included) q.set("bags", "1");
  if (p.filters.refundable_only) q.set("refund", "1");
  if (p.filters.no_overnight_layover) q.set("noovn", "1");
  return q.toString();
}

/** Whatever valid prefill the URL carries; unknown or bad values are dropped, past dates too. */
export function fromUrlQuery(q: URLSearchParams, today: string): Partial<SearchParams> {
  const out: Partial<SearchParams> = {};
  const futureDate = (d: string | undefined) => (d && ISO_DATE.test(d) && (!today || d >= today) ? d : "");

  const legs: SearchLeg[] = (q.get("legs") ?? "")
    .split("~")
    .filter(Boolean)
    .slice(0, MAX_LEGS)
    .map((raw) => {
      const [o = "", d = "", date, w = "any"] = raw.split(".");
      return {
        origin: IATA.test(o.toUpperCase()) ? o.toUpperCase() : "",
        destination: IATA.test(d.toUpperCase()) ? d.toUpperCase() : "",
        departure_date: futureDate(date),
        time_window: (WINDOWS.has(w) ? w : "any") as TimeWindow,
      };
    });
  if (!legs.length && (q.get("from") || q.get("to"))) {
    const o = (q.get("from") ?? "").toUpperCase();
    const d = (q.get("to") ?? "").toUpperCase();
    legs.push({
      origin: IATA.test(o) ? o : "",
      destination: IATA.test(d) ? d : "",
      departure_date: futureDate(q.get("date") ?? undefined),
      time_window: "any",
    });
  }
  if (legs.length) out.legs = legs;

  const trip = q.get("trip") as JourneyType | null;
  if (trip && JOURNEYS.includes(trip)) out.journey_type = trip;
  else if (legs.length) out.journey_type = legs.length === 1 ? "one_way" : legs.length === 2 && legs[1].destination === legs[0].origin ? "return" : "multi_city";

  const int = (key: string, lo: number, hi: number, dflt: number) => {
    const n = Number(q.get(key));
    return q.has(key) && Number.isInteger(n) && n >= lo && n <= hi ? n : dflt;
  };
  if (q.has("adt") || q.has("chd") || q.has("inf")) {
    const adults = int("adt", 1, MAX_PAX, 1);
    const children = Math.min(int("chd", 0, MAX_PAX, 0), MAX_PAX - adults);
    const infants = Math.min(int("inf", 0, MAX_PAX, 0), adults, MAX_PAX - adults - children);
    out.passengers = { adults, children, infants };
  }
  const cabin = q.get("cabin");
  if (cabin && CABIN_VALUES.has(cabin)) out.cabin = cabin;

  const filters: AdvancedFilters = {
    max_stops: q.has("stops") ? int("stops", 0, 2, 0) : null,
    preferred_airlines: (q.get("air") ?? "")
      .split(",")
      .map((a) => a.trim().toUpperCase())
      .filter((a) => /^[A-Z0-9]{2}$/.test(a)),
    bags_included: q.get("bags") === "1",
    refundable_only: q.get("refund") === "1",
    no_overnight_layover: q.get("noovn") === "1",
  };
  if (JSON.stringify(filters) !== JSON.stringify(DEFAULT_FILTERS)) out.filters = filters;
  return out;
}
