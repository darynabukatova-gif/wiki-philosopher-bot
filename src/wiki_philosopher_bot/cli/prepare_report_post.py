"""Freeze a rendered historical-death report into a pending outbox bundle."""
import argparse, json, os, tempfile
from pathlib import Path
from wiki_philosopher_bot.config import REPORT_POST_STATE_ROOT
from wiki_philosopher_bot.report_post_outbox import prepare_report_post

def parse_args(argv=None):
    p=argparse.ArgumentParser(description="Prepare one durable historical-death report post without Telegram.")
    p.add_argument("--report",required=True); p.add_argument("--manifest",required=True); p.add_argument("--state-root",default=REPORT_POST_STATE_ROOT)
    p.add_argument("--result-json",help="write the machine-readable result to this path")
    return p.parse_args(argv)
def _write(path,value):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True); fd,name=tempfile.mkstemp(dir=str(path.parent),prefix=".report-post-result-",suffix=".tmp")
    try:
        with os.fdopen(fd,"w",encoding="utf-8") as h: json.dump(value,h,ensure_ascii=False,indent=2); h.write("\n"); h.flush(); os.fsync(h.fileno())
        os.replace(name,str(path)); name=None
    finally:
        if name and os.path.exists(name): os.unlink(name)
def main(argv=None):
    a=parse_args(argv); result=prepare_report_post(a.report,a.manifest,a.state_root); report=result.as_report()
    if a.result_json: _write(a.result_json,report)
    print(json.dumps(report,ensure_ascii=False,indent=2)); return 0 if result.ok else 1
if __name__=="__main__": raise SystemExit(main())
