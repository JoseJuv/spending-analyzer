"""Chatbot that answers spending questions by picking a tool, never by calculating.

The LLM only ever emits a tool name and parameters (as schema-constrained JSON, the
same mechanism categorize.py uses for structured output — neither provider has native
function calling). Code resolves any relative date, runs the SQL aggregation, and
renders any chart; the LLM never sees a raw transaction and never does arithmetic.

Privacy: answering a question needs two LLM calls. The first ("router") only ever sees
the question text and a fixed system prompt (tool names and the category list) — no DB
data — so it may use either provider. The second ("local") phrases the final answer
from the real computed numbers, and always runs on the local Ollama model regardless of
--provider, so amounts and counterparty names never leave the machine. This is why no
pseudonymization is needed yet (see CLAUDE.md's "Chatbot" section).

No conversation memory: each question is answered independently.

Usage:
    python -m spending_analyzer.chatbot
    python -m spending_analyzer.chatbot --db tests/fixtures/transactions.db --provider claude
"""

import argparse
import calendar
import json
import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from .categorize import CATEGORIES, NOT_SPENDING_OR_INCOME
from .llm import AuditedClient, LLMClient, LLMError, make_client
from .store import Store


@dataclass
class Tool:
    name: str
    description: str  # one line, used verbatim in the tool-selection prompt


TOOLS = [
    Tool("spending_by_category",
         "Total for every category over a period. No extra parameters besides period."),
    Tool("category_total",
         'Total for one specific category over a period. Requires "category".'),
    Tool("top_merchants",
         "The biggest counterparties over a period, optionally one category. Optional "
         '"category", "limit" (default 10), "rank_by": "amount" (most money spent, the '
         'default) or "count" (most transactions, e.g. a favorite or most-visited store).'),
    Tool("monthly_trend",
         "Totals per month over the trailing N months. With \"category\", totals for "
         "that category per month; without one, net cash flow per month (income minus "
         'spending), excluding Transfers/Investments. Optional "category", '
         '"trailing_months" (default 6).'),
    Tool("counterparty_total",
         "Total sent to or received from one named person or merchant over a period. "
         'Requires "counterparty" (their name as the user wrote it).'),
    Tool("list_transactions",
         "Individual transactions, one row each (date, counterparty, amount), largest "
         "amount first — use when the user wants to see each transaction or the "
         "biggest single payments rather than totals. Optional \"category\", "
         '"counterparty", "direction" (out = paid or sent, in = received), "limit" '
         "(default 20)."),
]

MAX_CALLS = 3

CALL_SCHEMA = {
    "type": "object",
    "properties": {
        "tool": {"type": "string", "enum": [t.name for t in TOOLS] + ["none"]},
        "category": {"type": "string", "enum": CATEGORIES + ["all"]},
        "period": {"type": "string", "enum": ["this_month", "last_month", "this_year",
                                               "last_year", "all_time", "named_month"]},
        "month": {"type": "integer", "minimum": 1, "maximum": 12},
        "end_month": {"type": "integer", "minimum": 1, "maximum": 12},
        "year": {"type": "integer"},
        "limit": {"type": "integer"},
        "trailing_months": {"type": "integer"},
        "counterparty": {"type": "string"},
        "direction": {"type": "string", "enum": ["out", "in", "any"]},
        "rank_by": {"type": "string", "enum": ["amount", "count"]},
    },
    "required": ["tool", "period"],
    "additionalProperties": False,
}

# Several independent calls for a question with several parts ("my grocery trend, and
# my favorite store?"). Each runs on its own; a call can't use another's result —
# that would need an agent loop, which a small local model doesn't run reliably.
TOOL_SELECTION_SCHEMA = {
    "type": "object",
    "properties": {"calls": {"type": "array", "items": CALL_SCHEMA,
                             "minItems": 1, "maxItems": MAX_CALLS}},
    "required": ["calls"],
    "additionalProperties": False,
}

ANSWER_SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}


