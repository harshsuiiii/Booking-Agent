from decimal import Decimal

from booking_agent.connectors.errors import (
    FareExpired,
    PriceChanged,
    SoldOut,
    SupplierAuthError,
    SupplierBadResponse,
    SupplierError,
    SupplierRateLimited,
    SupplierTimeout,
)


class TestErrorHierarchy:
    def test_all_are_supplier_error(self):
        errors = [
            SupplierTimeout("s", "search"),
            SupplierAuthError("s"),
            SupplierRateLimited("s"),
            SupplierBadResponse("s", 500, "err"),
            FareExpired("s", "ref"),
            PriceChanged("s", "ref", Decimal("100"), Decimal("110")),
            SoldOut("s", "ref"),
        ]
        for e in errors:
            assert isinstance(e, SupplierError)
            assert isinstance(e, Exception)

    def test_supplier_id_preserved(self):
        for cls, kwargs in [
            (SupplierTimeout, {"operation": "search"}),
            (SupplierAuthError, {}),
            (SupplierRateLimited, {}),
        ]:
            err = cls(supplier_id="amadeus", **kwargs)  # type: ignore[call-arg]
            assert err.supplier_id == "amadeus"

    def test_supplier_id_in_str(self):
        e = SupplierTimeout("amadeus", "search")
        assert "amadeus" in str(e)


class TestSupplierTimeout:
    def test_operation_stored(self):
        e = SupplierTimeout("amadeus", "price")
        assert e.operation == "price"

    def test_message_includes_operation(self):
        assert "price" in str(SupplierTimeout("s", "price"))


class TestSupplierRateLimited:
    def test_retry_after_none_by_default(self):
        assert SupplierRateLimited("s").retry_after is None

    def test_retry_after_stored(self):
        e = SupplierRateLimited("s", retry_after=30)
        assert e.retry_after == 30

    def test_retry_after_in_message(self):
        assert "30" in str(SupplierRateLimited("s", retry_after=30))


class TestSupplierBadResponse:
    def test_fields_stored(self):
        e = SupplierBadResponse("s", 422, "Unprocessable", "search")
        assert e.status_code == 422
        assert e.body == "Unprocessable"
        assert e.operation == "search"

    def test_long_body_truncated_in_message(self):
        long_body = "x" * 500
        msg = str(SupplierBadResponse("s", 500, long_body))
        assert len(msg) < 600  # body truncated to 200 chars in message

    def test_status_code_in_message(self):
        assert "422" in str(SupplierBadResponse("s", 422, "err"))


class TestPriceChanged:
    def test_prices_stored(self):
        e = PriceChanged("s", "REF1", Decimal("3500"), Decimal("3800"))
        assert e.old_price == Decimal("3500")
        assert e.new_price == Decimal("3800")
        assert e.offer_ref == "REF1"

    def test_both_prices_in_message(self):
        msg = str(PriceChanged("s", "X", Decimal("3500"), Decimal("3800")))
        assert "3500" in msg
        assert "3800" in msg


class TestFareExpired:
    def test_offer_ref_stored(self):
        e = FareExpired("s", "AMX999")
        assert e.offer_ref == "AMX999"


class TestSoldOut:
    def test_offer_ref_stored(self):
        e = SoldOut("s", "AMX999")
        assert e.offer_ref == "AMX999"
