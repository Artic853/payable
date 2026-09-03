"""Merchant-side guarantees: pricing, expiry, idempotency, inventory, mandates."""

import time

import pytest

from payable.catalog import CATALOG
from payable.commerce import (
    FREE_SHIPPING_THRESHOLD_PAISE,
    GST_RATE,
    SHIPPING_FLAT_PAISE,
    CommerceError,
    CommerceService,
    price_for,
)
from payable.mandate import issue_mandate
from payable.models import FailureCode
from payable.payments import set_gateway
from payable.payments.simulated import SimulatedRazorpayGateway


@pytest.fixture
def commerce():
    set_gateway(SimulatedRazorpayGateway(failure_rate=0.0))
    return CommerceService()


def mandate_for(amount=100_000_000, categories=None):
    return issue_mandate("user:test", "agent:test", amount, categories or [])


# -- pricing ---------------------------------------------------------------

def test_gst_and_shipping_are_applied():
    breakdown = price_for(100_000, 1)  # below the free-shipping threshold
    assert breakdown.subtotal_paise == 100_000
    assert breakdown.gst_paise == round(100_000 * GST_RATE)
    assert breakdown.shipping_paise == SHIPPING_FLAT_PAISE
    assert breakdown.total_paise == 100_000 + breakdown.gst_paise + SHIPPING_FLAT_PAISE


def test_shipping_is_free_above_the_threshold():
    breakdown = price_for(FREE_SHIPPING_THRESHOLD_PAISE, 1)
    assert breakdown.shipping_paise == 0


def test_quantity_multiplies_the_subtotal():
    breakdown = price_for(79_000, 12)
    assert breakdown.subtotal_paise == 79_000 * 12


# -- quotes ----------------------------------------------------------------

def test_quote_for_unknown_sku_raises_no_match(commerce):
    with pytest.raises(CommerceError) as exc:
        commerce.quote("NOPE-123")
    assert exc.value.code is FailureCode.NO_MATCH


def test_quote_flags_insufficient_stock_without_failing(commerce):
    quote = commerce.quote("KB-MECH-87-RED", quantity=15)  # only 8 exist
    assert quote.available is False
    assert "8" in quote.availability_note


def test_expired_quote_cannot_be_ordered(commerce):
    quote = commerce.quote("CAB-USBC-2M-240W")
    quote.expires_at = time.time() - 1

    with pytest.raises(CommerceError) as exc:
        commerce.create_order(quote.quote_id, mandate_for(), "idem-1")
    assert exc.value.code is FailureCode.QUOTE_EXPIRED


# -- orders ----------------------------------------------------------------

def test_order_creation_reserves_inventory(commerce):
    before = CATALOG.get("DOCK-USBC-11P").stock
    quote = commerce.quote("DOCK-USBC-11P", quantity=2)
    commerce.create_order(quote.quote_id, mandate_for(), "idem-stock")
    assert CATALOG.get("DOCK-USBC-11P").stock == before - 2


def test_repeating_an_idempotency_key_returns_the_same_order(commerce):
    """A retried network call must not buy the thing twice."""
    quote = commerce.quote("CAB-USBC-2M-240W")
    first = commerce.create_order(quote.quote_id, mandate_for(), "idem-same")
    second = commerce.create_order(quote.quote_id, mandate_for(), "idem-same")

    assert first.order_id == second.order_id
    assert len(commerce.orders) == 1


def test_order_beyond_stock_is_refused(commerce):
    quote = commerce.quote("KB-MECH-87-RED", quantity=15)
    with pytest.raises(CommerceError) as exc:
        commerce.create_order(quote.quote_id, mandate_for(), "idem-oos")
    assert exc.value.code is FailureCode.OUT_OF_STOCK


def test_order_above_the_mandate_cap_is_refused(commerce):
    quote = commerce.quote("MON-27-4K-IPS")
    with pytest.raises(CommerceError) as exc:
        commerce.create_order(quote.quote_id, mandate_for(amount=1_000), "idem-cap")
    assert exc.value.code is FailureCode.OVER_BUDGET


def test_order_outside_the_mandate_category_is_refused(commerce):
    quote = commerce.quote("MON-27-4K-IPS")
    with pytest.raises(CommerceError) as exc:
        commerce.create_order(
            quote.quote_id, mandate_for(categories=["keyboard"]), "idem-scope"
        )
    assert exc.value.code is FailureCode.MANDATE_REJECTED


def test_refused_order_does_not_consume_inventory(commerce):
    before = CATALOG.get("MON-27-4K-IPS").stock
    quote = commerce.quote("MON-27-4K-IPS")
    with pytest.raises(CommerceError):
        commerce.create_order(quote.quote_id, mandate_for(amount=1_000), "idem-norsv")
    assert CATALOG.get("MON-27-4K-IPS").stock == before


# -- payment ---------------------------------------------------------------

def test_successful_payment_marks_the_order_paid(commerce):
    quote = commerce.quote("CAB-USBC-2M-240W")
    order = commerce.create_order(quote.quote_id, mandate_for(), "idem-pay")
    result = commerce.pay(order.order_id)

    assert result.status == "captured"
    assert commerce.get_order(order.order_id).status == "paid"
    assert result.amount_paise == quote.breakdown.total_paise


def test_declined_payment_releases_the_reservation(commerce):
    """A failed payment must not strand stock that nobody bought."""
    set_gateway(SimulatedRazorpayGateway(failure_rate=1.0))
    service = CommerceService()

    before = CATALOG.get("CAB-USBC-2M-240W").stock
    quote = service.quote("CAB-USBC-2M-240W", quantity=3)
    order = service.create_order(quote.quote_id, mandate_for(), "idem-decline")
    assert CATALOG.get("CAB-USBC-2M-240W").stock == before - 3

    result = service.pay(order.order_id)
    assert result.status == "failed"
    assert result.failure_code in {FailureCode.PAYMENT_DECLINED, FailureCode.GATEWAY_ERROR}
    assert CATALOG.get("CAB-USBC-2M-240W").stock == before


def test_paying_twice_is_idempotent(commerce):
    quote = commerce.quote("CAB-USBC-2M-240W")
    order = commerce.create_order(quote.quote_id, mandate_for(), "idem-twice")
    commerce.pay(order.order_id)
    second = commerce.pay(order.order_id)

    assert second.status == "captured"
    assert "idempotent" in second.failure_reason
