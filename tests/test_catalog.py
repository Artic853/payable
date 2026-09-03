"""Search has to be right about what it cannot offer, not just about what it can."""

from payable.catalog import CATALOG
from payable.models import AdvisoryCode, SearchRequest, SpecConstraint


def search(**kwargs):
    return CATALOG.search(SearchRequest(**kwargs))


def test_hard_constraint_violation_disqualifies():
    response = search(
        query="tkl keyboard",
        category="keyboard",
        constraints=[SpecConstraint(field="switch_type", op="eq", value="brown-tactile")],
    )
    winner = response.candidates[0]
    assert winner.product.sku == "KB-MECH-87-BRN"
    assert winner.fully_satisfies

    red = next(c for c in response.candidates if c.product.sku == "KB-MECH-87-RED")
    assert not red.fully_satisfies
    assert red.unmet_hard_constraints


def test_soft_constraints_rank_but_never_disqualify():
    response = search(
        query="keyboard",
        category="keyboard",
        constraints=[SpecConstraint(field="backlight", op="eq", value="rgb", hard=False)],
    )
    membrane = next(c for c in response.candidates if c.product.sku == "KB-MEMB-104-BLK")
    assert membrane.unmet_constraints          # it does miss the preference
    assert not membrane.unmet_hard_constraints  # but is still purchasable
    assert membrane.fully_satisfies


def test_contains_op_matches_inside_list_valued_specs():
    response = search(
        query="wireless keyboard",
        category="keyboard",
        constraints=[SpecConstraint(field="connectivity", op="contains", value="bluetooth")],
    )
    viable = {c.product.sku for c in response.candidates if c.fully_satisfies}
    assert viable == {"KB-MECH-87-BRN", "KB-MECH-87-RED"}


def test_not_contains_op_excludes_wireless_models():
    response = search(
        query="wired keyboard",
        category="keyboard",
        constraints=[SpecConstraint(field="connectivity", op="not_contains", value="bluetooth")],
    )
    viable = {c.product.sku for c in response.candidates if c.fully_satisfies}
    assert viable == {"KB-MECH-104-BRN", "KB-MEMB-104-BLK"}


def test_out_of_stock_is_hidden_by_default():
    response = search(query="gaming mouse 8k polling", category="mouse")
    assert "MS-GAME-8K-01" not in {c.product.sku for c in response.candidates}


def test_out_of_stock_is_visible_when_asked_for():
    response = search(query="gaming mouse", category="mouse", in_stock_only=False)
    assert "MS-GAME-8K-01" in {c.product.sku for c in response.candidates}


def test_two_equal_candidates_raise_an_ambiguity_advisory():
    """The keyboards differ only in switch feel -- a choice the buyer never made."""
    response = search(
        query="hot swappable tenkeyless bluetooth keyboard",
        category="keyboard",
        constraints=[
            SpecConstraint(field="layout", op="eq", value="TKL-87"),
            SpecConstraint(field="connectivity", op="contains", value="bluetooth"),
            SpecConstraint(field="hot_swappable", op="true"),
        ],
    )
    assert response.advisory_code is AdvisoryCode.AMBIGUOUS
    assert "switch_type" in response.advisory


def test_nothing_satisfying_is_reported_distinctly_from_ambiguity():
    response = search(
        query="numeric keypad",
        constraints=[SpecConstraint(field="layout", op="eq", value="numpad-22")],
    )
    assert response.advisory_code is AdvisoryCode.NO_SATISFYING_CANDIDATE
    assert not any(c.fully_satisfies for c in response.candidates)


def test_empty_result_is_reported_distinctly():
    response = search(query="anything", category="keyboard", max_price_paise=1)
    assert response.advisory_code is AdvisoryCode.EMPTY
    assert response.candidates == []


def test_a_single_clear_winner_carries_no_advisory():
    response = search(
        query="4k monitor usb-c",
        category="monitor",
        constraints=[SpecConstraint(field="resolution", op="eq", value="3840x2160")],
    )
    assert response.advisory_code is AdvisoryCode.NONE
    assert response.advisory is None


def test_reserve_decrements_and_refuses_beyond_stock():
    product = CATALOG.get("KB-MECH-87-RED")
    assert product.stock == 8

    assert CATALOG.reserve("KB-MECH-87-RED", 8) is True
    assert product.stock == 0
    assert CATALOG.reserve("KB-MECH-87-RED", 1) is False

    CATALOG.release("KB-MECH-87-RED", 8)
    assert product.stock == 8


def test_jsonld_offer_points_at_the_transactable_endpoint():
    product = CATALOG.get("KB-MECH-87-BRN")
    doc = product.jsonld("http://testserver", CATALOG.merchant)
    assert doc["@type"] == "Product"
    assert doc["offers"]["availability"] == "https://schema.org/InStock"
    assert doc["offers"]["potentialAction"]["@type"] == "BuyAction"
    assert doc["offers"]["potentialAction"]["target"].endswith("/mcp")