def _tool_system_prompt() -> str:
    lines = ["You help a user explore their personal spending by choosing tool calls, "
             'returned as {"calls": [...]}. Usually one call is enough. Use two or three '
             "calls only when the question asks for separate things (e.g. a trend and a "
             "ranking); each call runs on its own and cannot use another call's result.",
             "", "Tools:"]
    lines += [f"- {tool.name}: {tool.description}" for tool in TOOLS]
    lines.append("- none: the question is not about spending data (greeting, thanks, "
                  "or anything these tools cannot answer) — pick this and answer "
                  "without a tool.")
    lines += ["", "Categories: " + ", ".join(CATEGORIES) + ".", "",
              'Parameters: set "category" whenever the question mentions a kind of '
              'spending or shop ("grocery stores" → Groceries, "restaurants" → '
              'Restaurants, "transport costs" → Transport); it works with '
              'top_merchants, category_total, monthly_trend and list_transactions. '
              '"counterparty" is only '
              "for a specific person's or business's name, never a category or a month.",
              "",
              "Period: pick one of this_month, last_month, this_year, last_year, "
              "all_time, named_month. When the question names a month, use named_month "
              'and put its number in "month" ("in September" → period named_month, month '
              '9); for a span also set "end_month" ("from March to May" → month 3, '
              'end_month 5). Set "year" only if the question writes one out. Otherwise '
              "default to this_month. Never write out dates — the application resolves "
              "every period to real dates.", "",
              "Always set period, even for a tool where it doesn't matter (e.g. none or "
              "monthly_trend) — in that case just use this_month. Set only the other "
              "parameters relevant to the tool you pick; leave the rest unset."]
    return "\n".join(lines)


TOOL_SYSTEM_PROMPT = _tool_system_prompt()

ANSWER_SYSTEM_PROMPT = """\
You answer a question about the user's personal spending using ONLY the numbers given
to you below — never invent, estimate, or recompute a figure; the numbers are already
computed correctly in euros.

A positive total means money came in (received or income); a negative total means
money went out (sent), except a spending category or merchant total, which is already
shown as a positive "amount spent".

If "Transfers" or "Investments" appear in the data, briefly note they are money moved
between the user's own accounts, not spending or income — never add them into a
spending or income total.

If an entry is "MobilePay/Vipps (recipient not shown on the statement)", say plainly
that the bank statement does not show who received those payments; never guess or
invent names for it.

The data may hold the results of several tools, one per part of the question: answer
every part.

If the data is a list of transactions, summarize it (how many, their total, the largest)
and do not list the rows: a table of every row is shown separately.

If no data was given (no tool was needed for this question), answer briefly and
conversationally, but do not state any spending figures.

Keep the answer to 1-3 short sentences. Do not describe a chart; one is printed
separately if relevant."""


class PeriodError(ValueError):
    """The model picked a period without the parameters it needs."""


def resolve_period(period: str, start_date: str | None = None, end_date: str | None = None,
                    today: date | None = None, *, month: int | None = None,
                    end_month: int | None = None, year: int | None = None) -> tuple[str, str]:
    """Resolves a period enum to concrete ISO dates; the LLM never computes a
    relative date itself. Plain date arithmetic handles month/year boundaries (e.g.
    last_month in January resolves to December of the previous year).

    An incomplete period raises PeriodError instead of falling back to this_month:
    a silent fallback answered "in September" with October's numbers."""
    today = today or date.today()
    if period == "custom":
        try:
            return (date.fromisoformat(start_date).isoformat(),
                    date.fromisoformat(end_date).isoformat())
        except (TypeError, ValueError):
            raise PeriodError("custom period needs start_date and end_date (YYYY-MM-DD)")
    if period == "named_month":
        last = end_month or month
        if not (month and 1 <= month <= 12 and 1 <= last <= 12):
            raise PeriodError("named_month period needs month (1-12)")
        # Without a year, the most recent such month that has started
        start_year = year or (today.year if month <= today.month else today.year - 1)
        end_year = start_year if last >= month else start_year + 1
        return (date(start_year, month, 1).isoformat(),
                date(end_year, last, calendar.monthrange(end_year, last)[1]).isoformat())
    if period == "last_month":
        last_day_prev = today.replace(day=1) - timedelta(days=1)
        return last_day_prev.replace(day=1).isoformat(), last_day_prev.isoformat()
    if period == "this_year":
        return today.replace(month=1, day=1).isoformat(), today.isoformat()
    if period == "last_year":
        return date(today.year - 1, 1, 1).isoformat(), date(today.year - 1, 12, 31).isoformat()
    if period == "all_time":
        return "0001-01-01", today.isoformat()
    return today.replace(day=1).isoformat(), today.isoformat()  # this_month, and the fallback


