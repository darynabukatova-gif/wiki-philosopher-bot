"""Durable, file-backed outbox for rendered historical-death reports."""
from dataclasses import dataclass, field
from datetime import datetime, timezone, date
from pathlib import Path
from typing import List, Optional
import hashlib
import html
import json
import os
import shutil
import tempfile
import requests
import uuid

from wiki_philosopher_bot.config import TELEGRAM_CAPTION_MAX_LENGTH, TELEGRAM_MEDIA_GROUP_MAX_ITEMS
from wiki_philosopher_bot.death_report_rendering import validate_historical_death_report, png_dimensions
from wiki_philosopher_bot.telegram_bot import (TELEGRAM_OUTCOME_CONFIRMED_SUCCESS,
    TELEGRAM_OUTCOME_DEFINITE_REJECTION, send_media_group_to_chat)

SCHEMA_VERSION = 1
KIND = "historical-death-report"
UNRESOLVED_STATES = frozenset(("pending", "failed", "unknown"))
STATES = frozenset(("pending", "sent", "failed", "unknown", "cancelled"))
TRANSITIONS = {"pending": frozenset(("sent", "failed", "unknown", "cancelled")),
               "failed": frozenset(("cancelled",)), "unknown": frozenset(("sent", "cancelled")),
               "sent": frozenset(), "cancelled": frozenset()}
SHA_LENGTH = 64

class ReportPostError(ValueError):
    pass

@dataclass
class ReportPostResult:
    ok: bool
    operation: str
    attempt_id: Optional[str] = None
    starting_state: Optional[str] = None
    ending_state: Optional[str] = None
    telegram_called: bool = False
    telegram_message_ids: List[int] = field(default_factory=list)
    error_kind: Optional[str] = None
    error_summary: Optional[str] = None
    persistence_succeeded: bool = False
    manual_reconciliation_required: bool = False
    posting_fingerprint: Optional[str] = None
    content_fingerprint: Optional[str] = None
    bundle_path: Optional[str] = None
    def as_report(self): return self.__dict__.copy()

def _is_int(value): return isinstance(value, int) and not isinstance(value, bool)
def _sha_bytes(value): return hashlib.sha256(value).hexdigest()
def _sha_file(path): return _sha_bytes(Path(path).read_bytes())
def _is_sha(value): return isinstance(value, str) and len(value) == SHA_LENGTH and all(c in "0123456789abcdef" for c in value)
def _timestamp(now=None):
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None: value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
def _valid_uuid(value):
    try: return str(uuid.UUID(value)) == value
    except (ValueError, AttributeError): return False

def _write_bytes(path, data):
    with Path(path).open("wb") as handle:
        handle.write(data); handle.flush(); os.fsync(handle.fileno())

def _atomic_json(path, value):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".attempt-", suffix=".tmp", dir=str(path.parent))
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=False); handle.write("\n"); handle.flush(); os.fsync(handle.fileno())
        os.replace(str(temporary), str(path))
    finally:
        if temporary.exists(): temporary.unlink()

def _parse_iso_date(value, field):
    try: return date.fromisoformat(value)
    except (TypeError, ValueError): raise ReportPostError("{} must be an ISO date".format(field))

def _safe_filename(value):
    return isinstance(value, str) and value and Path(value).name == value and value not in (".", "..")

