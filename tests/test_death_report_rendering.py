import copy
import hashlib
import json
from collections import Counter
from statistics import median

import pytest

import wiki_philosopher_bot.cache as cache
import wiki_philosopher_bot.telegram_bot as telegram_bot
import wiki_philosopher_bot.wikipedia_api as wikipedia_api
from wiki_philosopher_bot.cli import render_death_report as render_cli
from wiki_philosopher_bot.death_report_rendering import (
    DeathReportRenderError,
    _display_age,
    load_historical_death_report,
    png_dimensions,
    render_historical_death_report,
    validate_historical_death_report,
)


def row(title, death_date, age=None, display_title=None):
    return {
        "title": title,
        "display_title": display_title if display_title is not None else title,
        "qid": "Q1",
        "birth_year": None,
        "birth_date": None,
        "death_year": int(death_date[:4]),
        "death_date": death_date,
        "age_at_death": age,
    }


def report(rows):
    rows = list(rows)
    ages = [item["age_at_death"] for item in rows if item["age_at_death"] is not None]
    years = Counter(item["death_date"][:4] for item in rows)
    return {
        "operation": "historical-death-report",
        "mode": "read-only",
        "generated_at": "2026-09-26T12:00:00Z",
        "database_sha256": "a" * 64,
        "interval": {
            "from": "2019-09-26", "to": "2026-09-26",
            "inclusive": True, "source": "explicit",
        },
        "population": {},
        "date_completeness": {},
        "interval_results": {
            "exact_deaths": len(rows), "rows": rows,
            "year_only_potential_matches": 0,
        },
        "deaths_by_year": {year: years[year] for year in sorted(years)},
        "age_statistics": {
            "exact_ages_available": len(ages),
            "insufficient_age_data": len(rows) - len(ages),
            "mean": sum(ages) / len(ages) if ages else None,
            "median": median(ages) if ages else None,
            "minimum": min(ages) if ages else None,
            "maximum": max(ages) if ages else None,
        },
    }


def write_report(path, rows):
    value = report(rows)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    return value


def assert_png(path):
    assert path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    width, height = png_dimensions(path)
    assert width > 0
    assert height > 0


def test_valid_report_renders_and_source_is_unchanged(tmp_path):
    source = tmp_path / "source.json"
    write_report(source, [
        row("Alpha", "2024-01-01", 80),
        row("Beta", "2025-01-01", None),
    ])
    before = source.read_bytes()

    manifest, manifest_path = render_historical_death_report(
        source, output_directory=tmp_path / "assets",
    )

    assert source.read_bytes() == before
    assert manifest["operation"] == "historical-death-report-render"
    assert manifest["renderer_version"] == 1
    assert manifest["exact_deaths"] == 2
    assert manifest["exact_ages"] == 1
    assert manifest_path.name == "render-manifest.json"
    for asset in manifest["assets"]:
        asset_path = manifest_path.parent / asset["filename"]
        assert_png(asset_path)
        assert asset["sha256"] == hashlib.sha256(asset_path.read_bytes()).hexdigest()
        assert asset["size_bytes"] == asset_path.stat().st_size
        assert (asset["width"], asset["height"]) == png_dimensions(asset_path)


def test_manifest_records_source_hash_and_report_relative_path(tmp_path):
    source = tmp_path / "report.json"
    write_report(source, [row("Alpha", "2024-01-01", 80)])
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()

    manifest, _ = render_historical_death_report(
        source, output_directory=tmp_path / "assets",
    )

    assert manifest["source_report_sha256"] == source_hash
    assert not manifest["source_report"].startswith("/")
    assert manifest["source_report"] == "../report.json"


def test_year_chart_uses_exact_report_mapping(tmp_path):
    source = tmp_path / "report.json"
    write_report(source, [
        row("A", "2024-01-01", 70),
        row("B", "2024-02-01", 71),
        row("C", "2025-01-01", 72),
    ])
    manifest, _ = render_historical_death_report(
        source, output_directory=tmp_path / "assets",
    )
    asset = next(item for item in manifest["assets"] if item["kind"] == "deaths-by-year")
    assert asset["years"] == ["2024", "2025"]
    assert asset["counts"] == [2, 1]


