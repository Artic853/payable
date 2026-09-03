"""Optional LLM front-end: natural language brief -> machine-checkable constraints.

The benchmark runs on a deterministic planner by default, because a benchmark
whose numbers move when a model is sampled is not measuring the merchant surface
any more. This module is what a real buyer agent puts in front of it: the model
turns a human sentence into typed constraints, and everything downstream --
merchant verification, mandate cap, audit trail -- is unchanged.

Enable with PAYABLE_USE_LLM=1 and an ANTHROPIC_API_KEY. Without either, or if the
call fails, `extract_constraints` returns the deterministic keyword extraction so
no caller has to branch.
"""

from __future__ import annotations

import json
import re

from ..config import SETTINGS
from ..models import SpecConstraint

_SYSTEM = """You convert a shopper's request into machine-checkable constraints for a \
consumer-electronics catalog.

Return ONLY a JSON object: {"category": <string|null>, "constraints": [...]}

Each constraint is {"field": str, "op": str, "value": any, "hard": bool}.
Ops: eq, neq, gte, lte, contains, not_contains, true, false ("true"/"false" take no value).

Rules:
- `field` must be a spec key the catalog would plausibly use (snake_case), e.g.
  switch_type, layout, connectivity, anc, anc_depth_db, refresh_hz, resolution,
  battery_hours, power_delivery_w, silent_click, hot_swappable, price_paise.
- Mark a constraint hard only when the shopper would reject the product without it.
  Preferences ("ideally", "would be nice") are soft.
- Prices are in paise: 1 rupee = 100 paise.
- Do not invent constraints the shopper did not state."""

# Field guesses for the offline path, keyed by phrases that actually appear in
# shopping language.
_PATTERNS: list[tuple[str, SpecConstraint]] = [
    (r"\btkl\b|\btenkeyless\b", SpecConstraint(field="layout", op="eq", value="TKL-87")),
    (r"\bfull[- ]?size\b|\bnumpad\b", SpecConstraint(field="layout", op="eq", value="full-104")),
    (r"\btactile\b|\bbrown switch", SpecConstraint(field="switch_type", op="eq", value="brown-tactile")),
    (r"\blinear\b|\bred switch", SpecConstraint(field="switch_type", op="eq", value="red-linear")),
    (r"\bhot[- ]?swap", SpecConstraint(field="hot_swappable", op="true")),
    (r"\bbluetooth\b|\bwireless\b", SpecConstraint(field="connectivity", op="contains", value="bluetooth")),
    (r"\bwired\b", SpecConstraint(field="connectivity", op="contains", value="wired")),
    (r"\banc\b|\bnoise[- ]cancel", SpecConstraint(field="anc", op="true")),
    (r"\bover[- ]ear\b", SpecConstraint(field="form", op="contains", value="over-ear")),
    (r"\bearbuds?\b|\btws\b|\bin[- ]ear\b", SpecConstraint(field="form", op="contains", value="in-ear")),
    (r"\bldac\b", SpecConstraint(field="codecs", op="contains", value="ldac")),
    (r"\bsilent\b|\bquiet click", SpecConstraint(field="silent_click", op="true")),
    (r"\bvertical\b", SpecConstraint(field="grip", op="eq", value="vertical")),
    (r"\b4k\b|\buhd\b", SpecConstraint(field="resolution", op="eq", value="3840x2160")),
    (r"\bqhd\b|\b1440p\b", SpecConstraint(field="resolution", op="eq", value="2560x1440")),
]

_CATEGORY_HINTS = {
    "keyboard": ["keyboard", "keycaps", "switches", "tkl", "numpad"],
    "mouse": ["mouse", "mice", "dpi", "grip"],
    "headphones": ["headphone", "headset", "earbuds", "anc", "tws", "audio"],
    "monitor": ["monitor", "display", "screen", "hz", "4k", "qhd"],
    "dock": ["dock", "docking", "hub"],
    "cable": ["cable", "cord", "lead"],
}


def guess_category(brief: str) -> str | None:
    low = brief.lower()
    best, best_hits = None, 0
    for category, hints in _CATEGORY_HINTS.items():
        hits = sum(1 for h in hints if h in low)
        if hits > best_hits:
            best, best_hits = category, hits
    return best


def _deterministic(brief: str) -> tuple[str | None, list[SpecConstraint]]:
    low = brief.lower()
    constraints = [c.model_copy() for pattern, c in _PATTERNS if re.search(pattern, low)]

    budget = re.search(r"(?:under|below|less than|upto|up to)\s*(?:rs\.?|inr|₹)?\s*([\d,]+)", low)
    if budget:
        rupees = int(budget.group(1).replace(",", ""))
        constraints.append(SpecConstraint(field="price_paise", op="lte", value=rupees * 100))

    return guess_category(brief), constraints


def extract_constraints(brief: str) -> tuple[str | None, list[SpecConstraint]]:
    """Return (category, constraints). Falls back to the offline extractor."""
    if not SETTINGS.llm_live:
        return _deterministic(brief)

    try:
        import anthropic

        client = anthropic.Anthropic(api_key=SETTINGS.anthropic_api_key)
        message = client.messages.create(
            model=SETTINGS.llm_model,
            max_tokens=1024,
            system=_SYSTEM,
            messages=[{"role": "user", "content": brief}],
        )
        text = "".join(block.text for block in message.content if block.type == "text")
        payload = json.loads(_strip_fence(text))
        constraints = [SpecConstraint(**c) for c in payload.get("constraints", [])]
        if not constraints:
            return _deterministic(brief)
        return payload.get("category"), constraints
    except Exception:
        # A model outage must not change what the merchant surface is measured on.
        return _deterministic(brief)


def _strip_fence(text: str) -> str:
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    return (fenced.group(1) if fenced else text).strip()
