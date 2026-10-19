"use client";

/**
 * Small, dependency-free charts for the dashboard. One series each, so one hue
 * (--series-1) and no legend: the card title names the measure. Bars ≤ 24px with a 4px
 * rounded data end, recessive grid, tooltip on hover/focus, and a table view.
 */
import { useId, useState, type KeyboardEvent } from "react";

const SERIES = "#2a78d6"; // dataviz reference palette, categorical slot 1 (light)

export interface Point {
  label: string; // axis/tooltip label
  value: number;
  detail?: string; // extra tooltip line
}

function niceMax(v: number): number {
  if (v <= 0) return 1;
  const mag = 10 ** Math.floor(Math.log10(v));
  const step = [1, 2, 2.5, 5, 10].find((s) => s * mag >= v) ?? 10;
  return step * mag;
}

function ViewToggle({ showTable, onToggle, id }: { showTable: boolean; onToggle: () => void; id: string }) {
  return (
    <button
      type="button"
      onClick={onToggle}
      aria-controls={id}
      className="rounded-md px-2 py-1 text-xs font-medium text-zinc-500 hover:bg-zinc-100 hover:text-zinc-900 focus-visible:outline-2 focus-visible:outline-blue-600"
    >
      {showTable ? "Show chart" : "Show table"}
    </button>
  );
}

