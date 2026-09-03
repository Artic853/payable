"""Shared buyer-agent vocabulary: intents, policy, and the result of one run."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..models import FailureCode, SpecConstraint

Outcome = Literal["purchased", "abstained", "failed"]


class BuyerIntent(BaseModel):
    """What the principal asked for.

    `brief` is the natural-language ask. `constraints` are the same requirements
    in machine-checkable form -- the buyer's own understanding of its brief, not
    something the merchant handed it. Both arms of the benchmark receive exactly
    this; what differs is whether the merchant can be *queried* with it.
    """

    task_id: str
    brief: str
    category: str | None = None
    constraints: list[SpecConstraint] = Field(default_factory=list)
    quantity: int = 1
    budget_paise: int = 1_000_000
    expected_sku: str | None = None          # ground truth; never shown to the agent
    acceptable_skus: list[str] = Field(default_factory=list)
    expect_abstain: bool = False             # task is a trap: correct move is to not buy
    note: str = ""

    def agent_view(self) -> dict:
        """The subset an agent is allowed to see."""
        return {
            "task_id": self.task_id,
            "brief": self.brief,
            "category": self.category,
            "constraints": [c.model_dump() for c in self.constraints],
            "quantity": self.quantity,
            "budget_paise": self.budget_paise,
        }


@dataclass
class BuyerPolicy:
    """How the agent behaves when it is not sure.

    `strict` is the responsible posture: never buy against an unverified hard
    constraint, and never buy through an ambiguity advisory. `optimistic` is what
    a typical scraping agent does -- if it cannot check, it assumes the best.
    Running both is the only way to show what the transactability layer buys you,
    because a cautious agent on a bad surface fails safe (low success) while a
    confident one fails expensively (wrong item, real money).
    """

    name: str = "strict"
    buy_when_unverifiable: bool = False
    buy_through_advisory: bool = False
    max_payment_attempts: int = 3
    require_full_constraint_satisfaction: bool = True

    @classmethod
    def strict(cls) -> "BuyerPolicy":
        return cls(name="strict")

    @classmethod
    def optimistic(cls) -> "BuyerPolicy":
        return cls(
            name="optimistic",
            buy_when_unverifiable=True,
            buy_through_advisory=True,
            require_full_constraint_satisfaction=False,
        )


class StageTiming(BaseModel):
    discover_ms: float = 0.0
    select_ms: float = 0.0
    quote_ms: float = 0.0
    order_ms: float = 0.0
    pay_ms: float = 0.0

    @property
    def total_ms(self) -> float:
        return self.discover_ms + self.select_ms + self.quote_ms + self.order_ms + self.pay_ms


class RunResult(BaseModel):
    run_id: str
    arm: str
    task_id: str
    brief: str
    outcome: Outcome
    failure_code: FailureCode = FailureCode.NONE

    purchased_sku: str | None = None
    expected_sku: str | None = None
    amount_paise: int = 0
    order_id: str | None = None
    payment_id: str | None = None

    http_calls: int = 0
    payment_attempts: int = 0
    payment_declines: int = 0
    latency_ms: float = 0.0
    stages: StageTiming = Field(default_factory=StageTiming)
    abstain_reason: str = ""
    notes: list[str] = Field(default_factory=list)

    # -- scoring ---------------------------------------------------------

    @property
    def bought_correct_item(self) -> bool:
        return self.outcome == "purchased" and self.purchased_sku == self.expected_sku

    @property
    def bought_wrong_item(self) -> bool:
        """A false positive: real money spent on the wrong thing."""
        return self.outcome == "purchased" and self.purchased_sku != self.expected_sku

    @property
    def correct_abstention(self) -> bool:
        """Refused to buy on a task where refusing was the right answer."""
        return self.outcome != "purchased" and self.expected_sku is None


@dataclass
class RunContext:
    """Mutable per-run bookkeeping shared by the buyer implementations."""

    run_id: str = field(default_factory=lambda: f"run_{uuid.uuid4().hex[:12]}")
    started: float = field(default_factory=time.perf_counter)
    http_calls: int = 0
    payment_attempts: int = 0
    payment_declines: int = 0
    notes: list[str] = field(default_factory=list)
    stages: StageTiming = field(default_factory=StageTiming)

    def note(self, message: str) -> None:
        self.notes.append(message)

    @property
    def elapsed_ms(self) -> float:
        return (time.perf_counter() - self.started) * 1000


def stage_timer() -> float:
    return time.perf_counter()


def since(mark: float) -> float:
    return (time.perf_counter() - mark) * 1000


def new_result(arm: str, intent: BuyerIntent, ctx: RunContext, **kwargs: Any) -> RunResult:
    return RunResult(
        run_id=ctx.run_id,
        arm=arm,
        task_id=intent.task_id,
        brief=intent.brief,
        expected_sku=intent.expected_sku,
        http_calls=ctx.http_calls,
        payment_attempts=ctx.payment_attempts,
        payment_declines=ctx.payment_declines,
        latency_ms=ctx.elapsed_ms,
        stages=ctx.stages,
        notes=list(ctx.notes),
        **kwargs,
    )