def _trailing_months_range(months: int, today: date) -> tuple[str, str]:
    year, month = today.year, today.month - (months - 1)
    while month <= 0:
        month += 12
        year -= 1
    return date(year, month, 1).isoformat(), today.isoformat()


def _display_value(category: str, value: Decimal) -> Decimal:
    """Spending categories display as a positive "amount spent"; Income and the
    NOT_SPENDING_OR_INCOME categories keep their natural sign."""
    if category == "Income" or category in NOT_SPENDING_OR_INCOME:
        return value
    return abs(value)


def render_bar_chart(data: list[tuple[str, Decimal]], width: int = 30) -> str:
    """data is already display-ready (sign/order decided by the caller); bar length
    is scaled by magnitude so a negative value still renders a visible bar."""
    if not data:
        return ""
    label_width = max(len(label) for label, _ in data)
    max_magnitude = max((abs(value) for _, value in data), default=Decimal(0)) or Decimal(1)
    return "\n".join(
        f"{label:<{label_width}} {'#' * int(width * abs(value) / max_magnitude)} {value}"
        for label, value in data)


def _period(params: dict, today: date) -> tuple[str, str]:
    return resolve_period(params.get("period", "this_month"), params.get("start_date"),
                          params.get("end_date"), today, month=params.get("month"),
                          end_month=params.get("end_month"), year=params.get("year"))


ChartRows = list[tuple[str, Decimal]]
CATEGORY_TOOLS = ("top_merchants", "category_total", "monthly_trend", "list_transactions")
# Word starts that name a category outright (plural/verb forms: "grocery", "transferred")
CATEGORY_STEMS = {"grocer": "Groceries", "restaurant": "Restaurants",
                  "transport": "Transport", "entertainment": "Entertainment",
                  "subscription": "Subscriptions", "shopping": "Shopping",
                  "health": "Health", "housing": "Housing", "utilit": "Utilities",
                  "income": "Income", "transfer": "Transfers", "investment": "Investments"}
MAX_LISTED = 200  # rows list_transactions returns at most
ROWS_IN_ANSWER_PROMPT = 10


def run_tool(tool_name: str, params: dict, store: Store, today: date
             ) -> tuple[dict, ChartRows | None]:
    """Executes one tool against the store; returns (JSON-safe result, chart rows).
    Chart rows are display-ready (label, value) pairs the caller renders as text or a
    real chart. Every Decimal is converted with str(), never float(), before being
    handed to the answer-phrasing prompt."""
    try:
        return _run_tool(tool_name, params, store, today)
    except PeriodError as error:
        return {"error": f"Could not tell which dates were meant: {error}"}, None


