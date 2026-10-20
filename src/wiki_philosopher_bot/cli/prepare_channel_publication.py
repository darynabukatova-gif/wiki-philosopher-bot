"""Prepare a statistical-report channel publication without Telegram."""

import argparse
import json
import os
import tempfile
from pathlib import Path

from wiki_philosopher_bot.channel_publication_outbox import prepare_channel_publication
from wiki_philosopher_bot.config import CHANNEL_PUBLICATION_STATE_ROOT, REPORT_POST_STATE_ROOT


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Freeze an already-sent private statistical report for later channel publication."
    )
    parser.add_argument("--source-type", required=True)
    parser.add_argument("--source-id", help="exact report-post attempt UUID")
    parser.add_argument("--report-post-root", default=REPORT_POST_STATE_ROOT)
    parser.add_argument("--state-root", default=CHANNEL_PUBLICATION_STATE_ROOT)
    parser.add_argument("--result-json", help="write machine-readable result JSON")
    return parser.parse_args(argv)


def _write_result(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".channel-publication-result-", suffix=".tmp", dir=str(path.parent),
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, str(path))
        temporary_name = None
    finally:
        if temporary_name and os.path.exists(temporary_name):
            os.unlink(temporary_name)


def main(argv=None):
    args = parse_args(argv)
    result = prepare_channel_publication(
        source_type=args.source_type,
        source_id=args.source_id,
        report_post_root=args.report_post_root,
        state_root=args.state_root,
    )
    report = result.as_report()
    if args.result_json:
        _write_result(args.result_json, report)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
