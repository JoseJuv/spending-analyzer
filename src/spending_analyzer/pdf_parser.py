"""Parse bank statement PDFs into a Statement.

Parsing is deterministic and based on word positions; see docs/statement-layout.md.

Usage (prints only counts and validation results, no transaction data):
    python -m spending_analyzer.pdf_parser statement.pdf
"""

import argparse
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

import pdfplumber

from .models import Statement, Transaction

LINE_TOLERANCE = 3  # points: words this close vertically are on the same line
MIN_BODY_FONT_SIZE = 8.5  # column headers are 8 pt, footer 7 pt, body 9 pt
CONTINUES_MARKER_X = 500  # lone word at bottom right ("Jatkuu") ends the page body

# Left edges of the columns
AMOUNT_X = 105
TEXT_X = 185
REFERENCE_X = 340
ACCOUNT_X = 465
FLAG_X = 570

DATE_RE = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})?")
IBAN_RE = re.compile(r"[A-Z]{2}\d{2}[A-Z0-9]{10,30}")
BIC_RE = re.compile(r"[A-Z]{6}[A-Z0-9]{2}(?:[A-Z0-9]{3})?")
MESSAGE_MARKER_RE = re.compile(r"\bviesti\b:?", re.IGNORECASE)
# Labelled fields printed after the name, e.g. "Alkuperäinen maksaja: OP-OSAKESALKKU A".
# They end the name; the label is kept as part of the message.
DETAIL_LABEL_RE = re.compile(
    r"\b(alkuperäinen (maksaja|saaja)|lopullinen saaja|maksajan viite)\b", re.IGNORECASE)


@dataclass
class Line:
    """One printed line split into columns."""
    date: str
    amount: str
    text: str
    reference: str
    account: str  # archive ID on a transaction's first line, IBAN/BIC on later lines


def parse_amount(text: str) -> Decimal:
    """'−1 234,56' → Decimal('-1234.56'). The statement uses U+2212 as minus."""
    cleaned = text.replace("−", "-").replace("+", "").replace(" ", "").replace(",", ".")
    return Decimal(cleaned)


def parse_date(text: str, year: int | None = None) -> date:
    match = DATE_RE.fullmatch(text)
    if not match:
        raise ValueError(f"Not a date: {text!r}")
    day, month, printed_year = match.groups()
    return date(int(printed_year or year), int(month), int(day))


def _group_lines(words):
    lines = []
    for word in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if lines and abs(lines[-1][0]["top"] - word["top"]) <= LINE_TOLERANCE:
            lines[-1].append(word)
        else:
            lines.append([word])
    return [sorted(line, key=lambda w: w["x0"]) for line in lines]


def _split_columns(words) -> Line:
    columns = {"date": [], "amount": [], "text": [], "reference": [], "account": []}
    for word in words:
        x = word["x0"]
        if x < AMOUNT_X:
            columns["date"].append(word["text"])
        elif x < TEXT_X:
            columns["amount"].append(word["text"])
        elif x < REFERENCE_X:
            columns["text"].append(word["text"])
        elif x < ACCOUNT_X:
            columns["reference"].append(word["text"])
        elif x < FLAG_X:
            columns["account"].append(word["text"])
    return Line(
        date=" ".join(columns["date"]),
        amount="".join(columns["amount"]),  # "+1" "234,56" → "+1234,56"
        text=" ".join(columns["text"]),
        reference=" ".join(columns["reference"]),
        account=" ".join(columns["account"]),
    )


def _body_lines(page) -> list[Line]:
    lines = _group_lines(page.extract_words(extra_attrs=["size"]))
    header_end = next(
        (i for i, line in enumerate(lines) if line[0]["text"] == "Arvopäivä"), None)
    if header_end is None:
        raise ValueError(f"Column header not found on page {page.page_number}")

    body = []
    for words in lines[header_end + 1:]:
        if words[0]["size"] < MIN_BODY_FONT_SIZE:
            break
        if len(words) == 1 and words[0]["x0"] > CONTINUES_MARKER_X:
            break
        body.append(_split_columns(words))
    return body


