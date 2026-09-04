"""The control arm: the same merchant, same catalog, no transactability layer.

This is an ordinary HTML storefront -- keyword search, specs as prose bullets,
prices as rupee strings with an MRP struck through beside them, stock as a vague
phrase, and a checkout form behind a CSRF token. Nothing here is sabotaged; it is
what a normal Indian D2C storefront looks like to a program.

The benchmark's whole claim rests on this being a fair comparison, so the rules
are: identical inventory, identical prices, identical GST and shipping maths, and
a checkout that a competent scraper can complete. The difficulty is structural,
not adversarial -- the information an agent needs exists, it is just not typed.
"""

from __future__ import annotations

import hashlib
import html
import time
import uuid

from fastapi import APIRouter, Form, HTTPException
from fastapi.responses import HTMLResponse

from ..catalog import CATALOG
from ..commerce import price_for
from ..payments.simulated import SimulatedRazorpayGateway

router = APIRouter(prefix="/legacy", tags=["legacy-storefront"])

_SESSIONS: dict[str, dict] = {}
_GATEWAY = SimulatedRazorpayGateway()

_SPEC_LABELS = {
    "layout": "Layout",
    "switch_type": "Switches",
    "connectivity": "Connectivity",
    "hot_swappable": "Hot-swappable",
    "backlight": "Backlight",
    "keycap_profile": "Keycaps",
    "battery_mah": "Battery",
    "battery_hours": "Playback",
    "weight_g": "Weight",
    "warranty_months": "Warranty",
    "dpi_max": "Max DPI",
    "buttons": "Buttons",
    "grip": "Grip style",
    "hand": "Handedness",
    "polling_hz": "Polling rate",
    "silent_click": "Silent click",
    "form": "Form factor",
    "anc": "Active noise cancellation",
    "anc_depth_db": "ANC depth",
    "codecs": "Codecs",
    "mic": "Microphone",
    "impedance_ohm": "Impedance",
    "size_inch": "Screen size",
    "resolution": "Resolution",
    "panel": "Panel",
    "refresh_hz": "Refresh rate",
    "ports": "Ports",
    "hdr": "HDR",
    "vesa": "VESA mount",
    "ports_total": "Total ports",
    "power_delivery_w": "Power delivery",
    "hdmi_outputs": "HDMI outputs",
    "max_display": "Max display",
    "ethernet": "Ethernet",
    "sd_reader": "SD card reader",
    "length_m": "Length",
    "power_w": "Power rating",
    "data_gbps": "Data rate",
    "connector": "Connector",
    "braided": "Braided",
    "resolution_p": "Video resolution",
    "fps": "Frame rate",
    "fov_deg": "Field of view",
    "autofocus": "Autofocus",
    "mic_builtin": "Built-in microphone",
    "privacy_shutter": "Privacy shutter",
    "connector_type": "Connector type",
    "polar_pattern": "Polar pattern",
    "sample_rate_khz": "Sample rate",
    "phantom_power": "Phantom power",
    "monitoring": "Zero-latency monitoring",
    "boom_arm": "Boom arm included",
    "capacity_gb": "Capacity",
    "read_mbps": "Sequential read",
    "write_mbps": "Sequential write",
    "interface": "Interface",
    "encryption": "Hardware encryption",
    "shock_rated": "Shock rated",
    "material": "Material",
    "adjustable": "Adjustable",
    "max_load_kg": "Maximum load",
    "foldable": "Foldable",
    "height_levels": "Height positions",
    "port_type": "Port type",
    "powered": "Externally powered",
    "cable_length_m": "Cable length",
    "inputs": "Inputs",
    "max_resolution": "Maximum resolution",
    "hotkey_switching": "Hotkey switching",
    "usb_peripheral_ports": "USB peripheral ports",
}

_UNITS = {
    "battery_mah": " mAh",
    "battery_hours": " hours",
    "weight_g": " g",
    "warranty_months": " months",
    "polling_hz": " Hz",
    "refresh_hz": " Hz",
    "impedance_ohm": " ohm",
    "size_inch": " inches",
    "power_delivery_w": "W",
    "power_w": "W",
    "data_gbps": " Gbps",
    "length_m": " m",
    "anc_depth_db": " dB",
    "resolution_p": "p",
    "fps": " fps",
    "fov_deg": " degrees",
    "sample_rate_khz": " kHz",
    "capacity_gb": " GB",
    "read_mbps": " MB/s",
    "write_mbps": " MB/s",
    "max_load_kg": " kg",
    "cable_length_m": " m",
}


def _rupees(paise: int) -> str:
    """Indian-format currency string, the way a storefront prints it."""
    rupees = paise // 100
    s = str(rupees)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts) + "," + tail
    return f"&#8377;{s}"


