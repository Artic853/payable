"""The web console.

Its job is to be honest about what the agent did. The grading tests matter most:
a console that quietly scores a correct refusal as "unscored" would hide the one
behaviour this whole project argues for.
"""

import contextlib

import pytest

from payable.server import console_api


@pytest.fixture(autouse=True)
def drive_in_process(client, monkeypatch):
    """Point console runs at the in-process app.

    A console run normally makes a real HTTP request back to its own socket.
    There is no socket under TestClient, so the seam is redirected here.
    """
    @contextlib.contextmanager
    def fake_client_for(base_url):
        yield client

    monkeypatch.setattr(console_api, "client_for", fake_client_for)


AMBIGUOUS = {
    "task_id": "kb-ambiguous-tkl",
    "brief": "Get me a hot swappable tenkeyless keyboard with bluetooth.",
    "category": "keyboard",
    "budget_paise": 800_000,
    "expected_sku": None,
    "graded": True,
    "constraints": [
        {"field": "layout", "op": "eq", "value": "TKL-87"},
        {"field": "connectivity", "op": "contains", "value": "bluetooth"},
        {"field": "hot_swappable", "op": "true"},
    ],
}

CLEAR = {
    "task_id": "kb-tkl-brown-bt",
    "brief": "Tenkeyless mechanical keyboard, tactile brown switches, bluetooth.",
    "category": "keyboard",
    "budget_paise": 700_000,
    "expected_sku": "KB-MECH-87-BRN",
    "graded": True,
    "constraints": [
        {"field": "layout", "op": "eq", "value": "TKL-87"},
        {"field": "switch_type", "op": "eq", "value": "brown-tactile"},
    ],
}


def run(client, payload, arm):
    response = client.post("/api/console/run", json={**payload, "arm": arm})
    assert response.status_code == 200, response.text
    return response.json()


# -- page and data ---------------------------------------------------------

def test_console_page_is_served(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Payable" in response.text


def test_static_assets_are_served(client):
    assert client.get("/static/style.css").status_code == 200
    assert client.get("/static/app.js").status_code == 200


def test_catalog_endpoint_matches_the_catalog(client):
    data = client.get("/api/console/catalog").json()
    from payable.catalog import CATALOG

    assert len(data["products"]) == len(CATALOG.products)
    assert data["merchant"]["display_name"] == CATALOG.merchant.display_name


def test_tasks_endpoint_serves_the_benchmark_suite(client):
    data = client.get("/api/console/tasks").json()
    assert len(data["tasks"]) >= 20
    assert any(t["expected_sku"] is None for t in data["tasks"]), "no trap tasks offered"


# -- running ---------------------------------------------------------------

def test_payable_arm_buys_the_right_thing(client):
    data = run(client, CLEAR, "payable")
    assert data["verdict"]["grade"] == "correct"
    assert data["result"]["purchased_sku"] == "KB-MECH-87-BRN"
    assert data["events"], "a run must produce an audit trail"


def test_a_run_returns_its_full_audit_trail(client):
    data = run(client, CLEAR, "payable")
    steps = [e["step"] for e in data["events"]]
    assert steps[0] == "run_start"
    assert steps[-1] == "run_complete"
    assert "mandate_check" in steps


def test_payable_runs_present_a_mandate_the_merchant_verified(client):
    data = run(client, CLEAR, "payable")
    mandate = data["mandate"]
    assert mandate["alg"] == "Ed25519"
    assert mandate["public_key"], "the console should show the key the merchant holds"
    assert mandate["signature"] != mandate["public_key"]


def test_legacy_runs_carry_no_mandate(client):
    """The storefront has no way to check one, which is the point."""
    assert run(client, CLEAR, "legacy-strict")["mandate"] is None


# -- grading ---------------------------------------------------------------

def test_refusing_a_trap_scores_as_correct(client):
    """The regression this file exists for.

    A trap's ground truth is `expected_sku is None`. If that were read as "no
    ground truth", every correct refusal would show as unscored and the console
    would fail to credit the behaviour the project is arguing for.
    """
    data = run(client, AMBIGUOUS, "payable")
    assert data["result"]["outcome"] == "abstained"
    assert data["verdict"]["grade"] == "correct"
    assert "refused" in data["verdict"]["label"].lower()


def test_buying_into_a_trap_scores_as_wrong(client):
    data = run(client, AMBIGUOUS, "legacy-optimistic")
    assert data["result"]["outcome"] == "purchased"
    assert data["verdict"]["grade"] == "wrong"
    assert "nothing at all" in data["verdict"]["label"]


def test_abstaining_on_a_buyable_task_scores_as_a_missed_sale(client):
    unverifiable = {
        "task_id": "hp-anc-depth-40",
        "brief": "Wireless noise cancelling headphones, at least 40 dB of cancellation.",
        "category": "headphones",
        "budget_paise": 1_200_000,
        "expected_sku": "HP-ANC-OVR-01",
        "graded": True,
        "constraints": [
            {"field": "anc", "op": "true"},
            {"field": "anc_depth_db", "op": "gte", "value": 40},
        ],
    }
    data = run(client, unverifiable, "legacy-strict")
    assert data["verdict"]["grade"] == "missed"
    assert "HP-ANC-OVR-01" in data["verdict"]["label"]


def test_an_ungraded_freeform_brief_is_reported_not_graded(client):
    data = run(client, {
        "task_id": "console", "brief": "a quiet mouse for my desk",
        "budget_paise": 500_000, "graded": False, "constraints": [],
    }, "payable")
    assert data["verdict"]["grade"] == "unscored"


# -- input handling --------------------------------------------------------

def test_an_empty_brief_is_rejected(client):
    assert client.post("/api/console/run", json={"brief": "  ", "arm": "payable"}).status_code == 400


def test_an_unknown_arm_is_rejected(client):
    assert client.post(
        "/api/console/run", json={"brief": "x", "arm": "not-an-arm"}
    ).status_code == 400


def test_a_malformed_constraint_is_rejected(client):
    response = client.post("/api/console/run", json={
        "brief": "x", "arm": "payable",
        "constraints": [{"field": "layout", "op": "definitely-not-an-op"}],
    })
    assert response.status_code == 400


def test_runs_do_not_deplete_inventory_for_the_next_visitor(client):
    """The console resets stock per run, so one demo cannot exhaust the shop."""
    from payable.catalog import CATALOG

    before = CATALOG.get("KB-MECH-87-BRN").stock
    for _ in range(3):
        run(client, CLEAR, "payable")
    assert CATALOG.get("KB-MECH-87-BRN").stock == before - 1
