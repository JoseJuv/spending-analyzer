from datetime import date
from decimal import Decimal

from spending_analyzer.categorize import Categorized, CategorizationReport
from spending_analyzer.models import Statement, Transaction
from spending_analyzer.store import Store


def _import(store: Store, rows: list[tuple[Transaction, str, str]], account_iban: str = "FI00") -> None:
    """rows: (transaction, category, source). Builds a minimal Statement/Report and
    imports it, so the new transactions are queryable via the aggregation methods."""
    transactions = [t for t, _, _ in rows]
    items = [Categorized(t, category, source) for t, category, source in rows]
    net = sum((t.amount for t in transactions), Decimal(0))
    statement = Statement(account_iban=account_iban, period_start=date(2026, 1, 1),
                          period_end=date(2026, 12, 31), opening_balance=Decimal(0),
                          closing_balance=net, transactions=transactions)
    store.import_statement(statement, CategorizationReport(items=items))


def _t(archive_id: str, booking_date: date, amount: Decimal, counterparty: str = "",
       type_: str = "OSTO") -> Transaction:
    return Transaction(booking_date, amount, type_, counterparty=counterparty, archive_id=archive_id)


def test_spending_by_category_filters_by_date_range(store):
    _import(store, [
        (_t("1", date(2026, 3, 5), Decimal("-20.00")), "Groceries", "llm"),
        (_t("2", date(2026, 3, 15), Decimal("-30.00")), "Groceries", "llm"),
        (_t("3", date(2026, 3, 10), Decimal("1000.00")), "Income", "type_rule"),
        (_t("4", date(2026, 4, 1), Decimal("-999.00")), "Groceries", "llm"),  # outside range
    ])
    totals = store.spending_by_category("2026-03-01", "2026-03-31")
    assert totals == {"Groceries": Decimal("-50.00"), "Income": Decimal("1000.00")}


def test_category_total_is_zero_not_none_when_no_rows(store):
    assert store.category_total("Groceries", "2026-01-01", "2026-12-31") == Decimal("0")


def test_top_counterparties_buckets_unresolved_as_unknown_and_excludes_income(store):
    store.create_tentative_counterparty("K-Market", "merchant", "Groceries")
    _import(store, [
        (_t("1", date(2026, 3, 1), Decimal("-20.00"), "K-Market"), "Groceries", "llm"),
        (_t("2", date(2026, 3, 2), Decimal("-5.00"), "Some New Shop"), "Shopping", "llm"),
        (_t("3", date(2026, 3, 3), Decimal("500.00")), "Income", "type_rule"),
    ])
    rows = store.top_counterparties("2026-01-01", "2026-12-31")
    assert ("K-Market", Decimal("-20.00")) in rows
    assert ("(counterparty unknown)", Decimal("-5.00")) in rows
    assert all(name != "Income" for name, _ in rows)  # positive rows excluded


def test_top_counterparties_respects_category_and_limit(store):
    store.create_tentative_counterparty("K-Market", "merchant", "Groceries")
    store.create_tentative_counterparty("S-Market", "merchant", "Groceries")
    _import(store, [
        (_t("1", date(2026, 3, 1), Decimal("-20.00"), "K-Market"), "Groceries", "llm"),
        (_t("2", date(2026, 3, 2), Decimal("-40.00"), "S-Market"), "Groceries", "llm"),
        (_t("3", date(2026, 3, 3), Decimal("-5.00"), "Netflix"), "Subscriptions", "llm"),
    ])
    rows = store.top_counterparties("2026-01-01", "2026-12-31", category="Groceries", limit=1)
    assert rows == [("S-Market", Decimal("-40.00"))]


def test_monthly_totals_groups_across_month_boundary(store):
    _import(store, [
        (_t("1", date(2026, 3, 30), Decimal("-10.00")), "Groceries", "llm"),
        (_t("2", date(2026, 4, 1), Decimal("-15.00")), "Groceries", "llm"),
        (_t("3", date(2026, 4, 20), Decimal("-5.00")), "Groceries", "llm"),
    ])
    totals = store.monthly_totals("2026-01-01", "2026-12-31", "Groceries")
    assert totals == {"2026-03": Decimal("-10.00"), "2026-04": Decimal("-20.00")}


def test_monthly_totals_with_no_category_sums_everything(store):
    _import(store, [
        (_t("1", date(2026, 3, 5), Decimal("-10.00")), "Groceries", "llm"),
        (_t("2", date(2026, 3, 6), Decimal("100.00")), "Income", "type_rule"),
    ])
    totals = store.monthly_totals("2026-01-01", "2026-12-31")
    assert totals == {"2026-03": Decimal("90.00")}


def test_counterparty_total_exact_then_substring_match(store):
    store.create_tentative_counterparty("Jari Sillanpää", "person", "Transfers")
    _import(store, [
        (_t("1", date(2026, 3, 1), Decimal("-20.00"), "Jari Sillanpää"), "Transfers", "person"),
        (_t("2", date(2026, 3, 2), Decimal("-15.00"), "Jari Sillanpää"), "Transfers", "person"),
    ])
    assert store.counterparty_total("Jari Sillanpää", "2026-01-01", "2026-12-31") == \
        ("Jari Sillanpää", Decimal("-35.00"))
    assert store.counterparty_total("jari", "2026-01-01", "2026-12-31") == \
        ("Jari Sillanpää", Decimal("-35.00"))


def test_counterparty_total_returns_none_for_unknown_name(store):
    assert store.counterparty_total("Nobody At All", "2026-01-01", "2026-12-31") is None


def test_unnamed_payment_app_spend_gets_an_explanatory_bucket(store):
    from datetime import date
    from decimal import Decimal

    from spending_analyzer.categorize import Categorized, CategorizationReport
    from spending_analyzer.models import Statement, Transaction
    from spending_analyzer.store import PAYMENT_APP_BUCKET

    rows = [(Transaction(date(2026, 9, d), Decimal(a), "TILISIIRTO", counterparty=name,
                         archive_id=str(d)), category, source)
            for d, a, name, category, source in [
                (1, "-20", "VIPPS MOBILEPAY AS", "Transfers", "payment_app"),
                (2, "-15", "MobilePay", "Transfers", "payment_app"),
                (3, "-50", "Liisa Virtanen", "Transfers", "person")]]
    store.create_tentative_counterparty("Liisa Virtanen", "person", "Transfers")
    statement = Statement("FI00", date(2026, 9, 1), date(2026, 9, 30), Decimal(0),
                          Decimal("-85"), [t for t, _, _ in rows])
    store.import_statement(statement, CategorizationReport(
        items=[Categorized(t, c, s) for t, c, s in rows]))

    top = store.top_counterparties("2026-09-01", "2026-09-30", "Transfers")

    assert top == [("Liisa Virtanen", Decimal("-50.00")),
                   (PAYMENT_APP_BUCKET, Decimal("-35.00"))]
