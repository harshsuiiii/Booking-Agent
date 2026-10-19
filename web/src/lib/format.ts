import type { BookingState } from "@/types/dashboard";

/** "₹1,23,456" (Indian grouping, no decimals). */
export function inr(value: string | number): string {
  const n = typeof value === "string" ? parseFloat(value) : value;
  return new Intl.NumberFormat("en-IN", {
    style: "currency",
    currency: "INR",
    maximumFractionDigits: 0,
  }).format(Number.isFinite(n) ? n : 0);
}

/** "₹1.7L" / "₹12.4K" for chart axes and tight spaces. */
export function inrCompact(value: string | number): string {
  const n = typeof value === "string" ? parseFloat(value) : value;
  if (n >= 1e7) return `₹${(n / 1e7).toFixed(1)}Cr`;
  if (n >= 1e5) return `₹${(n / 1e5).toFixed(1)}L`;
  if (n >= 1e3) return `₹${(n / 1e3).toFixed(1)}K`;
  return `₹${Math.round(n)}`;
}

/** Calendar date ("2026-11-20") as "20 Nov 2026", without timezone shifts. */
export function day(isoDate: string): string {
  const [y, m, d] = isoDate.slice(0, 10).split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d)).toLocaleDateString("en-IN", {
    day: "numeric",
    month: "short",
    year: "numeric",
    timeZone: "UTC",
  });
}

/** Timestamp in the viewer's local time: "18 Oct, 14:05". */
export function when(iso: string): string {
  return new Date(iso).toLocaleString("en-IN", {
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export const STATE_LABEL: Record<BookingState, string> = {
  DRAFT: "Draft",
  PRICED: "Priced",
  CONFIRMED_BY_USER: "Confirmed",
  PNR_CREATED: "PNR created",
  PAYMENT_PENDING: "Payment pending",
  PAID: "Paid",
  TICKETED: "Ticketed",
  FAILED: "Failed",
  CANCELLED: "Cancelled",
  EXPIRED: "Expired",
};

export const ALL_STATES = Object.keys(STATE_LABEL) as BookingState[];

/** Mirrors booking_agent/suppliers.py; offers and bookings also carry supplier_label. */
const SUPPLIER_LABEL: Record<string, string> = {
  duffel: "Duffel (sandbox)",
  sim_gds: "Simulated GDS",
  sim_consolidator: "Simulated consolidator",
  sim_ota: "Simulated OTA",
  demo: "Demo supplier",
  cache: "Cached results",
};

export function supplierLabel(id: string): string {
  return SUPPLIER_LABEL[id] ?? id;
}
