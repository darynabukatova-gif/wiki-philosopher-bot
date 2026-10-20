"""Render validated historical-death reports into auditable PNG assets."""

import hashlib
import json
import math
import os
import struct
import tempfile
import textwrap
from collections import Counter
from datetime import date
import calendar
from pathlib import Path
from statistics import median
from wiki_philosopher_bot.date_policy import completed_calendar_years
from wiki_philosopher_bot.report_intervals import completed_calendar_months
from wiki_philosopher_bot.report_formatting import (
    compact_percentage, format_report_interval_compact,
    format_report_interval_long, mean_age, median_age,
)

RENDERER_VERSION = 2
TELEGRAM_MEDIA_GROUP_MAX = 10
DEFAULT_ROWS_PER_PAGE = 22
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

# Canonical semantic identifiers used in render manifests and outbox media
# validation.  The legacy summary-completeness identifier remains supported
# only when reading previously generated manifests.
INTERVAL_SUMMARY_KIND = "interval-summary"
LEGACY_SUMMARY_KIND = "summary-completeness"
DEATHS_BY_MONTH_KIND = "deaths-by-month"
DEATHS_BY_YEAR_KIND = "deaths-by-year"
MEDIAN_AGE_BY_YEAR_KIND = "median-age-by-year"
INDIVIDUAL_AGES_KIND = "individual-ages"
AGE_AT_DEATH_KIND = "age-at-death"
DEATHS_TABLE_KIND = "deaths-table"

# Normalized axes coordinates are deliberately explicit so compact canvases do
# not collapse the title, subtitle, and first content section into one another.
SUMMARY_LAYOUT = {
    "title": 0.96,
    "subtitle": 0.84,
    "section": 0.66,
    "count": 0.60,
    "ages": 0.48,
    "statistics": 0.40,
    "extremes": 0.27,
    "youngest": 0.20,
    "oldest": 0.13,
}
TABLE_LAYOUT = {
    "title": 0.97,
    "subtitle": 0.87,
    "header": 0.75,
    "content_gap": 0.07,
    "bottom": 0.035,
}


class DeathReportRenderError(ValueError):
    """Raised when a source report cannot be rendered safely."""


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _parse_date(value, field_name):
    if not isinstance(value, str):
        raise DeathReportRenderError("{} must be an ISO date".format(field_name))
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise DeathReportRenderError(
            "{} must be an ISO date".format(field_name)
        ) from error


