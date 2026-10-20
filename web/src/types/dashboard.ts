export type BookingState =
  | "DRAFT"
  | "PRICED"
  | "CONFIRMED_BY_USER"
  | "PNR_CREATED"
  | "PAYMENT_PENDING"
  | "PAID"
  | "TICKETED"
  | "FAILED"
  | "CANCELLED"
  | "EXPIRED";

/** Price fields depend on role: net/markup are only ever present for admins. */
export interface MoneyView {
  sell_inr: string;
  currency: string;
  net_inr?: string;
  markup_inr?: string;
}

export interface BookingListItem {
  booking_id: string;
  reference: string;
  passenger: string;
  origin: string;
  destination: string;
  travel_date: string;
  supplier_id: string;
  supplier_label: string;
  state: BookingState;
  pnr: string | null;
  is_demo: boolean;
  created_at: string;
  price: MoneyView;
}

export interface BookingPage {
  items: BookingListItem[];
  total: number;
  page: number;
  page_size: number;
}

export interface Transition {
  from: BookingState;
  to: BookingState;
  actor: string;
  occurred_at: string;
}

export interface BookingDetail {
  booking_id: string;
  reference: string;
  state: BookingState;
  pnr: string | null;
  supplier_booking_ref: string | null;
  supplier_id: string;
  supplier_label: string;
  passenger_count: number;
  passengers: { name: string; type: string }[];
  is_demo: boolean;
  created_at: string;
  updated_at: string;
  transitions: Transition[];
  error_detail: string | null;
  // Same shape as a search offer (types/sse SerializedOffer), role view.
  offer: import("@/types/sse").SerializedOffer;
  price: MoneyView & {
    taxes_total: string;
    base_fare?: string;
    fuel_surcharge?: string;
    supplier_fee?: string;
    taxes?: { code: string; description: string | null; amount: string }[];
  };
}

export interface RecentSearch {
  origin: string;
  destination: string;
  departure_date: string;
  passengers: number;
  cabin: string;
  source: "search" | "assistant";
  offer_count: number;
  searched_at: string;
}

export interface Summary {
  as_of: string;
  month: string;
  bookings_this_month: number;
  sales_this_month_inr: string;
  markup_this_month_inr?: string;
  upcoming_trips: number;
  next_trips: {
    booking_id: string;
    reference: string;
    passenger: string;
    origin: string;
    destination: string;
    travel_date: string;
    is_demo: boolean;
  }[];
  status_counts: Partial<Record<BookingState, number>>;
  recent_searches: RecentSearch[];
}

export interface SupplierChartItem {
  supplier_id: string;
  supplier_label?: string;
  bookings: number;
  sales_inr: string;
  supplier_performance?: {
    net_inr: string;
    markup_inr: string;
    searches: number;
    wins: number;
    win_rate: number | null;
    error_rate: number | null;
    avg_latency_ms: number | null;
  };
}

export interface Charts {
  days: number;
  bookings_over_time: { date: string; bookings: number; sales_inr: string }[];
  by_supplier: SupplierChartItem[];
  metrics_since: string | null;
}
