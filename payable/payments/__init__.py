"""Gateway selection: real Razorpay when credentials exist, simulator otherwise."""

from __future__ import annotations

from ..config import SETTINGS
from .base import GatewayOrder, PaymentGateway
from .razorpay_api import RazorpayGateway
from .simulated import SimulatedRazorpayGateway

_GATEWAY: PaymentGateway | None = None


def get_gateway() -> PaymentGateway:
    global _GATEWAY
    if _GATEWAY is None:
        _GATEWAY = RazorpayGateway() if SETTINGS.razorpay_live else SimulatedRazorpayGateway()
    return _GATEWAY


def set_gateway(gateway: PaymentGateway) -> None:
    """Test seam."""
    global _GATEWAY
    _GATEWAY = gateway


def reset_gateway() -> None:
    """Drop the cached gateway so the next call re-selects from settings."""
    global _GATEWAY
    _GATEWAY = None


__all__ = [
    "GatewayOrder",
    "PaymentGateway",
    "RazorpayGateway",
    "SimulatedRazorpayGateway",
    "get_gateway",
    "reset_gateway",
    "set_gateway",
]
