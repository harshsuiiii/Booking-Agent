import type { SupplierStatus } from "@/types/sse";
import { supplierLabel } from "@/lib/format";

interface Props {
  statuses: Record<string, SupplierStatus>;
}

const STATUS_CONFIG: Record<SupplierStatus, { label: string; classes: string; dot: string }> = {
  loading: {
    label: "Searching…",
    classes: "bg-blue-50 text-blue-700 border-blue-200",
    dot: "bg-blue-400 animate-pulse",
  },
  done: {
    label: "Done",
    classes: "bg-green-50 text-green-700 border-green-200",
    dot: "bg-green-500",
  },
  failed: {
    label: "Failed",
    classes: "bg-red-50 text-red-700 border-red-200",
    dot: "bg-red-400",
  },
  timed_out: {
    label: "Timed out",
    classes: "bg-amber-50 text-amber-700 border-amber-200",
    dot: "bg-amber-400",
  },
  skipped: {
    label: "Skipped",
    classes: "bg-zinc-50 text-zinc-500 border-zinc-200",
    dot: "bg-zinc-300",
  },
  cache: {
    label: "Cache hit",
    classes: "bg-purple-50 text-purple-700 border-purple-200",
    dot: "bg-purple-400",
  },
};

export default function SupplierStatusStrip({ statuses }: Props) {
  const entries = Object.entries(statuses);
  if (entries.length === 0) return null;

  return (
    <div className="flex flex-wrap gap-2" role="status" aria-label="Supplier search status">
      {entries.map(([supplierId, status]) => {
        const cfg = STATUS_CONFIG[status];
        return (
          <span
            key={supplierId}
            className={`inline-flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs font-medium ${cfg.classes}`}
          >
            <span className={`h-1.5 w-1.5 rounded-full ${cfg.dot}`} />
            <span>{supplierLabel(supplierId)}</span>
            <span className="text-[10px] opacity-70">{cfg.label}</span>
          </span>
        );
      })}
    </div>
  );
}
