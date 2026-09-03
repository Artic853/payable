"""Live Razorpay test-mode gateway.

Honest boundary, stated plainly because it matters for how this demo is read:

  * Order creation is REAL. `POST /v1/orders` against Razorpay test mode with
    your `rzp_test_*` key returns a real `order_...` id you can see in the
    Razorpay dashboard.
  * Payment Link creation is REAL when enabled -- a URL a human can actually pay
    in test mode.
  * Capture is NOT automatable. Razorpay completes payment through its hosted
    checkout, which needs a browser and a payer. So `pay()` returns `pending`
    with the payable URL, and the orchestrator either stops there (strict mode)
    or simulates the capture leg and labels the result
    `razorpay-api+simulated-capture` so nothing in the audit log claims a
    capture that did not happen.

Set RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET to activate this backend.
"""

from __future__ import annotations

import time

import httpx

from ..config import SETTINGS
from ..models import FailureCode, PaymentResult
from .base import GatewayOrder


class RazorpayGateway:
    name = "razorpay-api"

    def __init__(
        self,
        key_id: str | None = None,
        key_secret: str | None = None,
        api_base: str | None = None,
        timeout: float = 12.0,
        create_payment_links: bool = True,
    ):
        self.key_id = key_id or SETTINGS.razorpay_key_id
        self.key_secret = key_secret or SETTINGS.razorpay_key_secret
        self.api_base = (api_base or SETTINGS.razorpay_api_base).rstrip("/")
        self.timeout = timeout
        self.create_payment_links = create_payment_links
        self._client = httpx.Client(
            auth=(self.key_id, self.key_secret),
            timeout=timeout,
            headers={"Content-Type": "application/json"},
        )

    # -- orders ----------------------------------------------------------

    def create_order(
        self,
        amount_paise: int,
        currency: str,
        receipt: str,
        notes: dict[str, str] | None = None,
    ) -> GatewayOrder:
        body = {
            "amount": amount_paise,
            "currency": currency,
            "receipt": receipt[:40],
            "notes": notes or {},
            "payment_capture": 1,
        }
        response = self._client.post(f"{self.api_base}/orders", json=body)
        response.raise_for_status()
        order = GatewayOrder(response.json())

        if self.create_payment_links:
            link = self._create_payment_link(amount_paise, currency, receipt, notes)
            if link:
                order["checkout_url"] = link.get("short_url")
                order["payment_link_id"] = link.get("id")
        return order

    def _create_payment_link(
        self,
        amount_paise: int,
        currency: str,
        receipt: str,
        notes: dict[str, str] | None,
    ) -> dict | None:
        body = {
            "amount": amount_paise,
            "currency": currency,
            "accept_partial": False,
            "description": f"Agent purchase {receipt}"[:255],
            "notes": notes or {},
            "reminder_enable": False,
        }
        try:
            response = self._client.post(f"{self.api_base}/payment_links", json=body)
            if response.status_code >= 400:
                return None
            return response.json()
        except httpx.HTTPError:
            return None

    def fetch_order(self, gateway_order_id: str) -> GatewayOrder:
        response = self._client.get(f"{self.api_base}/orders/{gateway_order_id}")
        response.raise_for_status()
        return GatewayOrder(response.json())

    # -- payment ---------------------------------------------------------

    def pay(
        self,
        gateway_order_id: str,
        amount_paise: int,
        method: str,
        vpa: str | None = None,
    ) -> PaymentResult:
        """Report the real state of the order. Never fabricates a capture.

        If Razorpay already shows the order paid (a human completed the hosted
        checkout), this returns `captured` with the real payment id. Otherwise it
        returns `pending`.
        """
        started = time.perf_counter()
        try:
            order = self.fetch_order(gateway_order_id)
        except httpx.HTTPError as exc:
            return PaymentResult(
                order_id=gateway_order_id,
                payment_id=None,
                status="failed",
                amount_paise=amount_paise,
                method=method,
                gateway=self.name,
                failure_code=FailureCode.GATEWAY_ERROR,
                failure_reason=f"razorpay api error: {exc}",
                retriable=True,
                latency_ms=(time.perf_counter() - started) * 1000,
            )

        if order.get("status") == "paid":
            payment_id = None
            try:
                payments = self._client.get(
                    f"{self.api_base}/orders/{gateway_order_id}/payments"
                ).json()
                items = payments.get("items") or []
                if items:
                    payment_id = items[0].get("id")
            except httpx.HTTPError:
                pass
            return PaymentResult(
                order_id=gateway_order_id,
                payment_id=payment_id,
                status="captured",
                amount_paise=amount_paise,
                method=method,
                gateway=self.name,
                latency_ms=(time.perf_counter() - started) * 1000,
            )

        return PaymentResult(
            order_id=gateway_order_id,
            payment_id=None,
            status="pending",
            amount_paise=amount_paise,
            method=method,
            gateway=self.name,
            failure_reason=(
                "order created in Razorpay test mode; capture requires hosted checkout "
                f"({order.get('checkout_url') or 'no payment link'})"
            ),
            retriable=True,
            latency_ms=(time.perf_counter() - started) * 1000,
        )

    def close(self) -> None:
        self._client.close()