def _run_tool(tool_name: str, params: dict, store: Store, today: date
              ) -> tuple[dict, ChartRows | None]:
    if tool_name == "spending_by_category":
        start, end = _period(params, today)
        totals = store.spending_by_category(start, end)
        spending_and_income = {c: _display_value(c, v) for c, v in totals.items()
                                if c not in NOT_SPENDING_OR_INCOME}
        moved = {c: v for c, v in totals.items() if c in NOT_SPENDING_OR_INCOME}
        # Spending only: income is a different kind of number and dwarfs the bars
        chart = sorted(((c, v) for c, v in spending_and_income.items() if c != "Income"),
                       key=lambda kv: kv[1])
        result = {"period": {"start": start, "end": end},
                  "spending_and_income": {c: str(v) for c, v in spending_and_income.items()},
                  "moved_between_own_assets": {c: str(v) for c, v in moved.items()}}
        return result, chart

    if tool_name == "category_total":
        category = params.get("category")
        if not category or category == "all":
            return {"error": "category_total requires a specific category"}, None
        start, end = _period(params, today)
        total = _display_value(category, store.category_total(category, start, end))
        return {"period": {"start": start, "end": end}, "category": category,
                "total": str(total)}, None

    if tool_name == "top_merchants":
        start, end = _period(params, today)
        category = params.get("category")
        category = None if category in (None, "all") else category
        limit = params.get("limit") or 10
        rank_by = "count" if params.get("rank_by") == "count" else "amount"
        stats = store.top_counterparty_stats(start, end, category, limit, rank_by)
        # The chart shows what the ranking is by: euros spent, or number of purchases
        chart = [(s["name"], Decimal(s["count"]) if rank_by == "count" else abs(s["total"]))
                 for s in stats]
        result = {"period": {"start": start, "end": end}, "category": category or "all",
                  "ranked_by": "number of transactions" if rank_by == "count"
                  else "amount spent",
                  "top_merchants": [{"name": s["name"], "total": str(abs(s["total"])),
                                     "transactions": s["count"]} for s in stats]}
        return result, chart

    if tool_name == "monthly_trend":
        months = params.get("trailing_months") or 6
        category = params.get("category")
        category = None if category in (None, "all") else category
        # Count back from the newest data, not from today: statements arrive after a
        # month ends, so "the last 3 months" would otherwise include an empty month
        bounds = store.date_bounds()
        anchor = min(today, bounds[1]) if bounds else today
        start, end = _trailing_months_range(months, anchor)
        if bounds:  # nor before the data begins, or the answer names empty months
            start = max(start, bounds[0].replace(day=1).isoformat())
        if category:
            totals = {m: _display_value(category, v)
                      for m, v in store.monthly_totals(start, end, category).items()}
            label = category
        else:
            net = store.monthly_totals(start, end)
            transfers = store.monthly_totals(start, end, "Transfers")
            investments = store.monthly_totals(start, end, "Investments")
            all_months = sorted(set(net) | set(transfers) | set(investments))
            totals = {m: net.get(m, Decimal("0")) - transfers.get(m, Decimal("0"))
                         - investments.get(m, Decimal("0")) for m in all_months}
            label = "net_cash_flow (income minus spending, excluding Transfers/Investments)"
        ordered = sorted(totals.items())
        chart = ordered
        result = {"start": start, "end": end, "label": label,
                  "monthly": {m: str(v) for m, v in ordered}}
        return result, chart

    if tool_name == "counterparty_total":
        counterparty = params.get("counterparty")
        if not counterparty:
            return {"error": "counterparty_total requires a counterparty name"}, None
        start, end = _period(params, today)
        found = store.counterparty_total(counterparty, start, end)
        if found is None:
            return {"error": f"No counterparty matching {counterparty!r} was found."}, None
        display_name, total = found
        return {"period": {"start": start, "end": end}, "counterparty": display_name,
                "total": str(total)}, None

    if tool_name == "list_transactions":
        start, end = _period(params, today)
        category = params.get("category")
        category = None if category in (None, "all") else category
        direction = params.get("direction")
        direction = None if direction in (None, "any") else direction
        limit = max(1, min(int(params.get("limit") or 20), MAX_LISTED))
        rows, count, total = store.list_transactions(
            start, end, category, direction, params.get("counterparty") or None, limit)
        return {"period": {"start": start, "end": end}, "category": category or "all",
                "direction": direction or "any", "count": count, "total": str(total),
                "transactions": [{"date": r["date"], "counterparty": r["counterparty"],
                                  "amount": str(r["amount"]), "category": r["category"]}
                                 for r in rows]}, None

    return {"error": f"Unknown tool {tool_name!r}"}, None


def _for_answer_prompt(result: dict) -> dict:
    """A long transaction list is shown as a table; the answering model only needs the
    summary and the first rows, which keeps a local model's prompt small."""
    rows = result.get("transactions")
    if not rows or len(rows) <= ROWS_IN_ANSWER_PROMPT:
        return result
    return result | {"transactions": rows[:ROWS_IN_ANSWER_PROMPT],
                     "more_rows_in_table": len(rows) - ROWS_IN_ANSWER_PROMPT}


