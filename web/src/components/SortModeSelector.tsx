import type { SortMode } from "@/types/sse";

interface Props {
  value: SortMode;
  onChange: (mode: SortMode) => void;
}

const MODES: { value: SortMode; label: string }[] = [
  { value: "cheapest", label: "Cheapest" },
  { value: "fastest", label: "Fastest" },
  { value: "best_value", label: "Best value" },
];

export default function SortModeSelector({ value, onChange }: Props) {
  return (
    <div className="flex rounded-lg border border-zinc-200 overflow-hidden w-fit" role="group" aria-label="Sort results">
      {MODES.map((mode) => (
        <button
          key={mode.value}
          onClick={() => onChange(mode.value)}
          className={`px-4 py-1.5 text-sm font-medium transition-colors border-r last:border-r-0 border-zinc-200 ${
            value === mode.value
              ? "bg-blue-600 text-white"
              : "bg-white text-zinc-600 hover:bg-zinc-50"
          }`}
        >
          {mode.label}
        </button>
      ))}
    </div>
  );
}