def _spec_value(key: str, value) -> str:
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    if key in _UNITS:
        return f"{value}{_UNITS[key]}"
    return str(value)


def _stock_phrase(stock: int) -> str:
    if stock == 0:
        return "Currently unavailable"
    if stock <= 10:
        return "Hurry, only a few left!"
    return "In stock"


_CSS = """
body{font-family:system-ui,Segoe UI,Arial,sans-serif;margin:0;background:#faf9f7;color:#1a1a1a}
header{background:#0f2d4a;color:#fff;padding:14px 24px}
header a{color:#fff;text-decoration:none;font-weight:600}
.wrap{max-width:940px;margin:0 auto;padding:24px}
.card{background:#fff;border:1px solid #e4e0d9;border-radius:8px;padding:16px;margin-bottom:14px}
.price{font-size:22px;font-weight:700;color:#0f2d4a}
.mrp{color:#8a8a8a;text-decoration:line-through;margin-left:8px;font-size:15px}
.stock{font-size:13px;color:#0a7a3f}
.stock.out{color:#b00020}
ul.specs{margin:8px 0;padding-left:20px;line-height:1.7}
input,select{padding:8px;border:1px solid #ccc;border-radius:4px;width:100%;box-sizing:border-box;margin:4px 0 10px}
button{background:#0f2d4a;color:#fff;border:0;padding:11px 18px;border-radius:5px;font-size:15px;cursor:pointer}
.muted{color:#666;font-size:13px}
table{border-collapse:collapse;width:100%}
td{padding:6px 0;border-bottom:1px solid #eee}
td.r{text-align:right}
"""


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(
        f"<!doctype html><html><head><meta charset='utf-8'><title>{html.escape(title)}"
        f" | Bharat Peripherals</title><style>{_CSS}</style></head><body>"
        f"<header><a href='/legacy/'>Bharat Peripherals</a></header>"
        f"<div class='wrap'>{body}</div></body></html>"
    )


@router.get("/", response_class=HTMLResponse)
def storefront(q: str = "") -> HTMLResponse:
    products = CATALOG.products
    if q:
        terms = [t for t in q.lower().split() if t]
        scored = []
        for p in products:
            hay = f"{p.name} {p.brand} {p.category} {' '.join(p.tags)}".lower()
            hits = sum(1 for t in terms if t in hay)
            if hits:
                scored.append((hits, p))
        scored.sort(key=lambda x: x[0], reverse=True)
        products = [p for _, p in scored]

    rows = []
    for p in products:
        out = "" if p.stock else " out"
        rows.append(
            f"<div class='card'>"
            f"<h3 style='margin:0 0 6px'><a href='/legacy/product/{p.sku}'>{html.escape(p.name)}</a></h3>"
            f"<div class='muted'>{html.escape(p.brand)} &middot; {html.escape(p.category)}</div>"
            f"<div class='price'>{_rupees(p.price_paise)}<span class='mrp'>{_rupees(p.mrp_paise)}</span></div>"
            f"<div class='stock{out}'>{_stock_phrase(p.stock)}</div>"
            f"</div>"
        )

    search_box = (
        "<form method='get' action='/legacy/' class='card'>"
        f"<input name='q' placeholder='Search products' value='{html.escape(q)}'>"
        "<button type='submit'>Search</button></form>"
    )
    heading = f"<h2>{len(products)} result(s) for &ldquo;{html.escape(q)}&rdquo;</h2>" if q else "<h2>All products</h2>"
    return _page("Shop", search_box + heading + "".join(rows))


@router.get("/product/{sku}", response_class=HTMLResponse)
def product_page(sku: str) -> HTMLResponse:
    product = CATALOG.get(sku)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")

    specs = "".join(
        f"<li><b>{html.escape(_SPEC_LABELS.get(k, k))}:</b> {html.escape(_spec_value(k, v))}</li>"
        for k, v in product.specs.items()
    )
    out = "" if product.stock else " out"
    token = hashlib.sha256(f"{sku}{time.time() // 300}".encode()).hexdigest()[:24]

    buy = (
        "<form method='post' action='/legacy/cart' class='card'>"
        f"<input type='hidden' name='sku' value='{product.sku}'>"
        f"<input type='hidden' name='csrf_token' value='{token}'>"
        "<label>Quantity</label><input name='quantity' value='1'>"
        "<button type='submit'>Add to cart</button></form>"
        if product.stock else "<div class='card'><b>Currently unavailable</b></div>"
    )

    body = (
        f"<div class='card'><h2 style='margin-top:0'>{html.escape(product.name)}</h2>"
        f"<div class='muted'>SKU {html.escape(product.sku)} &middot; {html.escape(product.brand)}</div>"
        f"<div class='price'>{_rupees(product.price_paise)}<span class='mrp'>{_rupees(product.mrp_paise)}</span></div>"
        f"<div class='stock{out}'>{_stock_phrase(product.stock)}</div>"
        f"<p class='muted'>Taxes and shipping calculated at checkout.</p>"
        f"<h4>Specifications</h4><ul class='specs'>{specs}</ul></div>{buy}"
    )
    return _page(product.name, body)


