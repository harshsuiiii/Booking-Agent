"use client";

import { useId, useState, useSyncExternalStore, FormEvent } from "react";
import AirportInput from "@/components/AirportInput";
import DateField from "@/components/search/DateField";
import PassengerPicker, { Chevron } from "@/components/search/PassengerPicker";
import {
  CABINS,
  DEFAULT_FILTERS,
  MAX_LEGS,
  TIME_WINDOWS,
  addDays,
  toLocalIsoDate,
  type AdvancedFilters,
  type JourneyType,
  type Passengers,
  type SearchLeg,
  type SearchParams,
  type TimeWindow,
} from "@/lib/searchQuery";

interface Props {
  onSearch: (params: SearchParams) => void;
  isLoading: boolean;
  /** Prefill, e.g. from the URL or a "recent search" link. */
  initial?: Partial<SearchParams>;
}

/** undefined = not touched (use the default date), null = typed but not a real date yet. */
type DateState = string | null | undefined;

interface LegDraft {
  origin: string;
  destination: string;
  date: DateState;
  window: TimeWindow;
}

const JOURNEYS: { value: JourneyType; label: string }[] = [
  { value: "one_way", label: "One way" },
  { value: "return", label: "Return" },
  { value: "multi_city", label: "Multi-city" },
];

const noSubscribe = () => () => {};
const clientToday = () => toLocalIsoDate(new Date());

const draftOf = (l: SearchLeg): LegDraft => ({
  origin: l.origin,
  destination: l.destination,
  date: l.departure_date || undefined,
  window: l.time_window,
});

function initialTrip(initial?: Partial<SearchParams>): { out: LegDraft; back: LegDraft } {
  const [a, b] = initial?.legs ?? [];
  const out = a ? draftOf(a) : { origin: "DEL", destination: "BOM", date: undefined, window: "any" as const };
  const isReturn = initial?.journey_type === "return" && b;
  return {
    out,
    back: isReturn ? draftOf(b) : { origin: out.destination, destination: out.origin, date: undefined, window: "any" },
  };
}

function initialMulti(initial: Partial<SearchParams> | undefined, out: LegDraft): LegDraft[] {
  if (initial?.journey_type === "multi_city" && initial.legs && initial.legs.length >= 2) {
    return initial.legs.map(draftOf);
  }
  return [out, { origin: out.destination, destination: "", date: undefined, window: "any" }];
}

const selectClass =
  "h-11 w-full appearance-none rounded-lg border border-zinc-300 bg-white pl-3 pr-9 text-sm text-zinc-900 focus:outline-none focus:ring-2 focus:ring-zinc-900";

function Select({ label, value, onChange, options, hideLabel }: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  options: readonly { value: string; label: string }[];
  hideLabel?: boolean;
}) {
  const id = useId();
  return (
    <div className="flex flex-col gap-1 min-w-0">
      <label htmlFor={id} className={hideLabel ? "sr-only" : "text-sm font-medium text-zinc-700"}>
        {label}
      </label>
      <div className="relative">
        <select id={id} value={value} onChange={(e) => onChange(e.target.value)} className={selectClass}>
          {options.map((o) => (
            <option key={o.value} value={o.value}>{o.label}</option>
          ))}
        </select>
        <span className="pointer-events-none absolute right-3 top-1/2 -translate-y-1/2 text-zinc-500">
          <Chevron />
        </span>
      </div>
    </div>
  );
}

function DateWithTime({ label, timeLabel, date, onDate, window, onWindow, min, invalid }: {
  label: string;
  timeLabel: string;
  date: string | null;
  onDate: (d: string | null) => void;
  window: TimeWindow;
  onWindow: (w: TimeWindow) => void;
  min?: string;
  invalid?: boolean;
}) {
  return (
    <div className="flex flex-col gap-2 min-w-0">
      <DateField label={label} value={date} onChange={onDate} min={min} invalid={invalid} />
      <Select label={timeLabel} hideLabel value={window} onChange={(v) => onWindow(v as TimeWindow)} options={TIME_WINDOWS} />
    </div>
  );
}

const SwapIcon = () => (
  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.75}
    strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false">
    <path d="M7 4 3 8l4 4M3 8h14M17 20l4-4-4-4M21 16H7" />
  </svg>
);

function parseAirlines(text: string): { codes: string[]; bad: string[] } {
  const tokens = text.split(/[\s,]+/).map((t) => t.trim().toUpperCase()).filter(Boolean);
  return {
    codes: [...new Set(tokens.filter((t) => /^[A-Z0-9]{2}$/.test(t)))],
    bad: tokens.filter((t) => !/^[A-Z0-9]{2}$/.test(t)),
  };
}

