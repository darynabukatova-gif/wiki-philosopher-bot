"""Find exact canonical philosopher titles without changing canonical state."""

import argparse

from wiki_philosopher_bot.cache import load_database
from wiki_philosopher_bot.config import CANONICAL_DATA_FOLDER, DATABASE_FILE
from wiki_philosopher_bot.title_search import (
    find_canonical_titles,
    posting_ineligibility_reasons,
)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Find exact canonical philosopher titles for manual posting. "
            "Default results satisfy the current posting-candidate predicate."
        )
    )
    parser.add_argument("query", help="Case-insensitive title text; diacritics are optional.")
    parser.add_argument("--all", action="store_true", help="Include titles that are not currently post-eligible.")
    parser.add_argument("--data-folder", default=CANONICAL_DATA_FOLDER)
    args = parser.parse_args(argv)
    if not args.query.strip():
        parser.error("query must contain non-whitespace text")
    return args


def _quote_count(entry):
    quotes = entry.get("quotes")
    items = quotes.get("items") if isinstance(quotes, dict) else None
    return len(items) if isinstance(items, list) else 0


def _print_match(match):
    entry = match["entry"]
    posting = entry.get("posting") if isinstance(entry.get("posting"), dict) else {}
    print(match["title"])
    print("eligible: {}".format("yes" if match["eligible"] else "no"))
    print("posted: {}".format("yes" if posting.get("has_been_posted") is True else "no"))
    print("quotes: {}".format(_quote_count(entry)))
    if not match["eligible"]:
        print("status: {}".format("; ".join(posting_ineligibility_reasons(entry))))


def main(argv=None):
    args = parse_args(argv)
    database = load_database(DATABASE_FILE, args.data_folder)
    matches = find_canonical_titles(database, args.query, include_all=args.all)
    if not matches:
        scope = "canonical" if args.all else "eligible canonical"
        print('No {} titles matched "{}".'.format(scope, args.query))
        return 1

    for index, match in enumerate(matches):
        if index:
            print()
        _print_match(match)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
