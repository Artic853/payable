"""Guards on the benchmark itself.

A benchmark is a claim, and these are the checks that make the claim auditable:
the ground truth is self-consistent, no arm gets an advantage the others do not,
the agent never sees the answer, and two runs agree.
"""

import pytest

from payable.bench.runner import load_tasks, run_benchmark, summarize
from payable.catalog import CATALOG
from payable.commerce import price_for
from payable.models import AdvisoryCode, SearchRequest


TASKS = load_tasks()


def satisfying_skus(intent):
    """Every in-stock SKU that clears all of a task's hard constraints."""
    response = CATALOG.search(SearchRequest(
        query=intent.brief,
        category=intent.category,
        constraints=intent.constraints,
        in_stock_only=True,
        limit=50,
    ))
    return [c for c in response.candidates if c.fully_satisfies]


# -- ground truth ----------------------------------------------------------

@pytest.mark.parametrize("intent", [t for t in TASKS if t.expected_sku], ids=lambda i: i.task_id)
def test_expected_sku_is_actually_buyable(intent):
    """The answer key must be reachable: in the catalog, in stock, in budget."""
    product = CATALOG.get(intent.expected_sku)
    assert product is not None, f"{intent.expected_sku} is not in the catalog"
    assert product.stock >= intent.quantity, "answer key is not stocked in the required quantity"

    total = price_for(product.price_paise, intent.quantity).total_paise
    assert total <= intent.budget_paise, (
        f"{intent.task_id}: answer key costs {total} but the budget is "
        f"{intent.budget_paise} -- the budget must cover GST and shipping"
    )


@pytest.mark.parametrize("intent", [t for t in TASKS if t.expected_sku], ids=lambda i: i.task_id)
def test_expected_sku_is_the_unique_satisfying_option(intent):
    """If two SKUs fit, the task has no single right answer and must be a trap."""
    viable = satisfying_skus(intent)
    assert [c.product.sku for c in viable] == [intent.expected_sku]


@pytest.mark.parametrize("intent", [t for t in TASKS if not t.expected_sku], ids=lambda i: i.task_id)
def test_trap_tasks_are_genuinely_unbuyable(intent):
    """A trap must be unbuyable for a *stated* reason, not by accident."""
    response = CATALOG.search(SearchRequest(
        query=intent.brief, category=intent.category,
        constraints=intent.constraints, in_stock_only=True, limit=50,
    ))
    viable = [c for c in response.candidates if c.fully_satisfies]

    if not viable:
        return  # nothing fits: correct to refuse

    if response.advisory_code is AdvisoryCode.AMBIGUOUS:
        return  # several fit equally: correct to ask

    # Otherwise the only honest reasons left are stock or budget.
    reasons = []
    for candidate in viable:
        product = candidate.product
        if product.stock < intent.quantity:
            reasons.append("stock")
        elif price_for(product.price_paise, intent.quantity).total_paise > intent.budget_paise:
            reasons.append("budget")
    assert len(reasons) == len(viable), (
        f"{intent.task_id}: {[c.product.sku for c in viable]} are buyable, "
        "so the task is not actually a trap"
    )


def test_task_ids_are_unique():
    ids = [t.task_id for t in TASKS]
    assert len(ids) == len(set(ids))


def test_the_suite_tests_both_buying_and_refusing():
    buys = [t for t in TASKS if t.expected_sku]
    traps = [t for t in TASKS if not t.expected_sku]
    assert len(buys) >= 5 and len(traps) >= 5, "a lopsided suite is easy to game"


# -- fairness --------------------------------------------------------------

def test_ground_truth_is_withheld_from_the_agent():
    """An agent that could see `expected_sku` would prove nothing."""
    intent = next(t for t in TASKS if t.expected_sku)
    view = intent.agent_view()
    assert "expected_sku" not in view
    assert "expect_abstain" not in view
    assert intent.expected_sku not in str(view)


def test_every_arm_receives_identical_tasks():
    first, second = load_tasks(), load_tasks()
    assert [t.model_dump() for t in first] == [t.model_dump() for t in second]


# -- reproducibility -------------------------------------------------------

def test_two_runs_of_the_benchmark_agree():
    arms = ("payable", "legacy-optimistic")

    def outcomes(report):
        return {
            arm: [(r["task_id"], r["outcome"], r["purchased_sku"], r["failure_code"])
                  for r in runs]
            for arm, runs in report["runs"].items()
        }

    assert outcomes(run_benchmark(arms=arms, seed=4242)) == \
           outcomes(run_benchmark(arms=arms, seed=4242))


def test_a_different_seed_changes_only_payment_luck():
    """Selection must be seed-independent; only declines may move."""
    def selections(report):
        return {
            arm: [(r["task_id"], r["purchased_sku"]) for r in runs if r["outcome"] == "purchased"]
            for arm, runs in report["runs"].items()
        }

    quiet = run_benchmark(arms=("payable",), seed=1729, failure_rate=0.0)
    noisy = run_benchmark(arms=("payable",), seed=99, failure_rate=0.0)
    assert selections(quiet) == selections(noisy)


def test_no_payment_failures_means_no_payment_failures():
    report = run_benchmark(arms=("payable",), failure_rate=0.0)
    summary = report["summaries"][0]
    assert summary["payment_declines"] == 0
    assert "payment_declined" not in summary["failure_taxonomy"]


# -- metrics ---------------------------------------------------------------

def test_summary_counts_reconcile():
    report = run_benchmark(arms=("payable", "legacy-optimistic"), failure_rate=0.0)
    for summary in report["summaries"]:
        assert summary["correct_purchases"] + summary["wrong_purchases"] == summary["purchases"]
        assert summary["buy_tasks"] + summary["trap_tasks"] == summary["tasks"]
        assert summary["correct_abstentions"] <= summary["trap_tasks"]


def test_wrong_purchases_are_the_only_source_of_misspend():
    report = run_benchmark(arms=("legacy-optimistic",), failure_rate=0.0)
    summary = report["summaries"][0]
    wrong = [r for r in report["runs"]["legacy-optimistic"]
             if r["outcome"] == "purchased" and r["purchased_sku"] != r["expected_sku"]]
    assert summary["misspent_paise"] == sum(r["amount_paise"] for r in wrong)


def test_summarize_handles_an_empty_arm():
    summary = summarize("empty", [])
    assert summary["tasks"] == 0
    assert summary["transaction_success_pct"] == 0.0
    assert summary["wrong_item_rate_pct"] == 0.0
