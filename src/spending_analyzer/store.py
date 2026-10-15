"""SQLite-backed counterparty resolution store, replacing the old JSON rule book.

A counterparty carries the categorization knowledge: an alias (substring, exact name,
or IBAN) resolves a transaction's name to a counterparty, whose default_category is
either confirmed by the review or tentatively guessed by the LLM. See CLAUDE.md
"Counterparty layer". Per-transaction overrides (for mixed merchants) live in their own
table, keyed by archive ID, so they resolve on every categorize() run independent of
whether the transaction has been imported yet.
"""

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from .models import Statement, Transaction

if TYPE_CHECKING:
    from .categorize import CategorizationReport

PAYMENT_APPS: tuple[str, ...] = ("mobilepay", "vipps")
# Full field is just the app's own name, e.g. a transfer with no identifiable sender
PAYMENT_APP_FULL_NAMES = frozenset({"mobilepay", "vipps", "vipps mobilepay as"})
# Bucket names for spend without a known counterparty. The bank statement shows only
# the app for MobilePay/Vipps payments; who received the money is known only to the app.
PAYMENT_APP_BUCKET = "MobilePay/Vipps (recipient not shown on the statement)"
UNKNOWN_BUCKET = "(counterparty unknown)"

SCHEMA = """
CREATE TABLE IF NOT EXISTS statements (
    id INTEGER PRIMARY KEY, account_iban TEXT NOT NULL,
    period_start TEXT NOT NULL, period_end TEXT NOT NULL,
    opening_balance_cents INTEGER NOT NULL, closing_balance_cents INTEGER NOT NULL,
    deposits_total_cents INTEGER, deposits_count INTEGER,
    withdrawals_total_cents INTEGER, withdrawals_count INTEGER,
    imported_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_statements_identity
    ON statements(account_iban, period_start, period_end);

CREATE TABLE IF NOT EXISTS counterparties (
    id INTEGER PRIMARY KEY, display_name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN
        ('merchant','person','payment_app','employer','investment','unknown')),
    default_category TEXT,
    category_status TEXT NOT NULL DEFAULT 'tentative'
        CHECK (category_status IN ('tentative','confirmed')),
    is_mixed INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS counterparty_aliases (
    id INTEGER PRIMARY KEY, counterparty_id INTEGER NOT NULL REFERENCES counterparties(id),
    pattern TEXT NOT NULL, match_type TEXT NOT NULL CHECK (match_type IN
        ('substring','exact_name','iban')),
    UNIQUE (pattern, match_type)
);
CREATE INDEX IF NOT EXISTS idx_aliases_counterparty_id ON counterparty_aliases(counterparty_id);

CREATE TRIGGER IF NOT EXISTS no_iban_alias_for_payment_apps_or_persons
BEFORE INSERT ON counterparty_aliases
WHEN NEW.match_type = 'iban' AND (
    SELECT kind FROM counterparties WHERE id = NEW.counterparty_id
) IN ('payment_app', 'person')
BEGIN
    SELECT RAISE(ABORT, 'IBAN aliasing is not allowed for payment_app/person counterparties');
END;

-- Per-transaction category overrides (mixed merchants), keyed by archive ID so they
-- apply on every categorize() run regardless of import order.
CREATE TABLE IF NOT EXISTS overrides (
    archive_id TEXT PRIMARY KEY,
    category TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY, account_iban TEXT NOT NULL, archive_id TEXT NOT NULL,
    archive_id_synthetic INTEGER NOT NULL DEFAULT 0,
    booking_date TEXT NOT NULL, amount_cents INTEGER NOT NULL, type TEXT NOT NULL,
    counterparty_raw TEXT NOT NULL, counterparty_iban TEXT, counterparty_bic TEXT,
    reference TEXT, message TEXT,
    counterparty_id INTEGER REFERENCES counterparties(id),
    category TEXT, source TEXT,
    first_statement_id INTEGER REFERENCES statements(id),
    UNIQUE (account_iban, archive_id)
);
CREATE INDEX IF NOT EXISTS idx_transactions_archive_id ON transactions(archive_id);
"""


def normalize(name: str) -> str:
    return " ".join(name.casefold().split())


