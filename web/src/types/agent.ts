/** Offer card returned by POST /v1/agent/chat. Prices come from backend code. */
export interface AgentOffer {
  offer_id: string;
  rank: number;
  is_test_data: boolean;
  airline: string;
  flight_number: string;
  route: string;
  departure: string;
  arrival: string;
  duration_minutes: number;
  stops: number;
  cabin: string;
  fare_basis: string;
  fare_family: string | null;
  customer_price_inr: string;
  effective_price_inr: string;
  checked_baggage: string;
  cabin_baggage: string;
  refundable: string;
  changeable: string;
  expires_at: string;
  pax_count: number;
  /** Adults/children/infants the fare is priced for; null when the supplier doesn't say. */
  pax_counts?: { adults: number; children: number; infants: number } | null;
  source: "live" | "simulated" | "indicative";
  via: string[];
  supplier_label?: string;
}

export interface ChatResponse {
  session_id: string;
  reply: string;
  offers: AgentOffer[];
}

export type PaxType = "ADULT" | "CHILD" | "INFANT";

export interface PassengerInput {
  pax_type: PaxType;
  title?: string;
  given_name: string;
  family_name: string;
  date_of_birth: string;
  document?: {
    doc_type: "PASSPORT" | "NATIONAL_ID";
    number: string;
    issuing_country: string;
    expiry_date: string;
    nationality: string;
  };
}

export interface PrepareResponse {
  booking_id: string;
  state: string;
  confirmation_token: string;
  confirmation_token_expires_at: string;
  customer_price_inr: string;
  offer_summary: {
    supplier_id: string;
    route: string;
    departure: string;
    cabin: string;
    fare_basis: string;
    valid_until: string;
  };
}

export interface BookingResponse {
  booking_id: string;
  state: string;
  pnr: string | null;
  supplier_booking_ref: string | null;
  passenger_count: number;
}
