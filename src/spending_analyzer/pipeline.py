"""The web app's steps, kept free of UI code so they can be tested directly.

Upload → parse and validate in memory → categorize (rules first, local LLM for the
rest) → store → review. Uploaded PDFs are never written to disk; only the parsed
transactions go into the local database.
"""

import io
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal

from .categorize import CONFIRMED_SOURCES, CategorizationReport, categorize
from .llm import LLMClient
from .models import Statement
from .pdf_parser import UnsupportedLayout, parse_statement
from .store import Store
from .synthetic import generate, render


@dataclass
class ParsedUpload:
    filename: str
    statement: Statement | None
    errors: list[str] = field(default_factory=list)  # read errors or failed validation

    @property
    def valid(self) -> bool:
        return self.statement is not None and not self.errors


def parse_upload(filename: str, data: bytes) -> ParsedUpload:
    """Parses an uploaded PDF from memory. A statement that reads but fails its own
    balance/count checks is returned with the errors, so the UI can show why."""
    try:
        statement = parse_statement(io.BytesIO(data))
    except UnsupportedLayout as error:
        return ParsedUpload(filename, None, [
            f"Not a supported statement ({error}). Only OP account statement PDFs can "
            "be read for now."])
    except Exception as error:  # not a PDF at all, or a damaged one
        return ParsedUpload(filename, None, [f"Could not read the statement: {error}"])
    return ParsedUpload(filename, statement, statement.validation_errors())


DEMO_MONTHS = ((2026, 7), (2026, 8), (2026, 9))


def demo_uploads() -> list[ParsedUpload]:
    """Made-up statements in the real layout, rendered and parsed exactly like an
    upload, so a demo runs the whole real pipeline without anyone's finances."""
    uploads = []
    for seed, (year, month) in enumerate(DEMO_MONTHS, start=101):
        statement, _ = generate(seed=seed, year=year, month=month)
        buffer = io.BytesIO()
        render(statement, buffer)
        uploads.append(parse_upload(f"demo_{year}-{month:02d}.pdf", buffer.getvalue()))
    return uploads


def is_imported(store: Store, statement: Statement) -> bool:
    return any(s["account_iban"] == statement.account_iban
               and s["period_start"] == statement.period_start.isoformat()
               and s["period_end"] == statement.period_end.isoformat()
               for s in store.statement_summaries())


def categorize_and_import(statement: Statement, store: Store, llm: LLMClient,
                          progress: Callable[[int, int], None] | None = None
                          ) -> CategorizationReport:
    report = categorize(statement.transactions, store, llm, progress=progress)
    store.import_statement(statement, report)
    return report


def confirmed_share(store: Store) -> float | None:
    """Share of all stored money moved whose category is confirmed; None when empty."""
    totals = store.source_totals()
    total = sum(totals.values(), Decimal(0))
    if not total:
        return None
    confirmed = sum((v for s, v in totals.items() if s in CONFIRMED_SOURCES), Decimal(0))
    return float(confirmed / total)