def validate_attempt(attempt):
    if not isinstance(attempt, dict) or attempt.get("schema_version") != SCHEMA_VERSION or attempt.get("kind") != KIND:
        raise ReportPostError("unsupported report-post attempt schema")
    if not _valid_uuid(attempt.get("attempt_id")): raise ReportPostError("attempt_id must be a canonical UUID")
    state = attempt.get("state")
    if state not in STATES: raise ReportPostError("invalid report-post state")
    for key in (
        "source_report_sha256", "render_manifest_sha256", "posting_fingerprint",
        "content_fingerprint",
    ):
        if not _is_sha(attempt.get(key)): raise ReportPostError("{} must be SHA-256".format(key))
    interval = attempt.get("interval")
    if not isinstance(interval, dict) or interval.get("inclusive") is not True: raise ReportPostError("invalid attempt interval")
    if _parse_iso_date(interval.get("from"), "interval.from") > _parse_iso_date(interval.get("to"), "interval.to"): raise ReportPostError("invalid attempt interval order")
    for key in ("exact_deaths", "exact_ages"):
        if not _is_int(attempt.get(key)) or attempt[key] < 0:
            raise ReportPostError("{} must be a non-negative integer".format(key))
    if attempt["exact_ages"] > attempt["exact_deaths"]:
        raise ReportPostError("exact_ages cannot exceed exact_deaths")
    caption = attempt.get("caption_html")
    if not isinstance(caption, str) or not caption or len(caption) > TELEGRAM_CAPTION_MAX_LENGTH: raise ReportPostError("invalid Telegram caption")
    media = attempt.get("media")
    if not isinstance(media, list) or not 2 <= len(media) <= TELEGRAM_MEDIA_GROUP_MAX_ITEMS: raise ReportPostError("media group must contain 2 to {} assets".format(TELEGRAM_MEDIA_GROUP_MAX_ITEMS))
    names = set()
    for index, item in enumerate(media, 1):
        if not isinstance(item, dict) or item.get("position") != index or not _safe_filename(item.get("filename")) or not _is_sha(item.get("sha256")): raise ReportPostError("invalid ordered media entry")
        if item["filename"] in names: raise ReportPostError("duplicate media filename")
        names.add(item["filename"])
    if media[0].get("kind") != "deaths-by-year" or media[1].get("kind") != "age-at-death":
        raise ReportPostError("media must begin with year and age charts")
    tables = media[2:]
    if not tables or any(item.get("kind") != "deaths-table" for item in tables):
        raise ReportPostError("media must end with death-table pages")
    if [item.get("page") for item in tables] != list(range(1, len(tables) + 1)):
        raise ReportPostError("attempt table pages must be contiguous")
    ids = attempt.get("telegram_message_ids")
    if not isinstance(ids, list) or any(not _is_int(x) or x <= 0 for x in ids): raise ReportPostError("invalid Telegram message IDs")
    if state == "sent" and len(ids) != len(media): raise ReportPostError("sent attempt requires a complete message-ID list")
    if state != "sent" and ids: raise ReportPostError("non-sent attempt cannot contain message IDs")
    for key in ("created_at", "state_changed_at"):
        value = attempt.get(key)
        if not isinstance(value, str) or not value: raise ReportPostError("{} is required".format(key))
    for key, limit in (("error_kind", 100), ("error_summary", 500), ("resolution_note", 2000)):
        value = attempt.get(key)
        if value is not None and (not isinstance(value, str) or not value.strip() or len(value) > limit):
            raise ReportPostError("{} is invalid".format(key))
    return attempt

def load_attempt(bundle):
    path = Path(bundle) / "attempt.json"
    try: value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error: raise ReportPostError("attempt.json is unreadable or malformed") from error
    return validate_attempt(value)

def list_attempts(state_root):
    root = Path(state_root)
    if not root.exists(): return []
    attempts=[]
    for child in sorted(root.iterdir()):
        if child.is_dir() and not child.name.startswith("."):
            attempts.append((child, load_attempt(child)))
    return attempts

