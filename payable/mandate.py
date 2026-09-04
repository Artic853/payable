"""Issue and verify agent spending mandates.

An agent never gets an open-ended right to pay. It carries a mandate signed by
its principal that caps the amount, scopes the categories, and expires. The
merchant verifies it before an order exists, so a misreasoning agent fails closed
rather than overspending.

Signing is **Ed25519**: the principal holds the private key, the merchant holds
only the public key. That asymmetry is the point -- a merchant compromise cannot
mint mandates, because the merchant never had the power to sign one. A symmetric
HMAC fallback exists for environments without `cryptography`, but it is dev-only
and the merchant refuses it for any principal that has a registered public key
(see `_reject_downgrade`), so a leaked shared secret cannot be used to forge
spending authority for a real principal.

`PrincipalKeyring` stands in for what would be a public-key registry -- an
issuer directory, or the principal's bank under a scheme like NPCI's UAP.
"""

from __future__ import annotations

import hashlib
import hmac
import time
import uuid

from .config import SETTINGS
from .models import FailureCode, Mandate

try:  # pragma: no cover - exercised by whichever branch the environment takes
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )

    ED25519_AVAILABLE = True
except ImportError:  # pragma: no cover
    ED25519_AVAILABLE = False

ALG_ED25519 = "Ed25519"
ALG_HMAC = "HMAC-SHA256"


class MandateError(Exception):
    def __init__(self, code: FailureCode, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# --------------------------------------------------------------------------
# Key registry
# --------------------------------------------------------------------------

class PrincipalKeyring:
    """Maps a principal to its signing key.

    The merchant side only ever needs `public_key_for`. Private keys live here
    solely because the demo runs both parties in one process; in production the
    principal's key never touches the merchant's machine.
    """

    def __init__(self) -> None:
        self._private: dict[str, object] = {}
        self._public: dict[str, bytes] = {}

    def enrol(self, principal: str) -> str:
        """Generate a keypair for a principal and register the public half."""
        if not ED25519_AVAILABLE:
            raise RuntimeError("Ed25519 unavailable; install `cryptography`")
        private = Ed25519PrivateKey.generate()
        self._private[principal] = private
        public_bytes = private.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        self._public[principal] = public_bytes
        return public_bytes.hex()

    def register_public_key(self, principal: str, public_key_hex: str) -> None:
        """Merchant-side enrolment: trust this principal's public key."""
        self._public[principal] = bytes.fromhex(public_key_hex)

    def forget(self, principal: str) -> None:
        self._private.pop(principal, None)
        self._public.pop(principal, None)

    def private_key_for(self, principal: str, create: bool = True):
        if principal not in self._private and create:
            self.enrol(principal)
        return self._private.get(principal)

    def public_key_for(self, principal: str):
        raw = self._public.get(principal)
        if raw is None or not ED25519_AVAILABLE:
            return None
        return Ed25519PublicKey.from_public_bytes(raw)

    def knows(self, principal: str) -> bool:
        return principal in self._public

    def export_public_keys(self) -> dict[str, str]:
        return {p: raw.hex() for p, raw in self._public.items()}

    def clear(self) -> None:
        self._private.clear()
        self._public.clear()


KEYRING = PrincipalKeyring()


# --------------------------------------------------------------------------
# Signing
# --------------------------------------------------------------------------

def _hmac_sign(payload: str, secret: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def issue_mandate(
    principal: str,
    agent_id: str,
    max_amount_paise: int,
    allowed_categories: list[str] | None = None,
    ttl_seconds: int = 900,
    secret: str | None = None,
    alg: str | None = None,
    keyring: PrincipalKeyring | None = None,
) -> Mandate:
    """Mint a signed mandate. Defaults to Ed25519 when it is available."""
    keyring = keyring or KEYRING
    alg = alg or (ALG_ED25519 if ED25519_AVAILABLE else ALG_HMAC)

    now = time.time()
    mandate = Mandate(
        mandate_id=f"mnd_{uuid.uuid4().hex[:16]}",
        principal=principal,
        agent_id=agent_id,
        max_amount_paise=max_amount_paise,
        allowed_categories=allowed_categories or [],
        issued_at=now,
        expires_at=now + ttl_seconds,
        alg=alg,
    )

    payload = mandate.signing_payload()
    if alg == ALG_ED25519:
        private = keyring.private_key_for(principal)
        mandate.signature = private.sign(payload.encode()).hex()  # type: ignore[union-attr]
    else:
        mandate.signature = _hmac_sign(payload, secret or SETTINGS.mandate_secret)
    return mandate


# --------------------------------------------------------------------------
# Verification
# --------------------------------------------------------------------------

def _reject_downgrade(mandate: Mandate, keyring: PrincipalKeyring) -> None:
    """Refuse a symmetric mandate for a principal that has enrolled a public key.

    Without this the whole asymmetric scheme is decorative: anyone holding the
    shared HMAC secret could sign for any principal, and the default secret is
    printed in `.env.example`.
    """
    if mandate.alg != ALG_ED25519 and keyring.knows(mandate.principal):
        raise MandateError(
            FailureCode.MANDATE_REJECTED,
            f"principal {mandate.principal!r} requires {ALG_ED25519}; "
            f"refusing a {mandate.alg} mandate",
        )


def _verify_signature(mandate: Mandate, secret: str | None, keyring: PrincipalKeyring) -> None:
    payload = mandate.signing_payload()

    if mandate.alg == ALG_ED25519:
        if not ED25519_AVAILABLE:
            raise MandateError(
                FailureCode.MANDATE_REJECTED,
                "Ed25519 mandate presented but `cryptography` is not installed",
            )
        public = keyring.public_key_for(mandate.principal)
        if public is None:
            raise MandateError(
                FailureCode.MANDATE_REJECTED,
                f"no registered public key for principal {mandate.principal!r}",
            )
        try:
            public.verify(bytes.fromhex(mandate.signature or ""), payload.encode())
        except (InvalidSignature, ValueError) as exc:
            raise MandateError(
                FailureCode.MANDATE_REJECTED, "mandate signature invalid"
            ) from exc
        return

    expected = _hmac_sign(payload, secret or SETTINGS.mandate_secret)
    if not hmac.compare_digest(expected, mandate.signature or ""):
        raise MandateError(FailureCode.MANDATE_REJECTED, "mandate signature invalid")


def verify_mandate(
    mandate: Mandate,
    amount_paise: int,
    category: str,
    secret: str | None = None,
    keyring: PrincipalKeyring | None = None,
) -> None:
    """Raise MandateError unless this mandate authorizes this exact spend."""
    keyring = keyring or KEYRING

    _reject_downgrade(mandate, keyring)
    _verify_signature(mandate, secret, keyring)

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
