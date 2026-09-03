"""Reset shared state between benchmark arms.

Both arms transact against the same catalog singleton, so inventory sold in one
arm would otherwise be missing in the next and the comparison would drift. Every
arm starts from the catalog exactly as it is on disk.
"""

from __future__ import annotations

import json

from .catalog import CATALOG
from .config import SETTINGS
from .commerce import COMMERCE
from .models import Product


def reset_all(clear_audit: bool = False) -> None:
    raw = json.loads(CATALOG.path.read_text(encoding="utf-8"))
    with CATALOG._lock:  # noqa: SLF001 - deliberate: this is the catalog's own reset path
        CATALOG._products = {p["sku"]: Product(**p) for p in raw["products"]}

    COMMERCE.quotes.clear()
    COMMERCE.orders.clear()
    COMMERCE._by_gateway_id.clear()  # noqa: SLF001
    COMMERCE._idempotency.clear()    # noqa: SLF001

    from .payments import get_gateway
    from .payments.simulated import SimulatedRazorpayGateway
    from .server.legacy import _GATEWAY as LEGACY_GATEWAY
    from .server.legacy import _SESSIONS  # local import avoids a cycle at import time

    _SESSIONS.clear()

    for gateway in (get_gateway(), LEGACY_GATEWAY):
        if isinstance(gateway, SimulatedRazorpayGateway):
            gateway.reset_attempts()

    if clear_audit:
        from .audit import AUDIT

        AUDIT.clear()


def configure_payments(seed: int | None = None, failure_rate: float | None = None) -> None:
    """Retune the simulated gateway at runtime.

    Both arms hold their own gateway instance (the storefront checks out through
    its own, exactly as a separate web channel would), so a seed change has to
    reach both or the arms stop being comparable.
    """
    from .payments import get_gateway
    from .payments.simulated import SimulatedRazorpayGateway
    from .server.legacy import _GATEWAY as LEGACY_GATEWAY

    if seed is not None:
        SETTINGS.seed = seed
    if failure_rate is not None:
        SETTINGS.payment_failure_rate = failure_rate

    for gateway in (get_gateway(), LEGACY_GATEWAY):
        if isinstance(gateway, SimulatedRazorpayGateway):
            if seed is not None:
                gateway.seed = seed
            if failure_rate is not None:
                gateway.failure_rate = failure_rate