def _validate_manifest(report_path, manifest_path):
    report_path=Path(report_path); manifest_path=Path(manifest_path)
    report_bytes=report_path.read_bytes(); manifest_bytes=manifest_path.read_bytes()
    try: report=json.loads(report_bytes.decode("utf-8")); manifest=json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error: raise ReportPostError("report or manifest is not valid UTF-8 JSON") from error
    try: normalized=validate_historical_death_report(report)
    except ValueError as error: raise ReportPostError(str(error)) from error
    if not isinstance(manifest, dict) or manifest.get("operation") != "historical-death-report-render" or manifest.get("renderer_version") != 1: raise ReportPostError("unsupported render manifest")
    report_sha=_sha_bytes(report_bytes)
    if manifest.get("source_report_sha256") != report_sha: raise ReportPostError("manifest source report SHA-256 does not match")
    if manifest.get("interval") != normalized["interval"] or manifest.get("exact_deaths") != len(normalized["rows"]) or manifest.get("exact_ages") != len(normalized["ages"]): raise ReportPostError("manifest summary does not match report")
    assets=manifest.get("assets")
    if not isinstance(assets, list): raise ReportPostError("manifest assets must be a list")
    base=manifest_path.parent.resolve(); checked=[]; names=set(); charts={"deaths-by-year":0,"age-at-death":0}; tables=[]
    for asset in assets:
        if not isinstance(asset, dict) or not _safe_filename(asset.get("filename")) or not _is_sha(asset.get("sha256")): raise ReportPostError("malformed manifest asset")
        name=asset["filename"]
        if name in names: raise ReportPostError("duplicate manifest filename")
        names.add(name); path=(base/name).resolve()
        if path.parent != base: raise ReportPostError("manifest asset escapes render directory")
        if not path.is_file(): raise ReportPostError("manifest asset is missing: {}".format(name))
        data=path.read_bytes()
        if _sha_bytes(data) != asset["sha256"]: raise ReportPostError("manifest asset SHA-256 mismatch: {}".format(name))
        try: width,height=png_dimensions(path)
        except ValueError as error: raise ReportPostError("invalid PNG asset: {}".format(name)) from error
        if asset.get("width") != width or asset.get("height") != height or asset.get("size_bytes") != len(data): raise ReportPostError("manifest asset metadata mismatch: {}".format(name))
        kind=asset.get("kind")
        if kind in charts: charts[kind]+=1
        elif kind == "deaths-table": tables.append(asset)
        else: raise ReportPostError("unsupported manifest asset kind")
        checked.append((asset,path,data))
    if charts != {"deaths-by-year":1,"age-at-death":1}: raise ReportPostError("manifest must contain exactly one year and age chart")
    if not tables: raise ReportPostError("manifest must contain table pages")
    pages=sorted(asset.get("page") for asset in tables)
    if pages != list(range(1,len(tables)+1)) or any(asset.get("total_pages") != len(tables) for asset in tables): raise ReportPostError("table pages must be contiguous")
    order={"deaths-by-year":0,"age-at-death":1}
    checked.sort(key=lambda row: (order.get(row[0]["kind"],2), row[0].get("page",0)))
    return report, normalized, manifest, report_bytes, manifest_bytes, checked

def _caption(normalized):
    interval=normalized["interval"]; stats=normalized["age_statistics"]
    def number(value): return "unavailable" if value is None else ("{:.1f}".format(value) if isinstance(value,float) and not value.is_integer() else "{:g}".format(value))
    text="\n".join(("<b>Philosopher deaths: {}–{}</b>".format(html.escape(interval["from"]),html.escape(interval["to"])),
        "Confirmed exact deaths: {}".format(len(normalized["rows"])), "Exact ages available: {}".format(len(normalized["ages"])),
        "Mean age: {}".format(number(stats["mean"])), "Median age: {}".format(number(stats["median"]))))
    if len(text)>TELEGRAM_CAPTION_MAX_LENGTH: raise ReportPostError("caption exceeds Telegram limit")
    return text

def _fingerprint(report_sha, manifest_sha, caption, media):
    value={"source_report_sha256":report_sha,"render_manifest_sha256":manifest_sha,"caption_html":caption,
           "media":[{"filename":x["filename"],"kind":x["kind"],"sha256":x["sha256"]} for x in media]}
    return _sha_bytes(json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode("utf-8"))

def content_fingerprint(normalized_report):
    """Return stable identity for normalized statistical report content.

    This deliberately excludes report-generation metadata and all frozen bundle
    byte hashes.  Those values remain protected separately by
    ``posting_fingerprint`` and dispatch-time bundle verification.
    """
    value = {
        "kind": KIND,
        "interval": normalized_report["interval"],
        "exact_death_rows": normalized_report["rows"],
        "deaths_by_year": normalized_report["deaths_by_year"],
        "age_statistics": normalized_report["age_statistics"],
    }
    serialized = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )
    return _sha_bytes(serialized.encode("utf-8"))


