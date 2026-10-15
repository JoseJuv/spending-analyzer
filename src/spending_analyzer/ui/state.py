"""Per-session resources shared by the pages: settings, the store and the local LLM.

The real database location comes from `-- --db <path>` after the script, the
SPENDING_DB environment variable, or defaults to data/transactions.db. Demo mode
switches to a separate demo.db in the same folder, so synthetic statements never mix
with real ones. Settings (chosen model, demo mode) persist in ui_settings.json there.
"""

import json
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import streamlit as st

from spending_analyzer.llm import (DEFAULT_OLLAMA_MODEL, AuditedClient, LLMClient, LLMError,
                                   OllamaClient, OllamaModel, list_ollama_models)
from spending_analyzer.store import Store

SOURCE_LABELS = {
    "llm": "AI suggestion",
    "counterparty_tentative": "Earlier AI suggestion",
    "person": "Looks like a private person",
    "counterparty_confirmed": "Confirmed by you",
    "override": "Confirmed by you (this purchase)",
    "type_rule": "Rule (salary)",
    "payment_app": "Rule (MobilePay/Vipps)",
}


# --- locations and settings ---

def real_db_path() -> Path:
    if "--db" in sys.argv[:-1]:
        return Path(sys.argv[sys.argv.index("--db") + 1])
    return Path(os.environ.get("SPENDING_DB", "data/transactions.db"))


def _settings_path() -> Path:
    return real_db_path().parent / "ui_settings.json"


def settings() -> dict:
    if "settings" not in st.session_state:
        try:
            st.session_state.settings = json.loads(_settings_path().read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            st.session_state.settings = {}
    return st.session_state.settings


def save_setting(key: str, value) -> None:
    settings()[key] = value
    path = _settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings(), indent=2), encoding="utf-8")


def demo_mode() -> bool:
    return bool(settings().get("demo_mode", False))


def db_path() -> Path:
    real = real_db_path()
    return real.with_name("demo.db") if demo_mode() else real


def _log_path(name: str) -> Path:
    return db_path().parent / (f"demo_{name}" if demo_mode() else name)


# --- store ---

def get_store() -> Store:
    """One connection per browser session, reopened when demo mode switches databases."""
    path = db_path()
    if st.session_state.get("store_path") != path:
        if "store" in st.session_state:
            st.session_state.store.close()
        st.session_state.store = Store(path)
        st.session_state.store_path = path
    return st.session_state.store


def _close_store() -> None:
    if "store" in st.session_state:
        st.session_state.store.close()
        del st.session_state["store"], st.session_state["store_path"]


def reset_demo_database() -> None:
    _close_store()
    for path in (real_db_path().with_name("demo.db"), _log_path("llm_audit.jsonl")):
        path.unlink(missing_ok=True)


def data_files() -> dict[str, Path]:
    """Every file holding data in the current mode (real or demo). Uploaded PDFs are
    never stored, and Ollama keeps nothing between requests, so this is everything."""
    return {"Database": db_path(), "AI request log": _log_path("llm_audit.jsonl"),
            "Review log": _log_path("review_log.jsonl")}


def delete_all_data() -> list[Path]:
    """Deletes the current mode's database and logs; returns what was deleted."""
    _close_store()
    deleted = [p for p in data_files().values() if p.exists()]
    for path in deleted:
        path.unlink()
    for key in ("pending", "last_results", "chat"):
        st.session_state.pop(key, None)
    return deleted


def audit_log_path() -> Path:
    return _log_path("llm_audit.jsonl")


# --- local models ---

@st.cache_data(ttl=10, show_spinner=False)
def installed_models() -> list[OllamaModel] | str:
    """Installed text models, or an error message when Ollama isn't running."""
    try:
        return list_ollama_models()
    except LLMError:
        return "Ollama is not running. Start it, then reload this page."


def selected_model() -> OllamaModel | None:
    models = installed_models()
    if isinstance(models, str) or not models:
        return None
    by_name = {m.name: m for m in models}
    chosen = settings().get("model")
    return by_name.get(chosen) or by_name.get(DEFAULT_OLLAMA_MODEL) or models[0]


def get_llm() -> LLMClient:
    model = selected_model()
    if model is None:
        raise LLMError("No local model available. Is Ollama running?")
    # Thinking off where the model has it (measured: 6x slower, no accuracy gain)
    client = OllamaClient(model=model.name, think=False if model.can_think else None)
    return AuditedClient(client, _log_path("llm_audit.jsonl"))


# --- formatting and logs ---

def money(amount: Decimal) -> str:
    """Display only — amounts stay Decimal everywhere else."""
    sign = "−" if amount < 0 else ""
    return f"{sign}{abs(amount):,.2f} €".replace(",", " ")


def log_review(summary, model: str) -> None:
    """Counts only (no names or amounts), like the CLI's review log."""
    log = _log_path("review_log.jsonl")
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"time": datetime.now(timezone.utc).isoformat(), "model": model,
                            "reviewed": dict(summary.reviewed),
                            "corrected": dict(summary.corrected), "via": "web"}) + "\n")
