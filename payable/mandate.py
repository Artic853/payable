"""Issue and verify agent spending mandates.

An agent never gets an open-ended right to pay. It carries a mandate signed by
its principal that caps the amount, scopes the categories, and expires. The
merchant verifies it server-side before an order exists, which means a
misreasoning agent fails closed rather than overspending.

The shared-secret HMAC here stands in for what would be an asymmetric,
registry-anchored credential in production (the merchant would hold the
principal's public key rather than a symmetric secret).
"""

from __future__ import annotations

import hashlib
import hmac
import time
import uuid

from .config import SETTINGS
from .models import FailureCode, Mandate


class MandateError(Exception):
    def __init__(self, code: FailureCode, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _sign(payload: str, secret: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def issue_mandate(
    principal: str,
    agent_id: str,
    max_amount_paise: int,
    allowed_categories: list[str] | None = None,
    ttl_seconds: int = 900,
    secret: str | None = None,
) -> Mandate:
    now = time.time()
    mandate = Mandate(
        mandate_id=f"mnd_{uuid.uuid4().hex[:16]}",
        principal=principal,
        agent_id=agent_id,
        max_amount_paise=max_amount_paise,
        allowed_categories=allowed_categories or [],
        issued_at=now,
        expires_at=now + ttl_seconds,
    )
    mandate.signature = _sign(mandate.signing_payload(), secret or SETTINGS.mandate_secret)
    return mandate


def verify_mandate(
    mandate: Mandate,
    amount_paise: int,
    category: str,
    secret: str | None = None,
) -> None:
    """Raise MandateError unless this mandate authorizes this exact spend."""
    expected = _sign(mandate.signing_payload(), secret or SETTINGS.mandate_secret)
    if not hmac.compare_digest(expected, mandate.signature or ""):
        raise MandateError(FailureCode.MANDATE_REJECTED, "mandate signature invalid")

    if time.time() > mandate.expires_at:
        raise MandateError(FailureCode.MANDATE_REJECTED, "mandate expired")

    if amount_paise > mandate.max_amount_paise:
        raise MandateError(
            FailureCode.OVER_BUDGET,
            f"amount {amount_paise} exceeds mandate cap {mandate.max_amount_paise}",
        )

    if mandate.allowed_categories and category not in mandate.allowed_categories:
        raise MandateError(
            FailureCode.MANDATE_REJECTED,
            f"category {category!r} outside mandate scope {mandate.allowed_categories}",
        )
