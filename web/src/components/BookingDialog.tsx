"use client";

import { useEffect, useState, FormEvent } from "react";
import type {
  AgentOffer,
  BookingResponse,
  PassengerInput,
  PaxType,
  PrepareResponse,
} from "@/types/agent";
import { ApiError, postJson } from "@/lib/api";
import { airportDate, airportTime, formatINR } from "@/lib/offerUtils";

interface Props {
  offer: AgentOffer;
  onClose: () => void;
}

interface PaxForm {
  pax_type: PaxType;
  title: string;
  given_name: string;
  family_name: string;
  date_of_birth: string;
  doc_number: string;
  doc_issuing_country: string;
  doc_nationality: string;
  doc_expiry: string;
}

type Step =
  | { kind: "form" }
  | { kind: "priced"; prep: PrepareResponse }
  | { kind: "done"; booking: BookingResponse };

const EMPTY_PAX: PaxForm = {
  pax_type: "ADULT",
  title: "",
  given_name: "",
  family_name: "",
  date_of_birth: "",
  doc_number: "",
  doc_issuing_country: "IN",
  doc_nationality: "IN",
  doc_expiry: "",
};

function toPassenger(p: PaxForm): PassengerInput {
  return {
    pax_type: p.pax_type,
    title: p.title || undefined,
    given_name: p.given_name.trim(),
    family_name: p.family_name.trim(),
    date_of_birth: p.date_of_birth,
    document: p.doc_number.trim()
      ? {
          doc_type: "PASSPORT",
          number: p.doc_number.trim(),
          issuing_country: p.doc_issuing_country.trim().toUpperCase(),
          expiry_date: p.doc_expiry,
          nationality: p.doc_nationality.trim().toUpperCase(),
        }
      : undefined,
  };
}

function prepareErrorMessage(err: unknown): string {
  if (err instanceof ApiError) {
    const d = err.detail as { code?: string; old_price_inr?: string; new_price_inr?: string };
    if (err.status === 409 && d && typeof d === "object" && d.code === "price_changed") {
      return (
        `The fare changed from ${formatINR(d.old_price_inr ?? "0")} to ` +
        `${formatINR(d.new_price_inr ?? "0")}. Ask the agent to search again for current prices.`
      );
    }
    if (err.status === 404) return "This offer is no longer available. Ask the agent to search again.";
    return err.message;
  }
  return err instanceof Error ? err.message : "Could not price this offer.";
}

const PAX_LABEL: Record<PaxType, string> = { ADULT: "Adult", CHILD: "Child", INFANT: "Infant" };
const PAX_HINT: Record<PaxType, string> = {
  ADULT: "12 or older on the day of travel",
  CHILD: "2 to 11 on the day of travel",
  INFANT: "Under 2 for the whole trip",
};

/** One type per passenger (adults, then children, then infants), or null if unknown. */
function paxTypesOf(offer: AgentOffer): PaxType[] | null {
  const c = offer.pax_counts;
  if (!c || c.adults + c.children + c.infants !== offer.pax_count) return null;
  return [
    ...Array<PaxType>(c.adults).fill("ADULT"),
    ...Array<PaxType>(c.children).fill("CHILD"),
    ...Array<PaxType>(c.infants).fill("INFANT"),
  ];
}

const input =
  "border border-zinc-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500";
const label = "text-xs font-medium text-zinc-500 uppercase tracking-wide";

