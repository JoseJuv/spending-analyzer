"""Step 5: ask questions. The local model only picks a tool and phrases the answer;
code resolves dates, computes every number and draws the chart."""

import json
from decimal import Decimal

import streamlit as st

from spending_analyzer.chatbot import Answer, Part, answer_question
from spending_analyzer.llm import LLMError
from spending_analyzer.ui.charts import ranked_bars, signed_month_bars
from spending_analyzer.ui.state import get_llm, get_store, money

EXAMPLES = [
    "Where did my money go last month?",
    "Which shops did I spend the most on groceries last month?",
    "How much did I spend on restaurants this year?",
    "Show my spending trend for the last 3 months",
]


def _show_part(part: Part) -> None:
    if part.table:
        st.dataframe([{"Date": r["date"], "Counterparty": r["counterparty"],
                       "Amount": money(Decimal(r["amount"])), "Category": r["category"]}
                      for r in part.table], hide_index=True, use_container_width=True)
        shown, count = len(part.table), part.result.get("count", len(part.table))
        if count > shown:
            st.caption(f"Showing the {shown} largest of {count} transactions.")
    elif part.chart:
        if part.tool == "monthly_trend":
            st.altair_chart(signed_month_bars(part.chart, "€"), use_container_width=True)
        elif part.result.get("ranked_by") == "number of transactions":
            st.altair_chart(ranked_bars(part.chart, "Transactions", euros=False),
                            use_container_width=True)
        else:
            st.altair_chart(ranked_bars(part.chart, "€"), use_container_width=True)


def _show(answer: Answer) -> None:
    st.markdown(answer.text)
    for part in answer.parts:
        _show_part(part)
    if not answer.parts:
        return
    with st.expander("How this was answered"):
        st.markdown(f"The AI chose {len(answer.parts)} tool call(s); the application then "
                    "resolved the dates and computed every number from your local database.")
        for part in answer.parts:
            st.markdown(f"**{part.tool}**")
            st.code(json.dumps({k: v for k, v in part.selection.items() if v not in (None, "")},
                               indent=2, ensure_ascii=False), language="json")
            st.markdown("Data the answer was written from:")
            st.code(json.dumps(part.result, indent=2, ensure_ascii=False), language="json")


def render() -> None:
    st.title("Ask about your spending")
    st.caption("Each question is answered on its own (no memory of earlier questions). "
               "Everything runs on this computer.")
    store = get_store()
    if store.date_bounds() is None:
        st.info("No transactions yet. Upload and categorize a statement first.")
        return

    history = st.session_state.setdefault("chat", [])
    for message in history:
        with st.chat_message(message["role"]):
            if message["role"] == "user":
                st.markdown(message["text"])
            else:
                _show(message["answer"])

    question = st.chat_input("e.g. Which shops did I spend the most on groceries last month?")
    if not history and not question:
        picked = st.pills("Try one of these", EXAMPLES)
        question = picked or question
    if not question:
        return

    history.append({"role": "user", "text": question})
    with st.chat_message("user"):
        st.markdown(question)
    with st.chat_message("assistant"):
        try:
            with st.spinner("Thinking…"):
                llm = get_llm()
                answer = answer_question(question, store, router=llm, local=llm)
        except LLMError as error:
            st.error(f"The local model did not answer: {error}")
            history.pop()
            return
        _show(answer)
    history.append({"role": "assistant", "answer": answer})
    if len(history) == 2:
        st.rerun()  # hide the example buttons after the first question
