/**
 * SSE event types and serialized domain types for the booking agent UI.
 * Derived from the FastAPI backend's serialization format (model_dump mode="json").
 */

export type Unknown = "UNKNOWN";

export interface Carrier {
  iata_code: string;
  name: string | null;
}

export interface Segment {
  marketing_carrier: Carrier;
  operating_carrier: Carrier | null;
  flight_number: string;
  origin: string;
  destination: string;
  departure_at: string;
  arrival_at: string;
  cabin_class: string;
  booking_class: string;
  duration_minutes: number;
  stops: number;
}

export interface SliceData {
  segments: Segment[];
}

export interface BaggageAllowance {
  cabin_pieces: number | null | Unknown;
  cabin_kg: string | null | Unknown;
  checked_pieces: number | null | Unknown;
  checked_kg: string | null | Unknown;
}

export interface FareRulePolicy {
  allowed: boolean | Unknown;
  fee: { amount: string; currency: string } | null | Unknown;
}

export interface FareRules {
  change: FareRulePolicy;
  cancel: FareRulePolicy;
  refund: FareRulePolicy;
}

export interface TaxLine {
  code: string;
  description: string | null;
  amount: string;
}

/**
 * Role-dependent. Agents get only `sell_total` and `taxes_total`; admins get the
 * supplier breakdown (base fare, taxes, fuel, supplier fee, agency markup).
 */
export interface PriceBreakdown {
  currency: string;
  sell_total?: string;
  taxes_total?: string;
  base_fare?: string;
  taxes?: TaxLine[];
  fuel_surcharge?: string;
  supplier_fee?: string;
  agency_markup?: string;
}

export interface SerializedOffer {
  offer_id: string;
  supplier_id: string;
  supplier_offer_ref: string;
  slices: SliceData[];
  fare_basis: string;
  fare_family: string | null;
  cabin_class: string;
  baggage: BaggageAllowance;
  fare_rules: FareRules;
  price: PriceBreakdown;
  /** What the customer pays (INR). Present for every role. */
  sell_inr: string;
  /** Admin only. */
  net_inr?: string;
  markup_inr?: string;
  expires_at: string;
  pax_count: number;
  /** "simulated" offers are generated for demos and are never real fares. */
  source?: "live" | "simulated" | "indicative";
  /** Sandbox/test supplier data (e.g. Duffel test mode): never a real fare or ticket. */
  is_test_data?: boolean;
  supplier_label?: string;
}

export interface AlternateSource {
  supplier_id: string;
  offer_id: string;
  sell_inr: string;
  supplier_label?: string;
  /** Admin only. */
  net_inr?: string;
  markup_inr?: string;
}

export interface SerializedGroup {
  dedupe_key: string;
  primary: SerializedOffer;
  alternates: AlternateSource[];
}

export type SSEEvent =
  | { type: "source_started"; supplier_id: string; searched_at: string }
  | { type: "offers_partial"; supplier_id: string; offers_count: number; groups: SerializedGroup[] }
  | { type: "source_failed"; supplier_id: string; reason: string }
  | { type: "source_timed_out"; supplier_id: string; timeout_seconds: number }
  | { type: "source_skipped"; supplier_id: string; reason: string }
  | { type: "cache_hit"; offers_count: number }
  | {
      type: "search_complete";
      groups: SerializedGroup[];
      suppliers_succeeded: string[];
      suppliers_failed: string[];
      suppliers_timed_out: string[];
      suppliers_skipped: string[];
      elapsed_seconds: number;
    };

export type SupplierStatus = "loading" | "done" | "failed" | "timed_out" | "skipped" | "cache";

export type SortMode = "cheapest" | "fastest" | "best_value";
