/** Shared UI primitives: status badge, demo label, cards, and empty/error/loading states. */
import type { ReactNode } from "react";
import type { BookingState } from "@/types/dashboard";
import { STATE_LABEL } from "@/lib/format";

type Tone = "good" | "progress" | "warning" | "critical" | "neutral";

const STATE_TONE: Record<BookingState, Tone> = {
  TICKETED: "good",
  PAID: "good",
  PNR_CREATED: "progress",
  CONFIRMED_BY_USER: "progress",
  PAYMENT_PENDING: "warning",
  PRICED: "warning",
  FAILED: "critical",
  CANCELLED: "neutral",
  EXPIRED: "neutral",
  DRAFT: "neutral",
};

const TONE_CLASS: Record<Tone, string> = {
  good: "bg-emerald-50 text-emerald-800 border-emerald-200",
  progress: "bg-sky-50 text-sky-800 border-sky-200",
  warning: "bg-amber-50 text-amber-800 border-amber-200",
  critical: "bg-red-50 text-red-800 border-red-200",
  neutral: "bg-zinc-100 text-zinc-700 border-zinc-200",
};

// Shape differs per tone too, so status never relies on colour alone.
const TONE_GLYPH: Record<Tone, string> = {
  good: "✓",
  progress: "→",
  warning: "!",
  critical: "×",
  neutral: "–",
};

export function StatusBadge({ state }: { state: BookingState }) {
  const tone = STATE_TONE[state];
  return (
    <span
      className={`inline-flex items-center gap-1 whitespace-nowrap rounded-full border px-2 py-0.5 text-xs font-medium ${TONE_CLASS[tone]}`}
    >
      <span aria-hidden="true" className="font-bold">
        {TONE_GLYPH[tone]}
      </span>
      {STATE_LABEL[state]}
    </span>
  );
}

export function DemoTag() {
  return (
    <span
      className="inline-flex items-center rounded border border-violet-200 bg-violet-50 px-1.5 py-px text-[10px] font-semibold uppercase tracking-wide text-violet-700"
      title="Seeded demo booking: not a real customer or ticket"
    >
      demo data
    </span>
  );
}

export function TestDataBadge() {
  return (
    <span
      className="inline-flex items-center rounded-full border border-red-200 bg-red-50 px-2 py-0.5 text-xs font-semibold text-red-700"
      title="Supplier sandbox (test mode): not a real fare; any booking is a test order with no real ticket."
      data-testid="test-data-badge"
    >
      Test data
    </span>
  );
}

export function Card({
  title,
  action,
  children,
  className = "",
}: {
  title?: string;
  action?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`min-w-0 rounded-xl border border-zinc-200 bg-white shadow-sm ${className}`}>
      {(title || action) && (
        <header className="flex items-center justify-between gap-3 border-b border-zinc-100 px-4 py-3">
          {title && <h2 className="text-sm font-semibold text-zinc-900">{title}</h2>}
          {action}
        </header>
      )}
      <div className="p-4">{children}</div>
    </section>
  );
}

export function EmptyState({ title, hint }: { title: string; hint?: ReactNode }) {
  return (
    <div className="flex flex-col items-center justify-center gap-1 py-10 text-center">
      <p className="text-sm font-medium text-zinc-700">{title}</p>
      {hint && <p className="text-sm text-zinc-500">{hint}</p>}
    </div>
  );
}

export function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div
      role="alert"
      className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-800"
    >
      <span>{message}</span>
      {onRetry && (
        <button
          type="button"
          onClick={onRetry}
          className="rounded-md border border-red-300 bg-white px-3 py-1 font-medium hover:bg-red-100 focus-visible:outline-2 focus-visible:outline-red-600"
        >
          Try again
        </button>
      )}
    </div>
  );
}

export function Skeleton({ className = "" }: { className?: string }) {
  return <div className={`animate-pulse rounded-md bg-zinc-100 ${className}`} aria-hidden="true" />;
}

export function PageHeader({ title, subtitle, action }: { title: string; subtitle?: string; action?: ReactNode }) {
  return (
    <div className="flex flex-wrap items-end justify-between gap-3">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight text-zinc-900">{title}</h1>
        {subtitle && <p className="mt-0.5 text-sm text-zinc-500">{subtitle}</p>}
      </div>
      {action}
    </div>
  );
}