export default function SearchForm({ onSearch, isLoading, initial }: Props) {
  const [journey, setJourney] = useState<JourneyType>(initial?.journey_type ?? "return");
  const [trip, setTrip] = useState(() => initialTrip(initial));
  const [multi, setMulti] = useState<LegDraft[]>(() => initialMulti(initial, trip.out));
  const [passengers, setPassengers] = useState<Passengers>(
    initial?.passengers ?? { adults: 1, children: 0, infants: 0 },
  );
  const [cabin, setCabin] = useState(initial?.cabin ?? "ECONOMY");
  const [filters, setFilters] = useState<AdvancedFilters>(initial?.filters ?? DEFAULT_FILTERS);
  const [airlinesText, setAirlinesText] = useState(filters.preferred_airlines.join(", "));
  const [showAdvanced, setShowAdvanced] = useState(Boolean(initial?.filters));
  const [errors, setErrors] = useState<string[]>([]);
  const [tried, setTried] = useState(false);
  const advancedId = useId();

  // "Today" is read on the client only: the server's date can differ from the browser's.
  const today = useSyncExternalStore(noSubscribe, clientToday, () => "");
  const min = today || undefined;

  // Effective dates: an untouched date defaults to a week out (return: a week after that).
  const resolve = (d: DateState, fallback: string | null): string | null => (d === undefined ? fallback : d);
  const outDate = resolve(trip.out.date, today ? addDays(today, 7) : null);
  const backDate = resolve(trip.back.date, outDate ? addDays(outDate, 7) : null);
  // Multi-city: an untouched date defaults to 3 days after the last real date before it.
  const multiDates: (string | null)[] = [];
  let lastDate: string | null = today ? addDays(today, 4) : null;
  for (const leg of multi) {
    const d = resolve(leg.date, lastDate ? addDays(lastDate, 3) : null);
    multiDates.push(d);
    if (d) lastDate = d;
  }

  // Carry the route across when switching between multi-city and one way/return.
  function changeJourney(next: JourneyType) {
    if (next === "multi_city" && journey !== "multi_city") {
      const out = { ...trip.out, date: outDate ?? trip.out.date };
      setMulti((m) => [out, { ...m[1], origin: out.destination }, ...m.slice(2)]);
    } else if (next !== "multi_city" && journey === "multi_city") {
      const first = multi[0];
      setTrip((t) => ({ ...t, out: { ...first, date: multiDates[0] ?? first.date } }));
    }
    setJourney(next);
  }

  function setOut(patch: Partial<LegDraft>) {
    setTrip((t) => ({ ...t, out: { ...t.out, ...patch } }));
  }
  function setBack(patch: Partial<LegDraft>) {
    setTrip((t) => ({ ...t, back: { ...t.back, ...patch } }));
  }
  function setLeg(i: number, patch: Partial<LegDraft>) {
    setMulti((legs) => legs.map((l, j) => (j === i ? { ...l, ...patch } : l)));
  }
  function swap() {
    setTrip((t) => ({ ...t, out: { ...t.out, origin: t.out.destination, destination: t.out.origin } }));
  }
  function addLeg() {
    setMulti((legs) => [...legs, { origin: legs[legs.length - 1]?.destination ?? "", destination: "", date: undefined, window: "any" }]);
  }
  function removeLeg(i: number) {
    setMulti((legs) => legs.filter((_, j) => j !== i));
  }
  function setFilter<K extends keyof AdvancedFilters>(key: K, value: AdvancedFilters[K]) {
    setFilters((f) => ({ ...f, [key]: value }));
  }

  function buildLegs(): SearchLeg[] {
    if (journey === "multi_city") {
      return multi.map((l, i) => ({
        origin: l.origin,
        destination: l.destination,
        departure_date: multiDates[i] ?? "",
        time_window: l.window,
      }));
    }
    const out = { origin: trip.out.origin, destination: trip.out.destination, departure_date: outDate ?? "", time_window: trip.out.window };
    if (journey === "one_way") return [out];
    return [out, { origin: out.destination, destination: out.origin, departure_date: backDate ?? "", time_window: trip.back.window }];
  }

  function validate(legs: SearchLeg[], badAirlines: string[]): string[] {
    const problems: string[] = [];
    const name = (i: number) =>
      journey === "multi_city" ? `Flight ${i + 1}` : i === 0 ? "Departure" : "Return";
    legs.forEach((l, i) => {
      if (journey === "return" && i === 1) {
        if (!l.departure_date) problems.push("Return date: enter a real date (DD / MM / YYYY).");
      } else {
        if (!l.origin) problems.push(`${name(i)}: choose an origin airport.`);
        if (!l.destination) problems.push(`${name(i)}: choose a destination airport.`);
        if (l.origin && l.origin === l.destination) problems.push(`${name(i)}: origin and destination are the same.`);
        if (!l.departure_date) problems.push(`${name(i)} date: enter a real date (DD / MM / YYYY).`);
      }
      if (l.departure_date && today && l.departure_date < today) problems.push(`${name(i)} date is in the past.`);
      const prev = legs[i - 1];
      if (prev?.departure_date && l.departure_date && l.departure_date < prev.departure_date) {
        problems.push(`${name(i)} date is before the previous flight.`);
      }
    });
    if (badAirlines.length) problems.push(`Airlines: use 2-character codes, e.g. AI, 6E (not ${badAirlines.join(", ")}).`);
    return problems;
  }

  function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setTried(true);
    const legs = buildLegs();
    const { codes, bad } = parseAirlines(airlinesText);
    const problems = validate(legs, bad);
    setErrors(problems);
    if (problems.length) return;
    onSearch({ journey_type: journey, legs, passengers, cabin, filters: { ...filters, preferred_airlines: codes } });
  }

  const activeFilters =
    (filters.max_stops !== null ? 1 : 0) +
    (parseAirlines(airlinesText).codes.length ? 1 : 0) +
    [filters.bags_included, filters.refundable_only, filters.no_overnight_layover].filter(Boolean).length;
  const invalidDate = (d: string | null) => tried && !d;

  return (
    <form
      onSubmit={handleSubmit}
      noValidate
      aria-label="Flight search"
      className="bg-white rounded-xl shadow-sm border border-zinc-200 p-4 sm:p-6 flex flex-col gap-5"
    >
      <fieldset className="flex flex-col gap-2">
        <legend className="text-sm font-medium text-zinc-700 mb-2">Journey type</legend>
        <div className="flex flex-wrap gap-x-6 gap-y-2">
          {JOURNEYS.map((j) => (
            <label key={j.value} className="inline-flex items-center gap-2 text-sm text-zinc-900 cursor-pointer">
              <input
                type="radio"
                name="journey"
                value={j.value}
                checked={journey === j.value}
                onChange={() => changeJourney(j.value)}
                className="size-4 accent-zinc-900"
              />
              {j.label}
            </label>
          ))}
        </div>
      </fieldset>

      {journey !== "multi_city" ? (
        <>
          <div className="grid grid-cols-1 sm:grid-cols-[1fr_auto_1fr] gap-3 sm:items-end">
            <AirportInput variant="form" label="Origin" value={trip.out.origin} onChange={(v) => setOut({ origin: v })} placeholder="City or airport" />
            <button
              type="button"
              onClick={swap}
              aria-label="Swap origin and destination"
              className="justify-self-center sm:mb-0.5 size-10 grid place-items-center rounded-full border border-zinc-300 text-zinc-600 hover:bg-zinc-100 rotate-90 sm:rotate-0"
            >
              <SwapIcon />
            </button>
            <AirportInput variant="form" label="Destination" value={trip.out.destination} onChange={(v) => setOut({ destination: v })} placeholder="City or airport" />
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <DateWithTime
              label="Departure date"
              timeLabel="Departure time"
              date={outDate}
              onDate={(d) => setOut({ date: d })}
              window={trip.out.window}
              onWindow={(w) => setOut({ window: w })}
              min={min}
              invalid={invalidDate(outDate)}
            />
            {journey === "return" && (
              <DateWithTime
                label="Return date"
                timeLabel="Return time"
                date={backDate}
                onDate={(d) => setBack({ date: d })}
                window={trip.back.window}
                onWindow={(w) => setBack({ window: w })}
                min={outDate ?? min}
                invalid={invalidDate(backDate)}
              />
            )}
          </div>
        </>
      ) : (
        <ol className="flex flex-col gap-4" aria-label="Flights">
          {multi.map((leg, i) => (
            <li key={i} className="rounded-lg border border-zinc-200 p-3 sm:p-4" data-testid="multi-city-leg">
              <div className="flex items-center justify-between mb-3">
                <p className="text-sm font-semibold text-zinc-900">Flight {i + 1}</p>
                {multi.length > 2 && (
                  <button type="button" onClick={() => removeLeg(i)} className="text-sm text-zinc-500 hover:text-red-600 underline underline-offset-2">
                    Remove<span className="sr-only"> flight {i + 1}</span>
                  </button>
                )}
              </div>
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                <AirportInput variant="form" label={`Origin (flight ${i + 1})`} value={leg.origin} onChange={(v) => setLeg(i, { origin: v })} placeholder="City or airport" />
                <AirportInput variant="form" label={`Destination (flight ${i + 1})`} value={leg.destination} onChange={(v) => setLeg(i, { destination: v })} placeholder="City or airport" />
                <DateWithTime
                  label={`Date (flight ${i + 1})`}
                  timeLabel={`Time (flight ${i + 1})`}
                  date={multiDates[i]}
                  onDate={(d) => setLeg(i, { date: d })}
                  window={leg.window}
                  onWindow={(w) => setLeg(i, { window: w })}
                  min={(i > 0 ? multiDates[i - 1] : null) ?? min}
                  invalid={invalidDate(multiDates[i])}
                />
              </div>
            </li>
          ))}
          {multi.length < MAX_LEGS && (
            <li>
              <button type="button" onClick={addLeg} className="text-sm font-medium text-zinc-900 underline underline-offset-4 hover:text-zinc-600">
                + Add another flight
              </button>
            </li>
          )}
        </ol>
      )}

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
        <PassengerPicker value={passengers} onChange={setPassengers} />
        <Select label="Class" value={cabin} onChange={setCabin} options={CABINS} />
      </div>

      <div className="flex flex-col gap-4">
        <button
          type="button"
          aria-expanded={showAdvanced}
          aria-controls={advancedId}
          onClick={() => setShowAdvanced((s) => !s)}
          className="self-start inline-flex items-center gap-1 text-sm font-medium text-zinc-900 underline underline-offset-4 hover:text-zinc-600"
        >
          Advanced options{activeFilters ? ` (${activeFilters})` : ""}
          <span className={`inline-flex transition-transform ${showAdvanced ? "rotate-180" : ""}`}><Chevron /></span>
        </button>

        {showAdvanced && (
          <div id={advancedId} className="grid grid-cols-1 sm:grid-cols-2 gap-4 rounded-lg bg-zinc-50 border border-zinc-200 p-4">
            <Select
              label="Stops (each flight)"
              value={filters.max_stops === null ? "any" : String(filters.max_stops)}
              onChange={(v) => setFilter("max_stops", v === "any" ? null : Number(v))}
              options={[
                { value: "any", label: "Any number of stops" },
                { value: "0", label: "Non-stop only" },
                { value: "1", label: "Up to 1 stop" },
              ]}
            />
            <div className="flex flex-col gap-1 min-w-0">
              <label htmlFor={`${advancedId}-air`} className="text-sm font-medium text-zinc-700">Preferred airlines</label>
              <input
                id={`${advancedId}-air`}
                value={airlinesText}
                onChange={(e) => setAirlinesText(e.target.value)}
                placeholder="e.g. AI, 6E, UK"
                autoComplete="off"
                className="h-11 rounded-lg border border-zinc-300 bg-white px-3 text-sm text-zinc-900 placeholder:text-zinc-400 focus:outline-none focus:ring-2 focus:ring-zinc-900"
              />
            </div>
            <div className="sm:col-span-2 flex flex-col sm:flex-row sm:flex-wrap gap-x-6 gap-y-2">
              {(
                [
                  ["bags_included", "Checked bag included"],
                  ["refundable_only", "Refundable fares only"],
                  ["no_overnight_layover", "No overnight layovers"],
                ] as const
              ).map(([key, label]) => (
                <label key={key} className="inline-flex items-center gap-2 text-sm text-zinc-900 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={filters[key]}
                    onChange={(e) => setFilter(key, e.target.checked)}
                    className="size-4 accent-zinc-900"
                  />
                  {label}
                </label>
              ))}
            </div>
          </div>
        )}
      </div>

      {errors.length > 0 && (
        <ul role="alert" className="rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700 list-disc pl-8">
          {errors.map((e) => <li key={e}>{e}</li>)}
        </ul>
      )}

      <button
        type="submit"
        disabled={isLoading}
        className="w-full sm:w-auto sm:self-start h-12 px-8 rounded-lg bg-zinc-900 text-white text-sm font-semibold hover:bg-zinc-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
      >
        {isLoading ? "Searching…" : "Find available flights"}
      </button>
    </form>
  );
}
