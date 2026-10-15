from datetime import date
from decimal import Decimal

from spending_analyzer import synthetic
from spending_analyzer.categorize import categorize, normalize
from spending_analyzer.models import Transaction
from spending_analyzer.review import items_to_review, review
from test_categorize import FakeLLM


def test_accepting_and_correcting_both_confirm_so_nothing_is_asked_twice(store):
    statement, _ = synthetic.generate(seed=0)
    report = categorize(statement.transactions, store, FakeLLM(runs_locally=True))
    asked = []

    def ask(label, category, source):
        asked.append(label)
        return "Groceries" if label.startswith("Hesburger") else category

    summary = review(report.items, store, ask)

    assert len(asked) == len(set(asked))
    assert sum(summary.corrected.values()) == 1
    assert store.category_for("Hesburger Kamppi") == "Groceries"
    again = categorize(statement.transactions, store, FakeLLM(runs_locally=True))
    assert again.confirmed_share() == 1.0
    assert items_to_review(again.items, store) == []


def test_mixed_merchants_are_reviewed_per_transaction(store):
    statement, _ = synthetic.generate(seed=0, card_payments=80)
    report = categorize(statement.transactions, store, FakeLLM(runs_locally=True))
    mixed = store.mixed_names()
    mixed_items = [i for i in report.items if normalize(i.transaction.counterparty) in mixed]
    assert len(mixed_items) >= 2

    review(report.items, store, lambda label, category, source: category)

    assert all(store.get_override_category(i.transaction.archive_id) is not None
               for i in mixed_items)
    assert not (store.confirmed_names() & mixed)


def test_mobilepay_and_vipps_prefixes_unify_to_one_person(store):
    llm = FakeLLM()
    t1 = Transaction(date(2026, 10, 1), Decimal("-20"), "TILISIIRTO",
                     counterparty="MobilePay Jari Sillanpää", archive_id="1")
    t2 = Transaction(date(2026, 10, 2), Decimal("-15"), "TILISIIRTO",
                     counterparty="Vipps Jari Sillanpää", archive_id="2")

    report = categorize([t1, t2], store, llm)
    groups = items_to_review(report.items, store)

    assert len(groups) == 1
    assert len(groups[0]) == 2
