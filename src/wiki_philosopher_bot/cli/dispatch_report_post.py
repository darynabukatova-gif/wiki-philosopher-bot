"""Dispatch one externally checkpointed report-post bundle exactly once."""
import argparse, json
from wiki_philosopher_bot.config import REPORT_POST_STATE_ROOT, get_report_telegram_media_group_settings, load_environment
from wiki_philosopher_bot.cli.prepare_report_post import _write
from wiki_philosopher_bot.report_post_outbox import dispatch_report_post

def parse_args(argv=None):
    p=argparse.ArgumentParser(description="Dispatch one exact checkpointed historical report media group.")
    p.add_argument("--attempt-id",required=True); p.add_argument("--state-root",default=REPORT_POST_STATE_ROOT)
    p.add_argument("--confirm-pending-dispatch",action="store_true",help="confirm this newly checkpointed pending attempt is safe to dispatch")
    p.add_argument("--result-json"); return p.parse_args(argv)
def main(argv=None):
    a=parse_args(argv); load_environment(); url,chat=get_report_telegram_media_group_settings()
    result=dispatch_report_post(a.attempt_id,a.state_root,url,chat,confirmed=a.confirm_pending_dispatch); report=result.as_report()
    if a.result_json: _write(a.result_json,report)
    print(json.dumps(report,ensure_ascii=False,indent=2)); return 0 if result.ok else 1
if __name__=="__main__": raise SystemExit(main())
