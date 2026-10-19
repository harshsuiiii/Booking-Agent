from decimal import Decimal


class SupplierError(Exception):
    """Base for all supplier-facing errors."""

    def __init__(self, supplier_id: str, message: str = "") -> None:
        self.supplier_id = supplier_id
        super().__init__(f"[{supplier_id}] {message}" if message else f"[{supplier_id}]")


class SupplierTimeout(SupplierError):
    def __init__(self, supplier_id: str, operation: str) -> None:
        self.operation = operation
        super().__init__(supplier_id, f"timeout on {operation}")


class SupplierAuthError(SupplierError):
    def __init__(self, supplier_id: str, message: str = "authentication failed") -> None:
        super().__init__(supplier_id, message)


class SupplierRateLimited(SupplierError):
    def __init__(self, supplier_id: str, retry_after: int | None = None) -> None:
        self.retry_after = retry_after
        detail = f", retry after {retry_after}s" if retry_after else ""
        super().__init__(supplier_id, f"rate limited{detail}")


class SupplierBadResponse(SupplierError):
    def __init__(
        self,
        supplier_id: str,
        status_code: int,
        body: str,
        operation: str = "",
    ) -> None:
        self.status_code = status_code
        self.body = body
        self.operation = operation
        super().__init__(supplier_id, f"HTTP {status_code} on {operation}: {body[:200]}")


class FareExpired(SupplierError):
    def __init__(self, supplier_id: str, offer_ref: str) -> None:
        self.offer_ref = offer_ref
        super().__init__(supplier_id, f"fare expired: {offer_ref}")


class PriceChanged(SupplierError):
    """Raised by price() when the supplier returns a different total than stored."""

    def __init__(
        self,
        supplier_id: str,
        offer_ref: str,
        old_price: Decimal,
        new_price: Decimal,
    ) -> None:
        self.offer_ref = offer_ref
        self.old_price = old_price
        self.new_price = new_price
        super().__init__(supplier_id, f"price changed {old_price} → {new_price} for {offer_ref}")


class SoldOut(SupplierError):
    def __init__(self, supplier_id: str, offer_ref: str) -> None:
        self.offer_ref = offer_ref
        super().__init__(supplier_id, f"sold out: {offer_ref}")


class BookingDisabled(SupplierError):
    """
    Raised by book() when live booking is not yet enabled.

    In v1 only test-mode tokens (duffel_test_*) are allowed, and even then
    BOOKING_LIVE_ENABLED must be true in non-test environments.  Step 11 will
    collect customer payment via gateway before enabling live bookings.
    """

    def __init__(self, supplier_id: str, reason: str) -> None:
        self.reason = reason
        super().__init__(supplier_id, f"booking disabled: {reason}")
