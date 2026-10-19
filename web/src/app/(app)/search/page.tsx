"use client";

import { Suspense, useState, useMemo, useCallback } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import type { SerializedGroup, SortMode } from "@/types/sse";
import { useSearch } from "@/hooks/useSearch";
import { fromUrlQuery, toLocalIsoDate, toUrlQuery, type SearchParams } from "@/lib/searchQuery";
import { effectivePrice, totalJourneyMinutes } from "@/lib/offerUtils";
import SearchForm from "@/components/SearchForm";
import SupplierStatusStrip from "@/components/SupplierStatusStrip";
import SortModeSelector from "@/components/SortModeSelector";
import OfferCard from "@/components/OfferCard";
import OfferDrawer from "@/components/OfferDrawer";

function sortGroups(groups: SerializedGroup[], mode: SortMode): SerializedGroup[] {
  return [...groups].sort((a, b) => {
    switch (mode) {
      case "cheapest":
        return parseFloat(a.primary.sell_inr) - parseFloat(b.primary.sell_inr);
      case "fastest":
        return totalJourneyMinutes(a.primary) - totalJourneyMinutes(b.primary);
      case "best_value":
        return effectivePrice(a.primary) - effectivePrice(b.primary);
    }
  });
}

export default function SearchPage() {
  // useSearchParams needs a Suspense boundary (the prefill is client-only).
  return (
    <Suspense fallback={null}>
      <SearchView />
    </Suspense>
  );
}

function SearchView() {
  const params = useSearchParams();
  const router = useRouter();
  // Read once: the form keeps its own state after that (the URL then follows the form).
  const [initial] = useState(() => fromUrlQuery(params, toLocalIsoDate(new Date())));
  const { state, search: runSearch } = useSearch();
  const search = useCallback(
    (p: SearchParams) => {
      router.replace(`/search?${toUrlQuery(p)}`, { scroll: false });
      runSearch(p);
    },
    [router, runSearch],
  );
  const [sortMode, setSortMode] = useState<SortMode>("best_value");
  const [selectedGroup, setSelectedGroup] = useState<SerializedGroup | null>(null);

  const sortedGroups = useMemo(
    () => sortGroups(state.groups, sortMode),
    [state.groups, sortMode]
  );

  const hasResults = state.groups.length > 0;
  const hasSuppliers = Object.keys(state.supplierStatuses).length > 0;

  return (
    <div>
      <div className="max-w-3xl mx-auto flex flex-col gap-6">
        {/* Title */}
        <div>
          <h1 className="text-2xl font-bold text-zinc-900">Flight Search</h1>
          <p className="text-sm text-zinc-500 mt-0.5">
            Compare fares across suppliers in real time
          </p>
        </div>

        {/* Search form */}
        <SearchForm onSearch={search} isLoading={state.isLoading} initial={initial} />

        {/* Error */}
        {state.error && (
          <div
            className="bg-red-50 border border-red-200 rounded-xl px-4 py-3 text-sm text-red-700"
            role="alert"
          >
            {state.error}
          </div>
        )}

        {/* Supplier status strip */}
        {hasSuppliers && (
          <SupplierStatusStrip statuses={state.supplierStatuses} />
        )}

        {/* Results header */}
        {hasResults && (
          <div className="flex items-center justify-between gap-4 flex-wrap">
            <p className="text-sm text-zinc-600">
              {state.groups.length} option{state.groups.length !== 1 ? "s" : ""}
              {state.isComplete && state.elapsedSeconds !== null && (
                <span className="text-zinc-400"> · {state.elapsedSeconds.toFixed(1)}s</span>
              )}
              {!state.isComplete && (
                <span className="text-zinc-400 animate-pulse"> · searching…</span>
              )}
            </p>
            <SortModeSelector value={sortMode} onChange={setSortMode} />
          </div>
        )}

        {/* Results */}
        <div className="flex flex-col gap-3" data-testid="results-list">
          {sortedGroups.map((group, i) => (
            <OfferCard
              key={group.dedupe_key}
              group={group}
              rank={i + 1}
              onViewDetails={setSelectedGroup}
            />
          ))}
        </div>

        {/* Empty state after complete */}
        {state.isComplete && state.groups.length === 0 && (
          <div className="text-center py-16 text-zinc-400">
            <p className="text-lg font-medium">No results found</p>
            <p className="text-sm mt-1">Try different dates or airports</p>
          </div>
        )}

        {/* Loading skeleton (first load) */}
        {state.isLoading && state.groups.length === 0 && (
          <div className="flex flex-col gap-3" aria-busy="true" aria-label="Loading results">
            {[1, 2, 3].map((n) => (
              <div
                key={n}
                className="bg-white rounded-xl border border-zinc-200 h-32 animate-pulse"
              />
            ))}
          </div>
        )}
      </div>

      {/* Offer drawer */}
      <OfferDrawer group={selectedGroup} onClose={() => setSelectedGroup(null)} />
    </div>
  );
}