export default function BookingDialog({ offer, onClose }: Props) {
  // When the offer says who it is priced for, the passenger types are fixed to match.
  const fixedTypes = paxTypesOf(offer);
  const [pax, setPax] = useState<PaxForm[]>(() =>
    Array.from({ length: offer.pax_count }, (_, i) => ({
      ...EMPTY_PAX,
      pax_type: fixedTypes?.[i] ?? "ADULT",
    })),
  );
  // One key per booking attempt; a new one only after the user starts over.
  const [idempotencyKey, setIdempotencyKey] = useState(() => crypto.randomUUID());
  const [step, setStep] = useState<Step>({ kind: "form" });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape" && !busy) onClose();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [busy, onClose]);

  useEffect(() => {
    if (step.kind !== "priced") return;
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [step.kind]);

  function update(i: number, field: keyof PaxForm, value: string) {
    setPax((prev) => prev.map((p, j) => (j === i ? { ...p, [field]: value } : p)));
  }

  async function handlePrepare(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const prep = await postJson<PrepareResponse>("/v1/bookings/prepare", {
        offer_id: offer.offer_id,
        idempotency_key: idempotencyKey,
        passengers: pax.map(toPassenger),
      });
      setNow(Date.now());
      setStep({ kind: "priced", prep });
    } catch (err) {
      setError(prepareErrorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  async function handleConfirm(prep: PrepareResponse) {
    setBusy(true);
    setError(null);
    try {
      const booking = await postJson<BookingResponse>(
        `/v1/bookings/${prep.booking_id}/confirm`,
        { confirmation_token: prep.confirmation_token },
      );
      setStep({ kind: "done", booking });
    } catch (err) {
      // Never retried automatically: a repeat could double-book.
      setError(
        err instanceof ApiError && err.status === 422
          ? `${err.message} Start again to get a fresh price.`
          : `Booking was not completed: ${err instanceof Error ? err.message : "unknown error"}. ` +
              `Reference ${prep.booking_id}. Check its status before trying again.`,
      );
    } finally {
      setBusy(false);
    }
  }

  function startOver() {
    setIdempotencyKey(crypto.randomUUID());
    setStep({ kind: "form" });
    setError(null);
  }

  const secondsLeft =
    step.kind === "priced"
      ? Math.max(0, Math.floor((Date.parse(step.prep.confirmation_token_expires_at) - now) / 1000))
      : 0;
  const expired = step.kind === "priced" && secondsLeft === 0;

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/40 p-4">
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="booking-dialog-title"
        className="bg-white rounded-xl shadow-xl w-full max-w-lg my-8 flex flex-col"
      >
        <div className="flex items-start justify-between gap-4 p-5 border-b border-zinc-200">
          <div>
            <h2 id="booking-dialog-title" className="text-lg font-semibold text-zinc-900">
              Book {offer.route}
            </h2>
            <p className="text-xs text-zinc-500 mt-0.5">
              {offer.airline} {offer.flight_number} ·{" "}
              {airportDate(offer.departure)} {airportTime(offer.departure)} (local time)
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            disabled={busy}
            aria-label="Close"
            className="text-zinc-400 hover:text-zinc-700 text-xl leading-none disabled:opacity-40"
          >
            ×
          </button>
        </div>

        {error && (
          <div
            role="alert"
            className="mx-5 mt-4 bg-red-50 border border-red-200 rounded-lg px-3 py-2 text-sm text-red-700"
          >
            {error}
          </div>
        )}

        {step.kind === "form" && (
          <form onSubmit={handlePrepare} className="p-5 flex flex-col gap-5">
            <p className="text-xs text-zinc-500">
              Passenger details go straight to the booking service. They are not shared with
              the AI agent.
            </p>
            {pax.map((p, i) => (
              <fieldset key={i} className="flex flex-col gap-3">
                <legend className="text-sm font-medium text-zinc-800 mb-2">
                  Passenger {i + 1}
                  {fixedTypes && <> · {PAX_LABEL[p.pax_type]}</>}
                  <span className="block text-xs font-normal text-zinc-500">{PAX_HINT[p.pax_type]}</span>
                </legend>
                <div className="grid grid-cols-2 gap-3">
                  {!fixedTypes && (
                    <label className="flex flex-col gap-1">
                      <span className={label}>Type</span>
                      <select
                        className={input}
                        value={p.pax_type}
                        onChange={(e) => update(i, "pax_type", e.target.value)}
                      >
                        <option value="ADULT">Adult</option>
                        <option value="CHILD">Child</option>
                        <option value="INFANT">Infant</option>
                      </select>
                    </label>
                  )}
                  <label className="flex flex-col gap-1">
                    <span className={label}>Title</span>
                    <select
                      className={input}
                      value={p.title}
                      onChange={(e) => update(i, "title", e.target.value)}
                    >
                      <option value="">—</option>
                      <option value="MR">Mr</option>
                      <option value="MS">Ms</option>
                      <option value="MRS">Mrs</option>
                      <option value="MSTR">Mstr</option>
                      <option value="MISS">Miss</option>
                    </select>
                  </label>
                  <label className="flex flex-col gap-1">
                    <span className={label}>Given name</span>
                    <input
                      className={input}
                      required
                      autoComplete="off"
                      value={p.given_name}
                      onChange={(e) => update(i, "given_name", e.target.value)}
                    />
                  </label>
                  <label className="flex flex-col gap-1">
                    <span className={label}>Family name</span>
                    <input
                      className={input}
                      required
                      autoComplete="off"
                      value={p.family_name}
                      onChange={(e) => update(i, "family_name", e.target.value)}
                    />
                  </label>
                  <label className="flex flex-col gap-1">
                    <span className={label}>Date of birth</span>
                    <input
                      type="date"
                      className={input}
                      required
                      value={p.date_of_birth}
                      onChange={(e) => update(i, "date_of_birth", e.target.value)}
                    />
                  </label>
                  <label className="flex flex-col gap-1">
                    <span className={label}>Passport no. (optional)</span>
                    <input
                      className={input}
                      autoComplete="off"
                      value={p.doc_number}
                      onChange={(e) => update(i, "doc_number", e.target.value)}
                    />
                  </label>
                  {p.doc_number.trim() && (
                    <>
                      <label className="flex flex-col gap-1">
                        <span className={label}>Passport expiry</span>
                        <input
                          type="date"
                          className={input}
                          required
                          value={p.doc_expiry}
                          onChange={(e) => update(i, "doc_expiry", e.target.value)}
                        />
                      </label>
                      <label className="flex flex-col gap-1">
                        <span className={label}>Issuing country</span>
                        <input
                          className={input}
                          required
                          maxLength={2}
                          pattern="[A-Za-z]{2}"
                          value={p.doc_issuing_country}
                          onChange={(e) => update(i, "doc_issuing_country", e.target.value)}
                        />
                      </label>
                      <label className="flex flex-col gap-1">
                        <span className={label}>Nationality</span>
                        <input
                          className={input}
                          required
                          maxLength={2}
                          pattern="[A-Za-z]{2}"
                          value={p.doc_nationality}
                          onChange={(e) => update(i, "doc_nationality", e.target.value)}
                        />
                      </label>
                    </>
                  )}
                </div>
              </fieldset>
            ))}
            <button
              type="submit"
              disabled={busy}
              className="self-end bg-blue-600 text-white text-sm font-medium rounded-lg px-5 py-2 hover:bg-blue-700 disabled:opacity-50"
            >
              {busy ? "Getting live price…" : "Get live price"}
            </button>
          </form>
        )}

        {step.kind === "priced" && (
          <div className="p-5 flex flex-col gap-4">
            <div className="bg-zinc-50 border border-zinc-200 rounded-lg p-4 flex flex-col gap-1">
              <span className="text-xs text-zinc-500 uppercase tracking-wide">Live price</span>
              <span className="text-2xl font-bold text-zinc-900" data-testid="live-price">
                {formatINR(step.prep.customer_price_inr)}
              </span>
              <span className="text-xs text-zinc-500">
                {step.prep.offer_summary.route} · {step.prep.offer_summary.cabin} ·{" "}
                {pax.length} passenger{pax.length !== 1 ? "s" : ""} · incl. taxes
              </span>
              {parseFloat(step.prep.customer_price_inr) !== parseFloat(offer.customer_price_inr) && (
                <span className="text-xs text-amber-700 mt-1">
                  Search showed {formatINR(offer.customer_price_inr)}; the live price above is what
                  will be charged.
                </span>
              )}
            </div>
            <p className={`text-sm ${expired ? "text-red-700" : "text-zinc-600"}`}>
              {expired
                ? "This price has expired."
                : `Confirm within ${Math.floor(secondsLeft / 60)}:${String(secondsLeft % 60).padStart(2, "0")} to book at this price.`}
            </p>
            <div className="flex justify-end gap-2">
              <button
                type="button"
                onClick={startOver}
                disabled={busy}
                className="text-sm px-4 py-2 rounded-lg border border-zinc-300 text-zinc-700 hover:bg-zinc-50 disabled:opacity-50"
              >
                {expired ? "Start again" : "Edit passengers"}
              </button>
              <button
                type="button"
                onClick={() => handleConfirm(step.prep)}
                disabled={busy || expired}
                className="text-sm font-medium px-5 py-2 rounded-lg bg-green-600 text-white hover:bg-green-700 disabled:opacity-50"
              >
                {busy ? "Booking…" : `Confirm and book ${formatINR(step.prep.customer_price_inr)}`}
              </button>
            </div>
          </div>
        )}

        {step.kind === "done" && (
          <div className="p-5 flex flex-col gap-3" data-testid="booking-result">
            <p className="text-lg font-semibold text-green-700">Booking {step.booking.state.toLowerCase()}</p>
            <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
              <dt className="text-zinc-500">PNR</dt>
              <dd className="font-mono font-semibold">{step.booking.pnr ?? "pending"}</dd>
              <dt className="text-zinc-500">Booking ID</dt>
              <dd className="font-mono text-xs break-all">{step.booking.booking_id}</dd>
              <dt className="text-zinc-500">Passengers</dt>
              <dd>{step.booking.passenger_count}</dd>
            </dl>
            <button
              type="button"
              onClick={onClose}
              className="self-end text-sm px-4 py-2 rounded-lg border border-zinc-300 text-zinc-700 hover:bg-zinc-50"
            >
              Done
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
