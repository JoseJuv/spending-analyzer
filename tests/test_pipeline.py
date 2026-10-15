import io

from reportlab.pdfgen import canvas

from spending_analyzer import synthetic
from spending_analyzer.pipeline import (categorize_and_import, confirmed_share, is_imported,
                                        parse_upload)
from spending_analyzer.review import ReviewSummary, apply_decision, pending_review
from test_categorize import FakeLLM


def _pdf(tmp_path, seed=0):
    statement, _ = synthetic.generate(seed=seed)
    path = tmp_path / f"statement_{seed}.pdf"
    synthetic.render(statement, path)
    return statement, path.read_bytes()


def test_parse_upload_reads_and_validates_from_memory(tmp_path):
    truth, data = _pdf(tmp_path)
    upload = parse_upload("statement.pdf", data)
    assert upload.valid and upload.statement == truth


def test_parse_upload_reports_an_unreadable_file():
    upload = parse_upload("notes.pdf", b"this is not a pdf")
    assert upload.statement is None and not upload.valid and upload.errors


def test_another_banks_statement_is_rejected_with_a_clear_reason():
    """Regression: the error used to be blank ("Could not read the statement: ")."""
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer)
    for y, line in ((800, "Other Bank Ltd - Account statement"),
                    (780, "01.09.2026  Grocery store  -23,40")):
        pdf.drawString(50, y, line)
    pdf.save()
    upload = parse_upload("other.pdf", buffer.getvalue())
    assert upload.statement is None
    assert "Not a supported statement" in upload.errors[0]


def test_upload_categorize_review_end_to_end(tmp_path, store):
    _, data = _pdf(tmp_path)
    statement = parse_upload("statement.pdf", data).statement
    categorize_and_import(statement, store, FakeLLM(runs_locally=True))
    assert is_imported(store, statement)
    assert 0 < confirmed_share(store) < 1

    items = pending_review(store)
    assert items and any(item.mixed for item in items)
    summary = ReviewSummary()
    for item in items:  # the user corrects K-market and accepts everything else
        answer = "Restaurants" if item.label.startswith("K-market") else item.suggested
        apply_decision(item, answer, store, summary)
    store.refresh_categories()

    assert pending_review(store) == []
    assert confirmed_share(store) == 1.0
    assert sum(summary.corrected.values()) == 1


def test_stored_categories_follow_the_review_without_recategorizing(tmp_path, store):
    """Regression: rows were imported before the review and kept their old category,
    so the chatbot answered with pre-review numbers."""
    _, data = _pdf(tmp_path)
    statement = parse_upload("statement.pdf", data).statement
    categorize_and_import(statement, store, FakeLLM(runs_locally=True))
    kmarket = next(i for i in pending_review(store) if i.label.startswith("K-market"))
    before = store.category_total("Restaurants", "2026-09-01", "2026-09-30")

    apply_decision(kmarket, "Restaurants", store, ReviewSummary())
    store.refresh_categories()

    after = store.category_total("Restaurants", "2026-09-01", "2026-09-30")
    assert after == before + kmarket.total