def _read_header(page) -> tuple[str, date, date]:
    """Account IBAN and statement period from the first page."""
    lines = _group_lines(page.extract_words())
    texts = [" ".join(w["text"] for w in line) for line in lines]
    iban = next(t.removeprefix("Tilinumero").replace(" ", "")
                for t in texts if t.startswith("Tilinumero"))
    period = texts[texts.index("Ajalta") + 1]  # e.g. "1.9. - 30.9.2026"
    start_text, end_text = (part.strip() for part in period.split(" - "))
    end = parse_date(end_text)
    start = parse_date(start_text, end.year)
    if start > end:  # period spans a year boundary
        start = start.replace(year=end.year - 1)
    return iban, start, end


def _build_transaction(block: list[Line]) -> Transaction:
    first = block[0]
    transaction = Transaction(
        booking_date=parse_date(first.date),
        amount=parse_amount(first.amount),
        type=first.text,
        reference=first.reference,
        archive_id=first.account,
    )
    counterparty, message = [], []
    in_message = False
    for line in block[1:]:
        text = " ".join(part for part in (line.text, line.reference) if part)
        # "Viesti:" usually starts its own line, but some transfers print it inline:
        # "VIPPS MOBILEPAY AS, VIESTI SEPA PIKASIIRTO"
        if not in_message:
            found = [m for m in (MESSAGE_MARKER_RE.search(text), DETAIL_LABEL_RE.search(text))
                     if m]
            if found:
                first = min(found, key=lambda m: m.start())
                in_message = True
                if name_part := text[:first.start()].strip(" ,"):
                    counterparty.append(name_part)
                is_viesti = first.re is MESSAGE_MARKER_RE
                text = text[first.end() if is_viesti else first.start():].strip()
        account = line.account.replace(" ", "")
        if IBAN_RE.fullmatch(account):
            transaction.counterparty_iban = account
        elif BIC_RE.fullmatch(account):
            transaction.counterparty_bic = account
        if text:
            (message if in_message else counterparty).append(text)
    transaction.counterparty = " ".join(counterparty).strip(" ,")
    transaction.message = "\n".join(message)
    return transaction


def parse_statement(path: Path) -> Statement:
    with pdfplumber.open(path) as pdf:
        iban, start, end = _read_header(pdf.pages[0])
        lines = [line for page in pdf.pages for line in _body_lines(page)]

    blocks: list[list[Line]] = []
    balances: list[Decimal] = []
    totals: dict[str, tuple[Decimal, int]] = {}
    for line in lines:
        if line.date and line.amount:
            blocks.append([line])
        elif line.amount:  # summary line: "Saldo", "Panot yhteensä", "Otot yhteensä"
            words = line.text.split()
            if words[0] == "Saldo":
                balances.append(parse_amount(line.amount))
            elif words[0] in ("Panot", "Otot"):
                totals[words[0]] = (parse_amount(line.amount), int(words[-2]))
        elif blocks:
            blocks[-1].append(line)

    if len(balances) < 2:
        raise ValueError("Opening or closing balance not found")
    deposits = totals.get("Panot", (None, None))
    withdrawals = totals.get("Otot", (None, None))
    return Statement(
        account_iban=iban,
        period_start=start,
        period_end=end,
        opening_balance=balances[0],
        closing_balance=balances[-1],
        transactions=[_build_transaction(block) for block in blocks],
        deposits_total=deposits[0],
        deposits_count=deposits[1],
        withdrawals_total=withdrawals[0],
        withdrawals_count=withdrawals[1],
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Parse a statement and validate the result.")
    parser.add_argument("pdf", type=Path)
    args = parser.parse_args()

    statement = parse_statement(args.pdf)
    deposits = sum(1 for t in statement.transactions if t.amount > 0)
    print(f"Period: {statement.period_start} – {statement.period_end}")
    print(f"Transactions: {len(statement.transactions)} "
          f"({deposits} deposits, {len(statement.transactions) - deposits} withdrawals)")
    print(f"Types: {sorted({t.type for t in statement.transactions})}")
    errors = statement.validation_errors()
    print("Validation: OK" if not errors else "Validation FAILED:\n  " + "\n  ".join(errors))


if __name__ == "__main__":
    main()
