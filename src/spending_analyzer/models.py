from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal


@dataclass
class Transaction:
    booking_date: date
    amount: Decimal  # negative = money out
    type: str  # as printed, e.g. "TILISIIRTO"
    counterparty: str = ""
    counterparty_iban: str = ""
    counterparty_bic: str = ""
    reference: str = ""
    archive_id: str = ""
    message: str = ""  # free text; never sent to an LLM


@dataclass
class Statement:
    account_iban: str
    period_start: date
    period_end: date
    opening_balance: Decimal
    closing_balance: Decimal
    transactions: list[Transaction] = field(default_factory=list)
    # Totals printed on the last page, used to validate parsing
    deposits_total: Decimal | None = None
    deposits_count: int | None = None
    withdrawals_total: Decimal | None = None
    withdrawals_count: int | None = None

    def validation_errors(self) -> list[str]:
        errors = []
        net = sum((t.amount for t in self.transactions), Decimal(0))
        if self.opening_balance + net != self.closing_balance:
            diff = self.closing_balance - self.opening_balance - net
            errors.append(f"Balance mismatch: transactions are off by {diff}")

        deposits = [t.amount for t in self.transactions if t.amount > 0]
        withdrawals = [t.amount for t in self.transactions if t.amount < 0]
        for name, amounts, count, total in (
            ("deposits", deposits, self.deposits_count, self.deposits_total),
            ("withdrawals", withdrawals, self.withdrawals_count, self.withdrawals_total),
        ):
            if count is not None and len(amounts) != count:
                errors.append(f"Found {len(amounts)} {name}, statement says {count}")
            if total is not None and sum(amounts, Decimal(0)) != total:
                errors.append(f"Sum of {name} does not match the statement total")

        outside = [t for t in self.transactions
                   if not self.period_start <= t.booking_date <= self.period_end]
        if outside:
            errors.append(f"{len(outside)} transactions dated outside the statement period")
        return errors