def validate_historical_death_report(report):
    """Validate and normalize rendering inputs without mutating *report*."""
    if not isinstance(report, dict):
        raise DeathReportRenderError("source report must be a JSON object")
    if report.get("operation") != "historical-death-report":
        raise DeathReportRenderError("unsupported report operation")
    if report.get("mode") != "read-only":
        raise DeathReportRenderError("historical report must have mode read-only")

    interval = report.get("interval")
    if not isinstance(interval, dict):
        raise DeathReportRenderError("interval must be an object")
    interval_from = _parse_date(interval.get("from"), "interval.from")
    interval_to = _parse_date(interval.get("to"), "interval.to")
    if interval_from > interval_to:
        raise DeathReportRenderError("interval.from must not be after interval.to")
    if interval.get("inclusive") is not True:
        raise DeathReportRenderError("renderer requires an inclusive interval")

    interval_results = report.get("interval_results")
    if not isinstance(interval_results, dict):
        raise DeathReportRenderError("interval_results must be an object")
    rows = interval_results.get("rows")
    if not isinstance(rows, list):
        raise DeathReportRenderError("interval_results.rows must be a list")
    exact_deaths = interval_results.get("exact_deaths")
    if not _is_int(exact_deaths) or exact_deaths < 0 or exact_deaths != len(rows):
        raise DeathReportRenderError(
            "interval_results.exact_deaths must equal the number of rows"
        )

    normalized_rows = []
    previous_key = None
    for index, row in enumerate(rows):
        prefix = "interval_results.rows[{}]".format(index)
        if not isinstance(row, dict):
            raise DeathReportRenderError("{} must be an object".format(prefix))
        title = row.get("title")
        if not isinstance(title, str) or not title.strip():
            raise DeathReportRenderError("{}.title must be non-empty".format(prefix))
        display_title = row.get("display_title")
        if display_title is None:
            display_title = title
        if not isinstance(display_title, str) or not display_title.strip():
            raise DeathReportRenderError(
                "{}.display_title must be non-empty or null".format(prefix)
            )
        death_date = _parse_date(row.get("death_date"), "{}.death_date".format(prefix))
        age = row.get("age_at_death")
        if age is not None and (not _is_int(age) or age < 0):
            raise DeathReportRenderError(
                "{}.age_at_death must be a non-negative integer or null".format(prefix)
            )
        key = (death_date.isoformat(), title)
        if previous_key is not None and key < previous_key:
            raise DeathReportRenderError(
                "exact rows must retain death_date, canonical title order"
            )
        previous_key = key
        normalized_rows.append({
            "title": title,
            "display_title": display_title,
            "death_date": death_date.isoformat(),
            "age_at_death": age,
        })

    deaths_by_year = report.get("deaths_by_year")
    if not isinstance(deaths_by_year, dict):
        raise DeathReportRenderError("deaths_by_year must be an object")
    normalized_years = {}
    for year, count in deaths_by_year.items():
        if not isinstance(year, str) or len(year) != 4 or not year.isdigit():
            raise DeathReportRenderError("deaths_by_year keys must be four-digit years")
        if not _is_int(count) or count < 0:
            raise DeathReportRenderError("deaths_by_year counts must be non-negative integers")
        normalized_years[year] = count
    row_years = Counter(row["death_date"][:4] for row in normalized_rows)
    expected_years = {year: row_years[year] for year in sorted(row_years)}
    if normalized_years != expected_years:
        raise DeathReportRenderError("deaths_by_year does not match exact report rows")

    age_statistics = report.get("age_statistics")
    if not isinstance(age_statistics, dict):
        raise DeathReportRenderError("age_statistics must be an object")
    ages = [
        row["age_at_death"] for row in normalized_rows
        if row["age_at_death"] is not None
    ]
    exact_ages = age_statistics.get("exact_ages_available")
    if not _is_int(exact_ages) or exact_ages != len(ages):
        raise DeathReportRenderError(
            "age_statistics.exact_ages_available does not match exact rows"
        )
    if age_statistics.get("insufficient_age_data") != len(normalized_rows) - len(ages):
        raise DeathReportRenderError(
            "age_statistics.insufficient_age_data does not match exact rows"
        )
    if ages:
        expected = {
            "mean": sum(ages) / len(ages),
            "median": median(ages),
            "minimum": min(ages),
            "maximum": max(ages),
        }
        for field, value in expected.items():
            reported = age_statistics.get(field)
            if not isinstance(reported, (int, float)) or isinstance(reported, bool):
                raise DeathReportRenderError("age_statistics.{} must be numeric".format(field))
            if not math.isclose(float(reported), float(value), rel_tol=0, abs_tol=1e-9):
                raise DeathReportRenderError(
                    "age_statistics.{} does not match exact rows".format(field)
                )
    else:
        for field in ("mean", "median", "minimum", "maximum"):
            if age_statistics.get(field) is not None:
                raise DeathReportRenderError(
                    "age_statistics.{} must be null without exact ages".format(field)
                )

    yearly = report.get("yearly_statistics")
    if yearly is None:
        yearly = []
        for year in range(interval_from.year, interval_to.year + 1):
            year_rows = [row for row in normalized_rows if int(row["death_date"][:4]) == year]
            year_ages = [row["age_at_death"] for row in year_rows if row["age_at_death"] is not None]
            yearly.append({
                "year": year, "exact_deaths": len(year_rows),
                "exact_ages_available": len(year_ages),
                "mean_age": sum(year_ages) / len(year_ages) if year_ages else None,
                "median_age": median(year_ages) if year_ages else None,
                "min_age": min(year_ages) if year_ages else None,
                "max_age": max(year_ages) if year_ages else None,
            })
    if not isinstance(yearly, list):
        raise DeathReportRenderError("yearly_statistics must be a list")
    normalized_yearly = []
    for item in yearly:
        if not isinstance(item, dict) or not _is_int(item.get("year")):
            raise DeathReportRenderError("yearly_statistics entries must contain integer years")
        values = {key: item.get(key) for key in ("exact_deaths", "exact_ages_available")}
        if any(not _is_int(value) or value < 0 for value in values.values()):
            raise DeathReportRenderError("yearly_statistics counts must be non-negative integers")
        for key in ("mean_age", "median_age", "min_age", "max_age"):
            value = item.get(key)
            if value is not None and (not isinstance(value, (int, float)) or isinstance(value, bool)):
                raise DeathReportRenderError("yearly_statistics ages must be numeric or null")
        normalized_yearly.append({"year": item["year"], **values, **{key: item.get(key) for key in ("mean_age", "median_age", "min_age", "max_age")}})
    if [item["year"] for item in normalized_yearly] != sorted(item["year"] for item in normalized_yearly):
        raise DeathReportRenderError("yearly_statistics must be chronological")
    data_completeness = report.get("data_completeness")
    age_extremes = report.get("age_extremes")
    if data_completeness is not None and not isinstance(data_completeness, dict):
        raise DeathReportRenderError("data_completeness must be an object")
    if age_extremes is not None and not isinstance(age_extremes, dict):
        raise DeathReportRenderError("age_extremes must be an object")

    return {
        "interval": {
            "from": interval_from.isoformat(),
            "to": interval_to.isoformat(),
            "inclusive": True,
        },
        "rows": normalized_rows,
        "deaths_by_year": {
            year: normalized_years[year] for year in sorted(normalized_years)
        },
        "ages": ages,
        "age_statistics": {
            field: age_statistics.get(field)
            for field in (
                "exact_ages_available", "insufficient_age_data", "mean",
                "median", "minimum", "maximum",
            )
        },
        "data_completeness": data_completeness or {},
        "yearly_statistics": normalized_yearly,
        "age_extremes": age_extremes or {"youngest": None, "oldest": None},
    }


