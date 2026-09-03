"""Buyer agent against the payable (MCP) surface.

A five-node state machine -- discover, select, quote, order, pay -- with every
transition written to the audit log under one run_id, and every stop condition
mapped to a single failure code. It is deliberately not an open-ended agent loop:
in commerce the interesting property is that the agent cannot wander, and that
every rupee it spends traces back to a decision you can read afterwards.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from ..audit import AUDIT
from ..mandate import issue_mandate
from ..models import FailureCode, Mandate
from .base import (
    BuyerIntent,
    BuyerPolicy,
    RunContext,
    RunResult,
    new_result,
    since,
    stage_timer,
)

ARM = "payable"


class PayableBuyer:
    def __init__(self, client: Any, policy: BuyerPolicy | None = None, principal: str = "user:demo"):
        self.client = client
        self.policy = policy or BuyerPolicy.strict()
        self.principal = principal

    # -- MCP plumbing ----------------------------------------------------

    def _call(self, ctx: RunContext, tool: str, arguments: dict) -> dict:
        ctx.http_calls += 1
        payload = {
            "jsonrpc": "2.0",
            "id": ctx.http_calls,
            "method": "tools/call",
            "params": {"name": tool, "arguments": {**arguments, "_run_id": ctx.run_id}},
        }
        response = self.client.post("/mcp", json=payload)
        body = response.json()
        if "error" in body:
            return {"error": "gateway_error", "message": body["error"].get("message", "")}
        result = body.get("result", {})
        structured = result.get("structuredContent")
        if structured is not None:
            return structured
        # Fall back to parsing the text content block.
        try:
            return json.loads(result["content"][0]["text"])
        except Exception:
            return {"error": "parse_error", "message": "unreadable tool response"}

    def handshake(self, ctx: RunContext) -> None:
        ctx.http_calls += 1
        self.client.post("/mcp", json={
            "jsonrpc": "2.0", "id": 0, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "payable-buyer", "version": "0.1.0"}},
        })

    # -- run -------------------------------------------------------------

    def run(self, intent: BuyerIntent, mandate: Mandate | None = None) -> RunResult:
        ctx = RunContext()
        AUDIT.record(
            ctx.run_id, "buyer_agent", "run_start",
            decision=ARM,
            rationale=intent.brief,
            payload={"policy": self.policy.name, **intent.agent_view()},
        )

        mandate = mandate or issue_mandate(
            principal=self.principal,
            agent_id="agent:payable-buyer",
            max_amount_paise=intent.budget_paise,
            allowed_categories=[intent.category] if intent.category else [],
        )

        # 1. discover -----------------------------------------------------
        mark = stage_timer()
        self.handshake(ctx)
        search = self._call(ctx, "search_products", {
            "query": intent.brief,
            "category": intent.category,
            "constraints": [c.model_dump() for c in intent.constraints],
            "in_stock_only": True,
            "limit": 5,
        })
        ctx.stages.discover_ms = since(mark)

        if search.get("error"):
            return self._fail(ctx, intent, FailureCode.GATEWAY_ERROR, search.get("message", "search failed"))

        candidates = search.get("candidates", [])
        if not candidates:
            return self._abstain(ctx, intent, FailureCode.NO_MATCH, "catalog returned no candidates")

        # 2. select -------------------------------------------------------
        mark = stage_timer()
        advisory = search.get("advisory")
        advisory_code = search.get("advisory_code", "none")

        # Only a genuine tie is an *ambiguity*. "Nothing fits" is a different
        # failure and is reported as one, so the taxonomy stays diagnostic.
        if advisory_code == "ambiguous" and not self.policy.buy_through_advisory:
            ctx.stages.select_ms = since(mark)
            return self._abstain(ctx, intent, FailureCode.AMBIGUOUS, advisory or "ambiguous result")

        viable = [c for c in candidates if c.get("fully_satisfies")]
        pool = viable if viable else candidates
        if not viable and self.policy.require_full_constraint_satisfaction:
            best = candidates[0]
            missed = best.get("unmet_hard_constraints", [])
            ctx.stages.select_ms = since(mark)
            # Nothing met even one requirement: the merchant does not stock this
            # kind of thing at all, which is a different report to the principal
            # than "close, but the spec is wrong".
            code = (
                FailureCode.NO_MATCH if not best.get("met_constraints")
                else FailureCode.SPEC_MISMATCH
            )
            return self._abstain(
                ctx, intent, code,
                f"best candidate {best['sku']} violates: {', '.join(missed)}",
            )

        # Cheapest among equally-satisfying options -- a defensible tie-break
        # that also keeps spend down.
        chosen = min(pool, key=lambda c: (-round(c["match_score"], 3), c["price_paise"]))
        ctx.stages.select_ms = since(mark)

        AUDIT.record(
            ctx.run_id, "buyer_agent", "select",
            decision=chosen["sku"],
            rationale=chosen.get("rationale", ""),
            payload={
                "considered": [c["sku"] for c in candidates],
                "match_score": chosen["match_score"],
                "price_paise": chosen["price_paise"],
            },
        )

        # 3. quote --------------------------------------------------------
        mark = stage_timer()
        quote = self._call(ctx, "create_quote", {
            "sku": chosen["sku"], "quantity": intent.quantity,
        })
        ctx.stages.quote_ms = since(mark)

        if quote.get("error"):
            return self._fail(ctx, intent, FailureCode(quote["error"]), quote.get("message", ""))
        if not quote.get("available"):
            return self._abstain(ctx, intent, FailureCode.OUT_OF_STOCK, quote.get("availability_note", ""))

        total = quote["breakdown"]["total_paise"]
        if total > intent.budget_paise:
            return self._abstain(
                ctx, intent, FailureCode.OVER_BUDGET,
                f"total {total} exceeds budget {intent.budget_paise}",
            )

        # 4. order --------------------------------------------------------
        mark = stage_timer()
        idempotency_key = f"idem_{ctx.run_id}_{chosen['sku']}"
        order = self._call(ctx, "place_order", {
            "quote_id": quote["quote_id"],
            "mandate": mandate.model_dump(),
            "idempotency_key": idempotency_key,
            "buyer_reference": intent.task_id,
        })
        ctx.stages.order_ms = since(mark)

        if order.get("error"):
            code = _as_failure(order["error"])
            return self._fail(ctx, intent, code, order.get("message", ""))

        # 5. pay ----------------------------------------------------------
        mark = stage_timer()
        result = None
        for attempt in range(1, self.policy.max_payment_attempts + 1):
            ctx.payment_attempts = attempt
            result = self._call(ctx, "pay_order", {
                "order_id": order["order_id"],
                "method": "upi" if attempt == 1 else "card",  # bounded fallback
            })
            if result.get("error"):
                ctx.stages.pay_ms = since(mark)
                return self._fail(ctx, intent, _as_failure(result["error"]), result.get("message", ""))
            if result.get("status") == "captured":
                break
            ctx.payment_declines += 1
            if not result.get("retriable"):
                ctx.note(f"attempt {attempt} failed non-retriably: {result.get('failure_reason')}")
                break
            ctx.note(f"attempt {attempt} failed retriably: {result.get('failure_reason')}; switching method")
        ctx.stages.pay_ms = since(mark)

        assert result is not None
        if result.get("status") != "captured":
            return self._fail(
                ctx, intent,
                FailureCode(result.get("failure_code") or FailureCode.PAYMENT_DECLINED.value),
                result.get("failure_reason", "payment not captured"),
            )

        outcome = new_result(
            ARM, intent, ctx,
            outcome="purchased",
            purchased_sku=order["sku"],
            amount_paise=order["amount_paise"],
            order_id=order["order_id"],
            payment_id=result.get("payment_id"),
        )
        AUDIT.record(
            ctx.run_id, "buyer_agent", "run_complete",
            decision="purchased",
            rationale=f"{order['sku']} for {order['amount_paise'] / 100:.2f} INR",
            payload={"intent": intent.brief, "order_id": order["order_id"], "arm": ARM},
            latency_ms=ctx.elapsed_ms,
        )
        return outcome

    # -- terminations ----------------------------------------------------

    def _abstain(self, ctx: RunContext, intent: BuyerIntent, code: FailureCode, reason: str) -> RunResult:
        AUDIT.record(
            ctx.run_id, "policy", "abstain",
            decision=code.value, rationale=reason,
            payload={"policy": self.policy.name},
        )
        AUDIT.record(
            ctx.run_id, "buyer_agent", "run_complete",
            decision="abstained", rationale=reason,
            payload={"intent": intent.brief, "arm": ARM, "failure_code": code.value},
            latency_ms=ctx.elapsed_ms,
        )
        return new_result(ARM, intent, ctx, outcome="abstained", failure_code=code, abstain_reason=reason)

    def _fail(self, ctx: RunContext, intent: BuyerIntent, code: FailureCode, reason: str) -> RunResult:
        AUDIT.record(
            ctx.run_id, "buyer_agent", "run_complete",
            decision="failed", rationale=reason,
            payload={"intent": intent.brief, "arm": ARM, "failure_code": code.value},
            latency_ms=ctx.elapsed_ms,
        )
        return new_result(ARM, intent, ctx, outcome="failed", failure_code=code, abstain_reason=reason)


def _as_failure(value: str) -> FailureCode:
    try:
        return FailureCode(value)
    except ValueError:
        return FailureCode.GATEWAY_ERROR
