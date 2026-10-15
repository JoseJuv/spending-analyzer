"""Web UI for the spending analyzer. Runs locally next to the Ollama model.

Usage:
    streamlit run src/spending_analyzer/ui/app.py
    streamlit run src/spending_analyzer/ui/app.py -- --db tests/fixtures/ui_demo.db
"""

import math

import streamlit as st

from spending_analyzer.llm import DEFAULT_OLLAMA_MODEL
from spending_analyzer.pipeline import confirmed_share
from spending_analyzer.ui import nav
from spending_analyzer.ui.state import (db_path, demo_mode, get_store, installed_models,
                                        reset_demo_database, save_setting, selected_model)

st.set_page_config(page_title="Spending Analyzer", page_icon="📊", layout="wide")

pages = st.navigation(nav.PAGES)


def _model_picker() -> None:
    models = installed_models()
    if isinstance(models, str):
        st.error(models)
        return
    if not models:
        st.error(f"No local models installed. Run: ollama pull {DEFAULT_OLLAMA_MODEL}")
        return
    current = selected_model()
    names = [m.name for m in models]
    choice = st.selectbox(
        "Local model", names, index=names.index(current.name),
        format_func=lambda n: next(f"{m.name} ({m.parameter_size}, {m.quantization})"
                                   for m in models if m.name == n),
        help="Models installed in Ollama on this computer. Add more with "
             "`ollama pull <model>`.")
    if choice != current.name:
        save_setting("model", choice)
        st.rerun()
    if choice != DEFAULT_OLLAMA_MODEL:
        st.caption(f"The recorded accuracy figures were measured with "
                   f"{DEFAULT_OLLAMA_MODEL}; other models may categorize differently.")


with st.sidebar:
    st.markdown("🔒 **Runs on this computer.** Nothing is sent anywhere.")
    _model_picker()

    demo = st.toggle("Demo mode (synthetic data)", value=demo_mode(),
                     help="Uses a separate demo database with made-up statements, so you "
                          "can show the app without your own finances.")
    if demo != demo_mode():
        save_setting("demo_mode", demo)
        st.rerun()
    if demo:
        st.info("Demo mode: made-up data in a separate database.")
        if st.button("Reset demo data"):
            reset_demo_database()
            for key in ("pending", "last_results", "chat"):
                st.session_state.pop(key, None)
            st.rerun()

    share = confirmed_share(get_store())
    if share is not None:
        # Rounded down: "100 %" must mean nothing is left to review
        st.metric("Confirmed categories", f"{math.floor(share * 100)} %",
                  help="Share of all money moved whose category you have confirmed "
                       "(or a fixed rule decided). The rest are AI suggestions.")
    st.caption(f"Database: `{db_path()}`")

pages.run()
