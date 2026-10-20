"""
Secret redaction for logs, errors and recorded fixtures.

Masks supplier tokens (Duffel test/live), Bearer credentials, and the configured
secret env values wherever they appear in text. Installed into structlog at app start,
so a token that slips into a log field or exception message is masked before output.
"""

import os
import re
from collections.abc import MutableMapping
from typing import Any

import structlog

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"duffel_(test|live)_[A-Za-z0-9_\-]+"), r"duffel_\1_REDACTED"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9\-._~+/]+=*"), "Bearer REDACTED"),
    (re.compile(r'(?i)("?(?:authorization|x-api-key)"?\s*[:=]\s*"?)[^",}\s]+'), r"\1REDACTED"),
    # Database URLs: keep scheme, user and host, hide the password.
    (re.compile(r"(?i)(postgres(?:ql)?(?:\+\w+)?://[^:/@\s]+:)[^@\s]+@"), r"\1REDACTED@"),
]
_SECRET_ENV = (
    "DUFFEL_ACCESS_TOKEN",
    "API_KEY",
    "DATABASE_URL",
    "BOOKING_ENCRYPTION_KEY",
    "GROQ_API_KEY",
    "GEMINI_API_KEY",
    "CEREBRAS_API_KEY",
    "ANTHROPIC_API_KEY",
)


def redact_secrets(text: str) -> str:
    for name in _SECRET_ENV:
        value = os.environ.get(name)
        if value and len(value) >= 8 and value in text:
            text = text.replace(value, f"[{name} REDACTED]")
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, BaseException):
        return redact_secrets(f"{type(value).__name__}: {value}")
    if isinstance(value, dict):
        return {k: _redact_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(_redact_value(v) for v in value)
    return value


def redact_processor(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    for key, value in list(event_dict.items()):
        if key == "exc_info":
            continue  # formatted (and redacted) below by format_exc_info + this processor
        event_dict[key] = _redact_value(value)
    return event_dict


_installed = False


def install_log_redaction() -> None:
    """Insert redaction just before the renderer in structlog's processor chain (idempotent)."""
    global _installed
    if _installed:
        return
    processors = list(structlog.get_config()["processors"])
    # Render exceptions to text first so tracebacks are redacted too.
    renderer = processors.pop() if processors else structlog.dev.ConsoleRenderer()
    processors = [p for p in processors if p is not structlog.dev.set_exc_info]
    processors += [structlog.processors.format_exc_info, redact_processor, renderer]
    structlog.configure(processors=processors)
    _installed = True