export function ColumnChart({
  data,
  label,
  format = (v) => String(v),
  height = 180,
}: {
  data: Point[];
  label: string;
  format?: (v: number) => string;
  height?: number;
}) {
  const [active, setActive] = useState<number | null>(null);
  const [showTable, setShowTable] = useState(false);
  const id = useId();
  const W = 640;
  const H = height;
  const pad = { top: 8, right: 4, bottom: 22, left: 34 };
  const innerW = W - pad.left - pad.right;
  const innerH = H - pad.top - pad.bottom;
  const max = niceMax(Math.max(0, ...data.map((d) => d.value)));
  const band = innerW / Math.max(data.length, 1);
  const barW = Math.min(24, band * 0.7);
  const y = (v: number) => pad.top + innerH - (v / max) * innerH;
  const ticks = [0, max / 2, max];
  const xLabels = new Set([0, Math.floor((data.length - 1) / 2), data.length - 1]);

  function onKey(e: KeyboardEvent<SVGSVGElement>) {
    if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
      e.preventDefault();
      const step = e.key === "ArrowRight" ? 1 : -1;
      setActive((i) => Math.min(data.length - 1, Math.max(0, (i ?? (step > 0 ? -1 : data.length)) + step)));
    } else if (e.key === "Escape") {
      setActive(null);
    }
  }

  const a = active !== null ? data[active] : null;

  return (
    <div className="relative">
      <div className="mb-1 flex justify-end">
        <ViewToggle showTable={showTable} onToggle={() => setShowTable((s) => !s)} id={id} />
      </div>
      {showTable ? (
        <div id={id} className="max-h-64 overflow-auto">
          <table className="w-full text-sm">
            <caption className="sr-only">{label}</caption>
            <thead>
              <tr className="text-left text-xs text-zinc-500">
                <th className="py-1 font-medium">Date</th>
                <th className="py-1 text-right font-medium">Value</th>
              </tr>
            </thead>
            <tbody>
              {data.map((d) => (
                <tr key={d.label} className="border-t border-zinc-100">
                  <td className="py-1 text-zinc-700">{d.label}</td>
                  <td className="py-1 text-right tabular-nums text-zinc-900">
                    {format(d.value)}
                    {d.detail && <span className="ml-2 text-zinc-500">{d.detail}</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <svg
          id={id}
          viewBox={`0 0 ${W} ${H}`}
          className="h-auto w-full touch-none rounded focus-visible:outline-2 focus-visible:outline-blue-600"
          role="img"
          aria-label={`${label}. Use left and right arrow keys to read values, or show the table.`}
          tabIndex={0}
          onKeyDown={onKey}
          onMouseLeave={() => setActive(null)}
          onBlur={() => setActive(null)}
        >
          {ticks.map((t) => (
            <g key={t}>
              <line x1={pad.left} x2={W - pad.right} y1={y(t)} y2={y(t)} stroke="#e7e5e4" strokeWidth={1} />
              <text x={pad.left - 6} y={y(t) + 4} textAnchor="end" fontSize={11} fill="#78716c">
                {format(t)}
              </text>
            </g>
          ))}
          {data.map((d, i) => {
            const x = pad.left + i * band + (band - barW) / 2;
            const top = y(d.value);
            const h = pad.top + innerH - top;
            const r = Math.min(4, h, barW / 2);
            const path =
              h <= 0
                ? ""
                : `M${x},${top + h} V${top + r} Q${x},${top} ${x + r},${top} H${x + barW - r} Q${x + barW},${top} ${x + barW},${top + r} V${top + h} Z`;
            return (
              <g key={d.label}>
                {path && <path d={path} fill={SERIES} opacity={active === null || active === i ? 1 : 0.45} />}
                <rect
                  x={pad.left + i * band}
                  y={pad.top}
                  width={band}
                  height={innerH}
                  fill="transparent"
                  onMouseEnter={() => setActive(i)}
                />
                {xLabels.has(i) && (
                  <text
                    x={pad.left + i * band + band / 2}
                    y={H - 6}
                    textAnchor={i === 0 ? "start" : i === data.length - 1 ? "end" : "middle"}
                    fontSize={11}
                    fill="#78716c"
                  >
                    {d.label}
                  </text>
                )}
              </g>
            );
          })}
        </svg>
      )}
      {!showTable && a && active !== null && (
        <div
          role="status"
          className="pointer-events-none absolute top-6 rounded-md border border-zinc-200 bg-white px-2.5 py-1.5 text-xs shadow-md"
          style={{
            left: `${((pad.left + active * band + band / 2) / W) * 100}%`,
            transform: `translateX(${active > data.length / 2 ? "-105%" : "5%"})`,
          }}
        >
          <p className="font-medium text-zinc-900">{a.label}</p>
          <p className="tabular-nums text-zinc-700">{format(a.value)}</p>
          {a.detail && <p className="text-zinc-500">{a.detail}</p>}
        </div>
      )}
    </div>
  );
}

/** Horizontal bars with direct value labels (few categories, e.g. suppliers). */
export function BarList({
  data,
  label,
  format = (v) => String(v),
}: {
  data: Point[];
  label: string;
  format?: (v: number) => string;
}) {
  const [showTable, setShowTable] = useState(false);
  const id = useId();
  const max = Math.max(0, ...data.map((d) => d.value)) || 1;
  return (
    <div>
      <div className="mb-1 flex justify-end">
        <ViewToggle showTable={showTable} onToggle={() => setShowTable((s) => !s)} id={id} />
      </div>
      {showTable ? (
        <table id={id} className="w-full text-sm">
          <caption className="sr-only">{label}</caption>
          <tbody>
            {data.map((d) => (
              <tr key={d.label} className="border-t border-zinc-100">
                <td className="py-1 text-zinc-700">{d.label}</td>
                <td className="py-1 text-right tabular-nums text-zinc-900">{format(d.value)}</td>
                <td className="py-1 pl-2 text-right text-zinc-500">{d.detail}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <ul id={id} aria-label={label} className="flex flex-col gap-3">
          {data.map((d) => (
            <li key={d.label} className="grid grid-cols-[minmax(6rem,9rem)_1fr_auto] items-center gap-3 text-sm">
              <span className="truncate font-mono text-xs text-zinc-600" title={d.label}>
                {d.label}
              </span>
              <span className="h-4 rounded-r bg-zinc-100" aria-hidden="true">
                <span
                  className="block h-4 rounded-r-[4px]"
                  style={{ width: `${Math.max(2, (d.value / max) * 100)}%`, background: SERIES }}
                  title={`${d.label}: ${format(d.value)}${d.detail ? ` (${d.detail})` : ""}`}
                />
              </span>
              <span className="text-right tabular-nums text-zinc-900">
                {format(d.value)}
                {d.detail && <span className="ml-1.5 text-xs text-zinc-500">{d.detail}</span>}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
