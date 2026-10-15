"""Smoke tests that run the real Streamlit pages headlessly (no browser, no model)."""

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from spending_analyzer import synthetic
from spending_analyzer.llm import OllamaModel
from spending_analyzer.pipeline import categorize_and_import
from spending_analyzer.review import pending_review
from spending_analyzer.store import Store
from test_categorize import FakeLLM
from test_chatbot import ScriptedLLM

APP = str(Path(__file__).parents[1] / "src" / "spending_analyzer" / "ui" / "app.py")
MODELS = [OllamaModel("qwen3.5:9b", "9.0B", "Q4_K_M", 1, ["completion", "thinking"]),
          OllamaModel("gemma4:12b", "12B", "Q4_0", 1, ["completion"])]


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "ui.db"
    monkeypatch.setenv("SPENDING_DB", str(path))
    # Don't depend on which models happen to be installed, or on Ollama running
    monkeypatch.setattr("spending_analyzer.ui.state.installed_models", lambda: MODELS)
    return path


def _with_synthetic_data(path: Path) -> None:
    store = Store(path)
    for seed, month in ((0, 8), (1, 9)):
        statement, _ = synthetic.generate(seed=seed, month=month)
        categorize_and_import(statement, store, FakeLLM(runs_locally=True))
    store.close()


def _run(page: str) -> AppTest:
    """Runs one page on its own, without the sidebar."""
    script = f"from spending_analyzer.ui import {page}\n{page}.render()\n"
    return AppTest.from_string(script, default_timeout=30).run()


def test_app_starts_on_the_upload_page_with_the_model_picker(db):
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception
    assert at.title[0].value == "Upload statements"
    picker = at.sidebar.selectbox[0]
    assert picker.value == "qwen3.5:9b" and len(picker.options) == 2


def test_choosing_another_model_is_remembered(db):
    at = AppTest.from_file(APP, default_timeout=30).run()
    at.sidebar.selectbox[0].select("gemma4:12b").run()
    assert not at.exception
    again = AppTest.from_file(APP, default_timeout=30).run()  # a new browser session
    assert again.sidebar.selectbox[0].value == "gemma4:12b"
    assert (db.parent / "ui_settings.json").exists()


def test_demo_mode_uses_a_separate_database(db):
    at = AppTest.from_file(APP, default_timeout=30).run()
    at.sidebar.toggle[0].set_value(True).run()
    assert not at.exception
    demo = next(b for b in at.button if b.label.startswith("Add 3 made-up"))
    demo.click().run()
    assert not at.exception
    assert any("demo_2026-09.pdf" in m.value for m in at.markdown)
    assert not db.exists() or Store(db).date_bounds() is None  # real DB untouched


def test_categorize_page_without_uploads_points_back_to_upload(db):
    at = _run("categorize_page")
    assert not at.exception
    assert "Upload statements first" in at.info[0].value


def test_review_page_saves_every_confirmation(db):
    _with_synthetic_data(db)
    assert pending_review(Store(db))

    at = _run("review_page")
    assert not at.exception
    at.button[0].click().run()

    assert not at.exception
    assert "Saved" in at.success[0].value
    assert pending_review(Store(db)) == []


def test_results_page_shows_totals_and_charts(db):
    _with_synthetic_data(db)
    at = _run("results_page")
    assert not at.exception
    assert [m.label for m in at.metric] == ["Spent", "Income", "Net"]
    at.segmented_control[0].set_value("Everything").run()
    assert not at.exception
    assert any(s.value == "Month by month" for s in at.subheader)


def test_chat_page_answers_with_computed_data(db, monkeypatch):
    _with_synthetic_data(db)
    llm = ScriptedLLM({"tool": "top_merchants", "category": "Groceries",
                       "period": "all_time", "answer": "K-market is your top grocery store."})
    monkeypatch.setattr("spending_analyzer.ui.chat_page.get_llm", lambda: llm)

    at = _run("chat_page")
    at.chat_input[0].set_value("Which grocery shops did I use most?").run()

    assert not at.exception
    assert any("top grocery store" in m.value for m in at.markdown)
    from spending_analyzer.chatbot import TOOL_SYSTEM_PROMPT
    router_prompts = [prompt for system, prompt in llm.prompts if system == TOOL_SYSTEM_PROMPT]
    assert router_prompts == ["Which grocery shops did I use most?"]  # no data to the router


def test_privacy_page_deletes_the_database_and_logs(db):
    _with_synthetic_data(db)
    log = db.parent / "llm_audit.jsonl"
    log.write_text('{"time": "2026-10-15T10:00:00", "model": "fake", "prompt": "0 | Shop"}\n',
                   encoding="utf-8")

    at = _run("privacy_page")
    assert not at.exception
    assert any("1 requests" in m.value for m in at.markdown)
    delete = next(b for b in at.button if b.label.startswith("Delete"))
    assert delete.disabled  # needs the confirmation tick first
    at.checkbox[0].check().run()
    next(b for b in at.button if b.label.startswith("Delete")).click().run()

    assert not at.exception
    assert "Deleted 2 file(s)" in at.success[0].value
    assert not log.exists() and Store(db).counts()["transactions"] == 0
