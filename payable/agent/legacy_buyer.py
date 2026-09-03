"""Buyer agent against the plain HTML storefront -- the benchmark control.

This is a competent scraper, not a strawman: it follows the real purchase path
(search -> product page -> cart -> checkout form), parses the Indian-format
prices correctly, reads the spec bullets, normalises labels through a synonym
table, coerces units and Yes/No values, and honours the same budget and retry
policy as the payable arm.

What it cannot do is verify with certainty. Prose specs lose type information --
"Brown (tactile)" is not `brown-tactile`, "In stock" is not a quantity, and a
label the synonym table misses is simply a field the agent cannot resolve. Each
unresolvable hard constraint becomes an *unverifiable* one, and what the agent
does then is the whole point of the experiment:

    strict policy     -> abstain (safe, but the sale is lost)
    optimistic policy -> assume it holds and buy (the sale happens, sometimes
                         for the wrong item, with real money)

The transactability layer's claim is that it removes the need to choose.
"""

from __future__ import annotations

import html
import re
from typing import Any

from ..audit import AUDIT
from ..models import FailureCode, SpecConstraint
from .base import (
    BuyerIntent,
    BuyerPolicy,
    RunContext,
    RunResult,
    new_result,
    since,
    stage_timer,
)

ARM = "legacy"
TOP_K_PAGES = 4

_CARD_RE = re.compile(r"<a href='/legacy/product/([A-Z0-9\-]+)'>(.*?)</a>", re.S)
# Listing cards print "Brand &middot; category" under the title; a competent
# scraper reads it and filters, so this one does too.
_CARD_CATEGORY_RE = re.compile(
    r"<a href='/legacy/product/([A-Z0-9\-]+)'>.*?</a>.*?<div class='muted'>[^<]*?&middot;\s*([a-z]+)</div>",
    re.S,
)
_SPEC_RE = re.compile(r"<li><b>(.*?):</b>\s*(.*?)</li>", re.S)
_PRICE_RE = re.compile(r"<div class='price'>&#8377;([\d,]+)")
_STOCK_RE = re.compile(r"<div class='stock(?: out)?'>(.*?)</div>")
_CSRF_RE = re.compile(r"name='csrf_token' value='([a-f0-9]+)'")
_SESSION_RE = re.compile(r"name='session_id' value='(cart_[a-f0-9]+)'")
_TOTAL_RE = re.compile(r"<td><b>Order total</b></td><td class='r'><b>&#8377;([\d,]+)</b></td>")
_ORDER_RE = re.compile(r"Order ID: <b>(.*?)</b>")
_PAYMENT_RE = re.compile(r"Payment ID: <b>(.*?)</b>")

# What a well-built scraper would carry: the obvious label-to-field mappings.
# It is not exhaustive, because no scraper's ever is -- the merchant never told
# it what the field names are.
_LABEL_SYNONYMS = {
    "layout": "layout",
    "switches": "switch_type",
    "switch type": "switch_type",
    "connectivity": "connectivity",
    "hot-swappable": "hot_swappable",
    "backlight": "backlight",
    "keycaps": "keycap_profile",
    "battery": "battery_mah",
    "playback": "battery_hours",
    "weight": "weight_g",
    "warranty": "warranty_months",
    "max dpi": "dpi_max",
    "buttons": "buttons",
    "grip style": "grip",
    "handedness": "hand",
    "polling rate": "polling_hz",
    "form factor": "form",
    "codecs": "codecs",
    "microphone": "mic",
    "impedance": "impedance_ohm",
    "screen size": "size_inch",
    "resolution": "resolution",
    "panel": "panel",
    "refresh rate": "refresh_hz",
    "ports": "ports",
    "hdr": "hdr",
    "connector": "connector",
    "length": "length_m",
    "data rate": "data_gbps",
}

_STOPWORDS = {
    "a", "an", "the", "for", "with", "and", "or", "of", "to", "i", "need", "want",
    "buy", "get", "me", "my", "please", "some", "that", "is", "it", "on", "in",
    "under", "below", "less", "than", "rs", "inr", "rupees", "good", "best", "budget",
}