def apply_guards(selection: dict, question: str) -> dict:
    """Deterministic checks on the router's choice, for mistakes a small model makes
    repeatably. Returns a corrected copy; never adds information the question lacks."""
    selection = dict(selection)
    if selection.get("year") and str(selection["year"]) not in question:
        del selection["year"]  # an invented year; code picks the most recent month
    by_name = {c.casefold(): c for c in CATEGORIES}
    counterparty = str(selection.get("counterparty") or "").strip().casefold()
    if counterparty in by_name:  # a category put in the wrong field
        selection.setdefault("category", by_name[counterparty])
        del selection["counterparty"]
    if selection.get("tool") in CATEGORY_TOOLS and selection.get("category") in (None, "all"):
        # The model often drops the category; restore it only when the question names
        # one outright ("groceries", "transferred"), never from synonyms like "fuel"
        named = {c for stem, c in CATEGORY_STEMS.items()
                 if re.search(rf"\b{stem}", question, re.IGNORECASE)}
        if len(named) == 1:
            selection["category"] = named.pop()
    return selection


@dataclass
class Part:
    """One executed tool call."""
    tool: str
    selection: dict  # the router's parameters for this call, after apply_guards()
    result: dict  # what the tool computed
    chart: ChartRows | None
    table: list[dict] | None = None  # rows of list_transactions, for a table view


@dataclass
class Answer:
    text: str
    parts: list[Part]  # empty when no tool was needed (tool "none")


def router_calls(raw: dict) -> list[dict]:
    """The router's calls. A bare single call (the pre-multi-call format, or a model
    ignoring the wrapper) is accepted too; at most MAX_CALLS run."""
    calls = raw.get("calls") if isinstance(raw.get("calls"), list) else [raw]
    return [c for c in calls if isinstance(c, dict)][:MAX_CALLS]


def answer_question(question: str, store: Store, router: LLMClient, local: LLMClient,
                    today: date | None = None) -> Answer:
    """router only ever receives `question` plus the fixed TOOL_SYSTEM_PROMPT — never
    a tool_result. local is the only client that ever sees real computed numbers."""
    today = today or date.today()
    raw = router.complete_json(TOOL_SYSTEM_PROMPT, question, TOOL_SELECTION_SCHEMA).data
    parts, seen = [], []
    for call in router_calls(raw):
        selection = apply_guards(call, question)
        tool_name = selection.get("tool", "none")
        if tool_name == "none" or selection in seen:  # skip no-ops and duplicate calls
            continue
        seen.append(selection)
        result, chart = run_tool(tool_name, selection, store, today)
        parts.append(Part(tool_name, selection, result, chart, result.get("transactions")))

    if not parts:
        data = "No data was queried."
    elif len(parts) == 1:
        data = json.dumps(_for_answer_prompt(parts[0].result))
    else:
        data = json.dumps([{"part": n, "tool": p.tool, "result": _for_answer_prompt(p.result)}
                           for n, p in enumerate(parts, 1)])
    prompt = "Question: " + question + "\n\nData:\n" + data
    text = local.complete_json(ANSWER_SYSTEM_PROMPT, prompt, ANSWER_SCHEMA).data["answer"]
    return Answer(text, parts)


def ask(question: str, store: Store, router: LLMClient, local: LLMClient,
        today: date | None = None) -> str:
    """Text answer with text charts and tables, for the terminal."""
    answer = answer_question(question, store, router, local, today)
    blocks = [answer.text]
    for part in answer.parts:
        if part.table:
            blocks.append("\n".join(f"{r['date']}  {r['amount']:>10}  {r['counterparty']}"
                                    for r in part.table))
        elif part.chart:
            blocks.append(render_bar_chart(part.chart))
    return "\n\n".join(blocks)


def main() -> None:
    parser = argparse.ArgumentParser(description="Ask questions about your spending.")
    parser.add_argument("--db", type=Path, default=Path("data/transactions.db"))
    parser.add_argument("--provider", choices=["claude", "ollama"], default="ollama")
    parser.add_argument("--model", help="router model ID or Ollama tag (default per provider)")
    args = parser.parse_args()

    store = Store(args.db)
    local = AuditedClient(make_client("ollama"))
    router = local if args.provider == "ollama" else AuditedClient(make_client("claude", args.model))

    print("Spending chatbot. Ask a question (blank line or 'quit' to exit).")
    while True:
        question = input("\n> ").strip()
        if not question or question.lower() in ("quit", "exit"):
            break
        try:
            print(ask(question, store, router, local))
        except LLMError as error:
            print(f"LLM error: {error}")
    store.close()


if __name__ == "__main__":
    main()
