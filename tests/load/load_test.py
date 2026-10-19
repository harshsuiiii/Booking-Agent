"""
Load test: 100 concurrent searches against FakeConnector.

Run directly:
    python -m tests.load.load_test

Or via pytest (slower output):
    python -m pytest tests/load/load_test.py -v -s

Reports:
  - Total time for N concurrent searches
  - Min / P50 / P95 / P99 / Max latency per search
  - Throughput (searches/second)
  - Failure count

The FakeConnector simulates realistic latency via `search_latency_seconds`.
Change CONCURRENCY and TOTAL_REQUESTS at the top of this file to scale the test.
"""

import asyncio
import statistics
import time
from datetime import date

import pytest

from booking_agent.connectors.fake import FakeConnector
from booking_agent.domain.search import SearchQuery
from booking_agent.orchestrator.search import SearchOrchestrator, TenantContext

# ── tuning knobs ──────────────────────────────────────────────────────────────

CONCURRENCY = 100  # simultaneous in-flight searches
TOTAL_REQUESTS = 200  # total searches to run
FAKE_LATENCY_S = 0.05  # simulated connector latency (50 ms per search)

# ── helpers ───────────────────────────────────────────────────────────────────


async def _single_search(orch: SearchOrchestrator) -> float:
    """Run one search and return elapsed seconds. Raises on failure."""
    query = SearchQuery(
        origin="DEL",
        destination="BOM",
        departure_date=date(2027, 9, 1),
    )
    ctx = TenantContext(tenant_id="load-test")
    t0 = time.monotonic()
    async for _ in orch.search(query, ctx):
        pass
    return time.monotonic() - t0


async def run_load_test(
    concurrency: int = CONCURRENCY,
    total: int = TOTAL_REQUESTS,
    latency_s: float = FAKE_LATENCY_S,
) -> dict:
    """
    Run the load test and return a results dict with latency percentiles.

    This function is importable so other tests can call it programmatically.
    """
    connector = FakeConnector(
        supplier_id="fake",
        max_retries=1,
        retry_min_wait=0.0,
        retry_max_wait=0.0,
        delay_seconds=latency_s,
    )
    # Raise the per-supplier concurrency limit so load test isn't bottlenecked
    # by the default semaphore of 2.
    orch = SearchOrchestrator(
        connectors=[connector],
        max_concurrent_per_supplier=concurrency,
    )

    semaphore = asyncio.Semaphore(concurrency)
    latencies: list[float] = []
    errors = 0

    async def bounded_search() -> None:
        nonlocal errors
        async with semaphore:
            try:
                elapsed = await _single_search(orch)
                latencies.append(elapsed)
            except Exception:
                errors += 1

    async with connector:
        wall_start = time.monotonic()
        await asyncio.gather(*[bounded_search() for _ in range(total)])
        wall_elapsed = time.monotonic() - wall_start

    if not latencies:
        return {"error": "All requests failed", "failures": errors}

    latencies_ms = [lat * 1000 for lat in latencies]
    sorted_lats = sorted(latencies_ms)
    n = len(sorted_lats)

    def pct(p: float) -> float:
        return sorted_lats[max(0, int(n * p / 100) - 1)]

    return {
        "total_requests": total,
        "concurrency": concurrency,
        "successful": n,
        "failures": errors,
        "wall_time_s": round(wall_elapsed, 3),
        "throughput_rps": round(n / wall_elapsed, 1),
        "latency_ms": {
            "min": round(min(latencies_ms), 1),
            "p50": round(pct(50), 1),
            "p95": round(pct(95), 1),
            "p99": round(pct(99), 1),
            "max": round(max(latencies_ms), 1),
            "mean": round(statistics.mean(latencies_ms), 1),
        },
    }


# ── pytest entry point ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_load_100_concurrent_searches():
    """
    Smoke-level load test: 100 concurrent searches must complete with
    < 5% failure rate and P95 latency under 500 ms (FakeConnector at 50 ms).
    """
    results = await run_load_test(concurrency=100, total=100, latency_s=0.05)

    print("\n=== Load Test Results ===")
    for k, v in results.items():
        print(f"  {k}: {v}")

    assert results["failures"] == 0, f"Expected 0 failures, got {results['failures']}"
    assert results["latency_ms"]["p95"] < 500, (
        f"P95 latency {results['latency_ms']['p95']} ms exceeds 500 ms"
    )


# ── CLI entry point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    results = asyncio.run(run_load_test())
    print("\n=== Booking Agent Load Test ===")
    print(f"  Requests: {results['total_requests']} total, {results['concurrency']} concurrent")
    print(f"  Success:  {results['successful']}  |  Failures: {results['failures']}")
    print(
        f"  Wall time: {results['wall_time_s']} s  |  Throughput: {results['throughput_rps']} rps"
    )
    print("  Latency (ms):")
    for k, v in results["latency_ms"].items():
        print(f"    {k:>4}: {v}")
    if results["failures"] > 0:
        sys.exit(1)