def test_age_chart_uses_only_non_null_exact_ages_and_never_inferrs(tmp_path):
    source = tmp_path / "report.json"
    write_report(source, [
        row("Known", "2024-01-01", 80),
        row("Missing", "2025-01-01", None),
    ])
    manifest, _ = render_historical_death_report(
        source, output_directory=tmp_path / "assets",
    )
    asset = next(item for item in manifest["assets"] if item["kind"] == "age-at-death")
    assert asset["sample_size"] == 1
    assert asset["mean"] == 80
    assert manifest["exact_ages"] == 1


def test_zero_age_data_produces_labelled_placeholder_asset(tmp_path):
    source = tmp_path / "report.json"
    write_report(source, [row("Missing", "2025-01-01", None)])
    manifest, manifest_path = render_historical_death_report(
        source, output_directory=tmp_path / "assets",
    )
    asset = next(item for item in manifest["assets"] if item["kind"] == "age-at-death")
    assert asset["sample_size"] == 0
    assert asset["zero_data_policy"] == "render-insufficient-data-placeholder"
    assert_png(manifest_path.parent / asset["filename"])


def test_single_year_chart_and_empty_interval_render(tmp_path):
    single = tmp_path / "single.json"
    write_report(single, [row("Only", "2024-01-01", 80)])
    single_manifest, _ = render_historical_death_report(
        single, output_directory=tmp_path / "single-assets",
    )
    year_asset = next(item for item in single_manifest["assets"] if item["kind"] == "deaths-by-year")
    assert year_asset["years"] == ["2024"]
    assert year_asset["counts"] == [1]

    empty = tmp_path / "empty.json"
    write_report(empty, [])
    empty_manifest, empty_manifest_path = render_historical_death_report(
        empty, output_directory=tmp_path / "empty-assets",
    )
    assert empty_manifest["exact_deaths"] == 0
    assert len(empty_manifest["assets"]) == 3
    table = next(item for item in empty_manifest["assets"] if item["kind"] == "deaths-table")
    assert table["row_count"] == 0
    assert table["row_start"] == 0
    assert table["row_end"] == 0
    for asset in empty_manifest["assets"]:
        assert_png(empty_manifest_path.parent / asset["filename"])


def test_table_pagination_retains_report_order_and_boundaries(tmp_path):
    source = tmp_path / "report.json"
    rows = [
        row("Title {:02d}".format(index), "2024-01-{:02d}".format(index), 60 + index)
        for index in range(1, 8)
    ]
    write_report(source, rows)
    manifest, _ = render_historical_death_report(
        source, output_directory=tmp_path / "assets", rows_per_page=3,
    )
    tables = [item for item in manifest["assets"] if item["kind"] == "deaths-table"]
    assert [(item["page"], item["row_start"], item["row_end"], item["row_count"])
            for item in tables] == [(1, 1, 3, 3), (2, 4, 6, 3), (3, 7, 7, 1)]
    assert [(item["first_title"], item["last_title"]) for item in tables] == [
        ("Title 01", "Title 03"),
        ("Title 04", "Title 06"),
        ("Title 07", "Title 07"),
    ]


def test_long_and_unicode_names_render_and_missing_age_has_explicit_marker(tmp_path):
    source = tmp_path / "report.json"
    long_name = "René Žižek and an Exceptionally Long Philosophical Display Name That Must Wrap"
    write_report(source, [row("Canonical", "2024-01-01", None, long_name)])
    manifest, manifest_path = render_historical_death_report(
        source, output_directory=tmp_path / "assets",
    )
    table = next(item for item in manifest["assets"] if item["kind"] == "deaths-table")
    assert table["first_title"] == "Canonical"
    assert_png(manifest_path.parent / table["filename"])
    assert _display_age(None) == "—"
    assert _display_age(81) == "81"


