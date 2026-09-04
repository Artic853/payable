"""Domain models for the transactability contract.

The contract is deliberately narrow: five verbs (search, describe, quote, order,
pay) plus a status read. Everything an agent needs to decide *and* to justify its
decision travels in these structures -- notably `unmet_constraints`, which is how
the merchant tells a buyer what it would be compromising on before it pays.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


# --------------------------------------------------------------------------
# Failure taxonomy
# --------------------------------------------------------------------------

class FailureCode(str, Enum):
    """Every unsuccessful buyer run terminates in exactly one of these."""

    NONE = "none"
    NO_MATCH = "no_match"                  # nothing in catalog plausibly satisfies intent
    AMBIGUOUS = "ambiguous"                # top candidates tie; agent refused to guess
    SPEC_MISMATCH = "spec_mismatch"        # best candidate violates a hard constraint
    OVER_BUDGET = "over_budget"            # cheapest satisfying item exceeds mandate cap
    OUT_OF_STOCK = "out_of_stock"
    QUOTE_EXPIRED = "quote_expired"
    MANDATE_REJECTED = "mandate_rejected"  # signature/scope/cap check failed
    PAYMENT_DECLINED = "payment_declined"
    GATEWAY_ERROR = "gateway_error"
    PARSE_ERROR = "parse_error"            # agent could not read the merchant surface
    TIMEOUT = "timeout"


TERMINAL_FAILURES = {c for c in FailureCode if c is not FailureCode.NONE}


# --------------------------------------------------------------------------
# Catalog
# --------------------------------------------------------------------------

class Merchant(BaseModel):
    id: str
    legal_name: str
    display_name: str
    gstin: str
    currency: str = "INR"
    country: str = "IN"
    razorpay_account_ref: str = ""
    support_email: str = ""
    return_window_days: int = 7
    ships_to: list[str] = Field(default_factory=lambda: ["IN"])


class Product(BaseModel):
    sku: str
    name: str
    category: str
    brand: str
    price_paise: int
    mrp_paise: int
    stock: int
    specs: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)

    @property
    def in_stock(self) -> bool:
        return self.stock > 0

    def jsonld(self, base_url: str, merchant: Merchant) -> dict:
        """schema.org/Product + Offer -- the discoverability surface."""
        return {
            "@context": "https://schema.org",
            "@type": "Product",
            "@id": f"{base_url}/products/{self.sku}",
            "sku": self.sku,
            "name": self.name,
            "brand": {"@type": "Brand", "name": self.brand},
            "category": self.category,
            "keywords": ", ".join(self.tags),
            "additionalProperty": [
                {"@type": "PropertyValue", "name": k, "value": v}
                for k, v in self.specs.items()
            ],
            "offers": {
                "@type": "Offer",
                "@id": f"{base_url}/products/{self.sku}#offer",
                "price": f"{self.price_paise / 100:.2f}",
                "priceCurrency": merchant.currency,
                "availability": (
                    "https://schema.org/InStock" if self.in_stock
                    else "https://schema.org/OutOfStock"
                ),
                "inventoryLevel": {"@type": "QuantitativeValue", "value": self.stock},
                "seller": {
                    "@type": "Organization",
                    "name": merchant.legal_name,
                    "identifier": merchant.gstin,
                },
                "acceptedPaymentMethod": ["UPI", "CreditCard", "DebitCard", "NetBanking"],
                "eligibleRegion": {"@type": "Country", "name": merchant.country},
                # The non-standard bit that makes the offer *actionable* rather
                # than merely readable: where an agent goes to transact on it.
                "potentialAction": {
                    "@type": "BuyAction",
                    "target": f"{base_url}/mcp",
                    "actionApplication": {"@type": "SoftwareApplication", "name": "payable-mcp"},
                },
            },
        }


class SpecConstraint(BaseModel):
    """One machine-checkable requirement extracted from a buyer intent."""

    field: str
    op: Literal["eq", "neq", "gte", "lte", "contains", "not_contains", "true", "false"]
    value: Any = None
    hard: bool = True   # hard constraints must hold; soft ones only rank

    def describe(self) -> str:
        if self.op in {"true", "false"}:
            return f"{self.field} is {self.op}"
        return f"{self.field} {self.op} {self.value!r}"


class SearchRequest(BaseModel):
    query: str = ""
    category: str | None = None
    max_price_paise: int | None = None
    constraints: list[SpecConstraint] = Field(default_factory=list)
    in_stock_only: bool = True
    limit: int = 5


class Candidate(BaseModel):
    """A ranked search hit, annotated with why it does or does not fit."""

    product: Product
    match_score: float
    met_constraints: list[str] = Field(default_factory=list)
    unmet_constraints: list[str] = Field(default_factory=list)
    unmet_hard_constraints: list[str] = Field(default_factory=list)
    rationale: str = ""

    @property
    def fully_satisfies(self) -> bool:
        return not self.unmet_hard_constraints


class AdvisoryCode(str, Enum):
    """Why the merchant thinks this result is unsafe to act on blindly.

    Distinguishing these matters: "two things fit and you have not chosen" and
    "nothing fits" both mean *do not buy*, but they are different failures and a
    buyer should report them differently.
    """

    NONE = "none"
    AMBIGUOUS = "ambiguous"                          # >1 candidate fits equally
    NO_SATISFYING_CANDIDATE = "no_satisfying_candidate"  # nothing clears the hard constraints
    EMPTY = "empty"                                  # nothing matched the filters at all


class SearchResponse(BaseModel):
    query: str
    candidates: list[Candidate]
    total_considered: int
    # Set when the merchant itself judges the result unsafe to act on blindly.
    advisory: str | None = None
    advisory_code: AdvisoryCode = AdvisoryCode.NONE


# --------------------------------------------------------------------------
# Mandate: the buyer's spending authorization
# --------------------------------------------------------------------------

class Mandate(BaseModel):
    """A signed, capped, expiring authorization for one agent to spend.

    The merchant verifies this before creating an order, so an agent cannot spend
    beyond what its principal authorized even if its reasoning goes wrong.
    """

    mandate_id: str
    principal: str                 # the human/org on whose behalf the agent acts
    agent_id: str
    max_amount_paise: int
    allowed_categories: list[str] = Field(default_factory=list)  # empty = any
    currency: str = "INR"
    issued_at: float
    expires_at: float
    alg: Literal["Ed25519", "HMAC-SHA256"] = "Ed25519"
    signature: str = ""

    def signing_payload(self) -> str:
        """Canonical bytes covered by the signature.

        `alg` is inside the payload so a mandate cannot be replayed under a
        different algorithm than the one its principal chose.
        """
        cats = ",".join(sorted(self.allowed_categories))
        return "|".join([
            self.mandate_id, self.principal, self.agent_id,
            str(self.max_amount_paise), cats, self.currency,
            f"{self.issued_at:.0f}", f"{self.expires_at:.0f}", self.alg,
        ])


# --------------------------------------------------------------------------
# Quote / order / payment
# --------------------------------------------------------------------------

class PriceBreakdown(BaseModel):
    unit_price_paise: int
    quantity: int
    subtotal_paise: int
    gst_rate: float
    gst_paise: int
    shipping_paise: int
    total_paise: int

    @property
    def total_rupees(self) -> float:
        return self.total_paise / 100


class QuoteRequest(BaseModel):
    sku: str
    quantity: int = 1
    ship_to_pincode: str = "500078"


class Quote(BaseModel):
    quote_id: str
    sku: str
    product_name: str
    quantity: int
    breakdown: PriceBreakdown
    currency: str = "INR"
    ship_to_pincode: str
    expires_at: float
    available: bool
    availability_note: str = ""


class OrderRequest(BaseModel):
    quote_id: str
    mandate: Mandate
    idempotency_key: str
    buyer_reference: str = ""


class Order(BaseModel):
    order_id: str                  # merchant-side id
    gateway_order_id: str          # Razorpay order id
    quote_id: str
    sku: str
    quantity: int
    amount_paise: int
    currency: str = "INR"
    status: Literal["created", "paid", "failed", "cancelled"] = "created"
    gateway: Literal["razorpay-api", "razorpay-simulated"] = "razorpay-simulated"
    checkout_url: str | None = None
    created_at: float = 0.0


class PaymentRequest(BaseModel):
    order_id: str
    method: Literal["upi", "card", "netbanking"] = "upi"
    vpa: str | None = "buyer@upi"
    mandate: Mandate | None = None


class PaymentResult(BaseModel):
    order_id: str
    payment_id: str | None
    status: Literal["captured", "failed", "pending"]
    amount_paise: int
    method: str
    gateway: str
    failure_code: FailureCode = FailureCode.NONE
    failure_reason: str = ""
    retriable: bool = False
    latency_ms: float = 0.0


# --------------------------------------------------------------------------
# Audit
# --------------------------------------------------------------------------

class AuditEvent(BaseModel):
    event_id: str
    run_id: str
    ts: float
    actor: Literal["buyer_agent", "merchant", "gateway", "policy"]
    step: str
    decision: str = ""
    rationale: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    latency_ms: float = 0.0
