"""Categorize transactions: confirmed rules first, an LLM suggests the rest.

Order of decisions for each transaction:
1. Per-transaction overrides set in the review (by archive ID)
2. Known counterparty: a confirmed or previously-suggested category for this name
3. Transaction type rules (salary → Income)
4. Payment apps (MobilePay, Vipps) with no extractable payee name → Transfers
5. Possible private persons → Transfers
6. Everything else is sent to the LLM in batches

How much the LLM sees depends on where it runs (see CLAUDE.md):
- Local model: each transaction with direction, amount and message, plus the user's
  confirmed counterparties as examples. It also flags "mixed" merchants that sell
  different kinds of things (a fuel station that sells food); those are decided per
  transaction and never cached, so they're asked again every run.
- Cloud model: only unique merchant names and transaction types.

Steps 1–4 are confirmed by the user or deterministic; LLM suggestions and the person
heuristic stay unconfirmed (tentative) until reviewed (see review.py).

Usage:
    python -m spending_analyzer.categorize statement.pdf
"""

import argparse
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from .llm import AuditedClient, LLMClient, make_client
from .models import Transaction
from .privacy import is_card_payment, may_be_private_person
from .store import Store, effective_name, normalize, strip_payment_app_prefix

CATEGORIES = ["Groceries", "Restaurants", "Transport", "Entertainment", "Subscriptions",
              "Shopping", "Health", "Housing", "Utilities", "Income", "Transfers",
              "Investments", "Other"]
# Money moved between the user's own assets: reported separately, not as spending or income
NOT_SPENDING_OR_INCOME = ("Transfers", "Investments")
CONFIRMED_SOURCES = ("type_rule", "payment_app", "override", "counterparty_confirmed")
TYPE_RULES = {"PALKKA": "Income"}
BATCH_SIZE = 40  # lines per LLM call; keeps a local model's context small
MAX_EXAMPLES = 40
MAX_MESSAGE_CHARS = 120

CATEGORY_GUIDE = f"""Pick the most likely category from: {", ".join(CATEGORIES)}.
Names may be abbreviated or contain store locations, city names or payment processor
prefixes such as PAYPAL.
Subscriptions are recurring digital services (streaming, music, software).
Investments are fund companies (rahastoyhtiö), brokers and stock or fund purchases and
sales; a fund "subscription" (merkintä) is an Investment, not a Subscription.
Utilities are electricity, water, phone and internet bills. A refund keeps the merchant's
category; Income is only salary and other earnings. Use Other only when the merchant
cannot be identified."""

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "category": {"type": "string", "enum": CATEGORIES},
                    "mixed": {"type": "boolean"},
                },
                "required": ["id", "category", "mixed"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["results"],
    "additionalProperties": False,
}


@dataclass
class Categorized:
    transaction: Transaction
    category: str
    source: str  # "override", "type_rule", "payment_app", "person",
                  # "counterparty_confirmed", "counterparty_tentative", "llm"

    @property
    def confirmed(self) -> bool:
        return self.source in CONFIRMED_SOURCES


@dataclass
class CategorizationReport:
    items: list[Categorized] = field(default_factory=list)
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0

    def confirmed_share(self) -> float:
        """Share of money moved (absolute amounts) whose category is confirmed."""
        total = sum(abs(i.transaction.amount) for i in self.items)
        confirmed = sum(abs(i.transaction.amount) for i in self.items if i.confirmed)
        return float(confirmed / total) if total else 1.0


def _clean(text: str) -> str:
    return text.replace("|", "/").replace("\n", " / ")


def _describe(t: Transaction, full_context: bool) -> str:
    cells = [t.counterparty, t.type]
    if full_context:
        cells += ["in" if t.amount > 0 else "out", f"{abs(t.amount):.2f} EUR"]
        if t.message and not is_card_payment(t):  # card messages are just card numbers
            cells.append(t.message[:MAX_MESSAGE_CHARS])
    return " | ".join(_clean(cell) for cell in cells)


def system_prompt(full_context: bool, store: Store) -> str:
    if not full_context:
        return ("You categorize merchants from Finnish bank statements.\n"
                'Each line is "id | merchant name as printed on the statement | '
                'transaction type".\n' + CATEGORY_GUIDE + "\nAlways set mixed to false.")
    prompt = ("You categorize transactions from a Finnish bank statement.\n"
              'Each line is "id | counterparty as printed | transaction type | direction | '
              'amount | message (optional)".\n' + CATEGORY_GUIDE + "\n"
              "Use the amount, direction and message as evidence for each transaction.\n"
              "Set mixed to true when the merchant sells clearly different kinds of things, "
              "so its category depends on the purchase (for example a department store with "
              "a café); categorize each such transaction on its own evidence.")
    examples = store.confirmed_examples(MAX_EXAMPLES)
    if examples:
        prompt += ("\n\nThe user's confirmed choices; follow them for similar merchants:\n"
                   + "\n".join(f"{pattern} → {category}" for pattern, category in examples))
    return prompt


