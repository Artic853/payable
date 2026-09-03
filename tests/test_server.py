"""Discovery surfaces and the MCP JSON-RPC contract."""

import json

from payable.server.mcp_http import PROTOCOL_VERSION, TOOLS


def rpc(client, method, params=None, request_id=1):
    body = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        body["params"] = params
    return client.post("/mcp", json=body).json()


def call(client, name, arguments):
    response = rpc(client, "tools/call", {"name": name, "arguments": arguments})
    return response["result"]["structuredContent"]


# -- discovery -------------------------------------------------------------

def test_capability_manifest_advertises_every_interface(client):
    manifest = client.get("/.well-known/payable.json").json()
    assert set(manifest["interfaces"]) == {"mcp", "a2a", "jsonld", "rest"}
    assert manifest["authorization"]["scheme"] == "signed-mandate"
    assert manifest["payments"]["processor"] == "razorpay"


def test_agent_card_lists_every_tool_as_a_skill(client):
    card = client.get("/.well-known/agent-card.json").json()
    assert {s["id"] for s in card["skills"]} == {t["name"] for t in TOOLS}


def test_catalog_feed_is_jsonld(client):
    response = client.get("/catalog.jsonld")
    assert response.headers["content-type"].startswith("application/ld+json")
    feed = response.json()
    assert feed["@type"] == "ItemList"
    assert feed["numberOfItems"] == len(feed["itemListElement"])


def test_unknown_sku_is_a_404(client):
    assert client.get("/products/NOPE").status_code == 404


# -- MCP protocol ----------------------------------------------------------

def test_initialize_reports_the_protocol_version(client):
    result = rpc(client, "initialize", {"protocolVersion": PROTOCOL_VERSION})["result"]
    assert result["protocolVersion"] == PROTOCOL_VERSION
    assert result["serverInfo"]["name"] == "payable-merchant"


def test_tools_list_matches_the_declared_tools(client):
    result = rpc(client, "tools/list")["result"]
    assert {t["name"] for t in result["tools"]} == {t["name"] for t in TOOLS}
    for tool in result["tools"]:
        assert tool["inputSchema"]["type"] == "object"


def test_unknown_method_is_a_jsonrpc_error(client):
    response = rpc(client, "does/not/exist")
    assert response["error"]["code"] == -32601


def test_unknown_tool_is_a_jsonrpc_error(client):
    response = rpc(client, "tools/call", {"name": "nope", "arguments": {}})
    assert response["error"]["code"] == -32602


def test_notifications_get_no_response_body(client):
    response = client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert response.status_code == 202


def test_batch_requests_are_answered_in_a_batch(client):
    response = client.post("/mcp", json=[
        {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]).json()
    assert [r["id"] for r in response] == [1, 2]


def test_tool_result_carries_both_text_and_structured_content(client):
    result = rpc(client, "tools/call", {
        "name": "get_product", "arguments": {"sku": "KB-MECH-87-BRN"},
    })["result"]
    assert json.loads(result["content"][0]["text"])["sku"] == "KB-MECH-87-BRN"
    assert result["structuredContent"]["sku"] == "KB-MECH-87-BRN"


# -- full purchase over MCP ------------------------------------------------

def test_search_quote_order_pay_roundtrip(client):
    search = call(client, "search_products", {
        "query": "tenkeyless bluetooth keyboard tactile",
        "category": "keyboard",
        "constraints": [
            {"field": "switch_type", "op": "eq", "value": "brown-tactile"},
            {"field": "layout", "op": "eq", "value": "TKL-87"},
        ],
    })
    assert search["advisory_code"] == "none"
    sku = search["candidates"][0]["sku"]
    assert sku == "KB-MECH-87-BRN"

    quote = call(client, "create_quote", {"sku": sku, "quantity": 1})
    assert quote["available"] is True

    mandate = client.post("/api/mandates", json={
        "max_amount_paise": quote["breakdown"]["total_paise"],
        "allowed_categories": ["keyboard"],
    }).json()

    order = call(client, "place_order", {
        "quote_id": quote["quote_id"],
        "mandate": mandate,
        "idempotency_key": "test-roundtrip",
    })
    assert order["gateway_order_id"]
    assert order["amount_paise"] == quote["breakdown"]["total_paise"]

    payment = call(client, "pay_order", {"order_id": order["order_id"], "method": "upi"})
    assert payment["status"] in {"captured", "failed"}

    status = call(client, "get_order_status", {"order_id": order["order_id"]})
    assert status["status"] in {"paid", "failed"}


def test_order_without_a_valid_mandate_is_refused(client):
    quote = call(client, "create_quote", {"sku": "CAB-USBC-2M-240W"})
    forged = {
        "mandate_id": "mnd_forged", "principal": "user:x", "agent_id": "agent:x",
        "max_amount_paise": 99_999_999, "allowed_categories": [],
        "issued_at": 0, "expires_at": 9_999_999_999, "signature": "deadbeef",
    }
    result = call(client, "place_order", {
        "quote_id": quote["quote_id"], "mandate": forged, "idempotency_key": "forged-1",
    })
    assert result["error"] == "mandate_rejected"


def test_audit_log_captures_the_run(client):
    run_id = "run_audit_test"
    client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "search_products", "arguments": {
            "query": "usb-c dock", "category": "dock", "_run_id": run_id,
        }},
    })
    events = client.get(f"/api/audit/runs/{run_id}").json()["events"]
    assert [e["step"] for e in events] == ["search"]
    assert events[0]["actor"] == "merchant"


# -- legacy storefront -----------------------------------------------------

def test_storefront_renders_and_search_narrows(client):
    assert client.get("/legacy/").status_code == 200
    page = client.get("/legacy/?q=keyboard").text
    assert "Aether 87 TKL" in page


def test_out_of_stock_product_offers_no_buy_form(client):
    page = client.get("/legacy/product/MS-GAME-8K-01").text
    assert "Currently unavailable" in page
    assert "Add to cart" not in page


def test_storefront_checkout_completes(client):
    product_page = client.get("/legacy/product/CAB-USBC-2M-240W").text
    csrf = product_page.split("name='csrf_token' value='")[1].split("'")[0]

    cart = client.post("/legacy/cart", data={
        "sku": "CAB-USBC-2M-240W", "quantity": "2", "csrf_token": csrf,
    }).text
    session_id = cart.split("name='session_id' value='")[1].split("'")[0]
    assert "Order total" in cart

    confirmation = client.post("/legacy/checkout", data={
        "session_id": session_id, "csrf_token": csrf, "full_name": "Test Buyer",
        "email": "t@example.com", "pincode": "500078", "method": "upi",
    }).text
    assert "order is confirmed" in confirmation.lower() or "Payment failed" in confirmation


def test_storefront_rejects_a_malformed_pincode(client):
    product_page = client.get("/legacy/product/CAB-USBC-2M-240W").text
    csrf = product_page.split("name='csrf_token' value='")[1].split("'")[0]
    cart = client.post("/legacy/cart", data={
        "sku": "CAB-USBC-2M-240W", "quantity": "1", "csrf_token": csrf,
    }).text
    session_id = cart.split("name='session_id' value='")[1].split("'")[0]

    response = client.post("/legacy/checkout", data={
        "session_id": session_id, "csrf_token": csrf, "full_name": "Test",
        "email": "t@example.com", "pincode": "abc", "method": "upi",
    })
    assert response.status_code == 400