def _keywords(brief: str, limit: int = 4) -> str:
    seen: list[str] = []
    for token in re.findall(r"[a-z0-9]+", brief.lower()):
        if token in _STOPWORDS or len(token) < 3 or token in seen:
            continue
        seen.append(token)
        if len(seen) >= limit:
            break
    return " ".join(seen)


def _rupees_to_paise(text: str) -> int:
    return int(text.replace(",", "")) * 100


def _coerce(raw: str) -> Any:
    """Best-effort typing of a prose spec value."""
    value = html.unescape(raw).strip()
    low = value.lower()
    if low == "yes":
        return True
    if low == "no":
        return False
    number = re.match(r"^([\d.]+)\s*[a-zA-Z]*$", value)
    if number:
        try:
            as_float = float(number.group(1))
            return int(as_float) if as_float.is_integer() else as_float
        except ValueError:
            pass
    if "," in value:
        return [part.strip().lower() for part in value.split(",")]
    return low


def _parse_specs(page: str) -> dict[str, Any]:
    specs: dict[str, Any] = {}
    for label, raw in _SPEC_RE.findall(page):
        clean = html.unescape(label).strip().lower()
        field = _LABEL_SYNONYMS.get(clean)
        if field is None:
            # Keep it under a normalised name so the agent at least has the data,
            # even though it will not line up with the constraint's field name.
            field = re.sub(r"[^a-z0-9]+", "_", clean).strip("_")
        specs[field] = _coerce(raw)
    return specs


def _check(constraint: SpecConstraint, value: Any) -> bool:
    op, target = constraint.op, constraint.value
    try:
        if op == "eq":
            return str(value).lower() == str(target).lower()
        if op == "neq":
            return str(value).lower() != str(target).lower()
        if op == "gte":
            return float(value) >= float(target)
        if op == "lte":
            return float(value) <= float(target)
        if op == "contains":
            needle = str(target).lower()
            if isinstance(value, (list, tuple)):
                return any(needle in str(v).lower() for v in value)
            return needle in str(value).lower()
        if op == "not_contains":
            needle = str(target).lower()
            if isinstance(value, (list, tuple)):
                return all(needle not in str(v).lower() for v in value)
            return needle not in str(value).lower()
        if op == "true":
            return value is True
        if op == "false":
            return value is False
    except (TypeError, ValueError):
        return False
    return False


