"""
Fixture-recording utility.

Captures a real supplier HTTP exchange and saves a redacted copy as a JSON fixture
under tests/fixtures/<supplier>/<operation>_<timestamp>.json.

Usage:
    uv run python scripts/record_fixture.py amadeus search \
        --origin DEL --destination BOM --date 2025-09-01

Requires real credentials in .env (see .env.example).
The saved fixture is safe to commit: tokens, names, and passport data are redacted.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ── redaction patterns ──────────────────────────────────────────────────────

_PATTERNS: list[tuple[str, str]] = [
    # OAuth bearer / API key tokens
    (r'"access_token"\s*:\s*"[^"]+"', '"access_token": "REDACTED"'),
    (r'"token"\s*:\s*"[^"]+"', '"token": "REDACTED"'),
    (r"Bearer [A-Za-z0-9\-._~+/]+=*", "Bearer REDACTED"),
    # Passport numbers (generic: 6-9 uppercase alphanumeric)
    (r"\b[A-Z]{1,2}[0-9]{6,8}\b", "PASSPORT_REDACTED"),
    # Typical Indian Aadhaar / PAN patterns
    (r"\b[A-Z]{5}[0-9]{4}[A-Z]\b", "PAN_REDACTED"),
    # Passenger names in JSON (first/last name fields)
    (r'"(firstName|lastName|givenName|surname)"\s*:\s*"[^"]+"', r'"\1": "REDACTED"'),
    # Email addresses
    (r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", "REDACTED@REDACTED.example"),
    # Phone numbers
    (r"\+?[0-9]{10,15}", "PHONE_REDACTED"),
]


def redact(text: str) -> str:
    for pattern, replacement in _PATTERNS:
        text = re.sub(pattern, replacement, text)
    return text


# ── FileRecorderHook ────────────────────────────────────────────────────────

class FileRecorderHook:
    """Writes each recorded exchange to a timestamped JSON file."""

    def __init__(self, supplier: str, fixtures_root: Path) -> None:
        self._dir = fixtures_root / supplier
        self._dir.mkdir(parents=True, exist_ok=True)

    async def record(
        self,
        supplier_id: str,
        operation: str,
        request: dict[str, Any],
        response: dict[str, Any],
    ) -> None:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = self._dir / f"{operation}_{ts}.json"
        payload = {
            "supplier_id": supplier_id,
            "operation": operation,
            "recorded_at": ts,
            "request": request,
            "response": {
                **response,
                "body": redact(response.get("body", "")),
            },
        }
        path.write_text(json.dumps(payload, indent=2, default=str))
        print(f"Fixture saved: {path}", file=sys.stderr)


# ── CLI ─────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Record a supplier HTTP exchange as a test fixture.")
    p.add_argument("supplier", choices=["amadeus", "sabre", "travelport"], help="Supplier name")
    p.add_argument("operation", choices=["search", "price", "book"], help="Operation to record")
    p.add_argument("--origin", default="DEL")
    p.add_argument("--destination", default="BOM")
    p.add_argument("--date", default="2025-09-01", help="YYYY-MM-DD")
    p.add_argument(
        "--fixtures-root",
        default="tests/fixtures",
        help="Root directory for fixture output",
    )
    return p


async def run(args: argparse.Namespace) -> None:
    from datetime import date

    fixtures_root = Path(args.fixtures_root)
    recorder = FileRecorderHook(supplier=args.supplier, fixtures_root=fixtures_root)

    # Import connector lazily so this script doesn't break if a connector
    # isn't implemented yet.
    if args.supplier == "amadeus":
        try:
            from booking_agent.connectors.amadeus import AmadeusConnector  # type: ignore[import]
            from booking_agent.connectors.secrets import EnvSecretsProvider

            connector = AmadeusConnector(
                secrets=EnvSecretsProvider(),
                recorder=recorder,  # type: ignore[arg-type]
            )
        except ImportError:
            print("AmadeusConnector not yet implemented (Step 3).", file=sys.stderr)
            sys.exit(1)
    else:
        print(f"Connector for '{args.supplier}' not yet implemented.", file=sys.stderr)
        sys.exit(1)

    from booking_agent.domain.search import SearchQuery

    query = SearchQuery(
        origin=args.origin.upper(),
        destination=args.destination.upper(),
        departure_date=date.fromisoformat(args.date),
    )

    print(f"Recording {args.supplier}.{args.operation} …", file=sys.stderr)
    async with connector:
        if args.operation == "search":
            offers = await connector.search(query)
            print(f"Got {len(offers)} offers.", file=sys.stderr)
        else:
            print(f"Operation '{args.operation}' requires an existing offer ref.", file=sys.stderr)
            print("Run 'search' first, note the supplier_offer_ref, then implement price/book recording.", file=sys.stderr)


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
