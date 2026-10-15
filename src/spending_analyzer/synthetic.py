"""Generate synthetic bank statement PDFs that mimic the real layout.

The layout follows docs/statement-layout.md. Every transaction's true category is
returned alongside the statement, so the data doubles as ground truth for parser
tests and categorization evaluation. All names and numbers are made up.

Usage:
    python -m spending_analyzer.synthetic data/sample.pdf --seed 1 --month 2026-09
"""

import argparse
import calendar
import random
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import BinaryIO

from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

from .models import Statement, Transaction

FONT = "Vera"  # bundled with reportlab; unlike Helvetica it has the U+2212 minus glyph
pdfmetrics.registerFont(TTFont(FONT, "Vera.ttf"))

PAGE_HEIGHT = A4[1]
LINE_STEP = 11.5
FIRST_PAGE_BODY_TOP = 365
OTHER_PAGES_BODY_TOP = 175
BODY_BOTTOM = 725

ACCOUNT_IBAN = "FI2112345600000785"
ACCOUNT_BIC = "OKOYFIHH"
HOLDER = "MATTI MEIKÄLÄINEN"

# (name as printed on the statement, category, min amount, max amount)
CARD_MERCHANTS = [
    ("K-market Kamppi Helsinki", "Groceries", 3, 60),
    ("S-market Sörnäinen", "Groceries", 5, 90),
    ("LIDL HELSINKI KALASATAMA", "Groceries", 5, 80),
    ("Alepa Hakaniemi", "Groceries", 2, 40),
    ("Prisma Itakeskus", "Groceries", 20, 120),
    ("R-kioski Rautatientori", "Groceries", 2, 12),
    ("WOLT OY HELSINKI", "Restaurants", 15, 45),
    ("FOODORA FI", "Restaurants", 15, 40),
    ("Hesburger Kamppi", "Restaurants", 8, 18),
    ("RAVINTOLA SAVOY", "Restaurants", 40, 120),
    ("Fazer Cafe Kluuvi", "Restaurants", 4, 15),
    ("HSL Mobiili", "Transport", 3, 4),
    ("VR MOBIILI", "Transport", 8, 60),
    ("Neste Helsinki Lauttasaari", "Transport", 30, 80),
    ("P_PAYPAL_Steamgames STEAM", "Entertainment", 5, 60),
    ("Finnkino Tennispalatsi", "Entertainment", 12, 30),
    ("Clas Ohlson Kamppi", "Shopping", 5, 80),
    ("Tokmanni Helsinki", "Shopping", 5, 60),
    ("ZALANDO SE", "Shopping", 30, 120),
    ("Yliopiston Apteekki", "Health", 5, 40),
    # Harder ones: chains without an obvious keyword, abbreviations, odd formats
    ("Sokos Herkku Helsinki", "Groceries", 10, 70),
    ("ST1 Kamppi", "Transport", 30, 70),
    ("UNICAFE PORTHANIA", "Restaurants", 3, 9),
    ("Lounasravintola Factory", "Restaurants", 11, 14),
    ("Verkkokauppa.com Jatkasaari", "Shopping", 15, 200),
    ("Motonet Konala", "Shopping", 10, 90),
    ("Terveystalo Kamppi", "Health", 40, 120),
    ("LIPPUPISTE OY", "Entertainment", 25, 90),
]
# Merchants selling different kinds of things: (name, [(category, min, max), ...])
MIXED_MERCHANTS = [
    ("NESTE HKI MALMI", [("Transport", 35, 85), ("Restaurants", 4, 14)]),
    ("ABC Kehä III Vantaa", [("Transport", 35, 85), ("Restaurants", 8, 18)]),
]
SUBSCRIPTIONS = [("Spotify P2F4A1B9C", "Subscriptions", "11.99"),
                 ("NETFLIX.COM", "Subscriptions", "13.99"),
                 ("Google *YouTubePremium", "Subscriptions", "13.99"),
                 ("APPLE.COM/BILL", "Subscriptions", "2.99")]
# (payee, category, min, max, day of month)
BILLS = [("Kiinteistö Oy Esimerkkitalo", "Housing", 690, 690, 1),
         ("Helen Oy", "Utilities", 25, 70, 12),
         ("Elisa Oyj", "Utilities", 25, 25, 20)]
