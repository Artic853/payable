"""Benchmark harness: the same buyer tasks against three merchant surfaces.

    payable            structured MCP surface, cautious agent
    legacy-strict      HTML storefront, same cautious agent
    legacy-optimistic  HTML storefront, agent that proceeds when it cannot verify

Both legacy arms exist because the interesting claim is not "structured beats
HTML" -- it is that on an unstructured surface a buyer has to *choose* between
losing the sale and risking the wrong one, and that a transactability layer
removes the choice. One legacy arm cannot show that; two can.

Fairness rules the harness enforces:
  * identical tasks, identical constraint sets, identical budgets per arm
  * catalog inventory reset to disk state before each arm
  * payment declines seeded on (sku, amount, attempt) so every arm meets the
    same failures at the same points
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path
from typing import Any, Iterable

from ..agent.base import BuyerIntent, BuyerPolicy, RunResult
from ..agent.legacy_buyer import LegacyBuyer
from ..agent.payable_buyer import PayableBuyer
from ..config import DATA_DIR, SETTINGS
from ..models import FailureCode, SpecConstraint
from ..state import configure_payments, reset_all

DEFAULT_TASKS = DATA_DIR / "tasks.json"


def load_tasks(path: Path | None = None) -> list[BuyerIntent]:
    raw = json.loads((path or DEFAULT_TASKS).read_text(encoding="utf-8"))
    intents = []
    for task in raw["tasks"]:
        intents.append(
            BuyerIntent(
                task_id=task["task_id"],
                brief=task["brief"],
                category=task.get("category"),
                constraints=[SpecConstraint(**c) for c in task.get("constraints", [])],
                quantity=task.get("quantity", 1),
                budget_paise=task.get("budget_paise", 1_000_000),
                expected_sku=task.get("expected_sku"),
                expect_abstain=task.get("expect_abstain", False),
                note=task.get("note", ""),
            )
        )
    return intents


def make_client(base_url: str | None = None):
    """In-process ASGI client by default; a real HTTP client when given a URL."""
    if base_url:
        import httpx

        return httpx.Client(base_url=base_url, timeout=30.0, follow_redirects=True)

    from fastapi.testclient import TestClient

    from ..server.app import app

    return TestClient(app)


def build_arms(client, arms: Iterable[str]) -> dict[str, Any]:
    available = {
        "payable": lambda: PayableBuyer(client, policy=BuyerPolicy.strict()),
        "legacy-strict": lambda: LegacyBuyer(client, policy=BuyerPolicy.strict()),
        "legacy-optimistic": lambda: LegacyBuyer(client, policy=BuyerPolicy.optimistic()),
    }
    selected = {}
    for name in arms:
        if name not in available:
            raise SystemExit(f"unknown arm {name!r}; choose from {', '.join(available)}")
        selected[name] = available[name]()
    return selected


# --------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------

def summarize(arm: str, results: list[RunResult]) -> dict:
    buy_tasks = [r for r in results if r.expected_sku is not None]
    trap_tasks = [r for r in results if r.expected_sku is None]
    purchases = [r for r in results if r.outcome == "purchased"]
    correct_buys = [r for r in results if r.bought_correct_item]
    wrong_buys = [r for r in results if r.bought_wrong_item]
    correct_abstentions = [r for r in trap_tasks if r.outcome != "purchased"]

    latencies = [r.latency_ms for r in results]
    failures: dict[str, int] = {}
    for r in results:
        if r.failure_code is not FailureCode.NONE:
            failures[r.failure_code.value] = failures.get(r.failure_code.value, 0) + 1

    def pct(numerator: int, denominator: int) -> float:
        return round(100 * numerator / denominator, 1) if denominator else 0.0

    return {
        "arm": arm,
        "tasks": len(results),
        "buy_tasks": len(buy_tasks),
        "trap_tasks": len(trap_tasks),
        "purchases": len(purchases),
        "correct_purchases": len(correct_buys),
        "wrong_purchases": len(wrong_buys),
        "correct_abstentions": len(correct_abstentions),
        # Of the tasks where something should have been bought, how often was the
        # right thing bought and paid for end to end.
        "transaction_success_pct": pct(len(correct_buys), len(buy_tasks)),
        # Of everything it did buy, how often it was the wrong thing. This is the
        # number that costs real money.
        "wrong_item_rate_pct": pct(len(wrong_buys), len(purchases)),
        # Right action on every task, buying and abstaining alike.
        "decision_accuracy_pct": pct(len(correct_buys) + len(correct_abstentions), len(results)),
        "misspent_paise": sum(r.amount_paise for r in wrong_buys),
        "median_latency_ms": round(statistics.median(latencies), 1) if latencies else 0.0,
        "p95_latency_ms": round(
            sorted(latencies)[max(0, int(0.95 * len(latencies)) - 1)], 1
        ) if latencies else 0.0,
        "mean_http_calls": round(statistics.mean([r.http_calls for r in results]), 1) if results else 0.0,
        "mean_payment_attempts": round(
            statistics.mean([r.payment_attempts for r in purchases]), 2
        ) if purchases else 0.0,
        # How often the gateway said no, and whether the bounded fallback
        # recovered the sale afterwards.
        "payment_declines": sum(r.payment_declines for r in results),
        "recovered_after_decline": len([r for r in purchases if r.payment_declines > 0]),
        "failure_taxonomy": dict(sorted(failures.items(), key=lambda kv: -kv[1])),
    }


def run_benchmark(
    arms: Iterable[str] = ("payable", "legacy-strict", "legacy-optimistic"),
    tasks_path: Path | None = None,
    base_url: str | None = None,
    seed: int | None = None,
    failure_rate: float | None = None,
) -> dict:
    configure_payments(seed=seed, failure_rate=failure_rate)
    intents = load_tasks(tasks_path)
    client = make_client(base_url)
    buyers = build_arms(client, arms)

    started = time.time()
    all_results: dict[str, list[RunResult]] = {}

    for arm_name, buyer in buyers.items():
        results = []
        for intent in intents:
            # Per task, not per arm: every (arm, task) pair then starts from
            # identical inventory and identical retry history, so no task can be
            # influenced by what an earlier task in the same arm bought.
            reset_all()
            try:
                results.append(buyer.run(intent))
            except Exception as exc:  # a crashed run is a failed run, not a lost row
                from ..agent.base import RunContext, new_result

                ctx = RunContext()
                ctx.note(f"unhandled exception: {exc}")
                results.append(
                    new_result(
                        arm_name, intent, ctx,
                        outcome="failed", failure_code=FailureCode.GATEWAY_ERROR,
                        abstain_reason=f"unhandled exception: {exc}",
                    )
                )
        all_results[arm_name] = results

    return {
        "generated_at": started,
        "config": SETTINGS.describe(),
        "payment_failure_rate": SETTINGS.payment_failure_rate,
        "task_count": len(intents),
        "summaries": [summarize(arm, results) for arm, results in all_results.items()],
        "runs": {
            arm: [r.model_dump(mode="json") for r in results]
            for arm, results in all_results.items()
        },
    }


# --------------------------------------------------------------------------
# Multi-seed suite
# --------------------------------------------------------------------------

VARIANCE_METRICS = [
    "transaction_success_pct",
    "wrong_item_rate_pct",
    "decision_accuracy_pct",
    "median_latency_ms",
    "misspent_paise",
    "payment_declines",
]


def aggregate(reports: list[dict]) -> list[dict]:
    """Mean and range per arm across seeds.

    Selection is seed-independent by construction, so the spread here is purely
    payment luck. Reporting it stops a single lucky seed from carrying a claim.
    """
    by_arm: dict[str, list[dict]] = {}
    for report in reports:
        for summary in report["summaries"]:
            by_arm.setdefault(summary["arm"], []).append(summary)

    rows = []
    for arm, summaries in by_arm.items():
        row: dict[str, Any] = {"arm": arm, "seeds": len(summaries)}
        for metric in VARIANCE_METRICS:
            values = [s[metric] for s in summaries]
            row[metric] = {
                "mean": round(statistics.mean(values), 2),
                "min": round(min(values), 2),
                "max": round(max(values), 2),
                "stdev": round(statistics.stdev(values), 2) if len(values) > 1 else 0.0,
            }
        taxonomy: dict[str, int] = {}
        for summary in summaries:
            for code, count in summary["failure_taxonomy"].items():
                taxonomy[code] = taxonomy.get(code, 0) + count
        row["failure_taxonomy_total"] = dict(sorted(taxonomy.items(), key=lambda kv: -kv[1]))
        rows.append(row)
    return rows


def run_suite(
    arms: Iterable[str] = ("payable", "legacy-strict", "legacy-optimistic"),
    tasks_path: Path | None = None,
    base_url: str | None = None,
    seeds: Iterable[int] = (1733,),
    failure_rate: float | None = None,
) -> dict:
    """Run the benchmark once per seed and aggregate."""
    seeds = list(seeds)
    reports = [
        run_benchmark(arms=arms, tasks_path=tasks_path, base_url=base_url,
                      seed=seed, failure_rate=failure_rate)
        for seed in seeds
    ]
    head = reports[0]
    return {
        "generated_at": head["generated_at"],
        "config": head["config"],
        "payment_failure_rate": head["payment_failure_rate"],
        "task_count": head["task_count"],
        "seeds": seeds,
        "aggregate": aggregate(reports),
        "per_seed": [
            {"seed": seed, "summaries": report["summaries"]}
            for seed, report in zip(seeds, reports)
        ],
        # Full per-task rows for the first seed only; the rest would just bloat
        # the artifact without adding anything a reader would use.
        "summaries": head["summaries"],
        "runs": head["runs"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Payable agent-commerce benchmark")
    parser.add_argument(
        "--arms",
        default="payable,legacy-strict,legacy-optimistic",
        help="comma-separated arms to run",
    )
    parser.add_argument("--tasks", type=Path, default=None, help="path to tasks.json")
    parser.add_argument("--base-url", default=None, help="run against a live server instead of in-process")
    parser.add_argument("--seed", type=int, default=None, help="override the decline seed")
    parser.add_argument(
        "--repeats", type=int, default=1,
        help="run the suite across this many consecutive seeds and report variance",
    )
    parser.add_argument(
        "--failure-rate", type=float, default=None,
        help="override the injected payment decline rate (0.0-1.0)",
    )
    parser.add_argument("--json-out", type=Path, default=DATA_DIR / "benchmark.json")
    parser.add_argument("--md-out", type=Path, default=None, help="also write a markdown report")
    args = parser.parse_args()

    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    base_seed = args.seed if args.seed is not None else SETTINGS.seed
    seeds = [base_seed + i for i in range(max(1, args.repeats))]

    report = run_suite(
        arms=arms,
        tasks_path=args.tasks,
        base_url=args.base_url,
        seeds=seeds,
        failure_rate=args.failure_rate,
    )

    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    from .report import render_markdown, render_text

    print(render_text(report))
    print(f"\nraw results -> {args.json_out}")

    if args.md_out:
        args.md_out.write_text(render_markdown(report), encoding="utf-8")
        print(f"markdown report -> {args.md_out}")


if __name__ == "__main__":
    main()