def test_validation_does_not_mutate_report_and_rejects_incompatible_data():
    value = report([row("Alpha", "2024-01-01", 80)])
    before = copy.deepcopy(value)
    normalized = validate_historical_death_report(value)
    assert value == before
    assert normalized["rows"][0]["title"] == "Alpha"

    invalid = copy.deepcopy(value)
    invalid["operation"] = "other"
    with pytest.raises(DeathReportRenderError, match="operation"):
        validate_historical_death_report(invalid)

    invalid = copy.deepcopy(value)
    invalid["interval_results"]["rows"][0]["age_at_death"] = "80"
    with pytest.raises(DeathReportRenderError, match="age_at_death"):
        validate_historical_death_report(invalid)

    invalid = copy.deepcopy(value)
    invalid["deaths_by_year"] = {"2024": 2}
    with pytest.raises(DeathReportRenderError, match="deaths_by_year"):
        validate_historical_death_report(invalid)


def test_unsorted_rows_are_rejected_instead_of_silently_reordered():
    value = report([
        row("Later", "2025-01-01", 80),
        row("Earlier", "2024-01-01", 81),
    ])
    with pytest.raises(DeathReportRenderError, match="retain"):
        validate_historical_death_report(value)


def test_existing_destination_refused_without_force_and_force_regenerates(tmp_path):
    source = tmp_path / "report.json"
    rows = [
        row("Title {}".format(index), "2024-01-{:02d}".format(index), 70)
        for index in range(1, 6)
    ]
    write_report(source, rows)
    destination = tmp_path / "assets"
    render_historical_death_report(
        source, output_directory=destination, rows_per_page=2,
    )
    assert (destination / "deaths-table-003.png").exists()
    with pytest.raises(DeathReportRenderError, match="not empty"):
        render_historical_death_report(source, output_directory=destination)

    write_report(source, [row("Only", "2024-01-01", 70)])
    manifest, _ = render_historical_death_report(
        source, output_directory=destination, rows_per_page=2, force=True,
    )
    assert len([item for item in manifest["assets"] if item["kind"] == "deaths-table"]) == 1
    assert not (destination / "deaths-table-002.png").exists()
    assert not (destination / "deaths-table-003.png").exists()


def test_renderer_does_not_read_database_backup_network_or_telegram(
    monkeypatch, tmp_path,
):
    source = tmp_path / "report.json"
    write_report(source, [row("Alpha", "2024-01-01", 80)])

    def forbidden(*args, **kwargs):
        raise AssertionError("unrelated external or canonical operation attempted")

    monkeypatch.setattr(cache, "load_database", forbidden)
    monkeypatch.setattr(cache, "create_database_backup", forbidden)
    monkeypatch.setattr(wikipedia_api, "get_wikidata_entities_batch", forbidden)
    monkeypatch.setattr(telegram_bot, "send_message_to_chat", forbidden)

    render_historical_death_report(source, output_directory=tmp_path / "assets")


def test_renderer_uses_headless_matplotlib_backend(tmp_path):
    source = tmp_path / "report.json"
    write_report(source, [row("Alpha", "2024-01-01", 80)])
    render_historical_death_report(source, output_directory=tmp_path / "assets")
    import matplotlib
    assert "agg" in matplotlib.get_backend().lower()


def test_cli_renders_and_prints_concise_summary(tmp_path, capsys):
    source = tmp_path / "report.json"
    write_report(source, [row("Alpha", "2024-01-01", 80)])
    destination = tmp_path / "assets"
    assert render_cli.main([
        str(source), "--output-dir", str(destination),
    ]) == 0
    output = capsys.readouterr().out
    assert "Historical death report rendered" in output
    assert "Assets: 3" in output
    assert "deaths-by-year.png" in output
    assert (destination / "render-manifest.json").exists()


def test_load_rejects_malformed_json(tmp_path):
    source = tmp_path / "bad.json"
    source.write_text("not json")
    with pytest.raises(DeathReportRenderError, match="UTF-8 JSON"):
        load_historical_death_report(source)
