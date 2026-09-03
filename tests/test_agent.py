"""Buyer behaviour, especially the refusals."""

import pytest

from payable.agent import BuyerIntent, BuyerPolicy, LegacyBuyer, PayableBuyer
from payable.audit import AUDIT
from payable.models import FailureCode, SpecConstraint
from payable.payments import set_gateway
from payable.payments.simulated import SimulatedRazorpayGateway


@pytest.fixture
def reliable_gateway():
    set_gateway(SimulatedRazorpayGateway(failure_rate=0.0))


def intent(**kwargs):
    base = dict(task_id="t", brief="", budget_paise=10_000_000, constraints=[])
    base.update(kwargs)
    base["constraints"] = [
        c if isinstance(c, SpecConstraint) else SpecConstraint(**c)
        for c in base["constraints"]
    ]
    return BuyerIntent(**base)


# -- payable arm -----------------------------------------------------------

def test_buys_the_item_that_satisfies_the_constraints(client, reliable_gateway):
    result = PayableBuyer(client).run(intent(
        brief="tenkeyless bluetooth keyboard with tactile switches",
        category="keyboard",
        constraints=[
            {"field": "layout", "op": "eq", "value": "TKL-87"},
            {"field": "switch_type", "op": "eq", "value": "brown-tactile"},
        ],
        expected_sku="KB-MECH-87-BRN",
    ))
    assert result.outcome == "purchased"
    assert result.purchased_sku == "KB-MECH-87-BRN"
    assert result.bought_correct_item
    assert result.payment_id


def test_abstains_rather_than_guessing_between_equal_options(client, reliable_gateway):
    result = PayableBuyer(client).run(intent(
        brief="hot swappable tenkeyless bluetooth keyboard",
        category="keyboard",
        constraints=[
            {"field": "layout", "op": "eq", "value": "TKL-87"},
            {"field": "hot_swappable", "op": "true"},
            {"field": "connectivity", "op": "contains", "value": "bluetooth"},
        ],
    ))
    assert result.outcome == "abstained"
    assert result.failure_code is FailureCode.AMBIGUOUS
    assert result.purchased_sku is None


def test_reports_no_match_rather_than_buying_something_adjacent(client, reliable_gateway):
    result = PayableBuyer(client).run(intent(
        brief="standalone numeric keypad",
        constraints=[{"field": "layout", "op": "eq", "value": "numpad-22"}],
    ))
    assert result.outcome == "abstained"
    assert result.failure_code is FailureCode.NO_MATCH


def test_refuses_a_quantity_the_merchant_cannot_supply(client, reliable_gateway):
    result = PayableBuyer(client).run(intent(
        brief="fifteen tenkeyless keyboards with red linear switches",
        category="keyboard",
        quantity=15,
        constraints=[{"field": "switch_type", "op": "eq", "value": "red-linear"}],
    ))
    assert result.outcome == "abstained"
    assert result.failure_code is FailureCode.OUT_OF_STOCK


def test_stops_when_the_total_exceeds_the_budget(client, reliable_gateway):
    result = PayableBuyer(client).run(intent(
        brief="4k monitor",
        category="monitor",
        budget_paise=2_000_000,  # the panel plus GST costs more
        constraints=[{"field": "resolution", "op": "eq", "value": "3840x2160"}],
    ))
    assert result.outcome == "abstained"
    assert result.failure_code in {FailureCode.OVER_BUDGET, FailureCode.SPEC_MISMATCH}
    assert result.amount_paise == 0


def test_a_non_retriable_decline_is_not_retried(client):
    """Retrying a hard decline just annoys the issuer; the agent stops."""
    set_gateway(SimulatedRazorpayGateway(failure_rate=1.0))
    result = PayableBuyer(client).run(intent(
        brief="240W usb-c cable",
        category="cable",
        constraints=[{"field": "power_w", "op": "gte", "value": 240}],
    ))
    assert result.outcome == "failed"
    assert result.failure_code in {FailureCode.PAYMENT_DECLINED, FailureCode.GATEWAY_ERROR}
    assert result.payment_attempts <= BuyerPolicy.strict().max_payment_attempts


def test_every_run_is_reconstructable_from_the_audit_log(client, reliable_gateway):
    result = PayableBuyer(client).run(intent(
        brief="vertical ergonomic bluetooth mouse",
        category="mouse",
        constraints=[{"field": "grip", "op": "eq", "value": "vertical"}],
    ))
    events = AUDIT.events(run_id=result.run_id)
    steps = [e.step for e in events]

    assert steps[0] == "run_start"
    assert steps[-1] == "run_complete"
    for required in ("search", "select", "quote", "mandate_check", "order_created", "payment_attempt"):
        assert required in steps, f"{required} missing from audit trail"

    # The selection must record why, not merely what.
    select = next(e for e in events if e.step == "select")
    assert select.decision == result.purchased_sku
    assert select.rationale


def test_an_abstention_records_its_reason(client, reliable_gateway):
    result = PayableBuyer(client).run(intent(
        brief="standalone numeric keypad",
        constraints=[{"field": "layout", "op": "eq", "value": "numpad-22"}],
    ))
    events = AUDIT.events(run_id=result.run_id)
    abstain = next(e for e in events if e.step == "abstain")
    assert abstain.actor == "policy"
    assert abstain.rationale


# -- legacy arm ------------------------------------------------------------

def test_strict_scraper_abstains_on_a_spec_it_cannot_verify(client, reliable_gateway):
    """`anc_depth_db` is on the page, but not under a label the scraper maps."""
    result = LegacyBuyer(client, policy=BuyerPolicy.strict()).run(intent(
        brief="wireless noise cancelling headphones for flights",
        category="headphones",
        constraints=[
            {"field": "anc", "op": "true"},
            {"field": "anc_depth_db", "op": "gte", "value": 40},
        ],
        expected_sku="HP-ANC-OVR-01",
    ))
    assert result.outcome == "abstained"
    assert result.failure_code is FailureCode.PARSE_ERROR


def test_optimistic_scraper_buys_through_the_same_uncertainty(client, reliable_gateway):
    result = LegacyBuyer(client, policy=BuyerPolicy.optimistic()).run(intent(
        brief="wireless noise cancelling headphones for flights",
        category="headphones",
        constraints=[
            {"field": "anc", "op": "true"},
            {"field": "anc_depth_db", "op": "gte", "value": 40},
        ],
        expected_sku="HP-ANC-OVR-01",
    ))
    assert result.outcome == "purchased"
    # It bought the cheaper of two it could not tell apart, which is the wrong one.
    assert result.bought_wrong_item
    assert result.purchased_sku == "HP-ANC-TWS-01"
    assert any("unverifiable" in note for note in result.notes)


def test_scraper_still_succeeds_when_every_label_maps(client, reliable_gateway):
    """The baseline is competent where the page happens to be legible."""
    result = LegacyBuyer(client, policy=BuyerPolicy.strict()).run(intent(
        brief="tenkeyless bluetooth keyboard tactile switches",
        category="keyboard",
        constraints=[
            {"field": "layout", "op": "eq", "value": "TKL-87"},
            {"field": "switch_type", "op": "eq", "value": "brown-tactile"},
        ],
        expected_sku="KB-MECH-87-BRN",
    ))
    assert result.outcome == "purchased"
    assert result.bought_correct_item
