"use client";

import { useEffect, useId, useRef, useState, KeyboardEvent } from "react";
import { apiFetch } from "@/lib/api";

interface Airport {
  iata: string;
  name: string;
  city: string;
  country: string;
}

interface Props {
  label: string;
  value: string;
  onChange: (iata: string) => void;
  placeholder: string;
  /** "form": full-width field with a sentence-case label (the search form). */
  variant?: "compact" | "form";
}

/**
 * IATA code input with suggestions from /v1/airports (code, city or airport name).
 * The field always holds a 3-letter code; picking a suggestion fills it.
 */
export default function AirportInput({ label, value, onChange, placeholder, variant = "compact" }: Props) {
  const form = variant === "form";
  const [text, setText] = useState(value);
  const [options, setOptions] = useState<Airport[]>([]);
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const listId = useId();
  const blurTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  // Follow outside changes to the code, but never wipe what the user is typing
  // (typing a city name sets the code to "" until a suggestion is picked).
  const [seenValue, setSeenValue] = useState(value);
  if (value !== seenValue) {
    setSeenValue(value);
    if (value && text.trim().toUpperCase() !== value) setText(value);
  }

  useEffect(() => {
    const q = text.trim();
    if (!open || q.length < 2) return;
    const controller = new AbortController();
    const timer = setTimeout(async () => {
      try {
        const res = await apiFetch(`/v1/airports?q=${encodeURIComponent(q)}&limit=8`, {
          signal: controller.signal,
        });
        if (res.ok) {
          setOptions((await res.json()) as Airport[]);
          setActive(0);
        }
      } catch {
        /* aborted or offline: keep the field usable as a plain code input */
      }
    }, 150);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [text, open]);

  function pick(a: Airport) {
    setText(a.iata);
    onChange(a.iata);
    setOpen(false);
  }

  function handleChange(raw: string) {
    setText(raw);
    setOpen(true);
    const code = raw.trim().toUpperCase();
    // Typing a bare code still works without picking a suggestion.
    onChange(/^[A-Z]{3}$/.test(code) ? code : "");
  }

  function handleKeyDown(e: KeyboardEvent<HTMLInputElement>) {
    if (!open || options.length === 0) return;
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActive((i) => (i + 1) % options.length);
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActive((i) => (i - 1 + options.length) % options.length);
    } else if (e.key === "Enter") {
      e.preventDefault();
      pick(options[active]);
    } else if (e.key === "Escape") {
      setOpen(false);
    }
  }

  const showList = open && text.trim().length >= 2 && options.length > 0;

  return (
    <div className="flex flex-col gap-1 relative min-w-0">
      <label
        className={
          form
            ? "text-sm font-medium text-zinc-700"
            : "text-xs font-medium text-zinc-500 uppercase tracking-wide"
        }
      >
        {label}
        <input
          type="text"
          role="combobox"
          aria-expanded={showList}
          aria-controls={listId}
          aria-autocomplete="list"
          aria-activedescendant={showList ? `${listId}-${active}` : undefined}
          value={text}
          placeholder={placeholder}
          onChange={(e) => handleChange(e.target.value)}
          onKeyDown={handleKeyDown}
          onFocus={() => setOpen(true)}
          onBlur={() => {
            blurTimer.current = setTimeout(() => {
              setOpen(false);
              const code = text.trim().toUpperCase();
              if (/^[A-Z]{3}$/.test(code)) setText(code);
            }, 150);
          }}
          required
          autoComplete="off"
          className={
            form
              ? "mt-1 block w-full h-11 border border-zinc-300 rounded-lg px-3 text-sm font-normal text-zinc-900 placeholder:text-zinc-400 focus:outline-none focus:ring-2 focus:ring-zinc-900"
              : "mt-1 block border border-zinc-300 rounded-lg px-3 py-2 text-sm font-mono w-36 normal-case font-normal text-zinc-900 focus:outline-none focus:ring-2 focus:ring-blue-500"
          }
        />
      </label>
      {showList && (
        <ul
          id={listId}
          role="listbox"
          className="absolute top-full left-0 z-20 mt-1 w-72 max-w-[calc(100vw-2rem)] max-h-72 overflow-auto bg-white border border-zinc-200 rounded-lg shadow-lg py-1"
          onMouseDown={() => clearTimeout(blurTimer.current)}
        >
          {options.map((a, i) => (
            <li
              key={a.iata}
              id={`${listId}-${i}`}
              role="option"
              aria-selected={i === active}
              onMouseDown={(e) => {
                e.preventDefault();
                pick(a);
              }}
              className={`px-3 py-1.5 text-sm cursor-pointer ${i === active ? "bg-blue-50" : ""}`}
            >
              <span className="font-mono font-semibold">{a.iata}</span>{" "}
              <span className="text-zinc-700">{a.city}</span>
              <span className="block text-xs text-zinc-400 truncate">
                {a.name} · {a.country}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
