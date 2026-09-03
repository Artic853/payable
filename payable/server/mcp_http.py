"""MCP tool server over HTTP JSON-RPC.

Six tools cover the whole purchase lifecycle. They are described the way an LLM
needs them described -- what the tool refuses to do, and what the caller must
check before advancing -- because the tool description is the merchant's only
chance to shape a buyer agent's behaviour before it acts.

Implemented against the JSON-RPC surface directly rather than the `mcp` SDK so
the service has no dependency beyond FastAPI; any MCP client that speaks
streamable HTTP can call it, and so can plain `curl`.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from ..audit import AUDIT
from ..catalog import CATALOG
from ..commerce import COMMERCE, CommerceError
from ..config import SETTINGS
from ..models import Mandate, SearchRequest, SpecConstraint

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "payable-merchant", "version": "0.1.0"}

_CONSTRAINT_SCHEMA = {
    "type": "object",
    "properties": {
        "field": {
            "type": "string",
            "description": "Spec key (e.g. 'switch_type', 'anc', 'refresh_hz') or product field (e.g. 'price_paise', 'brand').",
        },
        "op": {
            "type": "string",
            "enum": ["eq", "neq", "gte", "lte", "contains", "not_contains", "true", "false"],
        },
        "value": {"description": "Comparison value; omit for the 'true'/'false' ops."},
        "hard": {
            "type": "boolean",
            "default": True,
            "description": "Hard constraints disqualify a product. Soft constraints only affect ranking.",
        },
    },
    "required": ["field", "op"],
}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "search_products",
        "description": (
            "Search the merchant catalog with machine-checkable constraints. Returns ranked "
            "candidates, each annotated with the constraints it met and missed. "
            "IMPORTANT: check `advisory_code` and `unmet_hard_constraints` before acting. "
            "`ambiguous` means several candidates fit equally well and the choice between them "
            "has not actually been made -- narrow the query or escalate rather than picking. "
            "`no_satisfying_candidate` means nothing clears the hard constraints; buying the "
            "closest thing is a wrong purchase, not a partial success."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Free-text description of what is wanted."},
                "category": {
                    "type": "string",
                    "description": f"Optional filter. One of: {', '.join(CATALOG.categories())}",
                },
                "max_price_paise": {
                    "type": "integer",
                    "description": "Unit price ceiling in paise (100 paise = 1 INR). Excludes GST and shipping.",
                },
                "constraints": {"type": "array", "items": _CONSTRAINT_SCHEMA},
                "in_stock_only": {"type": "boolean", "default": True},
                "limit": {"type": "integer", "default": 5},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_product",
        "description": "Full specification, price and live stock for one SKU.",
        "inputSchema": {
            "type": "object",
            "properties": {"sku": {"type": "string"}},
            "required": ["sku"],
        },
    },
    {
        "name": "create_quote",
        "description": (
            "Price a purchase: unit price, GST, shipping and total, plus live availability. "
            "Quotes expire in 120 seconds. You must quote before ordering -- the total here is "
            "what your mandate cap is checked against."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "sku": {"type": "string"},
                "quantity": {"type": "integer", "default": 1, "minimum": 1},
                "ship_to_pincode": {"type": "string", "default": "500078"},
            },
            "required": ["sku"],
        },
    },
    {
        "name": "place_order",
        "description": (
            "Create a Razorpay order against a live quote. Requires a signed mandate; the "
            "merchant verifies its signature, expiry, category scope and amount cap server-side "
            "and refuses the order if any check fails. Pass a stable idempotency_key -- retrying "
            "with the same key returns the same order instead of creating a second one."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "quote_id": {"type": "string"},
                "mandate": {"type": "object", "description": "Signed mandate object from the principal."},
                "idempotency_key": {"type": "string"},
                "buyer_reference": {"type": "string"},
            },
            "required": ["quote_id", "mandate", "idempotency_key"],
        },
    },
    {
        "name": "pay_order",
        "description": (
            "Attempt payment for an order. Returns status captured|failed|pending with a "
            "failure_code and a `retriable` flag. Do not retry when retriable is false."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string"},
                "method": {"type": "string", "enum": ["upi", "card", "netbanking"], "default": "upi"},
                "vpa": {"type": "string", "default": "buyer@upi"},
            },
            "required": ["order_id"],
        },
    },
    {
        "name": "get_order_status",
        "description": "Current status of a merchant order.",
        "inputSchema": {
            "type": "object",
            "properties": {"order_id": {"type": "string"}},
            "required": ["order_id"],
        },
    },
]


# --------------------------------------------------------------------------
# Tool implementations
# --------------------------------------------------------------------------

def _run_id(arguments: dict) -> str:
    return arguments.get("_run_id") or f"run_{uuid.uuid4().hex[:12]}"


def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    run_id = _run_id(arguments)
    started = time.perf_counter()

    if name == "search_products":
        request = SearchRequest(
            query=arguments.get("query", ""),
            category=arguments.get("category"),
            max_price_paise=arguments.get("max_price_paise"),
            constraints=[SpecConstraint(**c) for c in arguments.get("constraints", [])],
            in_stock_only=arguments.get("in_stock_only", True),
            limit=arguments.get("limit", 5),
        )
        response = CATALOG.search(request)
        result = {
            "query": response.query,
            "total_considered": response.total_considered,
            "advisory": response.advisory,
            "advisory_code": response.advisory_code.value,
            "candidates": [
                {
                    "sku": c.product.sku,
                    "name": c.product.name,
                    "brand": c.product.brand,
                    "category": c.product.category,
                    "price_paise": c.product.price_paise,
                    "price_inr": c.product.price_paise / 100,
                    "stock": c.product.stock,
                    "match_score": c.match_score,
                    "met_constraints": c.met_constraints,
                    "unmet_constraints": c.unmet_constraints,
                    "unmet_hard_constraints": c.unmet_hard_constraints,
                    "fully_satisfies": c.fully_satisfies,
                    "rationale": c.rationale,
                    "specs": c.product.specs,
                }
                for c in response.candidates
            ],
        }
        AUDIT.record(
            run_id, "merchant", "search",
            decision=f"{len(response.candidates)} candidates",
            rationale=response.advisory or "no advisory",
            payload={"query": request.query, "constraints": [c.describe() for c in request.constraints]},
            latency_ms=(time.perf_counter() - started) * 1000,
        )
        return result

    if name == "get_product":
        product = CATALOG.get(arguments["sku"])
        if product is None:
            return {"error": "not_found", "sku": arguments["sku"]}
        return {
            **product.model_dump(),
            "price_inr": product.price_paise / 100,
            "jsonld": product.jsonld(SETTINGS.base_url, CATALOG.merchant),
        }

    if name == "create_quote":
        try:
            quote = COMMERCE.quote(
                sku=arguments["sku"],
                quantity=arguments.get("quantity", 1),
                ship_to_pincode=arguments.get("ship_to_pincode", "500078"),
            )
        except CommerceError as exc:
            return {"error": exc.code.value, "message": exc.message}
        AUDIT.record(
            run_id, "merchant", "quote",
            decision=quote.quote_id,
            rationale=f"{quote.product_name} x{quote.quantity} = INR {quote.breakdown.total_rupees:.2f}",
            payload={"sku": quote.sku, "total_paise": quote.breakdown.total_paise},
            latency_ms=(time.perf_counter() - started) * 1000,
        )
        return quote.model_dump()

    if name == "place_order":
        try:
            mandate = Mandate(**arguments["mandate"])
            order = COMMERCE.create_order(
                quote_id=arguments["quote_id"],
                mandate=mandate,
                idempotency_key=arguments["idempotency_key"],
                buyer_reference=arguments.get("buyer_reference", ""),
                run_id=run_id,
            )
        except CommerceError as exc:
            return {"error": exc.code.value, "message": exc.message}
        except Exception as exc:  # malformed mandate
            return {"error": "mandate_rejected", "message": str(exc)}
        AUDIT.record(
            run_id, "merchant", "order_created",
            decision=order.order_id,
            rationale=f"gateway={order.gateway} amount_paise={order.amount_paise}",
            payload={"gateway_order_id": order.gateway_order_id, "sku": order.sku},
            latency_ms=(time.perf_counter() - started) * 1000,
        )
        return order.model_dump()

    if name == "pay_order":
        try:
            result = COMMERCE.pay(
                order_id=arguments["order_id"],
                method=arguments.get("method", "upi"),
                vpa=arguments.get("vpa", "buyer@upi"),
                run_id=run_id,
            )
        except CommerceError as exc:
            return {"error": exc.code.value, "message": exc.message}
        return result.model_dump()

    if name == "get_order_status":
        order = COMMERCE.get_order(arguments["order_id"])
        if order is None:
            return {"error": "not_found", "order_id": arguments["order_id"]}
        return order.model_dump()

    return {"error": "unknown_tool", "name": name}


# --------------------------------------------------------------------------
# JSON-RPC dispatch
# --------------------------------------------------------------------------

def _ok(request_id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _err(request_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def dispatch(message: dict[str, Any]) -> dict[str, Any] | None:
    """Handle one JSON-RPC message. Returns None for notifications."""
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}

    if method == "initialize":
        return _ok(request_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
            "instructions": (
                "Merchant transactability layer. Search with explicit constraints, honour the "
                "`advisory` field, quote before ordering, and carry a signed mandate."
            ),
        })

    if method in {"notifications/initialized", "initialized"}:
        return None

    if method == "ping":
        return _ok(request_id, {})

    if method == "tools/list":
        return _ok(request_id, {"tools": TOOLS})

    if method == "tools/call":
        name = params.get("name", "")
        arguments = params.get("arguments") or {}
        if name not in {t["name"] for t in TOOLS}:
            return _err(request_id, -32602, f"unknown tool {name!r}")
        try:
            payload = call_tool(name, arguments)
        except Exception as exc:
            return _ok(request_id, {
                "content": [{"type": "text", "text": f"tool error: {exc}"}],
                "isError": True,
            })
        import json as _json
        return _ok(request_id, {
            "content": [{"type": "text", "text": _json.dumps(payload, default=str, indent=2)}],
            "structuredContent": payload,
            "isError": bool(payload.get("error")) if isinstance(payload, dict) else False,
        })

    return _err(request_id, -32601, f"method not found: {method}")