def prepare_report_post(report_path, manifest_path, state_root, attempt_id=None, now=None, publish=os.replace):
    try:
        report, normalized, manifest, report_bytes, manifest_bytes, assets=_validate_manifest(report_path,manifest_path)
        existing=list_attempts(state_root)
        blockers=[a for _,a in existing if a["state"] in UNRESOLVED_STATES]
        if blockers: raise ReportPostError("unresolved report-post attempt blocks preparation")
        caption=_caption(normalized)
        media=[{"position":i,"kind":a["kind"],"filename":a["filename"],"sha256":a["sha256"], **({"page":a["page"]} if a["kind"]=="deaths-table" else {})} for i,(a,_,_) in enumerate(assets,1)]
        report_sha=_sha_bytes(report_bytes); manifest_sha=_sha_bytes(manifest_bytes); fingerprint=_fingerprint(report_sha,manifest_sha,caption,media)
        stable_content_fingerprint=content_fingerprint(normalized)
        if any(a["state"]=="sent" and a["content_fingerprint"]==stable_content_fingerprint for _,a in existing): raise ReportPostError("identical report content was already sent")
        identifier=str(uuid.UUID(attempt_id)) if attempt_id else str(uuid.uuid4())
        if not _valid_uuid(identifier): raise ReportPostError("attempt_id must be a canonical UUID")
        stamp=_timestamp(now)
        attempt={"schema_version":SCHEMA_VERSION,"attempt_id":identifier,"kind":KIND,"created_at":stamp,"state":"pending","state_changed_at":stamp,
            "source_report_sha256":report_sha,"render_manifest_sha256":manifest_sha,"posting_fingerprint":fingerprint,"content_fingerprint":stable_content_fingerprint,"interval":normalized["interval"],
            "exact_deaths":len(normalized["rows"]),"exact_ages":len(normalized["ages"]),"caption_html":caption,"media":media,
            "telegram_message_ids":[],"error_kind":None,"error_summary":None,"resolution_note":None}
        validate_attempt(attempt)
        root=Path(state_root); root.mkdir(parents=True,exist_ok=True); final=root/identifier
        if final.exists(): raise ReportPostError("attempt bundle already exists")
        temp=Path(tempfile.mkdtemp(prefix=".report-post-{}-".format(identifier),dir=str(root)))
        try:
            _write_bytes(temp/"source-report.json",report_bytes); _write_bytes(temp/"render-manifest.json",manifest_bytes)
            for asset,_,data in assets: _write_bytes(temp/asset["filename"],data)
            _atomic_json(temp/"attempt.json",attempt)
            publish(str(temp),str(final)); temp=None
        finally:
            if temp is not None and temp.exists(): shutil.rmtree(str(temp))
        return ReportPostResult(True,"prepare",identifier,None,"pending",persistence_succeeded=True,posting_fingerprint=fingerprint,content_fingerprint=stable_content_fingerprint,bundle_path=str(final))
    except (OSError,ValueError,ReportPostError) as error:
        return ReportPostResult(False,"prepare",error_kind="validation_or_persistence",error_summary=str(error))

