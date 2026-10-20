"""Dispatch one frozen channel-publication bundle."""
import argparse, json, os, tempfile
from pathlib import Path
from wiki_philosopher_bot.channel_publication_outbox import dispatch_channel_publication
from wiki_philosopher_bot.config import CHANNEL_PUBLICATION_STATE_ROOT

def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Dispatch one prepared channel publication.")
    parser.add_argument("--publication-id", required=True)
    parser.add_argument("--state-root", default=CHANNEL_PUBLICATION_STATE_ROOT)
    parser.add_argument("--confirm-pending-dispatch", action="store_true")
    parser.add_argument("--result-json")
    return parser.parse_args(argv)

def _write(path, value):
    if not path: return
    target = Path(path); target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".channel-dispatch-", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2); handle.write("\n"); handle.flush(); os.fsync(handle.fileno())
        os.replace(name, target)
    finally:
        if os.path.exists(name): os.unlink(name)

def main(argv=None):
    args = parse_args(argv)
    result = dispatch_channel_publication(args.publication_id, args.state_root, confirmed=args.confirm_pending_dispatch)
    report = result.as_report(); _write(args.result_json, report)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.ok else 1

if __name__ == "__main__": raise SystemExit(main())
