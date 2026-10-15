"""Step 1: upload statements, parse them in memory and show whether they validate."""

import streamlit as st

from spending_analyzer.pipeline import ParsedUpload, demo_uploads, is_imported, parse_upload
from spending_analyzer.ui.state import demo_mode, get_store, money


def pending_uploads() -> dict[str, ParsedUpload]:
    """Parsed files waiting for categorization. Kept in the session rather than read
    from the upload widget, which Streamlit clears when you switch pages."""
    return st.session_state.setdefault("pending", {})


def render() -> None:
    st.title("Upload statements")
    st.caption("PDFs are read in memory on this computer and never saved or sent "
               "anywhere. Only the transactions go into the local database.")

    generation = st.session_state.setdefault("uploader_generation", 0)
    files = st.file_uploader("Bank statement PDFs", type="pdf", accept_multiple_files=True,
                             key=f"uploader_{generation}")
    pending = pending_uploads()
    for f in files or []:
        key = f"{f.name}:{f.size}"
        if key not in pending:
            with st.spinner(f"Reading {f.name}…"):
                pending[key] = parse_upload(f.name, f.getvalue())

    if demo_mode():
        if st.button("Add 3 made-up demo statements (July–September 2026)"):
            for upload in demo_uploads():
                pending[f"{upload.filename}:demo"] = upload
            st.rerun()

    store = get_store()
    if pending:
        st.subheader("Waiting for categorization")
        for upload in pending.values():
            _show_upload(upload, store)
        left, right = st.columns([1, 5])
        if left.button("Clear list"):
            pending.clear()
            st.session_state.uploader_generation += 1  # also empties the upload widget
            st.rerun()
        from spending_analyzer.ui import nav
        right.page_link(nav.CATEGORIZE, label="Next: categorize", icon="➡️")

    imported = store.statement_summaries()
    if imported:
        st.subheader("Already in the database")
        st.dataframe(
            [{"Account": f"…{s['account_iban'][-4:]}",
              "Period": f"{s['period_start']} – {s['period_end']}",
              "Transactions": s["transactions"]} for s in imported],
            hide_index=True)


def _show_upload(upload: ParsedUpload, store) -> None:
    s = upload.statement
    with st.container(border=True):
        if s is None:
            st.error(f"**{upload.filename}**: {upload.errors[0]}")
            return
        icon = "✅" if upload.valid else "⚠️"
        st.markdown(f"{icon} **{upload.filename}**: {s.period_start:%d.%m.%Y} – "
                    f"{s.period_end:%d.%m.%Y}, {len(s.transactions)} transactions")
        deposits = s.deposits_count if s.deposits_count is not None else "–"
        withdrawals = s.withdrawals_count if s.withdrawals_count is not None else "–"
        st.markdown(f"Balance {money(s.opening_balance)} → {money(s.closing_balance)} · "
                    f"{deposits} deposits · {withdrawals} withdrawals")
        if upload.valid:
            st.caption("Balances, totals and counts match the statement's own summary, "
                       "so every transaction was read.")
        else:
            st.warning("The numbers don't add up, so some transactions were probably "
                       "read wrong. It won't be categorized unless you allow it on the "
                       "next page.\n\n" + "\n".join(f"- {e}" for e in upload.errors))
        if is_imported(store, s):
            st.info("This statement is already in the database; categorizing it again "
                    "updates it.")
