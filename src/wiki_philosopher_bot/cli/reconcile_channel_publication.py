"""Read-only inspection and explicit reconciliation for channel publications."""
import argparse, json
from wiki_philosopher_bot.channel_publication_outbox import reconcile_channel_publication
from wiki_philosopher_bot.config import CHANNEL_PUBLICATION_STATE_ROOT

def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Inspect or reconcile a channel publication.")
    parser.add_argument("operation", choices=("show", "mark-sent", "cancel"))
    parser.add_argument("--publication-id", required=True)
    parser.add_argument("--state-root", default=CHANNEL_PUBLICATION_STATE_ROOT)
    parser.add_argument("--telegram-message-id", action="append", type=int, dest="message_ids")
    parser.add_argument("--note")
    parser.add_argument("--confirm-unsafe", action="store_true")
    parser.add_argument("--show-message", action="store_true")
    return parser.parse_args(argv)

def main(argv=None):
    args = parse_args(argv)
    result = reconcile_channel_publication(args.publication_id, args.state_root, args.operation,
        message_ids=args.message_ids, note=args.note, confirm_unsafe=args.confirm_unsafe, show_message=args.show_message)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("ok") else 1

if __name__ == "__main__": raise SystemExit(main())
