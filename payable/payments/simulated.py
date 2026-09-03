"""Deterministic Razorpay simulator.

Mirrors the shapes of the Razorpay Orders/Payments API so the buyer agent code
path is identical whether or not credentials are present, and injects realistic
failures at a configurable rate. Declines are keyed on a hash of the purchase and
the run seed, so a benchmark is reproducible: the same seed produces the same
declines at the same points in every arm, which is what makes an A/B between two
merchant surfaces meaningful.
"""

from __future__ import annotations

import hashlib
import time
import uuid

from ..config import SETTINGS
from ..models import FailureCode, PaymentResult
from .base import GatewayOrder

# Modelled on the decline reasons Razorpay actually surfaces.
_DECLINES = [
    ("BAD_REQUEST_ERROR", "payment failed: insufficient funds in payer account", True),
    ("GATEWAY_ERROR", "upstream PSP timed out before confirmation", True),
    ("BAD_REQUEST_ERROR", "UPI collect request expired without payer approval", True),
    ("BAD_REQUEST_ERROR", "payment declined by issuing bank risk check", False),
]


def _unit_interval(*parts: str) -> float:
    digest = hashlib.sha256("|".join(parts).encode()).hexdigest()
    return int(digest[:8], 16) / 0xFFFFFFFF


class SimulatedRazorpayGateway:
    name = "razorpay-simulated"

    def __init__(self, failure_rate: float | None = None, seed: int | None = None):
        self.failure_rate = SETTINGS.payment_failure_rate if failure_rate is None else failure_rate
        self.seed = SETTINGS.seed if seed is None else seed
        self._orders: dict[str, GatewayOrder] = {}
        # attempts per purchase, so a second try at the same basket re-rolls
        self._attempts: dict[str, int] = {}

    def create_order(
        self,
        amount_paise: int,
        currency: str,
        receipt: str,
        notes: dict[str, str] | None = None,
    ) -> GatewayOrder:
        order_id = f"order_SIM{uuid.uuid4().hex[:14]}"
        order = GatewayOrder(
            id=order_id,
            entity="order",
            amount=amount_paise,
            amount_paid=0,
            amount_due=amount_paise,
            currency=currency,
            receipt=receipt,
            status="created",
            attempts=0,
            notes=notes or {},
            created_at=int(time.time()),
            checkout_url=f"{SETTINGS.base_url}/checkout/{order_id}",
        )
        self._orders[order_id] = order
        return order

    def pay(
        self,
        gateway_order_id: str,
        amount_paise: int,
        method: str,
        vpa: str | None = None,
    ) -> PaymentResult:
        started = time.perf_counter()
        order = self._orders.get(gateway_order_id)
        if order is None:
            return PaymentResult(
                order_id=gateway_order_id,
                payment_id=None,
                status="failed",
                amount_paise=amount_paise,
                method=method,
                gateway=self.name,
                failure_code=FailureCode.GATEWAY_ERROR,
                failure_reason="unknown order id",
                retriable=False,
                latency_ms=(time.perf_counter() - started) * 1000,
            )

        order["attempts"] = order.get("attempts", 0) + 1

        # The decline roll is keyed on the *purchase*, not on the randomly
        # generated order id: same SKU, same amount, same attempt number => same
        # outcome in every arm of the benchmark. Without this, one arm could look
        # better than another purely because it drew luckier payment failures.
        #
        # The attempt count is tracked per purchase rather than per gateway order
        # because the two arms retry differently: the payable buyer re-pays one
        # order, while the storefront builds a fresh cart and order each time.
        # Keying on the order would freeze the storefront on its first roll and
        # make recovery impossible for it alone.
        purchase_key = str(order.get("notes", {}).get("sku") or order.get("receipt", gateway_order_id))
        attempt = self._attempts.get(purchase_key, 0) + 1
        self._attempts[purchase_key] = attempt
        roll_parts = (purchase_key, str(amount_paise), str(attempt), str(self.seed))

        # Simulate PSP round-trip latency; UPI collect is slower than card auth.
        base_latency = 0.34 if method == "upi" else 0.21
        jitter = _unit_interval(*roll_parts, "latency") * 0.4
        time.sleep(min(base_latency + jitter, 0.9) * 0.15)  # scaled down for demo runtime

        roll = _unit_interval(*roll_parts)

        if roll < self.failure_rate:
            idx = int(_unit_interval(*roll_parts, "which") * len(_DECLINES))
            code, reason, retriable = _DECLINES[min(idx, len(_DECLINES) - 1)]
            order["status"] = "attempted"
            return PaymentResult(
                order_id=gateway_order_id,
                payment_id=f"pay_SIM{uuid.uuid4().hex[:14]}",
                status="failed",
                amount_paise=amount_paise,
                method=method,
                gateway=self.name,
                failure_code=(
                    FailureCode.GATEWAY_ERROR if code == "GATEWAY_ERROR"
                    else FailureCode.PAYMENT_DECLINED
                ),
                failure_reason=reason,
                retriable=retriable,
                latency_ms=(time.perf_counter() - started) * 1000,
            )

        order["status"] = "paid"
        order["amount_paid"] = amount_paise
        order["amount_due"] = 0
        return PaymentResult(
            order_id=gateway_order_id,
            payment_id=f"pay_SIM{uuid.uuid4().hex[:14]}",
            status="captured",
            amount_paise=amount_paise,
            method=method,
            gateway=self.name,
            latency_ms=(time.perf_counter() - started) * 1000,
        )

    def reset_attempts(self) -> None:
        """Clear per-purchase retry history so the next run starts clean."""
        self._attempts.clear()

    def fetch_order(self, gateway_order_id: str) -> GatewayOrder:
        return self._orders.get(gateway_order_id, GatewayOrder(id=gateway_order_id, status="unknown"))
