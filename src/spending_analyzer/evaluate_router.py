"""Measure how well the chatbot's router picks a tool and parameters.

Each case is a question plus the parameters a correct selection must contain; other
parameters may be anything. The check runs on the selection after ask()'s code guards,
so it measures what actually reaches the tools. No database is needed.

Usage:
    python -m spending_analyzer.evaluate_router
"""

import argparse

from .chatbot import TOOL_SELECTION_SCHEMA, TOOL_SYSTEM_PROMPT, apply_guards, router_calls
from .llm import LLMClient, make_client

# Expected: a dict for a one-part question, a list of dicts (any order) for several parts
CASES: list[tuple[str, dict | list[dict]]] = [
    ("Which shops did I spend the most money on groceries last month?",
     {"tool": "top_merchants", "category": "Groceries", "period": "last_month"}),
    ("Top 3 restaurants I spent the most at in September?",
     {"tool": "top_merchants", "category": "Restaurants", "period": "named_month", "month": 9}),
    ("How much did I spend on transport from August to September?",
     {"tool": "category_total", "category": "Transport", "period": "named_month",
      "month": 8, "end_month": 9}),
    ("Which grocery stores did I use in September 2026?",
     {"tool": "top_merchants", "category": "Groceries", "month": 9, "year": 2026}),
    ("How much did I spend on restaurants this year?",
     {"tool": "category_total", "category": "Restaurants", "period": "this_year"}),
    ("Where does my money go this month?",
     {"tool": "spending_by_category", "period": "this_month"}),
    ("How much did I send to Liisa Virtanen last month?",
     {"tool": "counterparty_total", "counterparty": "liisa virtanen", "period": "last_month"}),
    ("Show my spending trend for the last 3 months",
     {"tool": "monthly_trend", "trailing_months": 3}),
    ("How have my grocery costs developed over the past 6 months?",
     {"tool": "monthly_trend", "category": "Groceries", "trailing_months": 6}),
    ("Hi, thanks for the help!", {"tool": "none"}),
    ("How much did I pay Spotify in June?",
     {"tool": "counterparty_total", "counterparty": "spotify", "period": "named_month",
      "month": 6}),
    ("What did I spend the most on last year?",
     {"tool": "spending_by_category", "period": "last_year"}),
    ("Who are my biggest merchants of all time?",
     {"tool": "top_merchants", "period": "all_time"}),
    ("How much did I spend at K-market in March?",
     {"tool": "counterparty_total", "counterparty": "k-market", "period": "named_month",
      "month": 3}),
    ("Show all my transfers by each transaction to individuals descending by amount.",
     {"tool": "list_transactions", "category": "Transfers", "direction": "out"}),
    ("What is my trend on grocery shopping, what is my favorite store (one that I have "
     "the most transactions in)?",
     [{"tool": "monthly_trend", "category": "Groceries"},
      {"tool": "top_merchants", "category": "Groceries", "rank_by": "count"}]),
]


# Never used when writing the prompt: other categories and wordings, so a gain on
# CASES that comes from prompt examples overlapping the questions shows up here.
HELD_OUT: list[tuple[str, dict]] = [
    ("Which pharmacies did I spend at this year?",
     {"tool": "top_merchants", "category": "Health", "period": "this_year"}),
    ("How much went to subscriptions last month?",
     {"tool": "category_total", "category": "Subscriptions", "period": "last_month"}),
    ("How much did fuel cost me in August?",
     {"tool": "category_total", "category": "Transport", "period": "named_month", "month": 8}),
    ("Show my eating-out costs per month for the past half year",
     {"tool": "monthly_trend", "category": "Restaurants", "trailing_months": 6}),
    ("Which utility companies did I pay last month?",
     {"tool": "top_merchants", "category": "Utilities", "period": "last_month"}),
    ("How much did I receive from Juha Korhonen this year?",
     {"tool": "counterparty_total", "counterparty": "juha korhonen", "period": "this_year"}),
    ("What were my five biggest single purchases last month?",
     {"tool": "list_transactions", "period": "last_month", "limit": 5}),
    ("Where do I shop most often, and how much did I spend on restaurants this year?",
     [{"tool": "top_merchants", "rank_by": "count"},
      {"tool": "category_total", "category": "Restaurants", "period": "this_year"}]),
]


def _matches(selection: dict, expected: dict) -> list[str]:
    """Returns the expected keys the selection got wrong."""
    wrong = []
    for key, value in expected.items():
        got = selection.get(key)
        if key == "counterparty":
            ok = isinstance(got, str) and value in got.casefold()
        else:
            ok = got == value
        if not ok:
            wrong.append(f"{key}={got!r} (expected {value!r})")
    return wrong


def _check(calls: list[dict], expected: dict | list[dict]) -> list[str]:
    """A case expects one call (dict) or several (list, any order). The number of
    calls must match: an extra call on a one-part question is a mistake too."""
    expected = [expected] if isinstance(expected, dict) else expected
    calls = [c for c in calls if c.get("tool", "none") != "none"] or \
        [{"tool": "none"}]  # how answer_question treats "none" calls
    if len(calls) != len(expected):
        return [f"{len(calls)} call(s) {[c.get('tool') for c in calls]} "
                f"(expected {len(expected)})"]
    unused, wrong = list(calls), []
    for want in expected:
        match = next((c for c in unused if not _matches(c, want)), None)
        if match is None:  # report against the call with the same tool, if any
            same = next((c for c in unused if c.get("tool") == want.get("tool")), unused[0])
            wrong += _matches(same, want)
            unused.remove(same)
        else:
            unused.remove(match)
    return wrong


def evaluate_router(router: LLMClient, cases=CASES) -> tuple[int, list[tuple[str, list[str]]]]:
    failures = []
    for question, expected in cases:
        raw = router.complete_json(TOOL_SYSTEM_PROMPT, question, TOOL_SELECTION_SCHEMA).data
        calls = [apply_guards(call, question) for call in router_calls(raw)]
        wrong = _check(calls, expected)
        if wrong:
            failures.append((question, wrong))
    return len(cases) - len(failures), failures


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate the chatbot's tool routing.")
    parser.add_argument("--provider", choices=["claude", "ollama"], default="ollama")
    parser.add_argument("--model", help="model ID or Ollama tag (default per provider)")
    args = parser.parse_args()

    router = make_client(args.provider, args.model)
    print(f"## Router eval ({args.provider}: {router.model})")
    for title, cases in (("Development set", CASES), ("Held-out set", HELD_OUT)):
        correct, failures = evaluate_router(router, cases)
        print(f"\n{title}: {correct}/{len(cases)} ({correct / len(cases):.0%})")
        for question, wrong in failures:
            print(f"- {question}\n    " + "; ".join(wrong))


if __name__ == "__main__":
    main()
