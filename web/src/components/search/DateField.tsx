"use client";

import { useId, useRef, useState, KeyboardEvent } from "react";
import { isoFromParts } from "@/lib/searchQuery";

interface Props {
  label: string;
  /** YYYY-MM-DD, or null while the typed date is incomplete or invalid. */
  value: string | null;
  onChange: (iso: string | null) => void;
  min?: string;
  invalid?: boolean;
}

type Parts = { d: string; m: string; y: string };

function partsOf(iso: string): Parts {
  const [y, m, d] = iso.split("-");
  return { d, m, y };
}

const CalendarIcon = () => (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.75}
    strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false">
    <rect x="3" y="5" width="18" height="16" rx="2" />
    <path d="M16 3v4M8 3v4M3 10h18" />
  </svg>
);

/**
 * DD / MM / YYYY date entry: three digit boxes that advance as you type, plus a calendar
 * button (the browser's own date picker). Reports a date only when it is a real one.
 */
export default function DateField({ label, value, onChange, min, invalid }: Props) {
  const [parts, setParts] = useState<Parts>(value ? partsOf(value) : { d: "", m: "", y: "" });
  // Follow outside changes (calendar, defaults) without wiping a half-typed date.
  const [seen, setSeen] = useState(value);
  if (value !== seen) {
    setSeen(value);
    if (value) setParts(partsOf(value));
  }
  const dayRef = useRef<HTMLInputElement>(null);
  const monthRef = useRef<HTMLInputElement>(null);
  const yearRef = useRef<HTMLInputElement>(null);
  const pickerRef = useRef<HTMLInputElement>(null);
  const labelId = useId();

  function update(next: Parts) {
    setParts(next);
    const iso = isoFromParts(next.d, next.m, next.y);
    setSeen(iso);
    onChange(iso);
  }

  function segment(
    key: keyof Parts,
    raw: string,
    max: number,
    next?: React.RefObject<HTMLInputElement | null>,
  ) {
    const digits = raw.replace(/\D/g, "").slice(0, max);
    update({ ...parts, [key]: digits });
    if (digits.length === max) next?.current?.focus();
  }

  function backspaceTo(
    e: KeyboardEvent<HTMLInputElement>,
    prev: React.RefObject<HTMLInputElement | null>,
  ) {
    if (e.key === "Backspace" && e.currentTarget.value === "") prev.current?.focus();
  }

  function openPicker() {
    const el = pickerRef.current;
    if (!el) return;
    try {
      el.showPicker();
    } catch {
      el.focus(); // older browsers: focus the native field instead
    }
  }

  const box =
    "w-7 bg-transparent text-center text-sm text-zinc-900 placeholder:text-zinc-400 focus:outline-none tabular-nums";

  return (
    <div className="flex flex-col gap-1 min-w-0">
      <span id={labelId} className="text-sm font-medium text-zinc-700">
        {label}
      </span>
      <div
        role="group"
        aria-labelledby={labelId}
        className={`flex items-center h-11 rounded-lg border bg-white px-3 focus-within:ring-2 focus-within:ring-zinc-900 ${
          invalid ? "border-red-500" : "border-zinc-300"
        }`}
      >
        <input ref={dayRef} aria-label={`${label}: day`} inputMode="numeric" autoComplete="off"
          placeholder="DD" value={parts.d} className={box}
          onChange={(e) => segment("d", e.target.value, 2, monthRef)} />
        <span className="text-zinc-400 px-0.5" aria-hidden="true">/</span>
        <input ref={monthRef} aria-label={`${label}: month`} inputMode="numeric" autoComplete="off"
          placeholder="MM" value={parts.m} className={box}
          onChange={(e) => segment("m", e.target.value, 2, yearRef)}
          onKeyDown={(e) => backspaceTo(e, dayRef)} />
        <span className="text-zinc-400 px-0.5" aria-hidden="true">/</span>
        <input ref={yearRef} aria-label={`${label}: year`} inputMode="numeric" autoComplete="off"
          placeholder="YYYY" value={parts.y} className={`${box} w-11`}
          onChange={(e) => segment("y", e.target.value, 4)}
          onKeyDown={(e) => backspaceTo(e, monthRef)} />
        <button type="button" onClick={openPicker} aria-label={`Choose ${label.toLowerCase()} from a calendar`}
          className="ml-auto -mr-1 p-1.5 rounded text-zinc-500 hover:text-zinc-900 hover:bg-zinc-100">
          <CalendarIcon />
        </button>
        <input
          ref={pickerRef}
          type="date"
          tabIndex={-1}
          aria-hidden="true"
          min={min}
          value={value ?? ""}
          onChange={(e) => e.target.value && update(partsOf(e.target.value))}
          className="sr-only"
        />
      </div>
    </div>
  );
}
