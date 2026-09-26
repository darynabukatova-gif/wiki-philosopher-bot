"""Pure read-only historical philosopher death reporting."""

from collections import Counter
from datetime import date, datetime, timezone
from statistics import median

from wiki_philosopher_bot.date_policy import completed_calendar_years
from wiki_philosopher_bot.utils import is_semantically_postable_philosopher


EXACT_DEATH_ROW_FIELDS = (
    "title",
    "display_title",
    "qid",
    "birth_year",
    "birth_date",
    "death_year",
    "death_date",
    "age_at_death",
)


def _require_date(value, name):
    if not isinstance(value, date) or isinstance(value, datetime):
        raise TypeError("{} must be a date".format(name))
    return value


def _parse_optional_date(value):
    if value is None:
        return None, None
    if not isinstance(value, str):
        return None, "must be an ISO date or null"
    try:
        return date.fromisoformat(value), None
    except ValueError:
        return None, "must be an ISO date or null"


def _is_year(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _age_at_death(birth_date, death_date):
    if birth_date is None or death_date is None or birth_date > death_date:
        return None
    return completed_calendar_years(birth_date, death_date)


def _generated_at_text(value):
    if value is None:
        value = datetime.now(timezone.utc)
    if not isinstance(value, datetime):
        raise TypeError("generated_at must be a datetime")
    if value.tzinfo is None:
        raise ValueError("generated_at must be timezone-aware")
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _exact_row(title, entry, wikidata, death_date, birth_date):
    return {
        "title": title,
        "display_title": entry.get("display_title") or title,
        "qid": wikidata.get("qid"),
        "birth_year": wikidata.get("birth_year"),
        "birth_date": birth_date.isoformat() if birth_date is not None else None,
        "death_year": wikidata.get("death_year"),
        "death_date": death_date.isoformat(),
        "age_at_death": _age_at_death(birth_date, death_date),
    }


def _age_statistics(rows):
    ages = [row["age_at_death"] for row in rows if row["age_at_death"] is not None]
    if not ages:
        return {
            "exact_ages_available": 0,
            "insufficient_age_data": len(rows),
            "mean": None,
            "median": None,
            "minimum": None,
            "maximum": None,
        }
    return {
        "exact_ages_available": len(ages),
        "insufficient_age_data": len(rows) - len(ages),
        "mean": sum(ages) / len(ages),
        "median": median(ages),
        "minimum": min(ages),
        "maximum": max(ages),
    }


def build_historical_death_report(
    database,
    interval_from,
    interval_to,
    *,
    database_sha256,
    generated_at=None,
    interval_source="explicit",
    last_years=None,
):
    """Build a deterministic report without mutating canonical records."""
    interval_from = _require_date(interval_from, "interval_from")
    interval_to = _require_date(interval_to, "interval_to")
    if interval_from > interval_to:
        raise ValueError("interval from date must not be after to date")
    if interval_source not in ("explicit", "last-years"):
        raise ValueError("unsupported interval source")

    semantic_entries = [
        (title, entry)
        for title, entry in database.items()
        if is_semantically_postable_philosopher(entry)
    ]

    exact_rows = []
    year_only_rows = []
    outside_rows = []
    unknown_rows = []
    invalid_rows = []
    death_year_available = 0
    exact_death_date_available = 0
    death_year_only = 0
    exact_birth_date_available = 0

    for title, entry in semantic_entries:
        wikidata = entry.get("wikidata")
        if not isinstance(wikidata, dict):
            invalid_rows.append({"title": title, "reason": "wikidata must be an object"})
            continue

        death_year = wikidata.get("death_year")
        if death_year is not None and not _is_year(death_year):
            invalid_rows.append({"title": title, "reason": "death_year must be an integer or null"})
            continue
        if death_year is not None:
            death_year_available += 1

        birth_date, birth_error = _parse_optional_date(wikidata.get("birth_date"))
        if birth_date is not None:
            exact_birth_date_available += 1

        death_date, death_error = _parse_optional_date(wikidata.get("death_date"))
        if death_error is not None:
            invalid_rows.append({
                "title": title,
                "reason": "death_date {}".format(death_error),
                "value": wikidata.get("death_date"),
            })
            continue

        if death_date is not None:
            exact_death_date_available += 1
            if interval_from <= death_date <= interval_to:
                # birth_date is not yet canonical schema, so invalid optional
                # future data is insufficient for age, not a reason to discard
                # an otherwise valid exact death.
                if birth_error is not None:
                    birth_date = None
                exact_rows.append(
                    _exact_row(title, entry, wikidata, death_date, birth_date)
                )
            else:
                outside_rows.append({
                    "title": title,
                    "death_date": death_date.isoformat(),
                    "death_year": death_year,
                    "basis": "exact-death-date",
                })
            continue

        if death_year is None:
            unknown_rows.append({"title": title})
            continue

        death_year_only += 1
        year_row = {"title": title, "death_year": death_year}
        if interval_from.year <= death_year <= interval_to.year:
            year_only_rows.append(year_row)
        else:
            outside_rows.append({**year_row, "basis": "death-year-only"})

    exact_rows.sort(key=lambda row: (row["death_date"], row["title"]))
    year_only_rows.sort(key=lambda row: (row["death_year"], row["title"]))
    outside_rows.sort(key=lambda row: row["title"])
    unknown_rows.sort(key=lambda row: row["title"])
    invalid_rows.sort(key=lambda row: row["title"])

    year_counts = Counter(row["death_date"][:4] for row in exact_rows)
    interval = {
        "from": interval_from.isoformat(),
        "to": interval_to.isoformat(),
        "inclusive": True,
        "source": interval_source,
    }
    if interval_source == "last-years":
        interval["last_years"] = last_years

    return {
        "operation": "historical-death-report",
        "mode": "read-only",
        "generated_at": _generated_at_text(generated_at),
        "database_sha256": database_sha256,
        "interval": interval,
        "population": {
            "canonical_records": len(database),
            "semantic_philosophers": len(semantic_entries),
            "non_semantic_records_skipped": len(database) - len(semantic_entries),
        },
        "date_completeness": {
            "death_year_available": death_year_available,
            "exact_death_date_available": exact_death_date_available,
            "death_year_only": death_year_only,
            "unknown_death": len(unknown_rows),
            "exact_birth_date_available": exact_birth_date_available,
        },
        "interval_results": {
            "exact_deaths": len(exact_rows),
            "year_only_potential_matches": len(year_only_rows),
            "deaths_outside_interval": len(outside_rows),
            "unknown_death_date": len(unknown_rows),
            "invalid_date_data": len(invalid_rows),
            "rows": exact_rows,
            "year_only_rows": year_only_rows,
            "invalid_rows": invalid_rows,
        },
        "deaths_by_year": {
            year: year_counts[year] for year in sorted(year_counts)
        },
        "age_statistics": _age_statistics(exact_rows),
    }
