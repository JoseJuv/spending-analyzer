from dataclasses import replace
from datetime import date
from decimal import Decimal

import pdfplumber
import pytest

from spending_analyzer.pdf_parser import parse_amount, parse_date, parse_statement
from spending_analyzer.synthetic import generate, render


@pytest.mark.parametrize("text, expected", [
    ("−99,99", Decimal("-99.99")),
    ("+2150,00", Decimal("2150.00")),
    ("−1234,56", Decimal("-1234.56")),
    ("999,99", Decimal("999.99")),
])
def test_parse_amount(text, expected):
    assert parse_amount(text) == expected


def test_parse_date_without_padding_and_without_year():
    assert parse_date("5.9.2026") == date(2026, 9, 5)
    assert parse_date("1.9.", 2026) == date(2026, 9, 1)


@pytest.mark.parametrize("seed", range(5))
def test_parser_recovers_generated_statement(tmp_path, seed):
    truth, _ = generate(seed=seed)
    pdf_path = tmp_path / "statement.pdf"
    render(truth, pdf_path)

    parsed = parse_statement(pdf_path)

    assert parsed.validation_errors() == []
    assert parsed.transactions == truth.transactions
    assert parsed == truth


def test_transaction_split_across_pages(tmp_path):
    truth, _ = generate(seed=0, card_payments=80)
    pdf_path = tmp_path / "statement.pdf"
    render(truth, pdf_path)
    with pdfplumber.open(pdf_path) as pdf:
        assert len(pdf.pages) >= 3
        # Some page must start mid-transaction, i.e. without a date in the first body line
        first_body_words = [
            next(w for w in page.extract_words() if w["top"] > 190) for page in pdf.pages[1:]]
    assert any(w["x0"] > 105 for w in first_body_words)

    assert parse_statement(pdf_path) == truth


def test_validation_catches_missing_transaction():
    statement, _ = generate(seed=1)
    broken = replace(statement, transactions=statement.transactions[1:])
    errors = broken.validation_errors()
    assert any("Balance mismatch" in e for e in errors)
    assert len(errors) >= 2  # count check fails too


def _line(text="", account="", date_="", amount=""):
    from spending_analyzer.pdf_parser import Line
    return Line(date=date_, amount=amount, text=text, reference="", account=account)


@pytest.mark.parametrize("lines", [
    # Marker inline with the name (Vipps MobilePay transfers)
    ["VIPPS MOBILEPAY AS, VIESTI SEPA PIKASIIRTO", "Maksajan viite: pp-1234567"],
    # Marker on its own line, uppercase, without a colon
    ["VIPPS MOBILEPAY AS,", "VIESTI SEPA PIKASIIRTO", "Maksajan viite: pp-1234567"],
    # Marker on its own line, the usual "Viesti:" form
    ["VIPPS MOBILEPAY AS", "Viesti:", "SEPA PIKASIIRTO", "Maksajan viite: pp-1234567"],
])
def test_message_marker_variants(lines):
    from spending_analyzer.pdf_parser import _build_transaction
    block = [_line("TILISIIRTO", "12345678901234567890", "1.10.2026", "+20,00")]
    block += [_line(text) for text in lines]

    t = _build_transaction(block)

    assert t.counterparty == "VIPPS MOBILEPAY AS"
    assert t.message == "SEPA PIKASIIRTO\nMaksajan viite: pp-1234567"


@pytest.mark.parametrize("lines", [
    ["OP-Rahastoyhtiö Oy", "Alkuperäinen maksaja: OP-OSAKESALKKU A"],
    ["OP-Rahastoyhtiö Oy Alkuperäinen maksaja: OP-OSAKESALKKU A"],
])
def test_labelled_field_ends_the_name(lines):
    from spending_analyzer.pdf_parser import _build_transaction
    block = [_line("TILISIIRTO", "12345678901234567890", "1.10.2026", "+2000,00")]
    block += [_line(text) for text in lines]

    t = _build_transaction(block)

    assert t.counterparty == "OP-Rahastoyhtiö Oy"
    assert t.message == "Alkuperäinen maksaja: OP-OSAKESALKKU A"
