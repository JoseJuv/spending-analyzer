"""Step 4: the overview. Every number is computed in code from the database; no AI is
involved on this page. Transfers and investments are shown apart from spending."""

import calendar
from datetime import date
from decimal import Decimal

import pandas as pd
import streamlit as st

from spending_analyzer.categorize import CATEGORIES, NOT_SPENDING_OR_INCOME
from spending_analyzer.pipeline import confirmed_share
from spending_analyzer.ui.charts import monthly_bars, ranked_bars
from spending_analyzer.ui.state import SOURCE_LABELS, get_store, money


def _month_start(d: date, months_back: int = 0) -> date:
    year, month = d.year, d.month - months_back
    while month <= 0:
        month += 12
        year -= 1
    return date(year, month, 1)


def _choose_period(first: date, last: date) -> tuple[date, date]:
    """Presets are relative to the newest transaction, not today: statements arrive
    after the month ends, so "this month" would usually be empty."""
    latest = _month_start(last)
    options = {
        f"{calendar.month_name[last.month]} {last.year}": (latest, last),
        "Last 3 months": (max(first, _month_start(last, 2)), last),
        "Everything": (first, last),
        "Custom": None,
    }
    choice = st.segmented_control("Period", list(options), default=list(options)[0],
                                  label_visibility="collapsed")
    if options.get(choice) is not None:
        return options[choice]
    picked = st.date_input("Dates", value=(latest, last), min_value=first, max_value=last,
                           format="DD.MM.YYYY")
    if isinstance(picked, tuple) and len(picked) == 2:
        return picked
    return latest, last  # while the user has picked only the start date


def render() -> None:
    st.title("Results")
    store = get_store()
    bounds = store.date_bounds()
    if bounds is None:
        st.info("No transactions yet. Upload and categorize a statement first.")
        return

    start, end = _choose_period(*bounds)
    s, e = start.isoformat(), end.isoformat()
    st.caption(f"{start:%d.%m.%Y} – {end:%d.%m.%Y}")

    share = confirmed_share(store)
    if share is not None and share < 1:
        st.info(f"{100 - int(share * 100)} % of the money is still categorized by unconfirmed "
                "AI suggestions. Confirm them on the Review page for exact numbers.")

    totals = store.spending_by_category(s, e)
    spending_by = {c: -v for c, v in totals.items()
                   if c not in NOT_SPENDING_OR_INCOME and c != "Income"}
    spent = sum(spending_by.values(), Decimal(0))
    income = totals.get("Income", Decimal(0))
    cols = st.columns(3)
    cols[0].metric("Spent", money(spent))
    cols[1].metric("Income", money(income))
    cols[2].metric("Net", money(income - spent),
                   help="Income minus spending. Transfers and investments are left out.")

    left, right = st.columns(2)
    with left:
        st.subheader("Spending by category")
        rows = sorted(((c, v) for c, v in spending_by.items() if v), key=lambda kv: -kv[1])
        if rows:
            st.altair_chart(ranked_bars(rows, "Spent (€)"), use_container_width=True)
        else:
            st.caption("No spending in this period.")
    with right:
        st.subheader("Top counterparties")
        options = ["All categories"] + [c for c in CATEGORIES if c != "Income"
                                        and c not in NOT_SPENDING_OR_INCOME]
        category = st.selectbox("Category", options, label_visibility="collapsed")
        top = store.top_counterparties(s, e, None if category == options[0] else category, 10)
        if top:
            st.altair_chart(ranked_bars(top, "Spent (€)"), use_container_width=True)
        else:
            st.caption("No spending in this category and period.")

    by_month = store.monthly_by_category(s, e)
    months = sorted({ym for ym, _ in by_month})
    if len(months) > 1:
        st.subheader("Month by month")
        spending = [-sum((v for (ym, c), v in by_month.items() if ym == m
                          and c not in NOT_SPENDING_OR_INCOME and c != "Income"), Decimal(0))
                    for m in months]
        income_by_month = [by_month.get((m, "Income"), Decimal(0)) for m in months]
        st.altair_chart(monthly_bars(months, spending, income_by_month),
                        use_container_width=True)

    moved = {c: totals[c] for c in NOT_SPENDING_OR_INCOME if c in totals}
    if moved:
        st.subheader("Moved between your own assets")
        st.caption("Not spending or income: transfers to people and accounts, and money "
                   "put into or taken out of investments.")
        st.dataframe([{"Category": c, "Net": money(v)} for c, v in moved.items()],
                     hide_index=True)

    with st.expander("Transactions"):
        pick = st.selectbox("Show", ["All"] + CATEGORIES, key="tx_category")
        rows = store.transactions_between(s, e, None if pick == "All" else pick)
        st.dataframe(pd.DataFrame([{
            "Date": r["date"], "Counterparty": r["counterparty"], "Amount": money(r["amount"]),
            "Category": r["category"], "Decided by": SOURCE_LABELS.get(r["source"], r["source"]),
        } for r in rows]), hide_index=True, use_container_width=True)
