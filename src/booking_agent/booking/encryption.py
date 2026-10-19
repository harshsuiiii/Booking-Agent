"""
Field-level encryption for passenger PII.

Key: BOOKING_ENCRYPTION_KEY env var — Fernet key (44-char base64, generate with
     `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`

In tests (no key set): the round-trip still works via a per-process ephemeral key.
Passengers are NEVER logged, only the count.
"""

import base64
import json
import os
from functools import lru_cache

from cryptography.fernet import Fernet

from booking_agent.domain.booking import Passenger


def check_encryption_key() -> None:
    """Fail fast unless BOOKING_ENCRYPTION_KEY is set to a valid Fernet key."""
    key = os.getenv("BOOKING_ENCRYPTION_KEY")
    if not key:
        raise RuntimeError(
            "BOOKING_ENCRYPTION_KEY must be set when bookings are stored in a database."
        )
    try:
        Fernet(key.encode())
    except ValueError as exc:
        raise RuntimeError("BOOKING_ENCRYPTION_KEY is not a valid Fernet key.") from exc


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    key = os.getenv("BOOKING_ENCRYPTION_KEY")
    if key:
        return Fernet(key.encode())
    # Ephemeral key for tests/dev — data is not durable across restarts.
    return Fernet(Fernet.generate_key())


def encrypt_passengers(passengers: list[Passenger]) -> str:
    """Serialize and encrypt passengers. Returns a base64-safe string for DB storage."""
    raw = json.dumps([p.model_dump(mode="json") for p in passengers])
    token = _fernet().encrypt(raw.encode())
    return base64.urlsafe_b64encode(token).decode()


def decrypt_passengers(data: str) -> list[Passenger]:
    """Decrypt and deserialize passengers from the stored string."""
    token = base64.urlsafe_b64decode(data.encode())
    raw = _fernet().decrypt(token)
    records = json.loads(raw.decode())
    return [Passenger.model_validate(r) for r in records]
