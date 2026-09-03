"""Payment gateway interface shared by the simulated and live Razorpay backends."""

from __future__ import annotations

from typing import Protocol

from ..models import PaymentResult


class GatewayOrder(dict):
    """Minimal order envelope: id, amount, currency, status, optional checkout_url."""


class PaymentGateway(Protocol):
    name: str

    def create_order(
        self,
        amount_paise: int,
        currency: str,
        receipt: str,
        notes: dict[str, str] | None = None,
    ) -> GatewayOrder:
        ...

    def pay(
        self,
        gateway_order_id: str,
        amount_paise: int,
        method: str,
        vpa: str | None = None,
    ) -> PaymentResult:
        ...

    def fetch_order(self, gateway_order_id: str) -> GatewayOrder:
        ...
