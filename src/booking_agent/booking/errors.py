"""Booking-service-specific exceptions."""

from decimal import Decimal


class BookingError(Exception):
    """Base class for all booking errors."""


class IdempotentReplay(BookingError):
    """Raised when a booking with the same idempotency key already exists."""

    def __init__(self, booking_id: str) -> None:
        self.booking_id = booking_id
        super().__init__(f"Booking already exists: {booking_id}")


class PriceChangedError(BookingError):
    """
    Raised by prepare() when the live re-price returns a higher price.

    Callers must show the new price and ask for re-confirmation.
    """

    def __init__(self, old_price: Decimal, new_price: Decimal) -> None:
        self.old_price = old_price
        self.new_price = new_price
        super().__init__(
            f"Price changed from {old_price} to {new_price} INR — re-confirmation required."
        )


class InvalidToken(BookingError):
    """Token is missing, malformed, or does not match the booking."""


class TokenExpired(BookingError):
    """Token is valid but has passed its expiry time."""


class BookingNotFound(BookingError):
    """No booking found for the given booking_id (or tenant mismatch)."""

    def __init__(self, booking_id: str) -> None:
        super().__init__(f"Booking not found: {booking_id}")


class BookingConflict(BookingError):
    """The booking changed since it was loaded (e.g. two confirms at once)."""

    def __init__(self, booking_id: str) -> None:
        self.booking_id = booking_id
        super().__init__(f"Booking {booking_id} was modified concurrently; reload and retry.")


class IllegalStateTransition(BookingError):
    """Wrapper around the domain ValueError for illegal state transitions."""