def strip_payment_app_prefix(name: str) -> str | None:
    """None when `name` isn't a payment-app row. Otherwise the real payee's name
    (e.g. "MobilePay Jari Sillanpää" -> "Jari Sillanpää"), or "" when the field is
    just the app's own name with no identifiable sender."""
    normalized = normalize(name)
    if normalized in PAYMENT_APP_FULL_NAMES:
        return ""
    for app in PAYMENT_APPS:
        if normalized.startswith(app):
            return name[len(app):].strip(" ,-")
    return None


def effective_name(t: Transaction) -> str:
    """Name used for counterparty matching: the payment-app prefix stripped when
    present and non-empty, else the raw counterparty text. Raw display text
    (counterparty_raw, labels, the audit log) is never altered anywhere else."""
    stripped = strip_payment_app_prefix(t.counterparty)
    return stripped if stripped else t.counterparty


def _row_to_transaction(row: sqlite3.Row) -> Transaction:
    return Transaction(
        booking_date=date.fromisoformat(row["booking_date"]),
        amount=Decimal(row["amount_cents"]).scaleb(-2), type=row["type"],
        counterparty=row["counterparty_raw"], counterparty_iban=row["counterparty_iban"] or "",
        counterparty_bic=row["counterparty_bic"] or "", reference=row["reference"] or "",
        archive_id=row["archive_id"], message=row["message"] or "")


@dataclass
class ResolvedCounterparty:
    id: int
    category: str | None
    status: str