def _decide_locally(t: Transaction, store: Store) -> tuple[str, str] | None:
    if t.archive_id and (category := store.get_override_category(t.archive_id)) is not None:
        return category, "override"

    stripped = strip_payment_app_prefix(t.counterparty)
    is_payment_app_row = stripped is not None
    name = stripped if stripped else t.counterparty  # "" (bare app name) falls back too

    match = store.resolve_by_alias(name, t.counterparty_iban)
    if match and match.category is not None:
        source = "counterparty_confirmed" if match.status == "confirmed" else "counterparty_tentative"
        return match.category, source

    if t.type in TYPE_RULES:
        return TYPE_RULES[t.type], "type_rule"

    if is_payment_app_row and not stripped:  # bare "MobilePay"/"VIPPS MOBILEPAY AS"
        return "Transfers", "payment_app"

    if match:  # registered counterparty, still uncategorized -> pending (LLM)
        return None
    if is_card_payment(t):  # never a person
        store.create_tentative_counterparty(name, "merchant", None)
        return None
    if may_be_private_person(t):
        store.create_tentative_counterparty(name, "person", "Transfers")
        return "Transfers", "person"
    store.create_tentative_counterparty(name, "unknown", None)
    return None


def categorize(transactions: list[Transaction], store: Store, llm: LLMClient | None,
               full_context: bool | None = None,
               progress: Callable[[int, int], None] | None = None) -> CategorizationReport:
    """Categorize transactions; full_context defaults to True for local models.
    progress(batches_done, batches_total) is called around each LLM call (for a UI)."""
    if full_context is None:
        full_context = bool(llm and llm.runs_locally)
    report = CategorizationReport()
    decided = {i: d for i, t in enumerate(transactions) if (d := _decide_locally(t, store))}
    pending = [i for i in range(len(transactions)) if i not in decided]

    suggestions: dict[int, str] = {}
    if pending and llm is not None:
        if full_context:  # one line per transaction
            units = [[i] for i in pending]
        else:  # one line per unique merchant
            by_name: dict[str, list[int]] = defaultdict(list)
            for i in pending:
                by_name[normalize(effective_name(transactions[i]))].append(i)
            units = list(by_name.values())

        flagged_mixed: set[str] = set()
        prompt_system = system_prompt(full_context, store)
        batches_total = -(-len(units) // BATCH_SIZE)
        if progress:
            progress(0, batches_total)
        for start in range(0, len(units), BATCH_SIZE):
            batch = units[start:start + BATCH_SIZE]
            prompt = "\n".join(f"{n} | {_describe(transactions[unit[0]], full_context)}"
                               for n, unit in enumerate(batch))
            result = llm.complete_json(prompt_system, prompt, RESPONSE_SCHEMA)
            report.llm_calls += 1
            report.input_tokens += result.input_tokens
            report.output_tokens += result.output_tokens
            report.cost_usd += result.cost_usd or 0.0
            if progress:
                progress(report.llm_calls, batches_total)
            for item in result.data["results"]:
                if not 0 <= item["id"] < len(batch):
                    continue
                for i in batch[item["id"]]:
                    suggestions[i] = item["category"]
                    if full_context and item["mixed"]:
                        flagged_mixed.add(normalize(effective_name(transactions[i])))

        # Cache one category per merchant, except for merchants flagged as mixed
        per_merchant: dict[str, Counter] = defaultdict(Counter)
        for i, category in suggestions.items():
            per_merchant[normalize(effective_name(transactions[i]))][category] += 1
        for name, counts in per_merchant.items():
            if name in flagged_mixed or store.is_mixed(name):
                store.flag_mixed(name)
            else:
                store.set_tentative_category(name, counts.most_common(1)[0][0])

    for i, t in enumerate(transactions):
        category, source = decided.get(i) or (suggestions.get(i, "Other"), "llm")
        report.items.append(Categorized(t, category, source))
    return report


def main() -> None:
    from .pdf_parser import parse_statement

    parser = argparse.ArgumentParser(description="Categorize a statement's transactions.")
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--db", type=Path, default=Path("data/transactions.db"))
    parser.add_argument("--provider", choices=["claude", "ollama"], default="ollama")
    parser.add_argument("--model", help="model ID or Ollama tag (default per provider)")
    parser.add_argument("--offline", action="store_true", help="don't call the LLM")
    args = parser.parse_args()

    statement = parse_statement(args.pdf)
    store = Store(args.db)
    llm = None if args.offline else AuditedClient(make_client(args.provider, args.model))
    report = categorize(statement.transactions, store, llm)
    store.import_statement(statement, report)

    totals: dict[str, Decimal] = defaultdict(Decimal)
    for item in report.items:
        totals[item.category] += item.transaction.amount
    for heading, categories in (
            ("Spending and income", [c for c in totals if c not in NOT_SPENDING_OR_INCOME]),
            ("Moved between own assets", [c for c in totals if c in NOT_SPENDING_OR_INCOME])):
        print(f"\n{heading}")
        for category in sorted(categories, key=lambda c: totals[c]):
            print(f"  {category:<14} {totals[category]:>10}")
    print(f"\nConfirmed categories cover {report.confirmed_share():.0%} of the money moved; "
          "run the review to confirm the rest.")
    print(f"LLM calls: {report.llm_calls}, tokens in/out: "
          f"{report.input_tokens}/{report.output_tokens}, cost: ${report.cost_usd:.4f}")
    store.close()


if __name__ == "__main__":
    main()
