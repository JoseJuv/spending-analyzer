"""Measure categorization accuracy and cost on synthetic statements.

Synthetic transactions carry their true category, so accuracy is measured exactly.
Months are categorized in order with one shared store, like real use: the first
month asks the LLM, later months mostly hit known counterparties.

Usage:
    python -m spending_analyzer.evaluate --months 3
    python -m spending_analyzer.evaluate --months 3 --model claude-haiku-5-5
    python -m spending_analyzer.evaluate --months 3 --provider ollama --model qwen3.5:9b
"""

import argparse
from collections import Counter
from dataclasses import dataclass, field

from .categorize import categorize
from .llm import AuditedClient, LLMClient, make_client
from .store import Store
from .synthetic import generate


@dataclass
class EvalResult:
    total: int = 0
    correct: int = 0
    by_source: Counter = field(default_factory=Counter)
    correct_by_source: Counter = field(default_factory=Counter)
    errors: Counter = field(default_factory=Counter)  # (name, expected, got) → count
    mixed_detected: set[str] = field(default_factory=set)
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0

    @property
    def accuracy(self) -> float:
        return self.correct / self.total if self.total else 0.0


def evaluate(llm: LLMClient, months: int = 3, seed: int = 0,
             full_context: bool | None = None) -> EvalResult:
    result = EvalResult()
    store = Store()  # in memory, starts empty
    for month in range(months):
        statement, truth = generate(seed=seed + month, month=1 + month % 12)
        report = categorize(statement.transactions, store, llm, full_context)
        result.llm_calls += report.llm_calls
        result.input_tokens += report.input_tokens
        result.output_tokens += report.output_tokens
        result.cost_usd += report.cost_usd
        for item, expected in zip(report.items, truth):
            ok = item.category == expected
            result.total += 1
            result.correct += ok
            result.by_source[item.source] += 1
            result.correct_by_source[item.source] += ok
            if not ok:
                result.errors[(item.transaction.counterparty, expected, item.category)] += 1
    result.mixed_detected = store.mixed_names()
    return result


def format_report(r: EvalResult, title: str) -> str:
    mixed = ", ".join(sorted(r.mixed_detected)) or "none"
    lines = [
        f"## Categorization eval ({title})",
        "",
        f"- Transactions: {r.total}, correct: {r.correct} ({r.accuracy:.1%})",
        f"- Merchants flagged as mixed: {mixed}",
        f"- LLM calls: {r.llm_calls}, tokens in/out: {r.input_tokens}/{r.output_tokens}, "
        f"cost: ${r.cost_usd:.4f}",
        "",
        "| Decided by | Transactions | Accuracy |",
        "|---|---|---|",
    ]
    for source, count in r.by_source.most_common():
        lines.append(f"| {source} | {count} | {r.correct_by_source[source] / count:.1%} |")
    if r.errors:
        lines += ["", "| Merchant | Expected | Got | Times |", "|---|---|---|---|"]
        lines += [f"| {name} | {expected} | {got} | {n} |"
                  for (name, expected, got), n in sorted(r.errors.items())]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate categorization on synthetic data.")
    parser.add_argument("--months", type=int, default=3)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--provider", choices=["claude", "ollama"], default="ollama")
    parser.add_argument("--model", help="model ID or Ollama tag (default per provider)")
    parser.add_argument("--minimal-context", action="store_true",
                        help="send only merchant names, as for a cloud model")
    args = parser.parse_args()

    client = make_client(args.provider, args.model)
    full_context = False if args.minimal_context else None
    result = evaluate(AuditedClient(client), args.months, args.seed, full_context)
    context = "minimal" if args.minimal_context or not client.runs_locally else "full"
    print(format_report(result, f"{args.provider}: {client.model}, {context} context"))


if __name__ == "__main__":
    main()
