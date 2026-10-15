"""Decide what may be sent to an LLM. See the privacy rules in CLAUDE.md."""

import re

from .models import Transaction

COMPANY_MARKERS = re.compile(
    r"\b(oy|oyj|ab|ky|ay|ry|tmi|se|ltd|inc|gmbh|as|aps|llc|plc)\b|\.(com|fi|net)\b",
    re.IGNORECASE)
MASKED_CARD = re.compile(r"\d{6}\*{6}\d{4}")


def is_card_payment(t: Transaction) -> bool:
    return "KORTTI" in t.type.upper() or bool(MASKED_CARD.search(t.message))


def may_be_private_person(t: Transaction) -> bool:
    """True unless the counterparty is clearly a business.

    Card payments always go to merchants. For transfers and bills, a name without a
    company marker (Oy, Ab, .com, ...) is treated as a person. When in doubt the answer
    is True: a wrong guess costs categorization quality, never privacy.
    """
    if is_card_payment(t):
        return False
    return not COMPANY_MARKERS.search(t.counterparty)
