import json
import re
from datetime import date
from decimal import Decimal

from spending_analyzer import chatbot
from spending_analyzer.categorize import Categorized, CategorizationReport
from spending_analyzer.chatbot import ask, render_bar_chart, resolve_period, run_tool
from spending_analyzer.llm import LLMResult
from spending_analyzer.models import Statement, Transaction


class ScriptedLLM:
    """Always returns the same response; records every (system, prompt) it saw."""

    model = "scripted"
    runs_locally = True

    def __init__(self, response: dict):
        self.response = response
        self.prompts: list[tuple[str, str]] = []

    def complete_json(self, system, prompt, schema):
        self.prompts.append((system, prompt))
        return LLMResult(self.response, self.model, input_tokens=0, output_tokens=0, cost_usd=0.0)


def _import(store, rows, account_iban="FI00"):
    transactions = [t for t, _, _ in rows]
    items = [Categorized(t, category, source) for t, category, source in rows]
    net = sum((t.amount for t in transactions), Decimal(0))
    statement = Statement(account_iban=account_iban, period_start=date(2026, 1, 1),
                          period_end=date(2026, 12, 31), opening_balance=Decimal(0),
                          closing_balance=net, transactions=transactions)
    store.import_statement(statement, CategorizationReport(items=items))


def _t(archive_id, booking_date, amount, counterparty=""):
    return Transaction(booking_date, amount, "OSTO", counterparty=counterparty, archive_id=archive_id)


def test_resolve_period_this_month():
    today = date(2026, 10, 9)
    assert resolve_period("this_month", None, None, today) == ("2026-10-01", "2026-10-09")


def test_resolve_period_last_month_crosses_year_boundary():
    today = date(2027, 1, 15)
    assert resolve_period("last_month", None, None, today) == ("2026-12-01", "2026-12-31")


def test_resolve_period_custom_uses_explicit_dates():
    today = date(2026, 10, 9)
    assert resolve_period("custom", "2026-03-01", "2026-04-10", today) == \
        ("2026-03-01", "2026-04-10")


def test_resolve_period_named_month_is_the_most_recent_one():
    today = date(2026, 10, 15)
    assert resolve_period("named_month", today=today, month=9) == ("2026-09-01", "2026-09-30")
    assert resolve_period("named_month", today=today, month=11) == ("2025-11-01", "2025-11-30")
    assert resolve_period("named_month", today=today, month=2, year=2024) == \
        ("2024-02-01", "2024-02-29")


def test_resolve_period_month_span_can_cross_a_year_boundary():
    today = date(2026, 10, 15)
    assert resolve_period("named_month", today=today, month=3, end_month=5) == \
        ("2026-03-01", "2026-05-31")
    assert resolve_period("named_month", today=today, month=11, end_month=2) == \
        ("2025-11-01", "2026-02-28")


def test_incomplete_period_is_an_error_not_a_silent_this_month(store):
    today = date(2026, 10, 15)
    for params in ({"period": "custom"}, {"period": "custom", "start_date": "2026-09-01"},
                   {"period": "named_month"}, {"period": "named_month", "month": 13}):
        result, chart = run_tool("top_merchants", params, store, today)
        assert "error" in result and chart is None


def test_resolve_period_all_time_and_last_year(store):
    today = date(2026, 10, 9)
    start, end = resolve_period("all_time", None, None, today)
    assert end == "2026-10-09" and start < "2000-01-01"
    assert resolve_period("last_year", None, None, today) == ("2025-01-01", "2025-12-31")


def test_ask_router_sees_no_db_data_but_local_sees_the_real_total(store):
    _import(store, [
        (_t("1", date(2026, 10, 2), Decimal("-20.00")), "Groceries", "llm"),
        (_t("2", date(2026, 10, 5), Decimal("-15.00")), "Groceries", "llm"),
    ])
    router = ScriptedLLM({"tool": "category_total", "category": "Groceries", "period": "this_month"})
    local = ScriptedLLM({"answer": "You spent 35.00 EUR on groceries."})

    answer = ask("How much did I spend on groceries?", store, router, local, today=date(2026, 10, 9))

    assert answer == "You spent 35.00 EUR on groceries."
    [(router_system, router_prompt)] = router.prompts
    assert router_system == chatbot.TOOL_SYSTEM_PROMPT
    assert router_prompt == "How much did I spend on groceries?"
    assert not re.search(r"\d", router_prompt)  # the question itself happens to have none here
    [(_, local_prompt)] = local.prompts
    assert "35.00" in local_prompt


