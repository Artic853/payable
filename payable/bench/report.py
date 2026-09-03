"""Render benchmark results as a console table or a markdown report."""

from __future__ import annotations

from typing import Any

_HEADLINE = [
    ("arm", "arm", 18),
    ("transaction_success_pct", "txn success", 12),
    ("wrong_item_rate_pct", "wrong item", 11),
    ("decision_accuracy_pct", "decision acc", 13),
    ("median_latency_ms", "p50 ms", 9),
    ("p95_latency_ms", "p95 ms", 9),
    ("mean_http_calls", "http/task", 10),
]


def _fmt(key: str, value: Any) -> str:
    if key.endswith("_pct"):
        return f"{value:.1f}%"
    if key.endswith("_ms"):
        return f"{value:,.0f}"
    return str(value)


def render_text(report: dict) -> str:
    lines: list[str] = []
    cfg = report["config"]
    lines.append("=" * 92)
    lines.append("PAYABLE — agent transactability benchmark")
    lines.append("=" * 92)
    lines.append(
        f"tasks: {report['task_count']}   "
        f"payments: {cfg['payments']}   "
        f"reasoning: {cfg['buyer_reasoning']}   "
        f"seed: {cfg['seed']}   "
        f"injected decline rate: {report['payment_failure_rate']:.0%}"
    )
    lines.append("")

    header = "  ".join(label.ljust(width) for _, label, width in _HEADLINE)
    lines.append(header)
    lines.append("-" * len(header))
    for summary in report["summaries"]:
        row = "  ".join(
            _fmt(key, summary[key]).ljust(width) for key, _, width in _HEADLINE
        )
        lines.append(row)

    lines.append("")
    lines.append("outcome detail")
    lines.append("-" * 92)
    for summary in report["summaries"]:
        lines.append(
            f"{summary['arm']:<18} "
            f"bought {summary['purchases']:>2} "
            f"({summary['correct_purchases']} right, {summary['wrong_purchases']} wrong)   "
            f"correct abstentions {summary['correct_abstentions']}/{summary['trap_tasks']}   "
            f"declines {summary['payment_declines']} "
            f"(recovered {summary['recovered_after_decline']})   "
            f"misspent INR {summary['misspent_paise'] / 100:,.2f}"
        )

    lines.append("")
    lines.append("failure taxonomy")
    lines.append("-" * 92)
    for summary in report["summaries"]:
        taxonomy = summary["failure_taxonomy"] or {"none": 0}
        rendered = ", ".join(f"{k}={v}" for k, v in taxonomy.items())
        lines.append(f"{summary['arm']:<18} {rendered}")

    return "\n".join(lines)


def render_markdown(report: dict) -> str:
    cfg = report["config"]
    out: list[str] = []
    out.append("# Payable — agent transactability benchmark\n")
    out.append(
        f"- **Tasks:** {report['task_count']}\n"
        f"- **Payments:** {cfg['payments']}\n"
        f"- **Buyer reasoning:** {cfg['buyer_reasoning']}\n"
        f"- **Seed:** {cfg['seed']}\n"
        f"- **Injected decline rate:** {report['payment_failure_rate']:.0%}\n"
    )

    out.append("\n## Headline\n")
    out.append(
        "| Arm | Txn success | Wrong-item rate | Decision accuracy | p50 latency | p95 latency | HTTP calls/task |"
    )
    out.append("|---|---|---|---|---|---|---|")
    for s in report["summaries"]:
        out.append(
            f"| `{s['arm']}` | {s['transaction_success_pct']:.1f}% | {s['wrong_item_rate_pct']:.1f}% | "
            f"{s['decision_accuracy_pct']:.1f}% | {s['median_latency_ms']:,.0f} ms | "
            f"{s['p95_latency_ms']:,.0f} ms | {s['mean_http_calls']:.1f} |"
        )

    out.append("\n## Outcomes\n")
    out.append(
        "| Arm | Purchases | Right | Wrong | Correct abstentions | Declines | Recovered | Money misspent |"
    )
    out.append("|---|---|---|---|---|---|---|---|")
    for s in report["summaries"]:
        out.append(
            f"| `{s['arm']}` | {s['purchases']} | {s['correct_purchases']} | {s['wrong_purchases']} | "
            f"{s['correct_abstentions']}/{s['trap_tasks']} | {s['payment_declines']} | "
            f"{s['recovered_after_decline']} | ₹{s['misspent_paise'] / 100:,.2f} |"
        )

    out.append("\n## Failure taxonomy\n")
    out.append("| Arm | Terminal failure codes |")
    out.append("|---|---|")
    for s in report["summaries"]:
        taxonomy = s["failure_taxonomy"]
        rendered = ", ".join(f"`{k}`×{v}" for k, v in taxonomy.items()) or "—"
        out.append(f"| `{s['arm']}` | {rendered} |")

    out.append("\n## Per-task detail\n")
    arms = list(report["runs"].keys())
    out.append("| Task | " + " | ".join(f"`{a}`" for a in arms) + " |")
    out.append("|---|" + "---|" * len(arms))
    by_task: dict[str, dict[str, dict]] = {}
    for arm, runs in report["runs"].items():
        for run in runs:
            by_task.setdefault(run["task_id"], {})[arm] = run
    for task_id, per_arm in by_task.items():
        cells = []
        for arm in arms:
            run = per_arm.get(arm)
            if run is None:
                cells.append("—")
                continue
            if run["outcome"] == "purchased":
                mark = "✅" if run["purchased_sku"] == run["expected_sku"] else "❌"
                cells.append(f"{mark} bought `{run['purchased_sku']}`")
            else:
                mark = "✅" if run["expected_sku"] is None else "⚠️"
                cells.append(f"{mark} {run['outcome']} (`{run['failure_code']}`)")
        out.append(f"| `{task_id}` | " + " | ".join(cells) + " |")

    return "\n".join(out) + "\n"