def load_historical_death_report(path):
    source_path = Path(path)
    source_bytes = source_path.read_bytes()
    try:
        report = json.loads(source_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DeathReportRenderError("source report is not valid UTF-8 JSON") from error
    return source_path, source_bytes, validate_historical_death_report(report)


def _load_pyplot():
    try:
        import matplotlib
    except ImportError as error:
        raise RuntimeError("death-report rendering requires matplotlib") from error
    matplotlib.use("Agg", force=True)
    import matplotlib.pyplot as pyplot
    return pyplot


def _atomic_figure_save(figure, destination):
    destination = Path(destination)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".{}-".format(destination.name), suffix=".tmp",
        dir=str(destination.parent),
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    try:
        figure.savefig(
            str(temporary_path), format="png", dpi=120,
            facecolor="white", bbox_inches="tight", pad_inches=0.25,
        )
        with temporary_path.open("rb") as handle:
            os.fsync(handle.fileno())
        return temporary_path
    except BaseException:
        if temporary_path.exists():
            temporary_path.unlink()
        raise


def png_dimensions(path):
    """Return PNG width/height after validating its signature and IHDR."""
    with Path(path).open("rb") as handle:
        header = handle.read(24)
    if len(header) != 24 or header[:8] != PNG_SIGNATURE or header[12:16] != b"IHDR":
        raise DeathReportRenderError("generated asset is not a valid PNG")
    return struct.unpack(">II", header[16:24])


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _interval_text(inputs, *, compact=False):
    interval = inputs["interval"]
    formatter = format_report_interval_compact if compact else format_report_interval_long
    return formatter(interval["from"], interval["to"])


def _render_interval_summary(pyplot, inputs, destination):
    # Keep the established width/type scale while avoiding a mostly empty
    # canvas for this compact interval-only card.
    figure = pyplot.figure(figsize=(10, 4.6))
    axes = figure.add_axes((0.07, 0.08, 0.86, 0.84))
    axes.axis("off")
    statistics = inputs["age_statistics"]
    exact = len(inputs["rows"])
    ages = len(inputs["ages"])
    age_denominator = exact or 0
    age_pct = (ages * 100.0 / exact) if exact else None
    lines = [
        (SUMMARY_LAYOUT["title"], "Philosopher deaths", 22, "#1f3b53", True),
        (SUMMARY_LAYOUT["subtitle"], _interval_text(inputs), 12, "#555555", False),
        (SUMMARY_LAYOUT["section"], "EXACT DEATHS", 10, "#315b7d", True),
        (SUMMARY_LAYOUT["count"], str(exact), 25, "#111111", True),
        (SUMMARY_LAYOUT["ages"], "Exact ages available: {} / {} ({})".format(ages, age_denominator, compact_percentage(age_pct, missing="—")), 12, "#222222", False),
        (SUMMARY_LAYOUT["statistics"], "Median age: {}    Mean age: {}".format(median_age(statistics["median"], missing="—"), mean_age(statistics["mean"], missing="—")), 12, "#222222", False),
    ]
    extremes = inputs.get("age_extremes", {})
    youngest, oldest = extremes.get("youngest"), extremes.get("oldest")
    def extreme_text(label, value):
        if not isinstance(value, dict):
            return "{}: unavailable".format(label)
        name = value.get("display_title") or value.get("title")
        age = value.get("age")
        if not isinstance(name, str) or not name.strip() or not isinstance(age, (int, float)) or isinstance(age, bool):
            return "{}: unavailable".format(label)
        return "{}: {} — {}".format(label, name, median_age(age, missing="—"))
    lines.append((SUMMARY_LAYOUT["extremes"], "AGE EXTREMES", 10, "#315b7d", True))
    lines.append((SUMMARY_LAYOUT["youngest"], extreme_text("Youngest", youngest), 11, "#222222", False))
    lines.append((SUMMARY_LAYOUT["oldest"], extreme_text("Oldest", oldest), 11, "#222222", False))
    for y, text, size, color, bold in lines:
        axes.text(0.02, y, text, fontsize=size, color=color, fontweight="bold" if bold else "normal", va="top")
    try:
        return _atomic_figure_save(figure, destination), {
            "exact_deaths": exact,
            "exact_ages": ages,
            "age_coverage_pct": round(age_pct, 1) if age_pct is not None else None,
            "median_age": statistics["median"],
            "mean_age": statistics["mean"],
            "youngest": youngest,
            "oldest": oldest,
        }
    finally:
        pyplot.close(figure)


# Kept as a private compatibility alias for callers from the v2 renderer.
_render_summary_completeness = _render_interval_summary


def _month_starts(interval_from, interval_to):
    current = date(interval_from.year, interval_from.month, 1)
    end = date(interval_to.year, interval_to.month, 1)
    months = []
    while current <= end:
        months.append(current)
        if current.month == 12:
            current = date(current.year + 1, 1, 1)
        else:
            current = date(current.year, current.month + 1, 1)
    return months


def _render_deaths_by_month(pyplot, inputs, destination):
    interval_from = date.fromisoformat(inputs["interval"]["from"])
    interval_to = date.fromisoformat(inputs["interval"]["to"])
    months = _month_starts(interval_from, interval_to)
    counts = {month: 0 for month in months}
    for row in inputs["rows"]:
        value = date.fromisoformat(row["death_date"])
        month = date(value.year, value.month, 1)
        if month in counts:
            counts[month] += 1
    labels = [month.strftime("%b %Y") for month in months]
    values = [counts[month] for month in months]
    figure, axes = pyplot.subplots(figsize=(max(10, len(months) * 0.65), 6.25))
    try:
        axes.set_title("Confirmed philosopher deaths by month\n{}".format(_interval_text(inputs, compact=True)))
        if months:
            positions = list(range(len(months)))
            axes.bar(positions, values, width=0.7, color="#315b7d")
            axes.set_xticks(positions)
            axes.set_xticklabels(labels, rotation=45, ha="right")
            axes.set_xlabel("Month")
            axes.set_ylabel("Confirmed exact deaths")
            from matplotlib.ticker import MaxNLocator
            axes.yaxis.set_major_locator(MaxNLocator(integer=True))
            axes.set_ylim(bottom=0)
            axes.grid(axis="y", alpha=0.2)
        else:
            axes.axis("off")
            axes.text(0.5, 0.48, "No interval months available", ha="center", va="center", transform=axes.transAxes, fontsize=14)
        figure.tight_layout()
        return _atomic_figure_save(figure, destination), {"months": labels, "counts": values}
    finally:
        pyplot.close(figure)


def _render_median_age_by_year(pyplot, inputs, destination):
    yearly = inputs.get("yearly_statistics", [])
    years = [item["year"] for item in yearly]
    medians = [item.get("median_age") for item in yearly]
    sample_sizes = [item.get("exact_ages_available", 0) for item in yearly]
    figure, axes = pyplot.subplots(figsize=(10, 6.25))
    try:
        axes.set_title("Median exact age at death by year\n{}".format(_interval_text(inputs, compact=True)))
        positions = list(range(len(years)))
        points = [(x, median_value) for x, median_value in zip(positions, medians) if median_value is not None]
        if points:
            axes.plot([point[0] for point in points], [point[1] for point in points], marker="o", color="#315b7d")
            for x, value in points:
                axes.annotate("n={}".format(sample_sizes[x]), (x, value), xytext=(0, 8), textcoords="offset points", ha="center", fontsize=9)
            axes.set_ylabel("Median age (completed calendar years)")
            axes.set_ylim(bottom=0)
            axes.grid(axis="y", alpha=0.2)
        else:
            axes.axis("off")
            axes.text(0.5, 0.48, "No exact-age yearly data available", ha="center", va="center", transform=axes.transAxes, fontsize=14)
        if years:
            axes.set_xticks(positions)
            axes.set_xticklabels([str(year) for year in years])
            axes.set_xlabel("Year")
        figure.tight_layout()
        return _atomic_figure_save(figure, destination), {"years": years, "medians": medians, "sample_sizes": sample_sizes, "zero_data_policy": "render-insufficient-data-placeholder"}
    finally:
        pyplot.close(figure)


def _should_render_median_age_by_year(inputs, interval_from, interval_to):
    """Whether a yearly median chart has a meaningful observation window."""
    qualifying_medians = {
        item["year"] for item in inputs.get("yearly_statistics", [])
        if item.get("median_age") is not None
    }
    return (
        completed_calendar_months(interval_from, interval_to) >= 24
        and len(qualifying_medians) >= 2
    )


def _render_deaths_by_year(pyplot, inputs, destination):
    years = list(inputs["deaths_by_year"])
    counts = [inputs["deaths_by_year"][year] for year in years]
    figure, axes = pyplot.subplots(figsize=(10, 6.25))
    try:
        axes.set_title("Confirmed philosopher deaths\n{}".format(_interval_text(inputs, compact=True)))
        if years:
            positions = list(range(len(years)))
            axes.bar(positions, counts, width=0.7, color="#315b7d")
            axes.set_xticks(positions)
            axes.set_xticklabels(years)
            axes.set_xlabel("Year")
            axes.set_ylabel("Confirmed exact deaths")
            from matplotlib.ticker import MaxNLocator
            axes.yaxis.set_major_locator(MaxNLocator(integer=True))
            axes.set_ylim(bottom=0)
            axes.grid(axis="y", alpha=0.2)
        else:
            axes.axis("off")
            axes.text(
                0.5, 0.48, "No confirmed exact deaths in this interval",
                ha="center", va="center", transform=axes.transAxes, fontsize=14,
            )
        figure.tight_layout()
        return _atomic_figure_save(figure, destination), {
            "years": years, "counts": counts,
        }
    finally:
        pyplot.close(figure)


def _age_bin_edges(ages):
    if not ages:
        return []
    minimum = min(ages)
    maximum = max(ages)
    span = maximum - minimum
    width = 1 if span <= 20 else 5 if span <= 60 else 10
    start = (minimum // width) * width
    stop = ((maximum // width) + 1) * width
    return [value - 0.5 for value in range(start, stop + width, width)]


def _render_age_at_death(pyplot, inputs, destination):
    ages = inputs["ages"]
    statistics = inputs["age_statistics"]
    figure, axes = pyplot.subplots(figsize=(10, 6.25))
    try:
        title = "Exact age at death\n{}".format(_interval_text(inputs, compact=True))
        if ages:
            title += "\nn={} · mean {:.1f} · median {:g}".format(
                len(ages), statistics["mean"], statistics["median"],
            )
        axes.set_title(title, pad=12)
        edges = _age_bin_edges(ages)
        if ages:
            axes.hist(ages, bins=edges, color="#6c4675", edgecolor="white")
            axes.set_xlabel("Age at death (completed calendar years)")
            axes.set_ylabel("Philosophers")
            from matplotlib.ticker import MaxNLocator
            axes.xaxis.set_major_locator(MaxNLocator(integer=True))
            axes.yaxis.set_major_locator(MaxNLocator(integer=True))
            axes.set_ylim(bottom=0)
            axes.grid(axis="y", alpha=0.2)
        else:
            axes.axis("off")
            axes.text(
                0.5, 0.48, "Insufficient exact age data (n=0)",
                ha="center", va="center", transform=axes.transAxes, fontsize=14,
            )
        figure.tight_layout()
        return _atomic_figure_save(figure, destination), {
            "sample_size": len(ages),
            "mean": statistics["mean"],
            "median": statistics["median"],
            "bin_edges": edges,
            "zero_data_policy": "render-insufficient-data-placeholder",
        }
    finally:
        pyplot.close(figure)


def _render_individual_ages(pyplot, inputs, destination):
    rows = sorted(
        (row for row in inputs["rows"] if row["age_at_death"] is not None),
        key=lambda row: (
            row["age_at_death"],
            (row.get("display_title") or row["title"]).casefold(),
            row["title"],
            row["death_date"],
        ),
    )
    ages = [row["age_at_death"] for row in rows]
    labels = [row["display_title"] for row in rows]
    figure_height = max(4.5, 1.2 + len(rows) * 0.48)
    figure, axes = pyplot.subplots(figsize=(10, figure_height))
    try:
        axes.set_title("Individual exact ages at death\n{}".format(_interval_text(inputs, compact=True)))
        if ages:
            positions = list(range(len(ages)))
            axes.scatter(ages, positions, color="#6c4675", s=55, zorder=3)
            axes.set_yticks(positions)
            axes.set_yticklabels(labels)
            axes.invert_yaxis()
            axes.set_xlabel("Age at death (completed calendar years)")
            axes.set_ylabel("Philosopher")
            axes.grid(axis="x", alpha=0.2)
            for position, age in zip(positions, ages):
                axes.annotate(str(age), (age, position), xytext=(6, 0), textcoords="offset points", va="center", fontsize=9)
        else:
            axes.axis("off")
            axes.text(0.5, 0.48, "Insufficient exact age data (n=0)", ha="center", va="center", transform=axes.transAxes, fontsize=14)
        figure.tight_layout()
        return _atomic_figure_save(figure, destination), {
            "sample_size": len(ages),
            "ages": ages,
            "titles": labels,
            "mean": inputs["age_statistics"]["mean"],
            "median": inputs["age_statistics"]["median"],
            "zero_data_policy": "render-insufficient-data-placeholder",
        }
    finally:
        pyplot.close(figure)


def _wrapped_title(value):
    return textwrap.wrap(
        value, width=42, break_long_words=True, break_on_hyphens=False,
    ) or [value]


def _display_age(value):
    return "—" if value is None else str(value)


def _render_table_page(pyplot, inputs, rows, destination, page, total_pages):
    wrapped = [(_wrapped_title(row["display_title"]), row) for row in rows]
    row_units = [max(1, len(lines)) for lines, _ in wrapped]
    total_units = max(1, sum(row_units))
    # Reserve a fixed header region; only the body grows with wrapped rows.
    figure_height = 2.8 + total_units * 0.38
    figure = pyplot.figure(figsize=(11.5, figure_height))
    axes = figure.add_axes((0.035, 0.035, 0.93, 0.93))
    axes.set_xlim(0, 1)
    axes.set_ylim(0, 1)
    axes.axis("off")
    try:
        axes.text(0, TABLE_LAYOUT["title"], "Confirmed philosopher deaths", fontsize=18,
                  fontweight="bold", va="top")
        axes.text(
            0, TABLE_LAYOUT["subtitle"],
            "{} · page {} of {}".format(_interval_text(inputs), page, total_pages),
            fontsize=11, color="#444444", va="top",
        )
        header_top = TABLE_LAYOUT["header"]
        axes.add_patch(pyplot.Rectangle(
            (0, header_top - 0.04), 1, 0.045, color="#dfe8ef",
            transform=axes.transAxes,
        ))
        axes.text(0.015, header_top, "Death date", fontweight="bold", va="top")
        axes.text(0.205, header_top, "Philosopher", fontweight="bold", va="top")
        axes.text(0.97, header_top, "Age", fontweight="bold", ha="right", va="top")

        content_top = header_top - TABLE_LAYOUT["content_gap"]
        content_bottom = TABLE_LAYOUT["bottom"]
        unit_height = (content_top - content_bottom) / total_units
        current_y = content_top
        for index, ((lines, row), units) in enumerate(zip(wrapped, row_units)):
            row_height = unit_height * units
            if index % 2:
                axes.add_patch(pyplot.Rectangle(
                    (0, current_y - row_height + 0.003), 1,
                    max(0, row_height - 0.004), color="#f4f6f8",
                    transform=axes.transAxes,
                ))
            line_step = row_height / units
            text_y = current_y - line_step * 0.18
            axes.text(0.015, text_y, row["death_date"], fontsize=10.5, va="top")
            axes.text(
                0.205, text_y, "\n".join(lines), fontsize=10.5,
                va="top", linespacing=1.15,
            )
            age_text = _display_age(row["age_at_death"])
            axes.text(0.97, text_y, age_text, fontsize=10.5, ha="right", va="top")
            current_y -= row_height
        return _atomic_figure_save(figure, destination)
    finally:
        pyplot.close(figure)


def _asset_metadata(kind, filename, temporary_path, **extra):
    width, height = png_dimensions(temporary_path)
    return {
        "kind": kind,
        "filename": filename,
        "sha256": _sha256(temporary_path),
        "size_bytes": temporary_path.stat().st_size,
        "width": width,
        "height": height,
        **extra,
    }


def _atomic_json_write(value, destination):
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".render-manifest-", suffix=".tmp", dir=str(destination.parent),
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary_path), str(destination))
    except BaseException:
        if temporary_path.exists():
            temporary_path.unlink()
        raise


def _stable_source_reference(source_path, output_directory):
    relative = os.path.relpath(
        str(source_path.resolve()), str(output_directory.resolve())
    )
    return Path(relative).as_posix()


def default_output_directory(source_path):
    source_path = Path(source_path)
    return source_path.parent / "assets" / source_path.stem


def render_historical_death_report(
    source_path, *, output_directory=None, force=False,
    rows_per_page=DEFAULT_ROWS_PER_PAGE, pyplot=None,
):
    """Render one validated report and return ``(manifest, manifest_path)``."""
    if not _is_int(rows_per_page) or rows_per_page <= 0:
        raise DeathReportRenderError("rows_per_page must be a positive integer")
    source_path, source_bytes, inputs = load_historical_death_report(source_path)
    output_directory = Path(
        output_directory if output_directory is not None
        else default_output_directory(source_path)
    )
    if output_directory.exists() and not output_directory.is_dir():
        raise DeathReportRenderError("output path exists and is not a directory")
    if output_directory.exists() and any(output_directory.iterdir()) and not force:
        raise DeathReportRenderError(
            "output directory is not empty; use --force to regenerate"
        )
    output_was_absent = not output_directory.exists()
    output_directory.mkdir(parents=True, exist_ok=True)
    if pyplot is None:
        pyplot = _load_pyplot()

    pending = []
    assets = []
    try:
        filename = "interval-summary.png"
        temporary, semantic = _render_interval_summary(
            pyplot, inputs, output_directory / filename,
        )
        pending.append((temporary, output_directory / filename))
        assets.append(_asset_metadata(INTERVAL_SUMMARY_KIND, filename, temporary, **semantic))

        interval_from = date.fromisoformat(inputs["interval"]["from"])
        interval_to = date.fromisoformat(inputs["interval"]["to"])
        short_interval = completed_calendar_years(interval_from, interval_to) < 2
        if short_interval:
            filename = "deaths-by-month.png"
            render_frequency = _render_deaths_by_month
            frequency_kind = DEATHS_BY_MONTH_KIND
        else:
            filename = "deaths-by-year.png"
            render_frequency = _render_deaths_by_year
            frequency_kind = DEATHS_BY_YEAR_KIND
        temporary, semantic = render_frequency(pyplot, inputs, output_directory / filename)
        pending.append((temporary, output_directory / filename))
        assets.append(_asset_metadata(frequency_kind, filename, temporary, **semantic))

        if _should_render_median_age_by_year(inputs, interval_from, interval_to):
            filename = "median-age-by-year.png"
            temporary, semantic = _render_median_age_by_year(pyplot, inputs, output_directory / filename)
            pending.append((temporary, output_directory / filename))
            assets.append(_asset_metadata(MEDIAN_AGE_BY_YEAR_KIND, filename, temporary, **semantic))

        if len(inputs["ages"]) < 8:
            filename = "individual-ages.png"
            temporary, semantic = _render_individual_ages(pyplot, inputs, output_directory / filename)
            age_kind = INDIVIDUAL_AGES_KIND
        else:
            filename = "age-at-death.png"
            temporary, semantic = _render_age_at_death(pyplot, inputs, output_directory / filename)
            age_kind = AGE_AT_DEATH_KIND
        pending.append((temporary, output_directory / filename))
        assets.append(_asset_metadata(age_kind, filename, temporary, **semantic))

        rows = inputs["rows"]
        total_pages = max(1, int(math.ceil(len(rows) / float(rows_per_page))))
        for page in range(1, total_pages + 1):
            row_start_index = (page - 1) * rows_per_page
            page_rows = rows[row_start_index:row_start_index + rows_per_page]
            filename = "deaths-table-{:03d}.png".format(page)
            temporary = _render_table_page(
                pyplot, inputs, page_rows, output_directory / filename,
                page, total_pages,
            )
            pending.append((temporary, output_directory / filename))
            assets.append(_asset_metadata(
                DEATHS_TABLE_KIND, filename, temporary,
                page=page, total_pages=total_pages,
                row_start=row_start_index + 1 if page_rows else 0,
                row_end=row_start_index + len(page_rows) if page_rows else 0,
                row_count=len(page_rows),
                first_title=page_rows[0]["title"] if page_rows else None,
                last_title=page_rows[-1]["title"] if page_rows else None,
            ))

        if len(assets) > TELEGRAM_MEDIA_GROUP_MAX:
            raise DeathReportRenderError(
                "rendered report requires {} Telegram media items; maximum supported is {}".format(
                    len(assets), TELEGRAM_MEDIA_GROUP_MAX,
                )
            )

        manifest = {
            "operation": "historical-death-report-render",
            "renderer_version": RENDERER_VERSION,
            "source_report": _stable_source_reference(source_path, output_directory),
            "source_report_sha256": hashlib.sha256(source_bytes).hexdigest(),
            "interval": inputs["interval"],
            "exact_deaths": len(inputs["rows"]),
            "exact_ages": len(inputs["ages"]),
            "rows_per_page": rows_per_page,
            "assets": assets,
        }

        for temporary, destination in pending:
            os.replace(str(temporary), str(destination))
        pending = []
        _atomic_json_write(manifest, output_directory / "render-manifest.json")

        expected_files = {asset["filename"] for asset in assets}
        if force:
            for path in output_directory.iterdir():
                if path.is_file() and (
                    path.name.startswith("deaths-table-")
                    or path.name in {"summary-completeness.png", "interval-summary.png", "deaths-by-month.png", "deaths-by-year.png", "median-age-by-year.png", "individual-ages.png", "age-at-death.png"}
                ) and path.name not in expected_files:
                    path.unlink()
        return manifest, output_directory / "render-manifest.json"
    except BaseException:
        for temporary, _ in pending:
            if temporary.exists():
                temporary.unlink()
        if output_was_absent and output_directory.exists() and not any(output_directory.iterdir()):
            output_directory.rmdir()
        raise
