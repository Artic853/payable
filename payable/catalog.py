"""Catalog: the structured, constraint-aware search surface.

The point of difference against a normal product search is that this one answers
in terms an agent can act on safely. Every candidate carries which constraints it
met, which it did not, and whether the misses are disqualifying -- and the
merchant raises an `advisory` when the top hits are close enough that picking one
would be a coin flip. That advisory is what lets a buyer agent abstain instead of
confidently buying the wrong thing.
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any

from .config import SETTINGS
from .models import (
    AdvisoryCode,
    Candidate,
    Merchant,
    Product,
    SearchRequest,
    SearchResponse,
    SpecConstraint,
)

_STOPWORDS = {
    "a", "an", "the", "for", "with", "and", "or", "of", "to", "i", "need", "want",
    "buy", "get", "me", "my", "please", "some", "that", "is", "it", "on", "in",
    "under", "below", "less", "than", "rs", "inr", "rupees", "good", "best",
}

# Ambiguity threshold: two fully-satisfying candidates this close are a coin flip.
AMBIGUITY_EPSILON = 0.05


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", text.lower()) if t and t not in _STOPWORDS}


def _field_value(product: Product, field: str) -> Any:
    """Resolve a constraint field against specs first, then product attributes."""
    if field in product.specs:
        return product.specs[field]
    if hasattr(product, field):
        return getattr(product, field)
    return None


def _check(constraint: SpecConstraint, value: Any) -> bool:
    op = constraint.op
    if value is None:
        return False
    try:
        if op == "eq":
            return str(value).lower() == str(constraint.value).lower()
        if op == "neq":
            return str(value).lower() != str(constraint.value).lower()
        if op == "gte":
            return float(value) >= float(constraint.value)
        if op == "lte":
            return float(value) <= float(constraint.value)
        if op == "contains":
            needle = str(constraint.value).lower()
            if isinstance(value, (list, tuple, set)):
                return any(needle in str(v).lower() for v in value)
            return needle in str(value).lower()
        if op == "not_contains":
            needle = str(constraint.value).lower()
            if isinstance(value, (list, tuple, set)):
                return all(needle not in str(v).lower() for v in value)
            return needle not in str(value).lower()
        if op == "true":
            return bool(value) is True
        if op == "false":
            return bool(value) is False
    except (TypeError, ValueError):
        return False
    return False


class Catalog:
    def __init__(self, path: Path | None = None):
        self.path = path or SETTINGS.catalog_path
        self._lock = threading.Lock()
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.merchant = Merchant(**raw["merchant"])
        self._products: dict[str, Product] = {
            p["sku"]: Product(**p) for p in raw["products"]
        }

    # -- reads -----------------------------------------------------------

    @property
    def products(self) -> list[Product]:
        return list(self._products.values())

    def get(self, sku: str) -> Product | None:
        return self._products.get(sku)

    def categories(self) -> list[str]:
        return sorted({p.category for p in self._products.values()})

    # -- inventory -------------------------------------------------------

    def reserve(self, sku: str, quantity: int) -> bool:
        """Decrement stock atomically; False if insufficient."""
        with self._lock:
            product = self._products.get(sku)
            if product is None or product.stock < quantity:
                return False
            product.stock -= quantity
            return True

    def release(self, sku: str, quantity: int) -> None:
        with self._lock:
            product = self._products.get(sku)
            if product is not None:
                product.stock += quantity

    # -- search ----------------------------------------------------------

    def search(self, request: SearchRequest) -> SearchResponse:
        query_tokens = _tokens(request.query)
        pool = self.products
        considered = 0
        scored: list[Candidate] = []

        for product in pool:
            if request.category and product.category != request.category:
                continue
            if request.in_stock_only and not product.in_stock:
                continue
            if request.max_price_paise is not None and product.price_paise > request.max_price_paise:
                continue
            considered += 1
            scored.append(self._score(product, query_tokens, request.constraints))

        scored.sort(key=lambda c: (c.fully_satisfies, c.match_score), reverse=True)
        top = scored[: max(1, request.limit)]

        advisory, advisory_code = self._advisory(top)
        return SearchResponse(
            query=request.query,
            candidates=top,
            total_considered=considered,
            advisory=advisory,
            advisory_code=advisory_code,
        )

    def _score(
        self,
        product: Product,
        query_tokens: set[str],
        constraints: list[SpecConstraint],
    ) -> Candidate:
        haystack = _tokens(
            " ".join([product.name, product.category, product.brand, " ".join(product.tags)])
        )
        haystack |= {str(v).lower() for v in product.specs.values() if not isinstance(v, (list, dict))}

        overlap = len(query_tokens & haystack)
        lexical = overlap / len(query_tokens) if query_tokens else 0.0

        met: list[str] = []
        unmet: list[str] = []
        unmet_hard: list[str] = []
        for constraint in constraints:
            ok = _check(constraint, _field_value(product, constraint.field))
            label = constraint.describe()
            if ok:
                met.append(label)
            else:
                unmet.append(label)
                if constraint.hard:
                    unmet_hard.append(label)

        satisfied_ratio = len(met) / len(constraints) if constraints else 1.0
        # Constraint satisfaction dominates lexical similarity: an agent should
        # not be nudged toward a keyword-similar item that fails the spec.
        score = round(0.75 * satisfied_ratio + 0.25 * lexical, 4)
        if unmet_hard:
            score = round(score * 0.35, 4)

        if unmet_hard:
            rationale = f"violates {len(unmet_hard)} hard constraint(s): {', '.join(unmet_hard)}"
        elif unmet:
            rationale = f"satisfies all hard constraints; misses soft: {', '.join(unmet)}"
        else:
            rationale = "satisfies every stated constraint"

        return Candidate(
            product=product,
            match_score=score,
            met_constraints=met,
            unmet_constraints=unmet,
            unmet_hard_constraints=unmet_hard,
            rationale=rationale,
        )

    def _advisory(self, top: list[Candidate]) -> tuple[str | None, AdvisoryCode]:
        if not top:
            return "No products matched the filters.", AdvisoryCode.EMPTY

        viable = [c for c in top if c.fully_satisfies]
        if not viable:
            return (
                "No candidate satisfies every hard constraint. "
                "Do not purchase without relaxing a constraint or escalating.",
                AdvisoryCode.NO_SATISFYING_CANDIDATE,
            )

        if len(viable) >= 2:
            best, runner = viable[0], viable[1]
            if abs(best.match_score - runner.match_score) <= AMBIGUITY_EPSILON:
                differing = _differing_specs(best.product, runner.product)
                if differing:
                    return (
                        f"{best.product.sku} and {runner.product.sku} both satisfy the stated "
                        f"constraints and differ on: {', '.join(sorted(differing))}. "
                        "Constrain further or escalate before purchasing.",
                        AdvisoryCode.AMBIGUOUS,
                    )
        return None, AdvisoryCode.NONE

    # -- discovery surface ----------------------------------------------

    def jsonld_feed(self, base_url: str) -> dict:
        return {
            "@context": "https://schema.org",
            "@type": "ItemList",
            "name": f"{self.merchant.display_name} catalog",
            "numberOfItems": len(self._products),
            "itemListElement": [
                {"@type": "ListItem", "position": i + 1, "item": p.jsonld(base_url, self.merchant)}
                for i, p in enumerate(self._products.values())
            ],
        }


def _differing_specs(a: Product, b: Product) -> set[str]:
    keys = set(a.specs) | set(b.specs)
    return {k for k in keys if a.specs.get(k) != b.specs.get(k)}


CATALOG = Catalog()