class LegacyBuyer:
    def __init__(self, client: Any, policy: BuyerPolicy | None = None):
        self.client = client
        self.policy = policy or BuyerPolicy.strict()

    @property
    def arm(self) -> str:
        return f"{ARM}-{self.policy.name}"

    def _get(self, ctx: RunContext, url: str) -> str:
        ctx.http_calls += 1
        return self.client.get(url).text

    def _post(self, ctx: RunContext, url: str, data: dict) -> Any:
        ctx.http_calls += 1
        return self.client.post(url, data=data)

    # -- run -------------------------------------------------------------

    def run(self, intent: BuyerIntent, mandate: Any = None) -> RunResult:
        ctx = RunContext()
        AUDIT.record(
            ctx.run_id, "buyer_agent", "run_start",
            decision=self.arm, rationale=intent.brief,
            payload={"policy": self.policy.name, **intent.agent_view()},
        )

        # 1. discover -----------------------------------------------------
        mark = stage_timer()
        listing = self._get(ctx, f"/legacy/?q={_keywords(intent.brief).replace(' ', '+')}")
        skus = self._skus_from(listing, intent.category)
        if not skus:
            # Retry against the unfiltered catalog before giving up -- a real
            # scraper would, and it keeps the comparison fair.
            listing = self._get(ctx, "/legacy/")
            skus = self._skus_from(listing, intent.category)
        ctx.stages.discover_ms = since(mark)

        if not skus:
            return self._stop(ctx, intent, "failed", FailureCode.PARSE_ERROR, "no products parsed from storefront")

        # 2. select -------------------------------------------------------
        mark = stage_timer()
        assessments = []
        for sku in skus[:TOP_K_PAGES]:
            page = self._get(ctx, f"/legacy/product/{sku}")
            assessment = self._assess(sku, page, intent)
            if assessment:
                assessments.append(assessment)

        if not assessments:
            ctx.stages.select_ms = since(mark)
            return self._stop(ctx, intent, "failed", FailureCode.PARSE_ERROR, "no product page could be parsed")

        in_stock = [a for a in assessments if a["in_stock"]]
        if not in_stock:
            ctx.stages.select_ms = since(mark)
            return self._stop(ctx, intent, "abstained", FailureCode.OUT_OF_STOCK, "no candidate shown as available")

        satisfying = [a for a in in_stock if not a["unmet"] and not a["unverifiable"]]
        if satisfying:
            pool = satisfying
        elif self.policy.buy_when_unverifiable:
            # Optimistic: treat what it could not check as satisfied. This is
            # exactly where wrong-item purchases come from.
            pool = [a for a in in_stock if not a["unmet"]]
            if pool:
                ctx.note(
                    "proceeding on unverifiable constraints: "
                    + "; ".join(sorted({c for a in pool for c in a["unverifiable"]}))
                )
        else:
            pool = []

        if not pool:
            ctx.stages.select_ms = since(mark)
            blocker = in_stock[0]
            reason = (
                f"{blocker['sku']}: unmet {blocker['unmet']}" if blocker["unmet"]
                else f"could not verify {blocker['unverifiable']} from the product page"
            )
            code = FailureCode.SPEC_MISMATCH if blocker["unmet"] else FailureCode.PARSE_ERROR
            return self._stop(ctx, intent, "abstained", code, reason)

        chosen = min(pool, key=lambda a: (-a["met_count"], a["price_paise"]))
        ctx.stages.select_ms = since(mark)

        AUDIT.record(
            ctx.run_id, "buyer_agent", "select",
            decision=chosen["sku"],
            rationale=(
                f"met {chosen['met_count']} constraint(s); "
                f"unverifiable={chosen['unverifiable']}"
            ),
            payload={"considered": [a["sku"] for a in assessments], "price_paise": chosen["price_paise"]},
        )

        # 3. cart (this is where the true total finally appears) -----------
        mark = stage_timer()
        cart_page = self._post(ctx, "/legacy/cart", {
            "sku": chosen["sku"],
            "quantity": str(intent.quantity),
            "csrf_token": chosen["csrf"],
        }).text

        session_match = _SESSION_RE.search(cart_page)
        total_match = _TOTAL_RE.search(cart_page)
        ctx.stages.quote_ms = since(mark)

        if not session_match or not total_match:
            return self._stop(ctx, intent, "failed", FailureCode.PARSE_ERROR, "could not read cart session or total")

        total_paise = _rupees_to_paise(total_match.group(1))
        if total_paise > intent.budget_paise:
            return self._stop(
                ctx, intent, "abstained", FailureCode.OVER_BUDGET,
                f"cart total {total_paise} exceeds budget {intent.budget_paise}",
            )

        # 4/5. checkout + payment in one form post -------------------------
        mark = stage_timer()
        confirmation = ""
        for attempt in range(1, self.policy.max_payment_attempts + 1):
            ctx.payment_attempts = attempt
            response = self._post(ctx, "/legacy/checkout", {
                "session_id": session_match.group(1),
                "csrf_token": chosen["csrf"],
                "full_name": "Demo Buyer",
                "email": "buyer@example.com",
                "pincode": "500078",
                "method": "upi" if attempt == 1 else "card",
            })
            confirmation = response.text
            if "your order is confirmed" in confirmation.lower():
                break
            # A sold-out or malformed checkout is not a payment decline; only
            # count the ones the gateway actually refused.
            if "payment failed" in confirmation.lower():
                ctx.payment_declines += 1
            if "Please try again" not in confirmation:
                ctx.note(f"attempt {attempt}: non-retriable decline")
                break
            ctx.note(f"attempt {attempt}: retriable decline; switching method")
            # The cart session is consumed by a failed checkout; re-create it.
            cart_page = self._post(ctx, "/legacy/cart", {
                "sku": chosen["sku"], "quantity": str(intent.quantity), "csrf_token": chosen["csrf"],
            }).text
            retry_session = _SESSION_RE.search(cart_page)
            if not retry_session:
                break
            session_match = retry_session
        ctx.stages.pay_ms = since(mark)

        if "your order is confirmed" not in confirmation.lower():
            code = (
                FailureCode.OUT_OF_STOCK if "out of stock" in confirmation.lower()
                else FailureCode.PAYMENT_DECLINED
            )
            return self._stop(ctx, intent, "failed", code, "checkout did not confirm")

        order_match = _ORDER_RE.search(confirmation)
        payment_match = _PAYMENT_RE.search(confirmation)
        result = new_result(
            self.arm, intent, ctx,
            outcome="purchased",
            purchased_sku=chosen["sku"],
            amount_paise=total_paise,
            order_id=order_match.group(1) if order_match else None,
            payment_id=payment_match.group(1) if payment_match else None,
        )
        AUDIT.record(
            ctx.run_id, "buyer_agent", "run_complete",
            decision="purchased",
            rationale=f"{chosen['sku']} for {total_paise / 100:.2f} INR",
            payload={"intent": intent.brief, "arm": self.arm},
            latency_ms=ctx.elapsed_ms,
        )
        return result

    # -- helpers ---------------------------------------------------------

    def _skus_from(self, listing: str, category: str | None) -> list[str]:
        """SKUs in listing order, narrowed to the wanted category when known."""
        ordered = [sku for sku, _ in _CARD_RE.findall(listing)]
        if not category:
            return ordered
        categories = dict(_CARD_CATEGORY_RE.findall(listing))
        narrowed = [sku for sku in ordered if categories.get(sku) == category]
        # If the category text could not be read, keep the unfiltered list rather
        # than silently returning nothing.
        return narrowed or ordered

    def _assess(self, sku: str, page: str, intent: BuyerIntent) -> dict | None:
        price_match = _PRICE_RE.search(page)
        stock_match = _STOCK_RE.search(page)
        csrf_match = _CSRF_RE.search(page)
        if not price_match:
            return None

        specs = _parse_specs(page)
        stock_phrase = (stock_match.group(1) if stock_match else "").lower()

        met: list[str] = []
        unmet: list[str] = []
        unverifiable: list[str] = []
        for constraint in intent.constraints:
            if constraint.field == "price_paise":
                value: Any = _rupees_to_paise(price_match.group(1))
            elif constraint.field in specs:
                value = specs[constraint.field]
            else:
                unverifiable.append(constraint.describe())
                continue
            (met if _check(constraint, value) else unmet).append(constraint.describe())

        return {
            "sku": sku,
            "price_paise": _rupees_to_paise(price_match.group(1)),
            "in_stock": "unavailable" not in stock_phrase,
            "csrf": csrf_match.group(1) if csrf_match else "",
            "specs": specs,
            "met": met,
            "unmet": unmet,
            "unverifiable": unverifiable,
            "met_count": len(met),
        }

    def _stop(
        self,
        ctx: RunContext,
        intent: BuyerIntent,
        outcome: str,
        code: FailureCode,
        reason: str,
    ) -> RunResult:
        if outcome == "abstained":
            AUDIT.record(
                ctx.run_id, "policy", "abstain",
                decision=code.value, rationale=reason, payload={"policy": self.policy.name},
            )
        AUDIT.record(
            ctx.run_id, "buyer_agent", "run_complete",
            decision=outcome, rationale=reason,
            payload={"intent": intent.brief, "arm": self.arm, "failure_code": code.value},
            latency_ms=ctx.elapsed_ms,
        )
        return new_result(
            self.arm, intent, ctx,
            outcome=outcome,  # type: ignore[arg-type]
            failure_code=code,
            abstain_reason=reason,
        )
