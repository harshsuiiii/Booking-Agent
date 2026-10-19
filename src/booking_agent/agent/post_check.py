"""
Post-check: verify the LLM reply contains no prices that weren't in tool results.

We look for amounts with explicit currency markers (₹, INR, Rs) to avoid false
positives on flight numbers, durations, or years. An empty allowed_prices set
means no search has been performed yet — no prices are expected in the reply.
"""

import contextlib
import re
from decimal import Decimal, InvalidOperation

# Matches ₹5,240 / INR 5240 / 5,240 INR / Rs.5240
_PATTERNS = [
    r"₹\s*([\d,]+(?:\.\d{1,2})?)",
    r"([\d,]+(?:\.\d{1,2})?)\s*INR\b",
    r"\bINR\s*([\d,]+(?:\.\d{1,2})?)",
    r"\bRs\.?\s*([\d,]+(?:\.\d{1,2})?)",
]


def extract_prices(text: str) -> set[str]:
    """Return every price-like string found in *text* (stripped of formatting)."""
    found: set[str] = set()
    for pat in _PATTERNS:
        for m in re.finditer(pat, text, re.IGNORECASE):
            raw = m.group(1).replace(",", "")
            # Normalise to a canonical Decimal string so "5240" == "5240.00"
            with contextlib.suppress(InvalidOperation):
                found.add(str(Decimal(raw)))
    return found


def check_reply(reply: str, allowed_prices: set[str]) -> None:
    """
    Raise ValueError if *reply* contains a currency-marked price not in
    *allowed_prices*.

    If no search has been performed (allowed_prices is empty), any price in the
    reply is considered invented and will be flagged.

    allowed_prices should contain Decimal-normalised strings (str(Decimal(...))).
    """
    found = extract_prices(reply)
    if not found:
        return
    if not allowed_prices:
        raise ValueError(f"Reply contains price(s) but no search has been performed: {found}")
    invented = found - allowed_prices
    if invented:
        raise ValueError(f"Reply contains price(s) not present in tool results: {invented}")
