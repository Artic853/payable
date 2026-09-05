"""Backend for the web console.

The console runs the *real* buyer agent against the *real* merchant surface --
it is not a replay of canned output. A demo run issues a live mandate, calls the
MCP tools (or scrapes the storefront, for a legacy arm) over HTTP, and returns
the run result together with the audit trail it produced.

The self-request is deliberate: these are sync endpoints, so FastAPI runs them in
a threadpool and a blocking HTTP call back to this same server is safe. It also
means the console exercises exactly the surface an external agent would, rather
than a shortcut through the Python API.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator
from pathlib import Path

import httpx
from fastapi import APIRouter, Body, HTTPException, Request

from ..agent.base import BuyerIntent, BuyerPolicy
from ..agent.legacy_buyer import LegacyBuyer
from ..agent.payable_buyer import PayableBuyer
from ..audit import AUDIT
from ..bench.runner import DEFAULT_TASKS
from ..catalog import CATALOG
from ..config import DATA_DIR, SETTINGS
from ..mandate import KEYRING, issue_mandate
from ..models import SpecConstraint
from ..state import reset_all

router = APIRouter(tags=["console"])

BENCHMARK_PATH = DATA_DIR / "benchmark.json"

ARMS = {
    "payable": ("payable", BuyerPolicy.strict),
    "legacy-strict": ("legacy", BuyerPolicy.strict),
    "legacy-optimistic": ("legacy", BuyerPolicy.optimistic),
}


@contextlib.contextmanager
def client_for(base_url: str) -> Iterator[httpx.Client]:
    """The HTTP client a console run drives the merchant with.

    Factored out as a seam: a test harness talks to the app in-process, where a
    self-request to the real socket cannot be reached, so it overrides this to
    yield its own client.
    """
    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        yield client


@router.get("/api/console/catalog")
def console_catalog() -> dict:
    return {
        "merchant": CATALOG.merchant.model_dump(),
        "products": [
            {**p.model_dump(), "price_inr": p.price_paise / 100, "mrp_inr": p.mrp_paise / 100}
            for p in CATALOG.products
        ],
    }


@router.get("/api/console/tasks")
def console_tasks() -> dict:
    """Benchmark tasks, offered as presets in the console.

    `expected_sku` is included here on purpose: the console is a human-facing
    scoreboard, so it shows whether the agent got it right. The agent itself
    still never receives it -- see `BuyerIntent.agent_view`.
    """
    raw = json.loads(Path(DEFAULT_TASKS).read_text(encoding="utf-8"))
    return {"tasks": raw["tasks"]}


@router.get("/api/console/benchmark")
def console_benchmark() -> dict:
    if not BENCHMARK_PATH.exists():
        raise HTTPException(
            status_code=404,
            detail="No benchmark results committed. Run: python -m payable.bench.runner --repeats 5",
        )
    return json.loads(BENCHMARK_PATH.read_text(encoding="utf-8"))


@router.post("/api/console/run")
def console_run(request: Request, payload: dict = Body(...)) -> dict:
    """Run one buyer task live and return the result plus its audit trail."""
    arm = payload.get("arm", "payable")
    if arm not in ARMS:
        raise HTTPException(status_code=400, detail=f"unknown arm {arm!r}")

    brief = (payload.get("brief") or "").strip()
    if not brief:
        raise HTTPException(status_code=400, detail="brief is required")

    try:
        constraints = [SpecConstraint(**c) for c in payload.get("constraints", [])]
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"malformed constraint: {exc}")

    intent = BuyerIntent(
        task_id=payload.get("task_id") or "console",
        brief=brief,
        category=payload.get("category") or None,
        constraints=constraints,
        quantity=int(payload.get("quantity", 1)),
        budget_paise=int(payload.get("budget_paise", 1_000_000)),
        expected_sku=payload.get("expected_sku"),
    )

    if payload.get("reset_inventory", True):
        reset_all()

    base_url = str(request.base_url).rstrip("/")
    kind, policy_factory = ARMS[arm]
    policy = policy_factory()

    with client_for(base_url) as client:
        if kind == "payable":
            mandate = issue_mandate(
                principal="user:console",
                agent_id="agent:console-buyer",
                max_amount_paise=intent.budget_paise,
                allowed_categories=[intent.category] if intent.category else [],
            )
            result = PayableBuyer(client, policy=policy, principal="user:console").run(
                intent, mandate=mandate
            )
            mandate_view = {
                "mandate_id": mandate.mandate_id,
                "principal": mandate.principal,
                "agent_id": mandate.agent_id,
                "alg": mandate.alg,
                "max_amount_paise": mandate.max_amount_paise,
                "allowed_categories": mandate.allowed_categories,
                "signature": mandate.signature,
                "public_key": KEYRING.export_public_keys().get(mandate.principal),
            }
        else:
            result = LegacyBuyer(client, policy=policy).run(intent)
            mandate_view = None

    events = [e.model_dump() for e in AUDIT.events(run_id=result.run_id)]
    scored = _score(result, graded=bool(payload.get("graded", True)))

    return {
        "arm": arm,
        "policy": policy.name,
        "result": result.model_dump(mode="json"),
        "mandate": mandate_view,
        "events": events,
        "verdict": scored,
        "config": SETTINGS.describe(),
    }


def _score(result, graded: bool) -> dict:
    """How a human should read this run.

    `graded` has to be passed explicitly rather than inferred from
    `expected_sku`, because a trap task's ground truth *is* "expected_sku is
    None" -- refusing to buy is the right answer. Inferring would silently mark
    every correct refusal as unscored, which is precisely the behaviour this
    project exists to reward.
    """
    if not graded:
        # Freeform brief with no ground truth: report, do not grade.
        if result.outcome == "purchased":
            return {"grade": "unscored", "label": f"Bought {result.purchased_sku}"}
        return {"grade": "unscored", "label": f"Did not buy ({result.failure_code.value})"}

    if result.bought_correct_item:
        return {"grade": "correct", "label": f"Correct: bought {result.purchased_sku}"}
    if result.bought_wrong_item:
        return {
            "grade": "wrong",
            "label": (
                f"Wrong item: bought {result.purchased_sku}, expected "
                f"{result.expected_sku or 'nothing at all'}"
            ),
        }
    if result.expected_sku is None:
        return {"grade": "correct", "label": "Correctly refused to buy"}
    return {
        "grade": "missed",
        "label": f"Missed the sale: {result.expected_sku} was buyable",
    }
