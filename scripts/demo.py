"""One agent purchase, narrated end to end, then replayed from the audit log.

    python scripts/demo.py            # a purchase that should go through
    python scripts/demo.py --refuse   # a purchase the agent should refuse to make
    python scripts/demo.py --decline  # forced payment decline and bounded fallback

Runs in-process by default, so no server is needed. Point it at a running
instance with --base-url http://127.0.0.1:8000 to watch it over real HTTP.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from payable.agent import BuyerIntent, BuyerPolicy, PayableBuyer  # noqa: E402
from payable.audit import AUDIT  # noqa: E402
from payable.bench.runner import make_client  # noqa: E402
from payable.config import SETTINGS  # noqa: E402
from payable.mandate import KEYRING, issue_mandate  # noqa: E402
from payable.models import SpecConstraint  # noqa: E402
from payable.payments import set_gateway  # noqa: E402
from payable.payments.simulated import SimulatedRazorpayGateway  # noqa: E402
from payable.state import reset_all  # noqa: E402

RULE = "-" * 78


def heading(text: str) -> None:
    print(f"\n{RULE}\n{text}\n{RULE}")


BUY = BuyerIntent(
    task_id="demo-buy",
    brief="Wireless noise cancelling headphones for long flights, at least 40 dB of cancellation.",
    category="headphones",
    budget_paise=1_200_000,
    expected_sku="HP-ANC-OVR-01",
    constraints=[
        SpecConstraint(field="anc", op="true"),
        SpecConstraint(field="anc_depth_db", op="gte", value=40),
        SpecConstraint(field="connectivity", op="contains", value="bluetooth"),
    ],
)

REFUSE = BuyerIntent(
    task_id="demo-refuse",
    brief="Get me a hot swappable tenkeyless keyboard with bluetooth.",
    category="keyboard",
    budget_paise=800_000,
    constraints=[
        SpecConstraint(field="layout", op="eq", value="TKL-87"),
        SpecConstraint(field="connectivity", op="contains", value="bluetooth"),
        SpecConstraint(field="hot_swappable", op="true"),
    ],
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refuse", action="store_true", help="run the task the agent should refuse")
    parser.add_argument("--decline", action="store_true", help="force the gateway to decline first")
    parser.add_argument("--base-url", default=None, help="drive a live server instead of in-process")
    args = parser.parse_args()

    reset_all()
    intent = REFUSE if args.refuse else BUY

    heading("CONFIGURATION")
    for key, value in SETTINGS.describe().items():
        print(f"  {key:<18} {value}")
    if args.decline:
        # A rate this high guarantees the first attempt fails so the bounded
        # fallback is visible; retries still re-roll and usually recover.
        set_gateway(SimulatedRazorpayGateway(failure_rate=0.55, seed=6))
        print(f"  {'fault injection':<18} forced decline (rate 0.55, seed 6)")

    client = make_client(args.base_url)

    heading("BUYER INTENT")
    print(f"  brief      {intent.brief}")
    print(f"  category   {intent.category}")
    print(f"  budget     INR {intent.budget_paise / 100:,.2f}")
    print("  constraints")
    for constraint in intent.constraints:
        kind = "hard" if constraint.hard else "soft"
        print(f"    - {constraint.describe():<40} [{kind}]")

    mandate = issue_mandate(
        principal="user:demo",
        agent_id="agent:payable-buyer",
        max_amount_paise=intent.budget_paise,
        allowed_categories=[intent.category] if intent.category else [],
    )
    heading("MANDATE ISSUED BY THE PRINCIPAL")
    print(f"  mandate_id   {mandate.mandate_id}")
    print(f"  principal    {mandate.principal}")
    print(f"  cap          INR {mandate.max_amount_paise / 100:,.2f}")
    print(f"  scope        {mandate.allowed_categories or 'any category'}")
    print(f"  algorithm    {mandate.alg}")
    print(f"  signature    {mandate.signature[:48]}...")
    public_key = KEYRING.export_public_keys().get(mandate.principal)
    if public_key:
        print(f"  merchant has {public_key[:48]}...  (public key only)")
    print("  The merchant re-verifies signature, expiry, cap and scope before an")
    print("  order exists -- and holds no key that could mint this mandate.")

    result = PayableBuyer(client, policy=BuyerPolicy.strict()).run(intent, mandate=mandate)

    heading("OUTCOME")
    print(f"  outcome            {result.outcome.upper()}")
    if result.outcome == "purchased":
        print(f"  purchased          {result.purchased_sku}")
        print(f"  amount             INR {result.amount_paise / 100:,.2f}")
        print(f"  merchant order     {result.order_id}")
        print(f"  gateway payment    {result.payment_id}")
    else:
        print(f"  failure code       {result.failure_code.value}")
        print(f"  reason             {result.abstain_reason}")
        print("  nothing was charged.")
    print(f"  payment attempts   {result.payment_attempts} ({result.payment_declines} declined)")
    print(f"  http calls         {result.http_calls}")
    print(f"  wall time          {result.latency_ms:,.0f} ms")
    for note in result.notes:
        print(f"  note               {note}")

    heading("STAGE LATENCY")
    stages = result.stages
    for label, value in [
        ("discover", stages.discover_ms), ("select", stages.select_ms),
        ("quote", stages.quote_ms), ("order", stages.order_ms), ("pay", stages.pay_ms),
    ]:
        bar = "#" * int(value / 4) if value else ""
        print(f"  {label:<10} {value:>8,.1f} ms  {bar}")

    heading(f"AUDIT TRAIL  (backend: {AUDIT.backend}, run_id: {result.run_id})")
    for event in AUDIT.events(run_id=result.run_id):
        print(f"  [{event.actor:<11}] {event.step:<16} -> {event.decision or '-'}")
        if event.rationale:
            print(f"                 why: {event.rationale}")

    print(f"\n{RULE}")
    print("  Every line above is reconstructable after the fact:")
    print(f"    GET /api/audit/runs/{result.run_id}")
    print(RULE)


if __name__ == "__main__":
    main()
