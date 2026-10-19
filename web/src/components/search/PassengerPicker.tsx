"use client";

import { useEffect, useId, useRef, useState } from "react";
import { MAX_PAX, passengerSummary, type Passengers } from "@/lib/searchQuery";

interface Props {
  value: Passengers;
  onChange: (p: Passengers) => void;
}

const ROWS: { key: keyof Passengers; label: string; hint: string }[] = [
  { key: "adults", label: "Adults", hint: "12 years and over" },
  { key: "children", label: "Children", hint: "2 to 11 years" },
  { key: "infants", label: "Infants", hint: "Under 2, on an adult's lap" },
];

/** Limits: at least 1 adult, at most 9 passengers, no more infants than adults. */
function limits(p: Passengers, key: keyof Passengers): { min: number; max: number } {
  const room = MAX_PAX - (p.adults + p.children + p.infants);
  if (key === "adults") return { min: Math.max(1, p.infants), max: p.adults + room };
  if (key === "infants") return { min: 0, max: Math.min(p.adults, p.infants + room) };
  return { min: 0, max: p.children + room };
}

export const Chevron = () => (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.75}
    strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false">
    <path d="m6 9 6 6 6-6" />
  </svg>
);

export default function PassengerPicker({ value, onChange }: Props) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const panelId = useId();

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: globalThis.KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  function step(key: keyof Passengers, delta: number) {
    const { min, max } = limits(value, key);
    const n = Math.min(max, Math.max(min, value[key] + delta));
    onChange({ ...value, [key]: n });
  }

  return (
    <div ref={rootRef} className="relative flex flex-col gap-1 min-w-0">
      <span className="text-sm font-medium text-zinc-700">Passengers</span>
      <button
        type="button"
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-controls={panelId}
        onClick={() => setOpen((o) => !o)}
        className="flex items-center justify-between gap-2 h-11 rounded-lg border border-zinc-300 bg-white px-3 text-sm text-zinc-900 text-left focus:outline-none focus:ring-2 focus:ring-zinc-900"
      >
        <span className="truncate">{passengerSummary(value)}</span>
        <Chevron />
      </button>
      {open && (
        <div
          id={panelId}
          role="dialog"
          aria-label="Passengers"
          className="absolute top-full left-0 z-30 mt-1 w-72 max-w-[calc(100vw-2rem)] rounded-xl border border-zinc-200 bg-white p-4 shadow-lg"
        >
          <ul className="flex flex-col gap-3">
            {ROWS.map(({ key, label, hint }) => {
              const { min, max } = limits(value, key);
              return (
                <li key={key} className="flex items-center justify-between gap-3">
                  <div>
                    <p className="text-sm font-medium text-zinc-900">{label}</p>
                    <p className="text-xs text-zinc-500">{hint}</p>
                  </div>
                  <div className="flex items-center gap-2">
                    <button type="button" aria-label={`Fewer ${label.toLowerCase()}`}
                      disabled={value[key] <= min} onClick={() => step(key, -1)}
                      className="size-8 rounded-full border border-zinc-300 text-zinc-700 hover:bg-zinc-100 disabled:opacity-30 disabled:cursor-not-allowed">
                      −
                    </button>
                    <output aria-label={label} className="w-5 text-center text-sm tabular-nums">{value[key]}</output>
                    <button type="button" aria-label={`More ${label.toLowerCase()}`}
                      disabled={value[key] >= max} onClick={() => step(key, 1)}
                      className="size-8 rounded-full border border-zinc-300 text-zinc-700 hover:bg-zinc-100 disabled:opacity-30 disabled:cursor-not-allowed">
                      +
                    </button>
                  </div>
                </li>
              );
            })}
          </ul>
          <p className="mt-3 text-xs text-zinc-500">Up to {MAX_PAX} passengers. Each infant needs an adult.</p>
          <button type="button" onClick={() => setOpen(false)}
            className="mt-3 w-full rounded-lg bg-zinc-900 py-2 text-sm font-medium text-white hover:bg-zinc-700">
            Done
          </button>
        </div>
      )}
    </div>
  );
}