class Store:
    def __init__(self, path: str | Path | None = None):
        if path is None:
            self.connection = sqlite3.connect(":memory:")
        else:
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            # The web UI reruns its script on different threads; one Store per browser
            # session is only ever used by one thread at a time
            self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.executescript(SCHEMA)

    def close(self) -> None:
        self.connection.close()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    # --- resolution ---

    def resolve_by_alias(self, name: str, iban: str = "") -> ResolvedCounterparty | None:
        normalized = normalize(name)
        row = self.connection.execute(
            """SELECT c.id, c.default_category, c.category_status
               FROM counterparty_aliases a JOIN counterparties c ON c.id = a.counterparty_id
               WHERE a.match_type = 'substring' AND instr(?, a.pattern) > 0
               ORDER BY length(a.pattern) DESC LIMIT 1""",
            (normalized,)).fetchone()
        if row is None:
            row = self.connection.execute(
                """SELECT c.id, c.default_category, c.category_status
                   FROM counterparty_aliases a JOIN counterparties c ON c.id = a.counterparty_id
                   WHERE a.match_type = 'exact_name' AND a.pattern = ?""",
                (normalized,)).fetchone()
        if row is None and iban:
            row = self.connection.execute(
                """SELECT c.id, c.default_category, c.category_status
                   FROM counterparty_aliases a JOIN counterparties c ON c.id = a.counterparty_id
                   WHERE a.match_type = 'iban' AND a.pattern = ?""",
                (iban,)).fetchone()
        if row is None:
            return None
        return ResolvedCounterparty(row["id"], row["default_category"], row["category_status"])

    def category_for(self, name: str) -> str | None:
        match = self.resolve_by_alias(name)
        return match.category if match else None

    def get_override_category(self, archive_id: str) -> str | None:
        row = self.connection.execute(
            "SELECT category FROM overrides WHERE archive_id = ?", (archive_id,)).fetchone()
        return row["category"] if row else None

    # --- counterparty creation / updates ---

    def _alias_counterparty_id(self, name: str) -> int | None:
        row = self.connection.execute(
            "SELECT counterparty_id FROM counterparty_aliases WHERE match_type='substring' AND pattern=?",
            (normalize(name),)).fetchone()
        return row["counterparty_id"] if row else None

    def create_tentative_counterparty(self, name: str, kind: str,
                                       default_category: str | None) -> int:
        """Find-or-create by normalize(name) as a substring alias. A repeat call for
        the same name is a no-op, returning the existing id unchanged."""
        existing = self._alias_counterparty_id(name)
        if existing is not None:
            return existing
        cursor = self.connection.execute(
            "INSERT INTO counterparties (display_name, kind, default_category, "
            "category_status, is_mixed, created_at) VALUES (?, ?, ?, 'tentative', 0, ?)",
            (name, kind, default_category, self._now()))
        counterparty_id = cursor.lastrowid
        self.connection.execute(
            "INSERT INTO counterparty_aliases (counterparty_id, pattern, match_type) "
            "VALUES (?, ?, 'substring')", (counterparty_id, normalize(name)))
        self.connection.commit()
        return counterparty_id

    def _get_or_create_id(self, name: str) -> int:
        existing = self._alias_counterparty_id(name)
        if existing is not None:
            return existing
        return self.create_tentative_counterparty(name, "unknown", None)

    def set_tentative_category(self, name: str, category: str) -> None:
        counterparty_id = self._get_or_create_id(name)
        self.connection.execute(
            "UPDATE counterparties SET default_category = ? WHERE id = ?",
            (category, counterparty_id))
        self.connection.commit()

    def confirm_counterparty(self, name: str, category: str) -> None:
        counterparty_id = self._get_or_create_id(name)
        self.connection.execute(
            "UPDATE counterparties SET default_category = ?, category_status = 'confirmed' "
            "WHERE id = ?", (category, counterparty_id))
        self.connection.commit()

    def flag_mixed(self, name: str) -> None:
        counterparty_id = self._get_or_create_id(name)
        self.connection.execute(
            "UPDATE counterparties SET is_mixed = 1, default_category = NULL WHERE id = ?",
            (counterparty_id,))
        self.connection.commit()

    def is_mixed(self, name: str) -> bool:
        row = self.connection.execute(
            """SELECT c.is_mixed FROM counterparty_aliases a JOIN counterparties c
               ON c.id = a.counterparty_id
               WHERE a.match_type = 'substring' AND a.pattern = ?""",
            (normalize(name),)).fetchone()
        return bool(row and row["is_mixed"])

    def mixed_names(self) -> set[str]:
        rows = self.connection.execute(
            """SELECT a.pattern FROM counterparty_aliases a JOIN counterparties c
               ON c.id = a.counterparty_id
               WHERE c.is_mixed = 1 AND a.match_type = 'substring'""").fetchall()
        return {row["pattern"] for row in rows}

    def confirmed_names(self) -> set[str]:
        rows = self.connection.execute(
            """SELECT a.pattern FROM counterparty_aliases a JOIN counterparties c
               ON c.id = a.counterparty_id
               WHERE c.category_status = 'confirmed' AND a.match_type = 'substring'""").fetchall()
        return {row["pattern"] for row in rows}

    def confirmed_examples(self, limit: int) -> list[tuple[str, str]]:
        """(pattern, category) pairs for confirmed counterparties, for the LLM prompt."""
        rows = self.connection.execute(
            """SELECT a.pattern, c.default_category FROM counterparty_aliases a
               JOIN counterparties c ON c.id = a.counterparty_id
               WHERE c.category_status = 'confirmed' AND a.match_type = 'substring'
                 AND c.default_category IS NOT NULL
               LIMIT ?""", (limit,)).fetchall()
        return [(row["pattern"], row["default_category"]) for row in rows]

    # --- per-transaction overrides (mixed merchants) ---

    def set_transaction_override(self, archive_id: str, category: str) -> None:
        self.connection.execute(
            "INSERT INTO overrides (archive_id, category) VALUES (?, ?) "
            "ON CONFLICT (archive_id) DO UPDATE SET category = excluded.category",
            (archive_id, category))
        self.connection.commit()

    # --- persistence ---

    def import_statement(self, statement: Statement, report: "CategorizationReport",
                          synthetic: bool = False) -> int:
        """Upsert the statement row and every transaction row (report.items must be
        positionally aligned with statement.transactions, as categorize() guarantees).
        Parse-derived columns always refresh to the latest parsed values; category and
        source simply reflect report.items, since an override (if any) was already
        applied by categorize()'s decision chain before report.items was built."""
        deposits_total_cents = (int(statement.deposits_total * 100)
                                 if statement.deposits_total is not None else None)
        withdrawals_total_cents = (int(statement.withdrawals_total * 100)
                                    if statement.withdrawals_total is not None else None)
        period_start = statement.period_start.isoformat()
        period_end = statement.period_end.isoformat()

        existing = self.connection.execute(
            "SELECT id FROM statements WHERE account_iban = ? AND period_start = ? "
            "AND period_end = ?", (statement.account_iban, period_start, period_end)).fetchone()
        if existing:
            statement_id = existing["id"]
            self.connection.execute(
                "UPDATE statements SET opening_balance_cents = ?, closing_balance_cents = ?, "
                "deposits_total_cents = ?, deposits_count = ?, withdrawals_total_cents = ?, "
                "withdrawals_count = ? WHERE id = ?",
                (int(statement.opening_balance * 100), int(statement.closing_balance * 100),
                 deposits_total_cents, statement.deposits_count, withdrawals_total_cents,
                 statement.withdrawals_count, statement_id))
        else:
            cursor = self.connection.execute(
                "INSERT INTO statements (account_iban, period_start, period_end, "
                "opening_balance_cents, closing_balance_cents, deposits_total_cents, "
                "deposits_count, withdrawals_total_cents, withdrawals_count, imported_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (statement.account_iban, period_start, period_end,
                 int(statement.opening_balance * 100), int(statement.closing_balance * 100),
                 deposits_total_cents, statement.deposits_count, withdrawals_total_cents,
                 statement.withdrawals_count, self._now()))
            statement_id = cursor.lastrowid

        seen_archive_ids: set[str] = set()
        for item in report.items:
            t = item.transaction
            if t.archive_id in seen_archive_ids:
                raise ValueError(f"Duplicate archive_id {t.archive_id!r} within one statement")
            seen_archive_ids.add(t.archive_id)
            match = self.resolve_by_alias(effective_name(t), t.counterparty_iban)
            self.connection.execute(
                """INSERT INTO transactions (
                       account_iban, archive_id, archive_id_synthetic, booking_date,
                       amount_cents, type, counterparty_raw, counterparty_iban,
                       counterparty_bic, reference, message, counterparty_id, category,
                       source, first_statement_id
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT (account_iban, archive_id) DO UPDATE SET
                       booking_date = excluded.booking_date, amount_cents = excluded.amount_cents,
                       type = excluded.type, counterparty_raw = excluded.counterparty_raw,
                       counterparty_iban = excluded.counterparty_iban,
                       counterparty_bic = excluded.counterparty_bic,
                       reference = excluded.reference, message = excluded.message,
                       counterparty_id = excluded.counterparty_id,
                       category = excluded.category, source = excluded.source""",
                (statement.account_iban, t.archive_id, int(synthetic), t.booking_date.isoformat(),
                 int(t.amount * 100), t.type, t.counterparty, t.counterparty_iban,
                 t.counterparty_bic, t.reference, t.message,
                 match.id if match else None, item.category, item.source, statement_id))
        self.connection.commit()
        return statement_id

    def refresh_categories(self) -> int:
        """Re-derive stored categories after a review, without re-running
        categorization or any LLM: per-transaction overrides first, then confirmed
        counterparties — the same precedence as categorize._decide_locally. Without
        this, rows imported before the review keep their pre-review categories and
        every query (chatbot, UI) reports stale numbers. Returns rows updated."""
        overridden = self.connection.execute(
            "UPDATE transactions SET category = o.category, source = 'override' "
            "FROM overrides o WHERE o.archive_id = transactions.archive_id "
            "AND (transactions.category IS NOT o.category "
            "     OR transactions.source IS NOT 'override')").rowcount
        confirmed = self.connection.execute(
            "UPDATE transactions SET category = c.default_category, "
            "source = 'counterparty_confirmed' FROM counterparties c "
            "WHERE c.id = transactions.counterparty_id AND c.category_status = 'confirmed' "
            "AND c.default_category IS NOT NULL AND c.is_mixed = 0 "
            "AND transactions.archive_id NOT IN (SELECT archive_id FROM overrides) "
            "AND (transactions.category IS NOT c.default_category "
            "     OR transactions.source IS NOT 'counterparty_confirmed')").rowcount
        self.connection.commit()
        return overridden + confirmed

    # --- queries for the review and the UI ---

    def transactions_with_source(self, sources: tuple[str, ...]
                                  ) -> list[tuple[Transaction, str, str]]:
        """Stored (transaction, category, source) rows decided by one of `sources`,
        oldest first — e.g. the review queue, which therefore survives restarts."""
        marks = ",".join("?" * len(sources))
        rows = self.connection.execute(
            f"SELECT * FROM transactions WHERE source IN ({marks}) "
            "ORDER BY booking_date, id", sources).fetchall()
        return [(_row_to_transaction(row), row["category"], row["source"]) for row in rows]

    def statement_summaries(self) -> list[dict]:
        rows = self.connection.execute(
            "SELECT s.id, s.account_iban, s.period_start, s.period_end, s.imported_at, "
            "COUNT(t.id) AS transactions FROM statements s "
            "LEFT JOIN transactions t ON t.first_statement_id = s.id "
            "GROUP BY s.id ORDER BY s.period_start").fetchall()
        return [dict(row) for row in rows]

    def source_totals(self) -> dict[str, Decimal]:
        """Absolute money moved per decision source, for the confirmed-share meter."""
        rows = self.connection.execute(
            "SELECT source, SUM(ABS(amount_cents)) AS s FROM transactions "
            "GROUP BY source").fetchall()
        return {row["source"]: Decimal(row["s"]).scaleb(-2) for row in rows}

    def counts(self) -> dict[str, int]:
        return {table: self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("statements", "transactions", "counterparties", "overrides")}

    def date_bounds(self) -> tuple[date, date] | None:
        row = self.connection.execute(
            "SELECT MIN(booking_date) AS first, MAX(booking_date) AS last "
            "FROM transactions").fetchone()
        if row["first"] is None:
            return None
        return date.fromisoformat(row["first"]), date.fromisoformat(row["last"])

    def monthly_by_category(self, start: str, end: str) -> dict[tuple[str, str], Decimal]:
        """Signed total per ("YYYY-MM", category) in [start, end]."""
        rows = self.connection.execute(
            "SELECT substr(booking_date, 1, 7) AS ym, category, SUM(amount_cents) AS s "
            "FROM transactions WHERE booking_date BETWEEN ? AND ? AND category IS NOT NULL "
            "GROUP BY ym, category", (start, end)).fetchall()
        return {(row["ym"], row["category"]): Decimal(row["s"]).scaleb(-2) for row in rows}

    def transactions_between(self, start: str, end: str,
                             category: str | None = None) -> list[dict]:
        """Rows for a drill-down table, newest first, with the counterparty's name."""
        rows = self.connection.execute(
            "SELECT t.booking_date, t.amount_cents, t.counterparty_raw, t.category, t.source, "
            "c.display_name FROM transactions t "
            "LEFT JOIN counterparties c ON c.id = t.counterparty_id "
            "WHERE t.booking_date BETWEEN ? AND ? AND (? IS NULL OR t.category = ?) "
            "ORDER BY t.booking_date DESC, t.id DESC",
            (start, end, category, category)).fetchall()
        return [{"date": row["booking_date"], "amount": Decimal(row["amount_cents"]).scaleb(-2),
                 "counterparty": row["display_name"] or row["counterparty_raw"],
                 "category": row["category"], "source": row["source"]} for row in rows]

    def list_transactions(self, start: str, end: str, category: str | None = None,
                          direction: str | None = None, counterparty: str | None = None,
                          limit: int = 20) -> tuple[list[dict], int, Decimal]:
        """Individual transactions, largest amount first, plus the count and signed
        total of *all* matches (so a summary stays right when the list is cut at
        `limit`). direction "out" = money paid/sent, "in" = received. `counterparty`
        matches the resolved name or the raw statement text, case-insensitively."""
        rows = self.connection.execute(
            "SELECT t.booking_date, t.amount_cents, t.counterparty_raw, t.category, "
            "t.source, c.display_name FROM transactions t "
            "LEFT JOIN counterparties c ON c.id = t.counterparty_id "
            "WHERE t.booking_date BETWEEN ? AND ? AND (? IS NULL OR t.category = ?) "
            "AND (? IS NULL OR (? = 'out' AND t.amount_cents < 0) "
            "               OR (? = 'in' AND t.amount_cents > 0))",
            (start, end, category, category, direction, direction, direction)).fetchall()
        wanted = normalize(counterparty) if counterparty else None
        matches = []
        for row in rows:
            name = (row["display_name"] or
                    (PAYMENT_APP_BUCKET if row["source"] == "payment_app"
                     else row["counterparty_raw"]))
            if wanted and wanted not in normalize(name) \
                    and wanted not in normalize(row["counterparty_raw"]):
                continue
            matches.append({"date": row["booking_date"], "counterparty": name,
                            "amount": Decimal(row["amount_cents"]).scaleb(-2),
                            "category": row["category"]})
        matches.sort(key=lambda m: (-abs(m["amount"]), m["date"]))
        total = sum((m["amount"] for m in matches), Decimal(0))
        return matches[:limit], len(matches), total

    # --- aggregation (chatbot) ---

    def spending_by_category(self, start: str, end: str) -> dict[str, Decimal]:
        """Signed total per category in [start, end]. Category-agnostic: callers
        decide which categories count as spending/income vs. moved between the
        user's own assets (Transfers/Investments)."""
        rows = self.connection.execute(
            "SELECT category, SUM(amount_cents) AS s FROM transactions "
            "WHERE booking_date BETWEEN ? AND ? AND category IS NOT NULL "
            "GROUP BY category", (start, end)).fetchall()
        return {row["category"]: Decimal(row["s"]).scaleb(-2) for row in rows}

    def category_total(self, category: str, start: str, end: str) -> Decimal:
        row = self.connection.execute(
            "SELECT SUM(amount_cents) AS s FROM transactions "
            "WHERE category = ? AND booking_date BETWEEN ? AND ?",
            (category, start, end)).fetchone()
        return Decimal(row["s"]).scaleb(-2) if row["s"] is not None else Decimal("0")

    def top_counterparties(self, start: str, end: str, category: str | None = None,
                            limit: int = 10) -> list[tuple[str, Decimal]]:
        """(name, signed total) pairs, most spent first; see top_counterparty_stats."""
        return [(r["name"], r["total"])
                for r in self.top_counterparty_stats(start, end, category, limit)]

    def top_counterparty_stats(self, start: str, end: str, category: str | None = None,
                               limit: int = 10, rank_by: str = "amount") -> list[dict]:
        """Spending only (amount_cents < 0), grouped by counterparty, with the signed
        total and the number of transactions; ranked by money spent or, with
        rank_by="count", by how often (a "favorite store"). Transactions without a
        resolved counterparty are bucketed rather than dropped, so spend is never
        silently hidden: MobilePay/Vipps payments whose statement row names only the
        app get their own, explanatory bucket."""
        order = "n DESC, s ASC" if rank_by == "count" else "s ASC, n DESC"
        rows = self.connection.execute(
            "SELECT CASE WHEN c.display_name IS NOT NULL THEN c.display_name "
            "            WHEN t.source = 'payment_app' THEN ? ELSE ? END AS name, "
            "SUM(t.amount_cents) AS s, COUNT(*) AS n "
            "FROM transactions t LEFT JOIN counterparties c ON c.id = t.counterparty_id "
            "WHERE t.booking_date BETWEEN ? AND ? AND t.amount_cents < 0 "
            "AND (? IS NULL OR t.category = ?) "
            f"GROUP BY name ORDER BY {order} LIMIT ?",
            (PAYMENT_APP_BUCKET, UNKNOWN_BUCKET, start, end, category, category,
             limit)).fetchall()
        return [{"name": row["name"], "total": Decimal(row["s"]).scaleb(-2),
                 "count": row["n"]} for row in rows]

    def monthly_totals(self, start: str, end: str,
                        category: str | None = None) -> dict[str, Decimal]:
        """Signed total per "YYYY-MM" in [start, end]; category=None sums every
        category (net cash flow). A caller wanting a spending trend nets out
        Transfers/Investments itself — this method stays free of that rule."""
        rows = self.connection.execute(
            "SELECT substr(booking_date, 1, 7) AS ym, SUM(amount_cents) AS s "
            "FROM transactions WHERE booking_date BETWEEN ? AND ? "
            "AND (? IS NULL OR category = ?) GROUP BY ym ORDER BY ym",
            (start, end, category, category)).fetchall()
        return {row["ym"]: Decimal(row["s"]).scaleb(-2) for row in rows}

    def counterparty_total(self, name: str, start: str,
                            end: str) -> tuple[str, Decimal] | None:
        """Resolves `name` against counterparties.display_name (exact normalize()
        match, falling back to substring) — not resolve_by_alias, which matches raw
        transaction text during import, not a conversationally-named counterparty.
        Returns (actual display name, signed total), or None if nothing matches.
        An ambiguous substring match silently takes the first row, which is
        acceptable at personal-finance scale (a handful of counterparties)."""
        normalized = normalize(name)
        rows = self.connection.execute("SELECT id, display_name FROM counterparties").fetchall()
        candidates = [r for r in rows if normalize(r["display_name"]) == normalized]
        if not candidates:
            candidates = [r for r in rows if normalized in normalize(r["display_name"])]
        if not candidates:
            return None
        counterparty_id, display_name = candidates[0]["id"], candidates[0]["display_name"]
        row = self.connection.execute(
            "SELECT SUM(amount_cents) AS s FROM transactions "
            "WHERE counterparty_id = ? AND booking_date BETWEEN ? AND ?",
            (counterparty_id, start, end)).fetchone()
        total = Decimal(row["s"]).scaleb(-2) if row["s"] is not None else Decimal("0")
        return display_name, total