def test_ask_with_tool_none_never_queries_the_store(store, monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("run_tool should not be called when tool is 'none'")
    monkeypatch.setattr(chatbot, "run_tool", fail)

    router = ScriptedLLM({"tool": "none"})
    local = ScriptedLLM({"answer": "You're welcome!"})

    assert ask("Thanks, bye", store, router, local) == "You're welcome!"


def test_spending_by_category_never_sums_transfers_or_investments_as_spending(store):
    _import(store, [
        (_t("1", date(2026, 10, 1), Decimal("100.00")), "Transfers", "person"),
        (_t("2", date(2026, 10, 2), Decimal("500.00")), "Investments", "llm"),
    ])
    result, _ = run_tool("spending_by_category", {"period": "this_month"}, store, date(2026, 10, 9))
    assert result["spending_and_income"] == {}
    assert result["moved_between_own_assets"] == {"Transfers": "100.00", "Investments": "500.00"}


def test_render_bar_chart_empty_and_nonempty():
    assert render_bar_chart([]) == ""
    chart = render_bar_chart([("Groceries", Decimal("50.00")), ("Transport", Decimal("20.00"))])
    assert "Groceries" in chart and "Transport" in chart


def test_router_year_is_ignored_unless_the_question_names_it(store):
    _import(store, [(_t("1", date(2026, 9, 3), Decimal("-40"), "Neste"), "Transport", "llm")])
    today = date(2026, 10, 15)
    router = ScriptedLLM({"tool": "category_total", "category": "Transport",
                          "period": "named_month", "month": 9, "year": 2023})
    local = ScriptedLLM({"answer": "ok"})

    ask("How much on transport in September?", store, router, local, today=today)
    assert "2026-09-01" in local.prompts[-1][1] and "40.00" in local.prompts[-1][1]

    ask("How much on transport in September 2023?", store, router, local, today=today)
    assert "2023-09-01" in local.prompts[-1][1]


def test_trend_counts_back_from_the_newest_data_not_from_today(store):
    _import(store, [(_t(str(m), date(2026, m, 10), Decimal("-10"), "Cafe"), "Restaurants", "llm")
                    for m in (7, 8, 9)])
    result, rows = run_tool("monthly_trend", {"category": "Restaurants", "trailing_months": 3},
                            store, today=date(2026, 10, 15))
    assert [month for month, _ in rows] == ["2026-07", "2026-08", "2026-09"]
    # a longer window starts where the data starts, so the answer names no empty months
    result, _ = run_tool("monthly_trend", {"category": "Restaurants", "trailing_months": 6},
                         store, today=date(2026, 10, 15))
    assert result["start"] == "2026-07-01"


def test_guard_moves_a_category_out_of_the_counterparty_field():
    from spending_analyzer.chatbot import apply_guards
    fixed = apply_guards({"tool": "category_total", "period": "this_month",
                          "counterparty": "transport"}, "How much on transport?")
    assert fixed["category"] == "Transport" and "counterparty" not in fixed
    person = apply_guards({"tool": "counterparty_total", "period": "this_month",
                           "counterparty": "Liisa"}, "How much to Liisa?")
    assert person["counterparty"] == "Liisa" and "category" not in person


def test_list_transactions_returns_each_row_largest_first_with_full_count(store):
    _import(store, [
        (_t("1", date(2026, 9, 1), Decimal("-20.00"), "VIPPS MOBILEPAY AS"), "Transfers",
         "payment_app"),
        (_t("2", date(2026, 9, 2), Decimal("-75.00"), "MobilePay"), "Transfers", "payment_app"),
        (_t("3", date(2026, 9, 3), Decimal("40.00"), "MobilePay"), "Transfers", "payment_app"),
        (_t("4", date(2026, 9, 4), Decimal("-9.90"), "K-Market"), "Groceries", "llm"),
    ])
    params = {"period": "all_time", "category": "Transfers", "direction": "out", "limit": 1}
    result, chart = run_tool("list_transactions", params, store, date(2026, 10, 15))

    assert chart is None
    assert result["count"] == 2 and result["total"] == "-95.00"  # all matches, not just 1
    [row] = result["transactions"]
    assert row["amount"] == "-75.00" and "recipient not shown" in row["counterparty"]


def test_long_lists_are_summarized_for_the_answer_model_but_kept_for_the_table(store):
    _import(store, [(_t(str(i), date(2026, 9, 1 + i % 28), Decimal(-i - 1), "Shop"),
                     "Shopping", "llm") for i in range(30)])
    router = ScriptedLLM({"tool": "list_transactions", "period": "all_time", "limit": 30})
    local = ScriptedLLM({"answer": "30 purchases."})

    answer = chatbot.answer_question("List my purchases", store, router, local,
                                     today=date(2026, 10, 15))

    assert len(answer.parts[0].table) == 30
    sent = json.loads(local.prompts[0][1].split("Data:\n", 1)[1])
    assert len(sent["transactions"]) == 10 and sent["more_rows_in_table"] == 20


def test_guard_restores_a_category_the_question_names_outright():
    from spending_analyzer.chatbot import apply_guards
    q = "Show all my transfers by each transaction to individuals"
    assert apply_guards({"tool": "list_transactions", "period": "all_time"}, q)["category"] \
        == "Transfers"
    # never from synonyms, never when two categories are named, never for other tools
    for tool, question in (("category_total", "How much did fuel cost me?"),
                           ("top_merchants", "Groceries or restaurants: where do I spend?"),
                           ("spending_by_category", "Show my groceries")):
        assert "category" not in apply_guards({"tool": tool, "period": "this_month"}, question)


def test_several_calls_answer_several_parts_and_rank_by_count(store):
    _import(store, [
        (_t("1", date(2026, 9, 1), Decimal("-80.00"), "Prisma"), "Groceries", "llm"),
        (_t("2", date(2026, 9, 2), Decimal("-5.00"), "Alepa"), "Groceries", "llm"),
        (_t("3", date(2026, 9, 3), Decimal("-6.00"), "Alepa"), "Groceries", "llm"),
        (_t("4", date(2026, 9, 4), Decimal("-4.00"), "Alepa"), "Groceries", "llm"),
    ])
    for name in ("Prisma", "Alepa"):
        store.create_tentative_counterparty(name, "merchant", "Groceries")
    store.connection.execute(
        "UPDATE transactions SET counterparty_id = (SELECT counterparty_id FROM "
        "counterparty_aliases WHERE pattern = lower(transactions.counterparty_raw))")
    router = ScriptedLLM({"calls": [
        {"tool": "monthly_trend", "period": "this_month", "category": "Groceries"},
        {"tool": "top_merchants", "period": "all_time", "category": "Groceries",
         "rank_by": "count"},
        {"tool": "top_merchants", "period": "all_time", "category": "Groceries",
         "rank_by": "count"}]})  # a duplicate call runs once
    local = ScriptedLLM({"answer": "ok"})

    answer = chatbot.answer_question("Grocery trend, and my favorite store?", store, router,
                                     local, today=date(2026, 10, 15))

    assert [p.tool for p in answer.parts] == ["monthly_trend", "top_merchants"]
    ranking = answer.parts[1]
    assert ranking.chart[0] == ("Alepa", Decimal(3))  # 3 visits beat one 80 € purchase
    sent = json.loads(local.prompts[0][1].split("Data:\n", 1)[1])
    assert [part["tool"] for part in sent] == ["monthly_trend", "top_merchants"]


def test_a_bare_single_call_is_still_accepted(store):
    router = ScriptedLLM({"tool": "none"})
    local = ScriptedLLM({"answer": "Hi!"})
    answer = chatbot.answer_question("Hello", store, router, local)
    assert answer.parts == [] and answer.text == "Hi!"