@router.post("/cart", response_class=HTMLResponse)
def cart(sku: str = Form(...), quantity: str = Form("1"), csrf_token: str = Form(...)) -> HTMLResponse:
    product = CATALOG.get(sku)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    try:
        qty = max(1, int(quantity))
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid quantity")

    breakdown = price_for(product.price_paise, qty)
    session_id = f"cart_{uuid.uuid4().hex[:12]}"
    _SESSIONS[session_id] = {
        "sku": sku,
        "quantity": qty,
        "total_paise": breakdown.total_paise,
        "created_at": time.time(),
    }

    summary = (
        "<table>"
        f"<tr><td>{html.escape(product.name)} &times; {qty}</td><td class='r'>{_rupees(breakdown.subtotal_paise)}</td></tr>"
        f"<tr><td>GST (18%)</td><td class='r'>{_rupees(breakdown.gst_paise)}</td></tr>"
        f"<tr><td>Shipping</td><td class='r'>{'FREE' if breakdown.shipping_paise == 0 else _rupees(breakdown.shipping_paise)}</td></tr>"
        f"<tr><td><b>Order total</b></td><td class='r'><b>{_rupees(breakdown.total_paise)}</b></td></tr>"
        "</table>"
    )
    form = (
        "<form method='post' action='/legacy/checkout' class='card'>"
        f"<input type='hidden' name='session_id' value='{session_id}'>"
        f"<input type='hidden' name='csrf_token' value='{csrf_token}'>"
        "<label>Full name</label><input name='full_name' required>"
        "<label>Email</label><input name='email' type='email' required>"
        "<label>PIN code</label><input name='pincode' required>"
        "<label>Payment method</label><select name='method'>"
        "<option value='upi'>UPI</option><option value='card'>Card</option>"
        "<option value='netbanking'>Net banking</option></select>"
        "<button type='submit'>Place order &amp; pay</button></form>"
    )
    return _page("Cart", f"<div class='card'><h2 style='margin-top:0'>Order summary</h2>{summary}</div>{form}")


@router.post("/checkout", response_class=HTMLResponse)
def checkout(
    session_id: str = Form(...),
    csrf_token: str = Form(...),
    full_name: str = Form(...),
    email: str = Form(...),
    pincode: str = Form(...),
    method: str = Form("upi"),
) -> HTMLResponse:
    session = _SESSIONS.get(session_id)
    if session is None:
        raise HTTPException(status_code=400, detail="Cart session expired")
    if not pincode.isdigit() or len(pincode) != 6:
        raise HTTPException(status_code=400, detail="Invalid PIN code")

    if not CATALOG.reserve(session["sku"], session["quantity"]):
        return _page("Sold out", "<div class='card'><h2>Sorry, this item just went out of stock.</h2></div>")

    order = _GATEWAY.create_order(
        amount_paise=session["total_paise"],
        currency="INR",
        receipt=f"rcpt_{session_id}",
        notes={"sku": session["sku"], "channel": "legacy-web", "email": email},
    )
    result = _GATEWAY.pay(order["id"], session["total_paise"], method)

    if result.status == "captured":
        CATALOG.get(session["sku"])  # reservation stands
        body = (
            "<div class='card'><h2 style='margin-top:0'>Thank you, your order is confirmed</h2>"
            f"<p>Order ID: <b>{order['id']}</b></p>"
            f"<p>Payment ID: <b>{result.payment_id}</b></p>"
            f"<p>Amount paid: <b>{_rupees(result.amount_paise)}</b></p>"
            f"<p class='muted'>A confirmation has been sent to {html.escape(email)}.</p></div>"
        )
        return _page("Order confirmed", body)

    CATALOG.release(session["sku"], session["quantity"])
    body = (
        "<div class='card'><h2 style='margin-top:0'>Payment failed</h2>"
        f"<p>{html.escape(result.failure_reason)}</p>"
        f"<p class='muted'>Order {order['id']} was not charged. "
        f"{'Please try again.' if result.retriable else 'Please use a different payment method.'}</p></div>"
    )
    return _page("Payment failed", body)
