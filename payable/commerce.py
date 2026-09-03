"""Commerce service: quote -> order -> payment, with the merchant-side checks.

This is where the merchant refuses to be talked into a bad transaction. Quotes
expire, mandates are verified server-side before an order exists, inventory is
reserved rather than assumed, and identical idempotency keys return the same
order instead of charging twice. An agent that retries a network blip does not
buy two keyboards.
"""

from __future__ import annotations

import time
import uuid

from .audit import AUDIT
from .catalog import CATALOG, Catalog
from .config import SETTINGS
from .mandate import MandateError, verify_mandate
from .models import (
    FailureCode,
    Mandate,
    Order,
    PaymentResult,
    PriceBreakdown,
    Quote,
)
from .payments import get_gateway

GST_RATE = 0.18
FREE_SHIPPING_THRESHOLD_PAISE = 500_000
SHIPPING_FLAT_PAISE = 9_900
QUOTE_TTL_SECONDS = 120

# When a live Razorpay order cannot be captured headlessly, simulate the capture
# leg rather than stalling the demo -- and label it so, everywhere.
ALLOW_SIMULATED_CAPTURE = True


class CommerceError(Exception):
    def __init__(self, code: FailureCode, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def price_for(unit_price_paise: int, quantity: int) -> PriceBreakdown:
    subtotal = unit_price_paise * quantity
    gst = round(subtotal * GST_RATE)
    shipping = 0 if subtotal >= FREE_SHIPPING_THRESHOLD_PAISE else SHIPPING_FLAT_PAISE
    return PriceBreakdown(
        unit_price_paise=unit_price_paise,
        quantity=quantity,
        subtotal_paise=subtotal,
        gst_rate=GST_RATE,
        gst_paise=gst,
        shipping_paise=shipping,
        total_paise=subtotal + gst + shipping,
    )


class CommerceService:
    def __init__(self, catalog: Catalog | None = None):
        self.catalog = catalog or CATALOG
        self.quotes: dict[str, Quote] = {}
        self.orders: dict[str, Order] = {}
        self._by_gateway_id: dict[str, str] = {}
        self._idempotency: dict[str, str] = {}

    # -- quote -----------------------------------------------------------

    def quote(self, sku: str, quantity: int = 1, ship_to_pincode: str = "500078") -> Quote:
        product = self.catalog.get(sku)
        if product is None:
            raise CommerceError(FailureCode.NO_MATCH, f"unknown sku {sku!r}")
        if quantity < 1:
            raise CommerceError(FailureCode.SPEC_MISMATCH, "quantity must be >= 1")

        breakdown = price_for(product.price_paise, quantity)
        available = product.stock >= quantity
        quote = Quote(
            quote_id=f"qt_{uuid.uuid4().hex[:16]}",
            sku=sku,
            product_name=product.name,
            quantity=quantity,
            breakdown=breakdown,
            currency=self.catalog.merchant.currency,
            ship_to_pincode=ship_to_pincode,
            expires_at=time.time() + QUOTE_TTL_SECONDS,
            available=available,
            availability_note=(
                "in stock" if available else f"only {product.stock} unit(s) available"
            ),
        )
        self.quotes[quote.quote_id] = quote
        return quote

    # -- order -----------------------------------------------------------

    def create_order(
        self,
        quote_id: str,
        mandate: Mandate,
        idempotency_key: str,
        buyer_reference: str = "",
        run_id: str = "",
    ) -> Order:
        if idempotency_key in self._idempotency:
            return self.orders[self._idempotency[idempotency_key]]

        quote = self.quotes.get(quote_id)
        if quote is None:
            raise CommerceError(FailureCode.NO_MATCH, f"unknown quote {quote_id!r}")
        if time.time() > quote.expires_at:
            raise CommerceError(FailureCode.QUOTE_EXPIRED, "quote expired; re-quote before ordering")

        product = self.catalog.get(quote.sku)
        if product is None:
            raise CommerceError(FailureCode.NO_MATCH, f"sku {quote.sku!r} no longer listed")

        try:
            verify_mandate(mandate, quote.breakdown.total_paise, product.category)
        except MandateError as exc:
            AUDIT.record(
                run_id or "unbound", "merchant", "mandate_check", decision="rejected",
                rationale=exc.message,
                payload={"mandate_id": mandate.mandate_id, "amount": quote.breakdown.total_paise},
            )
            raise CommerceError(exc.code, exc.message) from exc

        AUDIT.record(
            run_id or "unbound", "merchant", "mandate_check", decision="accepted",
            rationale=(
                f"amount {quote.breakdown.total_paise} within cap {mandate.max_amount_paise}; "
                f"category {product.category!r} in scope"
            ),
            payload={"mandate_id": mandate.mandate_id},
        )

        if not self.catalog.reserve(quote.sku, quote.quantity):
            raise CommerceError(
                FailureCode.OUT_OF_STOCK,
                f"cannot reserve {quote.quantity} x {quote.sku}",
            )

        gateway = get_gateway()
        try:
            gw_order = gateway.create_order(
                amount_paise=quote.breakdown.total_paise,
                currency=quote.currency,
                receipt=f"rcpt_{quote.quote_id}",
                notes={
                    "sku": quote.sku,
                    "agent_id": mandate.agent_id,
                    "principal": mandate.principal,
                    "mandate_id": mandate.mandate_id,
                    "buyer_reference": buyer_reference,
                },
            )
        except Exception as exc:  # network / auth / rate limit
            self.catalog.release(quote.sku, quote.quantity)
            raise CommerceError(FailureCode.GATEWAY_ERROR, f"gateway rejected order: {exc}") from exc

        order = Order(
            order_id=f"ord_{uuid.uuid4().hex[:16]}",
            gateway_order_id=gw_order.get("id", ""),
            quote_id=quote.quote_id,
            sku=quote.sku,
            quantity=quote.quantity,
            amount_paise=quote.breakdown.total_paise,
            currency=quote.currency,
            status="created",
            gateway=gateway.name,  # type: ignore[arg-type]
            checkout_url=gw_order.get("checkout_url"),
            created_at=time.time(),
        )
        self.orders[order.order_id] = order
        self._by_gateway_id[order.gateway_order_id] = order.order_id
        self._idempotency[idempotency_key] = order.order_id
        return order

    # -- payment ---------------------------------------------------------

    def pay(
        self,
        order_id: str,
        method: str = "upi",
        vpa: str | None = "buyer@upi",
        run_id: str = "",
    ) -> PaymentResult:
        order = self.orders.get(order_id)
        if order is None:
            raise CommerceError(FailureCode.NO_MATCH, f"unknown order {order_id!r}")
        if order.status == "paid":
            return PaymentResult(
                order_id=order.gateway_order_id,
                payment_id=None,
                status="captured",
                amount_paise=order.amount_paise,
                method=method,
                gateway=order.gateway,
                failure_reason="already paid (idempotent replay)",
            )

        gateway = get_gateway()
        result = gateway.pay(order.gateway_order_id, order.amount_paise, method, vpa)

        if result.status == "pending" and ALLOW_SIMULATED_CAPTURE:
            # Live Razorpay path: the order is real, the capture leg is not
            # automatable. Simulate it and say so in the gateway label.
            from .payments.simulated import SimulatedRazorpayGateway

            sim = SimulatedRazorpayGateway()
            sim._orders[order.gateway_order_id] = {  # type: ignore[index]
                "id": order.gateway_order_id,
                "amount": order.amount_paise,
                "status": "created",
            }
            simulated = sim.pay(order.gateway_order_id, order.amount_paise, method, vpa)
            simulated.gateway = f"{order.gateway}+simulated-capture"
            simulated.failure_reason = simulated.failure_reason or result.failure_reason
            result = simulated

        if result.status == "captured":
            order.status = "paid"
        else:
            order.status = "failed"
            # Release the reservation so a declined payment does not strand stock.
            self.catalog.release(order.sku, order.quantity)

        AUDIT.record(
            run_id or "unbound", "gateway", "payment_attempt",
            decision=result.status,
            rationale=result.failure_reason or f"{method} capture via {result.gateway}",
            payload={
                "order_id": order.order_id,
                "gateway_order_id": order.gateway_order_id,
                "payment_id": result.payment_id,
                "amount_paise": result.amount_paise,
                "failure_code": result.failure_code.value,
                "retriable": result.retriable,
            },
            latency_ms=result.latency_ms,
        )
        return result

    # -- reads -----------------------------------------------------------

    def get_order(self, order_id: str) -> Order | None:
        return self.orders.get(order_id)


COMMERCE = CommerceService()
