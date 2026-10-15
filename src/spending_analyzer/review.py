"""Review categories on real data: the LLM suggests, the user confirms or corrects.

Every answer is saved, so nothing is asked twice: accepting or correcting a merchant
confirms its counterparty; for "mixed" merchants (category depends on the purchase)
each transaction is confirmed separately as a per-transaction override. The summary
(counts only, no names or amounts) measures how accurate the suggestions were on real
data, and is appended to data/review_log.jsonl.

Usage:
    python -m spending_analyzer.review statement.pdf
"""

import argparse
import json
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from .categorize import CATEGORIES, Categorized, categorize
from .llm import AuditedClient, make_client
from .store import Store, effective_name, normalize

# Deterministic and user-confirmed sources are not reviewed
REVIEWED_SOURCES = ("llm", "counterparty_tentative", "person")

# ask(label, suggested category, source) → correct category, or None to skip
Ask = Callable[[str, str, str], str | None]


@dataclass
class ReviewSummary:
    reviewed: Counter = field(default_factory=Counter)  # by source
    corrected: Counter = field(default_factory=Counter)
    skipped: int = 0

    def lines(self) -> list[str]:
        out = ["| Decided by | Reviewed | Correct |", "|---|---|---|"]
        for source, count in self.reviewed.most_common():
            correct = count - self.corrected[source]
            out.append(f"| {source} | {count} | {correct} ({correct / count:.0%}) |")
        total = sum(self.reviewed.values())
        if total:
            correct = total - sum(self.corrected.values())
            out.append(f"| **all** | {total} | {correct} ({correct / total:.0%}) |")
        if self.skipped:
            out.append(f"\nSkipped: {self.skipped}")
        return out


def items_to_review(items: list[Categorized], store: Store) -> list[list[Categorized]]:
    """Groups to ask about: one per merchant, or one per transaction for mixed merchants."""
    groups: dict[str, list[Categorized]] = {}
    for item in items:
        if item.source not in REVIEWED_SOURCES:
            continue
        name = normalize(effective_name(item.transaction))
        key = f"{name}#{item.transaction.archive_id}" if store.is_mixed(name) else name
        groups.setdefault(key, []).append(item)
    return list(groups.values())


@dataclass
class ReviewItem:
    """One question for the user: a merchant, or one transaction of a mixed merchant."""
    name: str  # normalized effective name, the counterparty key
    label: str  # as printed on the statement
    suggested: str
    source: str
    mixed: bool
    archive_id: str
    booking_date: date
    transactions: int
    total: Decimal


def review_items(items: list[Categorized], store: Store) -> list[ReviewItem]:
    out = []
    for group in items_to_review(items, store):
        first = group[0]
        t = first.transaction
        name = normalize(effective_name(t))
        out.append(ReviewItem(
            name=name, label=t.counterparty, suggested=first.category, source=first.source,
            mixed=store.is_mixed(name), archive_id=t.archive_id, booking_date=t.booking_date,
            transactions=len(group), total=sum((i.transaction.amount for i in group), Decimal(0))))
    return out


def pending_review(store: Store) -> list[ReviewItem]:
    """The review queue straight from the database, so it survives restarts."""
    rows = store.transactions_with_source(REVIEWED_SOURCES)
    return review_items([Categorized(t, category, source) for t, category, source in rows],
                        store)


def apply_decision(item: ReviewItem, answer: str, store: Store,
                   summary: ReviewSummary) -> None:
    """Saves the user's category: per transaction for a mixed merchant, otherwise as the
    counterparty's confirmed category. Call store.refresh_categories() afterwards."""
    if answer not in CATEGORIES:
        raise ValueError(f"Unknown category {answer!r}")
    summary.reviewed[item.source] += 1
    summary.corrected[item.source] += answer != item.suggested
    if item.mixed:
        if item.archive_id:
            store.set_transaction_override(item.archive_id, answer)
    else:
        store.confirm_counterparty(item.name, answer)


def review(items: list[Categorized], store: Store, ask: Ask) -> ReviewSummary:
    summary = ReviewSummary()
    for item in review_items(items, store):
        label = (f"{item.label}  {item.booking_date:%d.%m.}  {item.total} EUR  (mixed merchant)"
                 if item.mixed else item.label)
        answer = ask(label, item.suggested, item.source)
        if answer is None:
            summary.skipped += 1
            continue
        apply_decision(item, answer, store, summary)
    store.refresh_categories()  # so stored rows (chatbot, UI) reflect the answers
    return summary


def _ask_in_terminal(total: int) -> Ask:
    menu = "  ".join(f"{i}={c}" for i, c in enumerate(CATEGORIES, 1))
    counter = iter(range(1, total + 1))

    def ask(label: str, category: str, source: str) -> str | None:
        print(f"\n[{next(counter)}/{total}] {label}\n    → {category}  ({source})")
        while True:
            answer = input("    Enter=correct, number=fix, s=skip: ").strip().lower()
            if answer == "":
                return category
            if answer == "s":
                return None
            if answer.isdigit() and 1 <= int(answer) <= len(CATEGORIES):
                return CATEGORIES[int(answer) - 1]
            print(f"    {menu}")

    return ask


def main() -> None:
    from .pdf_parser import parse_statement

    parser = argparse.ArgumentParser(description="Review and confirm categories.")
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--db", type=Path, default=Path("data/transactions.db"))
    parser.add_argument("--provider", choices=["claude", "ollama"], default="ollama")
    parser.add_argument("--model", help="model ID or Ollama tag (default per provider)")
    args = parser.parse_args()

    statement = parse_statement(args.pdf)
    store = Store(args.db)
    client = make_client(args.provider, args.model)
    report = categorize(statement.transactions, store, AuditedClient(client))
    store.import_statement(statement, report)  # persist now, so overrides land on real rows

    print(f"Categories: {', '.join(CATEGORIES)}")
    print(f"Confirmed before review: {report.confirmed_share():.0%} of the money moved")
    groups = items_to_review(report.items, store)
    summary = review(report.items, store, _ask_in_terminal(len(groups)))

    print(f"\nReview of {args.provider}: {client.model}")
    print("\n".join(summary.lines()))
    log = Path("data/review_log.jsonl")
    with log.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"time": datetime.now(timezone.utc).isoformat(),
                            "model": client.model, "reviewed": dict(summary.reviewed),
                            "corrected": dict(summary.corrected),
                            "skipped": summary.skipped}) + "\n")
    store.close()


if __name__ == "__main__":
    main()
