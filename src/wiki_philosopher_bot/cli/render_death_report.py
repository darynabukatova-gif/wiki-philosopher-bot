"""CLI for rendering historical-death report JSON into PNG assets."""

import argparse
from pathlib import Path

from wiki_philosopher_bot.death_report_rendering import (
    DEFAULT_ROWS_PER_PAGE,
    DeathReportRenderError,
    render_historical_death_report,
)


def _positive_rows(value):
    try:
        result = int(value)
    except (TypeError, ValueError) as error:
        raise argparse.ArgumentTypeError(
            "rows per page must be a positive integer"
        ) from error
    if result <= 0:
        raise argparse.ArgumentTypeError(
            "rows per page must be a positive integer"
        )
    return result


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Render a historical philosopher death report into PNG assets."
    )
    parser.add_argument("report", help="existing historical-death report JSON")
    parser.add_argument("--output-dir")
    parser.add_argument(
        "--rows-per-page", type=_positive_rows, default=DEFAULT_ROWS_PER_PAGE,
    )
    parser.add_argument("--force", action="store_true")
    return parser.parse_args(argv)


def format_summary(manifest, manifest_path):
    interval = manifest["interval"]
    lines = [
        "Historical death report rendered",
        "Source: {}".format(manifest["source_report"]),
        "Interval: {} through {}".format(interval["from"], interval["to"]),
        "Exact deaths: {}".format(manifest["exact_deaths"]),
        "Exact ages: {}".format(manifest["exact_ages"]),
        "Assets: {}".format(len(manifest["assets"])),
        "Output: {}".format(Path(manifest_path).parent),
    ]
    lines.extend(
        "- {}".format(asset["filename"]) for asset in manifest["assets"]
    )
    lines.append("- {}".format(Path(manifest_path).name))
    return "\n".join(lines)


def main(argv=None):
    args = parse_args(argv)
    try:
        manifest, manifest_path = render_historical_death_report(
            args.report,
            output_directory=args.output_dir,
            force=args.force,
            rows_per_page=args.rows_per_page,
        )
    except (DeathReportRenderError, OSError, RuntimeError) as error:
        raise SystemExit(str(error))
    print(format_summary(manifest, manifest_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