def verify_bundle(bundle, attempt=None):
    bundle=Path(bundle); attempt=attempt or load_attempt(bundle)
    if _sha_file(bundle/"source-report.json") != attempt["source_report_sha256"]: raise ReportPostError("frozen source report hash mismatch")
    if _sha_file(bundle/"render-manifest.json") != attempt["render_manifest_sha256"]: raise ReportPostError("frozen render manifest hash mismatch")
    _report, normalized, _manifest, _report_bytes, _manifest_bytes, checked = _validate_manifest(bundle/"source-report.json", bundle/"render-manifest.json")
    expected=[{"position":i,"kind":asset["kind"],"filename":asset["filename"],"sha256":asset["sha256"], **({"page":asset["page"]} if asset["kind"]=="deaths-table" else {})} for i,(asset,_,_) in enumerate(checked,1)]
    if attempt["media"] != expected: raise ReportPostError("stored media ordering does not match frozen manifest")
    if attempt["exact_deaths"] != len(normalized["rows"]) or attempt["exact_ages"] != len(normalized["ages"]): raise ReportPostError("stored report counts do not match frozen report")
    if attempt["interval"] != normalized["interval"]: raise ReportPostError("stored interval does not match frozen report")
    if attempt["caption_html"] != _caption(normalized): raise ReportPostError("stored caption does not match frozen report")
    expected_fingerprint=_fingerprint(attempt["source_report_sha256"],attempt["render_manifest_sha256"],attempt["caption_html"],attempt["media"])
    if attempt["posting_fingerprint"] != expected_fingerprint: raise ReportPostError("posting fingerprint does not match frozen payload")
    if attempt["content_fingerprint"] != content_fingerprint(normalized): raise ReportPostError("content fingerprint does not match frozen report")
    payload=[]
    for media in attempt["media"]:
        path=bundle/media["filename"]
        if not path.is_file() or _sha_file(path)!=media["sha256"]: raise ReportPostError("frozen media missing or hash mismatch: {}".format(media["filename"]))
        payload.append({"filename":media["filename"],"bytes":path.read_bytes()})
    return payload

def _save_transition(bundle, attempt, new_state, now=None, ids=None, error_kind=None, error_summary=None, resolution_note=None):
    if new_state not in TRANSITIONS[attempt["state"]]: raise ReportPostError("transition {} -> {} is not allowed".format(attempt["state"],new_state))
    changed=dict(attempt); changed["state"]=new_state; changed["state_changed_at"]=_timestamp(now); changed["telegram_message_ids"]=list(ids or [])
    changed["error_kind"]=error_kind; changed["error_summary"]=error_summary
    if resolution_note is not None: changed["resolution_note"]=resolution_note
    validate_attempt(changed); _atomic_json(Path(bundle)/"attempt.json",changed); return changed

def locate_attempt(state_root, attempt_id):
    if not _valid_uuid(attempt_id): return None
    bundle=Path(state_root)/attempt_id
    return (bundle,load_attempt(bundle)) if bundle.is_dir() else None

def dispatch_report_post(attempt_id,state_root,telegram_url,chat_id,confirmed=False,sender=None,now=None):
    try:
        located=locate_attempt(state_root,attempt_id)
    except (OSError, ReportPostError) as error:
        return ReportPostResult(False,"dispatch",attempt_id,error_kind="payload_integrity",error_summary=str(error),manual_reconciliation_required=True)
    if located is None: return ReportPostResult(False,"dispatch",attempt_id,error_kind="not_found",error_summary="attempt was not found")
    bundle,attempt=located; result=ReportPostResult(False,"dispatch",attempt_id,attempt["state"],attempt["state"])
    if not confirmed: result.error_kind="confirmation_required"; result.error_summary="explicit pending-dispatch confirmation is required"; return result
    if attempt["state"]!="pending": result.error_kind="invalid_state"; result.error_summary="only a pending attempt can be dispatched"; return result
    attempts=list_attempts(state_root)
    if attempts and max((a["created_at"],a["attempt_id"]) for _,a in attempts)!=(attempt["created_at"],attempt_id): result.error_kind="stale_attempt"; result.error_summary="attempt is not the newest report-post attempt"; return result
    try: payload=verify_bundle(bundle,attempt)
    except (OSError,ReportPostError) as error:
        try: _save_transition(bundle,attempt,"failed",now=now,error_kind="payload_integrity",error_summary=str(error)); result.ending_state="failed"; result.persistence_succeeded=True
        except (OSError,ReportPostError): result.error_kind="persistence_error"; result.error_summary="payload integrity failed and failure state could not be persisted"; return result
        result.error_kind="payload_integrity"; result.error_summary=str(error); return result
    sender=sender or send_media_group_to_chat
    try: telegram=sender(payload,attempt["caption_html"],telegram_url,chat_id)
    except requests.RequestException:
        telegram=type("Ambiguous",(),{"outcome":"ambiguous","message_ids":None,"error_reason":"request_exception","ok":False})()
    result.telegram_called=True
    ids=telegram.message_ids or []
    if telegram.outcome==TELEGRAM_OUTCOME_CONFIRMED_SUCCESS and len(ids)==len(attempt["media"]) and all(_is_int(x) and x>0 for x in ids): state="sent"; kind=summary=None
    elif telegram.outcome==TELEGRAM_OUTCOME_DEFINITE_REJECTION: state="failed"; kind="telegram_rejected"; summary="Telegram explicitly rejected the media group"; ids=[]
    else: state="unknown"; kind="response_invalid" if getattr(telegram,"error_reason",None) in ("invalid_json","invalid_response") or getattr(telegram,"ok",False) else "transport_ambiguous"; summary="Telegram media-group delivery could not be established"; ids=[]
    try: _save_transition(bundle,attempt,state,now=now,ids=ids,error_kind=kind,error_summary=summary)
    except (OSError,ReportPostError):
        result.error_kind="persistence_error"; result.error_summary="Telegram may have accepted the media group but terminal state could not be persisted"; result.manual_reconciliation_required=True; return result
    result.ok=state=="sent"; result.ending_state=state; result.telegram_message_ids=list(ids); result.error_kind=kind; result.error_summary=summary; result.persistence_succeeded=True; result.manual_reconciliation_required=state=="unknown"; return result

