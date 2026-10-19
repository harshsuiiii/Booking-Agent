"""
structlog processor: redact PII before it reaches any log sink.

Add to the structlog processor chain BEFORE any sink (console, file, remote).
Patterns covered:
  - Passport numbers (most international formats)
  - Indian PAN numbers
  - Payment card numbers (16-digit groups)
  - Indian mobile numbers
  - Generic email addresses
"""

import re
from typing import Any

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Passport: 1-2 uppercase letters followed by 6–9 digits (e.g. P1234567, AB1234567)
    (re.compile(r"\b([A-Z]{1,2}[0-9]{6,9})\b"), "[PASSPORT_REDACTED]"),
    # PAN: 5 uppercase letters + 4 digits + 1 uppercase letter (ABCDE1234F)
    (re.compile(r"\b([A-Z]{5}[0-9]{4}[A-Z])\b"), "[PAN_REDACTED]"),
    # Payment card: 4 groups of 4 digits with optional space/dash separator
    (
        re.compile(r"\b(\d{4}[- ]?\d{4}[- ]?\d{4}[- ]?\d{4})\b"),
        "[CARD_REDACTED]",
    ),
    # Indian mobile: optional +91 or 91 prefix, then 6–9 starting digit, then 9 more
    (re.compile(r"\b(?:\+91|91)?([6-9]\d{9})\b"), "[PHONE_REDACTED]"),
    # Email addresses
    (
        re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"),
        "[EMAIL_REDACTED]",
    ),
]

# Keys whose values should always be redacted regardless of content pattern.
_SENSITIVE_KEYS = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "api_key",
        "token",
        "access_token",
        "refresh_token",
        "authorization",
        "card_number",
        "cvv",
        "card_cvv",
        "passport_number",
        "dob",
        "date_of_birth",
        "pan",
    }
)


def scrub_pii(
    logger: Any,  # noqa: ARG001 — structlog passes this but we don't need it
    method: str,  # noqa: ARG001
    event_dict: dict[str, Any],
) -> dict[str, Any]:
    """
    structlog processor that redacts PII from every log event.

    Must be placed BEFORE the final renderer in the processor chain.
    """
    for key, value in list(event_dict.items()):
        if isinstance(key, str) and key.lower() in _SENSITIVE_KEYS:
            event_dict[key] = "[REDACTED]"
            continue
        if isinstance(value, str):
            for pattern, replacement in _PATTERNS:
                value = pattern.sub(replacement, value)
            event_dict[key] = value
    return event_dict
