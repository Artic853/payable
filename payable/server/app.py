"""The merchant service.

Three surfaces over one catalog and one commerce core:

  /mcp                       MCP tool server (what an AI buyer calls)
  /.well-known/*             discovery: capability manifest + A2A agent card
  /catalog.jsonld            schema.org feed (what a crawler indexes)
  /legacy/*                  the same shop as plain HTML (the benchmark control)

Plus a REST mirror of the tools for curl, and the audit log read API.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from ..audit import AUDIT
from ..catalog import CATALOG
from ..commerce import COMMERCE, CommerceError
from ..config import SETTINGS
from ..mandate import ALG_ED25519, ALG_HMAC, ED25519_AVAILABLE, KEYRING, issue_mandate
from ..models import Mandate, SearchRequest
from . import mcp_http
from .legacy import router as legacy_router

app = FastAPI(
    title="Payable — merchant transactability layer",
    description="Turns a Razorpay merchant's catalog into an agent-callable, auditable commerce interface.",
    version="0.1.0",
)
app.include_router(legacy_router)


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------

@app.get("/.well-known/payable.json", tags=["discovery"])
def payable_manifest() -> dict:
    """Capability manifest: how an agent learns this merchant is transactable."""
    return {
        "payable_version": "0.1",
        "merchant": CATALOG.merchant.model_dump(),
        "interfaces": {
            "mcp": {
                "transport": "http-jsonrpc",
                "endpoint": f"{SETTINGS.base_url}/mcp",
                "protocol_version": mcp_http.PROTOCOL_VERSION,
                "tools": [t["name"] for t in mcp_http.TOOLS],
            },
            "a2a": {"agent_card": f"{SETTINGS.base_url}/.well-known/agent-card.json"},
            "jsonld": {"catalog_feed": f"{SETTINGS.base_url}/catalog.jsonld"},
            "rest": {"base": f"{SETTINGS.base_url}/api"},
        },
        "payments": {
            "processor": "razorpay",
            "mode": "test",
            "methods": ["upi", "card", "netbanking"],
            "currency": CATALOG.merchant.currency,
            "gateway_backend": SETTINGS.describe()["payments"],
        },
        "authorization": {
            "scheme": "signed-mandate",
            "algorithm": ALG_ED25519 if ED25519_AVAILABLE else ALG_HMAC,
            "supported_algorithms": (
                [ALG_ED25519, ALG_HMAC] if ED25519_AVAILABLE else [ALG_HMAC]
            ),
            "required_fields": [
                "mandate_id", "principal", "agent_id", "max_amount_paise",
                "allowed_categories", "issued_at", "expires_at", "alg", "signature",
            ],
            "verified": ["signature", "expiry", "amount_cap", "category_scope"],
            "key_registry": f"{SETTINGS.base_url}/api/principals",
            "notes": (
                "The merchant holds only public keys, so a merchant compromise "
                "cannot mint mandates. A principal with a registered public key "
                "may not fall back to the symmetric algorithm."
            ),
        },
        "guarantees": {
            "quote_ttl_seconds": 120,
            "idempotent_order_creation": True,
            "inventory_reserved_at_order": True,
            "audit_log": "every decision recorded per run_id",
        },
    }


@app.get("/.well-known/agent-card.json", tags=["discovery"])
def agent_card() -> dict:
    """A2A agent card describing the merchant as a callable counterparty."""
    return {
        "protocolVersion": "0.3.0",
        "name": CATALOG.merchant.display_name,
        "description": (
            "Consumer electronics merchant exposing search, quoting, ordering and "
            "Razorpay payment as agent-callable skills."
        ),
        "url": f"{SETTINGS.base_url}/mcp",
        "preferredTransport": "JSONRPC",
        "provider": {
            "organization": CATALOG.merchant.legal_name,
            "url": SETTINGS.base_url,
        },
        "version": "0.1.0",
        "capabilities": {"streaming": False, "pushNotifications": False},
        "defaultInputModes": ["application/json"],
        "defaultOutputModes": ["application/json"],
        "skills": [
            {
                "id": t["name"],
                "name": t["name"].replace("_", " "),
                "description": t["description"],
                "tags": ["commerce", "razorpay", "india"],
            }
            for t in mcp_http.TOOLS
        ],
    }


@app.get("/catalog.jsonld", tags=["discovery"])
def catalog_feed() -> JSONResponse:
    return JSONResponse(
        CATALOG.jsonld_feed(SETTINGS.base_url),
        media_type="application/ld+json",
    )


@app.get("/products/{sku}", tags=["discovery"])
def product_jsonld(sku: str) -> JSONResponse:
    product = CATALOG.get(sku)
    if product is None:
        raise HTTPException(status_code=404, detail="unknown sku")
    return JSONResponse(
        product.jsonld(SETTINGS.base_url, CATALOG.merchant),
        media_type="application/ld+json",
    )


# --------------------------------------------------------------------------
# MCP
# --------------------------------------------------------------------------

@app.post("/mcp", tags=["mcp"])
async def mcp_endpoint(request: Request) -> JSONResponse:
    body = await request.json()
    if isinstance(body, list):  # JSON-RPC batch
        responses = [r for r in (mcp_http.dispatch(m) for m in body) if r is not None]
        return JSONResponse(responses)
    response = mcp_http.dispatch(body)
    if response is None:
        return JSONResponse(status_code=202, content=None)
    return JSONResponse(response)


@app.get("/mcp/tools", tags=["mcp"])
def mcp_tools() -> dict:
    return {"tools": mcp_http.TOOLS}


# --------------------------------------------------------------------------
# REST mirror
# --------------------------------------------------------------------------

@app.post("/api/search", tags=["commerce"])
def api_search(request: SearchRequest) -> dict:
    return mcp_http.call_tool("search_products", request.model_dump())


@app.get("/api/products/{sku}", tags=["commerce"])
def api_product(sku: str) -> dict:
    result = mcp_http.call_tool("get_product", {"sku": sku})
    if result.get("error"):
        raise HTTPException(status_code=404, detail=result)
    return result


@app.post("/api/quote", tags=["commerce"])
def api_quote(payload: dict = Body(...)) -> dict:
    try:
        quote = COMMERCE.quote(
            sku=payload["sku"],
            quantity=payload.get("quantity", 1),
            ship_to_pincode=payload.get("ship_to_pincode", "500078"),
        )
    except CommerceError as exc:
        raise HTTPException(status_code=400, detail={"code": exc.code.value, "message": exc.message})
    return quote.model_dump()


@app.post("/api/orders", tags=["commerce"])
def api_create_order(payload: dict = Body(...)) -> dict:
    try:
        order = COMMERCE.create_order(
            quote_id=payload["quote_id"],
            mandate=Mandate(**payload["mandate"]),
            idempotency_key=payload.get("idempotency_key", uuid.uuid4().hex),
            buyer_reference=payload.get("buyer_reference", ""),
            run_id=payload.get("run_id", ""),
        )
    except CommerceError as exc:
        raise HTTPException(status_code=402, detail={"code": exc.code.value, "message": exc.message})
    return order.model_dump()


@app.post("/api/orders/{order_id}/pay", tags=["commerce"])
def api_pay(order_id: str, payload: dict = Body(default={})) -> dict:
    try:
        result = COMMERCE.pay(
            order_id=order_id,
            method=payload.get("method", "upi"),
            vpa=payload.get("vpa", "buyer@upi"),
            run_id=payload.get("run_id", ""),
        )
    except CommerceError as exc:
        raise HTTPException(status_code=404, detail={"code": exc.code.value, "message": exc.message})
    return result.model_dump()


@app.get("/api/orders/{order_id}", tags=["commerce"])
def api_order(order_id: str) -> dict:
    order = COMMERCE.get_order(order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="unknown order")
    return order.model_dump()


@app.post("/api/mandates", tags=["authorization"])
def api_issue_mandate(payload: dict = Body(default={})) -> dict:
    """Dev convenience: mint a signed mandate.

    In production the principal's own wallet or bank issues this, never the
    merchant -- the merchant would never hold the private key. It lives here so
    the demo has one moving part fewer.
    """
    mandate = issue_mandate(
        principal=payload.get("principal", "user:demo"),
        agent_id=payload.get("agent_id", "agent:buyer-1"),
        max_amount_paise=int(payload.get("max_amount_paise", 1_000_000)),
        allowed_categories=payload.get("allowed_categories") or [],
        ttl_seconds=int(payload.get("ttl_seconds", 900)),
    )
    return mandate.model_dump()


@app.get("/api/principals", tags=["authorization"])
def api_principals() -> dict:
    """Public keys the merchant will verify mandates against.

    Public halves only. Nothing here confers the ability to sign.
    """
    return {
        "algorithm": ALG_ED25519 if ED25519_AVAILABLE else ALG_HMAC,
        "principals": [
            {"principal": principal, "public_key": key, "alg": ALG_ED25519}
            for principal, key in sorted(KEYRING.export_public_keys().items())
        ],
    }


@app.post("/api/principals", tags=["authorization"])
def api_enrol_principal(payload: dict = Body(...)) -> dict:
    """Enrol a principal's public key with the merchant.

    Either register a key the principal generated (`public_key`), or -- for the
    demo only -- have the keyring generate the pair.
    """
    principal = payload.get("principal")
    if not principal:
        raise HTTPException(status_code=400, detail="principal is required")

    public_key = payload.get("public_key")
    if public_key:
        try:
            KEYRING.register_public_key(principal, public_key)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"malformed public key: {exc}")
    else:
        if not ED25519_AVAILABLE:
            raise HTTPException(status_code=501, detail="Ed25519 unavailable on this server")
        public_key = KEYRING.enrol(principal)

    return {"principal": principal, "public_key": public_key, "alg": ALG_ED25519}


# --------------------------------------------------------------------------
# Audit
# --------------------------------------------------------------------------

@app.get("/api/audit/runs", tags=["audit"])
def api_runs(limit: int = 50) -> dict:
    return {"backend": AUDIT.backend, "runs": AUDIT.runs(limit=limit)}


@app.get("/api/audit/runs/{run_id}", tags=["audit"])
def api_run(run_id: str) -> dict:
    events = AUDIT.events(run_id=run_id)
    if not events:
        raise HTTPException(status_code=404, detail="unknown run")
    return {
        "run_id": run_id,
        "backend": AUDIT.backend,
        "events": [e.model_dump() for e in events],
    }


# --------------------------------------------------------------------------
# Console
# --------------------------------------------------------------------------

@app.get("/health", tags=["ops"])
def health() -> dict:
    return {"status": "ok", "ts": time.time(), "config": SETTINGS.describe()}


@app.get("/", response_class=HTMLResponse, tags=["ops"])
def index() -> HTMLResponse:
    cfg = SETTINGS.describe()
    rows = "".join(f"<tr><td>{k}</td><td><code>{v}</code></td></tr>" for k, v in cfg.items())
    links = [
        ("/.well-known/payable.json", "capability manifest"),
        ("/.well-known/agent-card.json", "A2A agent card"),
        ("/catalog.jsonld", "schema.org catalog feed"),
        ("/mcp/tools", "MCP tool definitions"),
        ("/api/audit/runs", "audit log — recent runs"),
        ("/legacy/", "control arm: plain HTML storefront"),
        ("/docs", "OpenAPI docs"),
    ]
    link_rows = "".join(f"<li><a href='{href}'>{href}</a> — {label}</li>" for href, label in links)
    return HTMLResponse(
        "<!doctype html><meta charset='utf-8'><title>Payable</title>"
        "<style>body{font-family:system-ui,Segoe UI,Arial,sans-serif;max-width:760px;margin:40px auto;padding:0 20px;color:#1a1a1a}"
        "code{background:#f2f0ec;padding:2px 6px;border-radius:4px}"
        "table{border-collapse:collapse;margin:12px 0}td{padding:5px 16px 5px 0;border-bottom:1px solid #eee}"
        "li{line-height:1.9}</style>"
        "<h1>Payable</h1>"
        f"<p>Merchant transactability layer for <b>{CATALOG.merchant.display_name}</b> "
        f"({len(CATALOG.products)} SKUs).</p>"
        f"<h3>Active backends</h3><table>{rows}</table>"
        f"<h3>Surfaces</h3><ul>{link_rows}</ul>"
    )
