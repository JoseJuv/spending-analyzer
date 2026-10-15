"""Step 2: categorize pending statements with rules first and the local model for the
rest, then store them."""

import time
from collections import Counter, defaultdict
from decimal import Decimal

import streamlit as st

from spending_analyzer.categorize import NOT_SPENDING_OR_INCOME
from spending_analyzer.llm import LLMError
from spending_analyzer.pipeline import categorize_and_import
from spending_analyzer.ui.state import SOURCE_LABELS, get_llm, get_store, money


def render() -> None:
    from spending_analyzer.ui import nav
    from spending_analyzer.ui.upload_page import pending_uploads

    st.title("Categorize")
    st.caption("Known merchants are decided by your confirmed choices and fixed rules. "
               "Only new ones go to the AI model, which runs on this computer.")

    pending = pending_uploads()
    readable = {k: u for k, u in pending.items() if u.statement is not None}
    if not readable:
        st.info("Nothing waiting. Upload statements first.")
        st.page_link(nav.UPLOAD, label="Go to upload", icon="📄")
        _show_last_results()
        return

    allow_invalid = False
    if any(not u.valid for u in readable.values()):
        allow_invalid = st.checkbox(
            "Also categorize statements that failed validation (their totals may be wrong)")
    selected = {k: u for k, u in readable.items() if u.valid or allow_invalid}
    for upload in readable.values():
        mark = "✅" if upload.valid else ("⚠️" if allow_invalid else "⛔ skipped")
        st.markdown(f"- {mark} {upload.filename} ({len(upload.statement.transactions)} "
                    "transactions)")

    if not st.button(f"Categorize {len(selected)} statement(s)", type="primary",
                     disabled=not selected):
        _show_last_results()
        return

    results = []
    with st.status("Categorizing…", expanded=True) as status:
        for key, upload in selected.items():
            bar = st.progress(0.0, text=f"{upload.filename}: applying rules…")

            def progress(done: int, total: int, name: str = upload.filename) -> None:
                bar.progress(done / total if total else 1.0,
                             text=f"{name}: AI batch {done}/{total}")

            started = time.perf_counter()
            try:
                report = categorize_and_import(upload.statement, get_store(), get_llm(),
                                               progress)
            except LLMError as error:
                status.update(label="The local model did not answer", state="error")
                st.error(f"{error}\n\nIs Ollama running? No transactions from this "
                         "statement were saved; try again once it is.")
                return
            bar.progress(1.0, text=f"{upload.filename}: done")
            results.append((upload.filename, report, time.perf_counter() - started))
            del pending[key]
        status.update(label="Categorized", state="complete", expanded=False)
    st.session_state.last_results = results
    st.rerun()  # redraw without the finished upload list, with the sidebar meter updated


def _show_last_results() -> None:
    from spending_analyzer.ui import nav

    results = st.session_state.get("last_results")
    if not results:
        return
    st.subheader("Result")
    for filename, report, seconds in results:
        with st.container(border=True):
            st.markdown(f"**{filename}**: {len(report.items)} transactions in {seconds:.0f} s, "
                        f"{report.llm_calls} AI call(s)")
            by_source = Counter(item.source for item in report.items)
            st.markdown("Decided by: " + " · ".join(
                f"**{count}** {SOURCE_LABELS.get(source, source)}"
                for source, count in by_source.most_common()))

            totals: dict[str, Decimal] = defaultdict(Decimal)
            for item in report.items:
                totals[item.category] += item.transaction.amount
            spending = {c: v for c, v in totals.items() if c not in NOT_SPENDING_OR_INCOME}
            moved = {c: v for c, v in totals.items() if c in NOT_SPENDING_OR_INCOME}
            left, right = st.columns(2)
            left.markdown("**Spending and income**")
            left.dataframe([{"Category": c, "Total": money(v)}
                            for c, v in sorted(spending.items(), key=lambda kv: kv[1])],
                           hide_index=True, height=36 * (len(spending) + 1) + 3)
            if moved:
                right.markdown("**Moved between your own assets**")
                right.dataframe([{"Category": c, "Total": money(v)} for c, v in moved.items()],
                                hide_index=True)
                right.caption("Transfers and investments are not spending or income.")
    st.page_link(nav.REVIEW, label="Next: review the AI's suggestions", icon="➡️")
