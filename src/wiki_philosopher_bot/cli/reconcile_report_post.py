"""Inspect or explicitly reconcile a historical report-post attempt."""
import argparse, json
from wiki_philosopher_bot.config import REPORT_POST_STATE_ROOT
from wiki_philosopher_bot.report_post_outbox import locate_attempt, reconcile_report_post

def parse_args(argv=None):
    p=argparse.ArgumentParser(description="Inspect or reconcile a report-post outbox attempt."); p.add_argument("--state-root",default=REPORT_POST_STATE_ROOT)
    sub=p.add_subparsers(dest="operation",required=True)
    show=sub.add_parser("show"); show.add_argument("--attempt-id",required=True); show.add_argument("--show-caption",action="store_true")
    sent=sub.add_parser("mark-sent"); sent.add_argument("--attempt-id",required=True); sent.add_argument("--telegram-message-id",type=int,action="append",required=True); sent.add_argument("--note",required=True)
    cancel=sub.add_parser("cancel"); cancel.add_argument("--attempt-id",required=True); cancel.add_argument("--note",required=True); cancel.add_argument("--confirm-unsafe",action="store_true")
    return p.parse_args(argv)
def main(argv=None):
    a=parse_args(argv)
    if a.operation=="show":
        located=locate_attempt(a.state_root,a.attempt_id)
        if located is None: print(json.dumps({"ok":False,"error_kind":"not_found"})); return 1
        _,attempt=located; keys=("attempt_id","kind","created_at","state","state_changed_at","source_report_sha256","render_manifest_sha256","posting_fingerprint","content_fingerprint","interval","exact_deaths","exact_ages","media","telegram_message_ids","error_kind","error_summary","resolution_note")
        safe={k:attempt.get(k) for k in keys}
        if a.show_caption: safe["caption_html"]=attempt["caption_html"]
        print(json.dumps(safe,ensure_ascii=False,indent=2)); return 0
    result=reconcile_report_post(a.attempt_id,a.state_root,a.operation,message_ids=getattr(a,"telegram_message_id",None),note=a.note,confirm_unsafe=getattr(a,"confirm_unsafe",False))
    print(json.dumps(result.as_report(),ensure_ascii=False,indent=2)); return 0 if result.ok else 1
if __name__=="__main__": raise SystemExit(main())
