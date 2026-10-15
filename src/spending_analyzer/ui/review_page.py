"""Step 3: the AI suggests, the user confirms. Each answer is saved, so a merchant is
asked about once; mixed merchants (fuel stations that also sell food) per purchase."""

import pandas as pd
import streamlit as st

from spending_analyzer.categorize import CATEGORIES
from spending_analyzer.review import ReviewSummary, apply_decision, pending_review
from spending_analyzer.ui.state import (SOURCE_LABELS, get_store, log_review, money,
                                        selected_model)


def render() -> None:
    st.title("Review")
    store = get_store()

    if saved := st.session_state.pop("review_saved", None):
        st.success(saved)

    items = pending_review(store)
    if not items:
        st.info("Nothing to review: every category is confirmed or decided by a rule.")
        return

    st.caption("Check the suggested categories and change the wrong ones. Your answer "
               "for a merchant applies to all of its transactions, now and in future "
               "statements. Merchants marked *per purchase* sell different kinds of "
               "things, so each purchase is confirmed separately.")

    # Editable columns first, so they stay visible on a narrow screen
    table = pd.DataFrame([{
        "Counterparty": item.label,
        "Category": item.suggested,
        "Confirm": True,
        "Amount": money(item.total),
        "Scope": (f"per purchase, {item.booking_date:%d.%m.}" if item.mixed
                  else f"{item.transactions} transaction(s)"),
        "Suggested by": SOURCE_LABELS.get(item.source, item.source),
    } for item in items])

    edited = st.data_editor(
        table, hide_index=True, num_rows="fixed", use_container_width=True,
        disabled=["Counterparty", "Scope", "Amount", "Suggested by"],
        column_config={
            "Category": st.column_config.SelectboxColumn(options=CATEGORIES, required=True),
            "Confirm": st.column_config.CheckboxColumn(
                help="Untick to leave this one for later"),
        },
        key=f"review_editor_{len(items)}")

    to_save = edited[edited["Confirm"]]
    changed = int((to_save["Category"] != table.loc[to_save.index, "Category"]).sum())
    if st.button(f"Save {len(to_save)} confirmation(s)", type="primary",
                 disabled=to_save.empty):
        summary = ReviewSummary()
        for index, row in to_save.iterrows():
            apply_decision(items[index], row["Category"], store, summary)
        store.refresh_categories()
        model = selected_model()
        log_review(summary, model.name if model else "unknown")
        st.session_state.review_saved = (
            f"Saved {len(to_save)} confirmation(s). You corrected {changed} of the "
            f"AI's suggestions.")
        st.rerun()