def reconcile_report_post(attempt_id,state_root,operation,message_ids=None,note=None,confirm_unsafe=False,now=None):
    try:
        located=locate_attempt(state_root,attempt_id)
    except (OSError, ReportPostError) as error:
        return ReportPostResult(False,operation,attempt_id,error_kind="payload_integrity",error_summary=str(error),manual_reconciliation_required=True)
    if located is None: return ReportPostResult(False,operation,attempt_id,error_kind="not_found",error_summary="attempt was not found")
    bundle,attempt=located; state=attempt["state"]
    if operation=="show": return ReportPostResult(True,operation,attempt_id,state,state,persistence_succeeded=True)
    if not isinstance(note,str) or not note.strip(): return ReportPostResult(False,operation,attempt_id,state,state,error_kind="validation",error_summary="a non-empty resolution note is required")
    if operation=="mark-sent":
        ids=list(message_ids or [])
        if state not in ("pending","unknown") or len(ids)!=len(attempt["media"]) or any(not _is_int(x) or x<=0 for x in ids): return ReportPostResult(False,operation,attempt_id,state,state,error_kind="validation",error_summary="mark-sent requires pending/unknown state and one positive ID per media item")
        target="sent"
    elif operation=="cancel":
        ids=[]
        if state not in ("pending","failed","unknown"): return ReportPostResult(False,operation,attempt_id,state,state,error_kind="invalid_state",error_summary="only unresolved attempts can be cancelled")
        if state=="unknown" and not confirm_unsafe: return ReportPostResult(False,operation,attempt_id,state,state,error_kind="unsafe_confirmation_required",error_summary="cancelling unknown delivery may cause a duplicate; pass explicit confirmation")
        target="cancelled"
    else: return ReportPostResult(False,operation,attempt_id,state,state,error_kind="invalid_operation",error_summary="unsupported reconciliation operation")
    try: _save_transition(bundle,attempt,target,now=now,ids=ids,error_kind=attempt.get("error_kind") if target=="cancelled" else None,error_summary=attempt.get("error_summary") if target=="cancelled" else None,resolution_note=note)
    except (OSError,ReportPostError) as error: return ReportPostResult(False,operation,attempt_id,state,state,error_kind="persistence_error",error_summary=str(error))
    return ReportPostResult(True,operation,attempt_id,state,target,telegram_message_ids=ids,persistence_succeeded=True)
