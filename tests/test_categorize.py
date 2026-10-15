import json
import re
from datetime import date
from decimal import Decimal

import pytest

from spending_analyzer import synthetic
from spending_analyzer.categorize import categorize, normalize
from spending_analyzer.evaluate import evaluate
from spending_analyzer.llm import AuditedClient, LLMResult
from spending_analyzer.models import Transaction
from spending_analyzer.store import Store

KNOWN = {normalize(name): category for name, category, *_ in
         synthetic.CARD_MERCHANTS + synthetic.SUBSCRIPTIONS + synthetic.BILLS}
KNOWN[normalize(synthetic.FUND_COMPANY)] = "Investments"
MIXED = {normalize(name): purchases for name, purchases in synthetic.MIXED_MERCHANTS}


class FakeLLM:
    """Answers like a perfect model, from the generator's ground truth.

    Like a real model, it can only tell a mixed merchant's purchases apart when it
    sees the amount, i.e. with full context.
    """

    model = "fake"

    def __init__(self, runs_locally: bool = False):
        self.runs_locally = runs_locally
        self.prompts: list[str] = []

    def complete_json(self, system, prompt, schema):
        self.prompts.append(prompt)
        results = []
        for line in prompt.splitlines():
            index, name, *rest = (part.strip() for part in line.split("|"))
            name = normalize(name)
            if name in MIXED and len(rest) >= 3:  # type, direction, amount
                amount = float(rest[2].removesuffix(" EUR"))
                category = next(c for c, low, high in MIXED[name] if low <= amount <= high)
                results.append({"id": int(index), "category": category, "mixed": True})
            else:
                category = MIXED[name][0][0] if name in MIXED else KNOWN.get(name, "Other")
                results.append({"id": int(index), "category": category, "mixed": False})
        return LLMResult({"results": results}, "fake", input_tokens=100, output_tokens=50)


@pytest.mark.parametrize("seed", range(3))
def test_local_model_with_full_context_gives_ground_truth(seed, store):
    statement, truth = synthetic.generate(seed=seed)
    report = categorize(statement.transactions, store, FakeLLM(runs_locally=True))
    assert [item.category for item in report.items] == truth


def test_full_context_includes_amount_and_message_but_not_card_numbers(store):
    llm = FakeLLM(runs_locally=True)
    statement, _ = synthetic.generate(seed=0)
    categorize(statement.transactions, store, llm)
    prompt = "\n".join(llm.prompts)
    assert " EUR" in prompt and "| out |" in prompt
    assert "******" not in prompt


def test_mixed_merchants_are_not_cached_and_asked_again(store):
    llm = FakeLLM(runs_locally=True)
    statement, _ = synthetic.generate(seed=0, card_payments=80)
    categorize(statement.transactions, store, llm)
    mixed = store.mixed_names()
    assert mixed and not any(store.category_for(name) for name in mixed)

    llm.prompts.clear()
    report = categorize(statement.transactions, store, llm)
    asked = {normalize(line.split("|")[1]) for line in "\n".join(llm.prompts).splitlines()}
    assert asked == mixed
    assert all(i.source == "llm" for i in report.items
               if normalize(i.transaction.counterparty) in mixed)


def test_cloud_model_gets_one_line_per_merchant_and_answers_are_cached(store):
    llm = FakeLLM(runs_locally=False)
    statement, _ = synthetic.generate(seed=0)

    categorize(statement.transactions, store, llm)
    report = categorize(statement.transactions, store, llm)

    assert len(llm.prompts) == 1
    assert report.llm_calls == 0
    lines = llm.prompts[0].splitlines()
    names = [line.split("|")[1].strip() for line in lines]
    assert len(names) == len(set(names)), "each merchant is sent only once"
    assert all(line.count("|") == 2 for line in lines), "only id, name and type"


def test_counterparty_confirmed_overrides_llm(store):
    store.confirm_counterparty("K-market", "Restaurants")
    statement, _ = synthetic.generate(seed=0)

    report = categorize(statement.transactions, store, FakeLLM())

    kmarket = [i for i in report.items if i.transaction.counterparty.startswith("K-market")]
    assert kmarket and all(i.category == "Restaurants" and i.source == "counterparty_confirmed"
                           for i in kmarket)


def test_confirmed_category_persists_across_reopen(tmp_path):
    db_path = tmp_path / "transactions.db"
    store = Store(db_path)
    store.confirm_counterparty("K-market", "Restaurants")
    store.close()

    store = Store(db_path)
    statement, _ = synthetic.generate(seed=0)
    report = categorize(statement.transactions, store, FakeLLM())

    kmarket = [i for i in report.items if i.transaction.counterparty.startswith("K-market")]
    assert kmarket and all(i.category == "Restaurants" and i.source == "counterparty_confirmed"
                           for i in kmarket)


def test_cloud_payloads_contain_no_personal_data(tmp_path, store):
    log = tmp_path / "audit.jsonl"
    llm = AuditedClient(FakeLLM(runs_locally=False), log)
    statements = [synthetic.generate(seed=seed)[0] for seed in range(5)]
    for statement in statements:
        categorize(statement.transactions, store, llm)

    payload = "\n".join(entry["system"] + entry["prompt"]
                        for entry in map(json.loads, log.read_text("utf-8").splitlines()))
    assert payload
    forbidden = [synthetic.HOLDER, *synthetic.PEOPLE, *synthetic.PERSON_MESSAGES,
                 "MobilePay", "VIPPS", "******", "OSTOPVM", "OSAKESALKKU", " EUR"]
    for statement in statements:
        forbidden += [statement.account_iban]
        forbidden += [t.counterparty_iban for t in statement.transactions if t.counterparty_iban]
    leaked = [value for value in forbidden if value.casefold() in payload.casefold()]
    assert leaked == []
    assert not re.search(r"FI\d{16}", payload)


def test_confirmed_share(store):
    statement, _ = synthetic.generate(seed=0)
    report = categorize(statement.transactions, store, FakeLLM())
    assert 0 < report.confirmed_share() < 1
    for name in {normalize(i.transaction.counterparty) for i in report.items}:
        store.confirm_counterparty(name, "Other")
    assert categorize(statement.transactions, store, FakeLLM()).confirmed_share() == 1


@pytest.mark.parametrize("runs_locally", [True, False])
def test_evaluate_with_perfect_llm(runs_locally):
    result = evaluate(FakeLLM(runs_locally), months=2)
    if runs_locally:
        assert result.accuracy == 1.0
    else:  # without amounts, a mixed merchant's food purchases can't be told apart
        assert 0.9 < result.accuracy < 1.0
        assert {expected for (_, expected, _), _ in result.errors.items()} == {"Restaurants"}


@pytest.mark.parametrize("name", ["VIPPS MOBILEPAY AS", "MobilePay", "Vipps"])
def test_payment_apps_are_transfers_and_never_sent(name, store):
    llm = FakeLLM()
    t = Transaction(date(2026, 10, 1), Decimal("-20"), "TILISIIRTO", counterparty=name)

    [item] = categorize([t], store, llm).items

    assert (item.category, item.source) == ("Transfers", "payment_app")
    assert llm.prompts == []


def test_payment_app_prefixed_name_resolves_to_person(store):
    llm = FakeLLM()
    t = Transaction(date(2026, 10, 1), Decimal("-20"), "TILISIIRTO",
                    counterparty="MobilePay Jari Sillanpää")

    [item] = categorize([t], store, llm).items

    assert (item.category, item.source) == ("Transfers", "person")
    assert store.category_for("Jari Sillanpää") == "Transfers"
    assert llm.prompts == []