EMPLOYER = "Esimerkki Oy"
VIPPS = "VIPPS MOBILEPAY AS"
FUND_COMPANY = "OP-Rahastoyhtiö Oy"
PEOPLE = ["Liisa Virtanen", "Juha Korhonen", "Anna Nieminen", "Mikko Mäkinen",
          "Sanna Hämäläinen", "Antti Laine"]
PERSON_MESSAGES = ["Kiitos lounaasta", "Konserttiliput", "Osuus vuokrasta",
                   "Synttärilahja", "Mökkireissu"]
BICS = ["NDEAFIHH", "HELSFIHH", "DABAFIHH", "OKOYFIHH"]


def _digits(rng: random.Random, n: int) -> str:
    return "".join(rng.choice("0123456789") for _ in range(n))


def _letters(rng: random.Random, n: int) -> str:
    return "".join(rng.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ") for _ in range(n))


def _iban(rng: random.Random) -> str:
    return "FI" + _digits(rng, 16)


def _amount(rng: random.Random, low: float, high: float) -> Decimal:
    return Decimal(rng.randint(int(low * 100), int(high * 100))) / 100


def generate(seed: int = 0, year: int = 2026, month: int = 9,
             card_payments: int = 45) -> tuple[Statement, list[str]]:
    """Return a statement and the true category of each transaction."""
    rng = random.Random(seed)
    start = date(year, month, 1)
    end = date(year, month, calendar.monthrange(year, month)[1])

    def day() -> date:
        return start + timedelta(days=rng.randint(0, (end - start).days))

    def card(when, name, amount):
        return Transaction(
            booking_date=when, amount=amount, type="PKORTTIMAKSU", counterparty=name,
            reference=_digits(rng, 6),
            archive_id=_digits(rng, 9) + _letters(rng, 4) + _digits(rng, 7),
            message=(f"{_digits(rng, 6)}******{_digits(rng, 4)} OSTOPVM {when:%d%m%y}\n"
                     f"OS NRO {_digits(rng, 23)}\nKORTTITILI {_digits(rng, 3)}"))

    def transfer(when, type_, name, amount, message="", reference=""):
        return Transaction(
            booking_date=when, amount=amount, type=type_, counterparty=name,
            counterparty_iban=_iban(rng), counterparty_bic=rng.choice(BICS) + "XXX",
            reference=reference, archive_id=_digits(rng, 20), message=message)

    rows: list[tuple[Transaction, str]] = []
    for _ in range(card_payments):
        if rng.random() < 0.15:  # the true category depends on what was bought
            name, purchases = rng.choice(MIXED_MERCHANTS)
            category, low, high = rng.choice(purchases)
        else:
            name, category, low, high = rng.choice(CARD_MERCHANTS)
        rows.append((card(day(), name, -_amount(rng, low, high)), category))
    for name, category, price in SUBSCRIPTIONS:
        rows.append((card(day(), name, -Decimal(price)), category))
    refund_name, refund_category, low, high = rng.choice(CARD_MERCHANTS)
    rows.append((card(day(), refund_name, _amount(rng, low, high)), refund_category))

    for name, category, low, high, day_of_month in BILLS:
        when = start.replace(day=day_of_month)
        rows.append((transfer(when, "E-LASKU", name, -_amount(rng, low, high),
                              reference=_digits(rng, 10)), category))
    rows.append((transfer(start.replace(day=min(15, end.day)), "PALKKA", EMPLOYER,
                          _amount(rng, 2100, 2900), message=f"Palkka {month}/{year}"),
                 "Income"))
    for _ in range(rng.randint(3, 6)):
        sign = rng.choice([-1, -1, 1])
        if rng.random() < 0.5:  # older layout
            payment = transfer(day(), "TILISIIRTO", "MobilePay", sign * _amount(rng, 5, 60),
                               message=f"mp-{_digits(rng, 11)}")
        else:  # since the rebrand; rendered with the message marker inline, see below
            payment = transfer(day(), "TILISIIRTO", VIPPS, sign * _amount(rng, 5, 60),
                               message=f"SEPA PIKASIIRTO\nMaksajan viite: pp-{_digits(rng, 7)}"
                                       f"\n{_digits(rng, 23)}")
            payment.counterparty_bic = ""
        rows.append((payment, "Transfers"))
    # Monthly fund saving, and now and then a sale paid out from the stock portfolio
    rows.append((transfer(start.replace(day=min(5, end.day)), "TILISIIRTO", FUND_COMPANY,
                          -Decimal(rng.choice([25, 50, 100])), reference=_digits(rng, 10)),
                 "Investments"))
    if rng.random() < 0.5:
        sale = transfer(day(), "TILISIIRTO", FUND_COMPANY, _amount(rng, 200, 2500),
                        message="Alkuperäinen maksaja: OP-OSAKESALKKU A")
        rows.append((sale, "Investments"))
    for _ in range(rng.randint(1, 3)):
        sign = rng.choice([-1, 1])
        rows.append((transfer(day(), "TILISIIRTO", rng.choice(PEOPLE),
                              sign * _amount(rng, 10, 150),
                              message=rng.choice(PERSON_MESSAGES)), "Transfers"))

    rows.sort(key=lambda row: row[0].booking_date)
    transactions = [t for t, _ in rows]
    opening = _amount(rng, 300, 3000)
    deposits = [t.amount for t in transactions if t.amount > 0]
    withdrawals = [t.amount for t in transactions if t.amount < 0]
    statement = Statement(
        account_iban=ACCOUNT_IBAN, period_start=start, period_end=end,
        opening_balance=opening, closing_balance=opening + sum(deposits) + sum(withdrawals),
        transactions=transactions,
        deposits_total=sum(deposits, Decimal(0)), deposits_count=len(deposits),
        withdrawals_total=sum(withdrawals, Decimal(0)), withdrawals_count=len(withdrawals))
    return statement, [category for _, category in rows]


# --- Rendering -------------------------------------------------------------

def _format_amount(amount: Decimal, signed: bool = True) -> str:
    """Decimal('-1234.5') → '−1 234,50' (U+2212 minus, space as thousands separator)."""
    digits = f"{abs(amount):,.2f}".replace(",", " ").replace(".", ",")
    if amount < 0:
        return "−" + digits
    return ("+" + digits) if signed else digits


def _format_date(d: date) -> str:
    return f"{d.day}.{d.month}.{d.year}"


def _spaced(account: str) -> str:
    return " ".join(account[i:i + 4] for i in range(0, len(account), 4))


# A row is a list of cells: (x, text, right-aligned?). Gap is the space above the row.
Row = tuple[float, list[tuple[float, str, bool]]]


def _transaction_rows(t: Transaction) -> list[Row]:
    first = [(58, _format_date(t.booking_date), False), (185, _format_amount(t.amount), True),
             (190, t.type, False), (469, t.archive_id, False), (572, "A", False)]
    if t.reference:
        first.append((343, t.reference, False))
    rows: list[Row] = [(LINE_STEP, first)]
    if t.counterparty == VIPPS:  # "VIPPS MOBILEPAY AS, VIESTI SEPA PIKASIIRTO"
        first_message, *rest = t.message.splitlines()
        second = [(190, f"{t.counterparty}, VIESTI {first_message}", False),
                  (469, _spaced(t.counterparty_iban), False)]
        return rows + [(LINE_STEP, second)] + [(LINE_STEP, [(190, line, False)])
                                               for line in rest]
    second = [(190, t.counterparty, False)]
    if t.counterparty_iban:
        second.append((469, _spaced(t.counterparty_iban), False))
    rows.append((LINE_STEP, second))
    if t.message.startswith("Alkuperäinen"):  # labelled field instead of a "Viesti:" block
        lines = t.message.splitlines()
        third = [(190, lines[0], False)]
        if t.counterparty_bic:
            third.append((469, t.counterparty_bic, False))
        return rows + [(LINE_STEP, third)] + [(LINE_STEP, [(190, line, False)])
                                              for line in lines[1:]]
    if t.message or t.counterparty_bic:
        third = [(190, "Viesti:", False)]
        if t.counterparty_bic:
            third.append((469, t.counterparty_bic, False))
        rows.append((LINE_STEP, third))
        rows += [(LINE_STEP, [(190, line, False)]) for line in t.message.splitlines()]
    return rows


def _body_rows(s: Statement) -> list[Row]:
    opening_date = s.period_start - timedelta(days=1)
    rows: list[Row] = [(0, [(185, _format_amount(s.opening_balance), True),
                            (190, f"Saldo {_format_date(opening_date)}", False)])]
    for i, t in enumerate(s.transactions):
        transaction_rows = _transaction_rows(t)
        if i == 0:  # extra space after the opening balance
            transaction_rows[0] = (17.5, transaction_rows[0][1])
        rows += transaction_rows
    rows += [
        (16.5, [(185, _format_amount(s.deposits_total, signed=False), True),
                (190, f"Panot yhteensä {s.deposits_count} kpl", False)]),
        (12, [(185, _format_amount(s.withdrawals_total), True),
              (190, f"Otot yhteensä {s.withdrawals_count} kpl", False)]),
        (12, [(185, _format_amount(s.closing_balance), True),
              (190, f"Saldo {_format_date(s.period_end)}", False)]),
    ]
    return rows


def _paginate(rows: list[Row]) -> list[list[tuple[float, list]]]:
    pages, page = [], []
    top = FIRST_PAGE_BODY_TOP
    for gap, cells in rows:
        if page:
            top += gap
        if top > BODY_BOTTOM:  # page break, possibly in the middle of a transaction
            pages.append(page)
            page, top = [], OTHER_PAGES_BODY_TOP
        page.append((top, cells))
    pages.append(page)
    return pages


def _draw(c: canvas.Canvas, x: float, top: float, text: str, size: float = 9,
          right: bool = False, squeeze: bool = False) -> None:
    baseline = PAGE_HEIGHT - top - size * 0.76
    if right:
        x -= pdfmetrics.stringWidth(text, FONT, size)
    obj = c.beginText(x, baseline)
    obj.setFont(FONT, size)
    if squeeze:  # Vera is wide; keep 20-char archive IDs clear of the flag column
        obj.setHorizScale(75)
    obj.textOut(text)
    c.drawText(obj)


def _draw_page_header(c, s: Statement, number: int, total: int) -> float:
    _draw(c, 320, 30.8, "Tiliote", 10)
    _draw(c, 514, 30.8, f"{number} ({total})", 10)
    _draw(c, 320, 43.4, "Käyttötili", 10)
    _draw(c, 320, 56.1, "Ajalta", 7)
    _draw(c, 320, 65.9, f"{s.period_start.day}.{s.period_start.month}. - "
                        f"{_format_date(s.period_end)}", 10)
    if number == 1:
        _draw(c, 60, 114.9, HOLDER, 10)
        account_top = 270.5
    else:
        account_top = 114.6
    _draw(c, 60, account_top, "Tilinumero", 9)
    _draw(c, 186, account_top, _spaced(s.account_iban), 9)
    _draw(c, 60, account_top + 11.9, "BIC", 9)
    _draw(c, 186, account_top + 11.9, ACCOUNT_BIC, 9)
    header_top = 330.0 if number == 1 else 143.3
    for offset, cells in ((0, [(59, "Kirjaus-"), (111, "Määrä,"), (190, "Selitys"),
                               (343, "Viite"), (470, "Arkistointitunnus")]),
                          (10, [(59, "päivä"), (111, "euroa"), (190, "Maksunsaaja/maksaja"),
                                (470, "Saajan tilinumero")]),
                          (20, [(59, "Arvopäivä"), (190, "Viesti"),
                                (470, "Saajan pankin BIC")])):
        for x, text in cells:
            _draw(c, x, header_top + offset, text, 8)
    return header_top


def render(statement: Statement, path: Path | BinaryIO) -> None:
    """Writes the PDF to a file path or a binary stream (e.g. BytesIO for the web demo)."""
    pages = _paginate(_body_rows(statement))
    c = canvas.Canvas(path if hasattr(path, "write") else str(path), pagesize=A4)
    for number, page in enumerate(pages, 1):
        _draw_page_header(c, statement, number, len(pages))
        for top, cells in page:
            for x, text, right in cells:
                _draw(c, x, top, text, right=right, squeeze=(x == 469))
        if number < len(pages):
            _draw(c, 536, 738.2, "Jatkuu")
        _draw(c, 58, 750.7, "Esimerkkipankki Oyj, Esimerkkikatu 1, 00100 Helsinki", 7)
        c.showPage()
    c.save()


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a synthetic statement PDF.")
    parser.add_argument("output", type=Path)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--month", default="2026-09", help="YYYY-MM")
    args = parser.parse_args()

    year, month = (int(part) for part in args.month.split("-"))
    statement, _ = generate(args.seed, year, month)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    render(statement, args.output)
    print(f"Wrote {args.output} ({len(statement.transactions)} transactions)")


if __name__ == "__main__":
    main()
