"""thinkorswim fill emails -> fills with exact times (pushed by the user's Gmail Apps Script).

Flow: the Apps Script POSTs each new email from alerts@thinkorswim.com to /api/ingest/tos-email
with the per-install ingest token. Each email is stored once (idempotent on message_id), parsed
into `tos_email` fills (time = fill time in the body if it has a zone, else the email's received
time) and merged with the other sources by the regular cross-source matcher:

  * a SnapTrade same-day order (provisional) for the same execution is dropped in favour of the
    timed email fills once the emails cover its quantity;
  * the next-day SnapTrade activity replaces the email fill(s), keeping the email's exact time
    and adding fees (partial-fill emails are aggregated against the one activity row);
  * a thinkorswim statement import of the same fills merges into them (no duplicates).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
from collections import defaultdict
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.importers import tos_email
from app.models import Account, Execution, InboundEmail, SourceState, TradeFill
from app.services import (PROVISIONAL_SOURCES, _row_day, _row_to_record, get_state, ingest_records,
                          rebuild_trades, set_state)

log = logging.getLogger(__name__)
SOURCE = "tos_email"
TOKEN_STATE = "tos_email:token"          # Fernet-encrypted token (so Settings can show the script)
TOKEN_HASH_STATE = "tos_email:token_sha256"
MAX_BODY = 200_000
QTY_EPS = 1e-6


# ------------------------------------------------------------------------------------ token
def _sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _cipher():
    """Fernet keyed by TOKEN_ENCRYPTION_KEY, else SECRET_KEY (domain-separated)."""
    import base64
    from cryptography.fernet import Fernet
    from app.config import get_settings
    s = get_settings()
    secret = s.token_encryption_key or ("" if getattr(s, "secret_key_is_ephemeral", False) else s.secret_key)
    if not secret:
        raise RuntimeError("Set TOKEN_ENCRYPTION_KEY or SECRET_KEY to use the email ingest token")
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(b"tos-email-token:" + secret.encode()).digest()))


def get_token(db: Session) -> str | None:
    from cryptography.fernet import InvalidToken
    enc = get_state(db, TOKEN_STATE)
    if not enc:
        return None
    try:
        return _cipher().decrypt(enc.encode()).decode()
    except (InvalidToken, RuntimeError):
        return None


def regenerate_token(db: Session) -> str:
    token = secrets.token_urlsafe(32)
    set_state(db, TOKEN_STATE, _cipher().encrypt(token.encode()).decode())
    set_state(db, TOKEN_HASH_STATE, _sha(token))
    db.commit()
    return token


def verify_token(db: Session, provided: str | None) -> bool:
    want = get_state(db, TOKEN_HASH_STATE)
    if not want or not provided:
        return False
    return hmac.compare_digest(_sha(provided.strip()), want)


# ------------------------------------------------------------------------------------ ingest
def parse_received(value) -> datetime:
    """ISO 8601 with offset, or epoch milliseconds/seconds -> naive UTC."""
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().lstrip("-").isdigit()):
        v = float(value)
        if v > 1e11:
            v /= 1000.0
        return datetime.fromtimestamp(v, tz=timezone.utc).replace(tzinfo=None)
    if isinstance(value, str) and value.strip():
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if dt.tzinfo is None:
            raise ValueError("received_at needs a time zone offset")
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    raise ValueError("received_at is required")


def _pick_account(db: Session, suffix: str | None) -> Account | None:
    accts = list(db.scalars(select(Account).where(Account.is_demo.is_(False)).order_by(Account.id)))
    if suffix:
        hit = [a for a in accts if (a.account_number_masked or "").endswith(suffix)]
        if len(hit) == 1:
            return hit[0]
    if len(accts) == 1:
        return accts[0]
    snap = [a for a in accts if (a.external_ref or "").startswith("snaptrade:")]
    return (snap or accts or [None])[0]


def _canon(db_aliases, sym):
    from app.symbols import canonical_symbol
    return canonical_symbol(sym, db_aliases)


def reconcile_provisional(db: Session, account_id: int, days=None) -> int:
    """Drop provisional order fills that timed email fills fully cover (same ET day, symbol,
    side). Returns the number of provisional rows removed."""
    from app.symbols import load_aliases
    aliases = load_aliases(db)
    rows = list(db.scalars(select(Execution).where(
        Execution.account_id == account_id,
        Execution.source.in_(PROVISIONAL_SOURCES | {SOURCE}))))
    groups: dict[tuple, dict[str, list[Execution]]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        day = _row_day(r)
        if days is not None and day not in days:
            continue
        groups[(day, _canon(aliases, r.symbol), r.side, r.kind)][
            "email" if r.source == SOURCE else "prov"].append(r)
    removed = 0
    links = db.info.setdefault("trade_exec_links", defaultdict(set))
    for g in groups.values():
        em, pv = g.get("email", []), g.get("prov", [])
        if not em or not pv:
            continue
        if sum(e.quantity for e in em) + QTY_EPS < sum(p.quantity for p in pv):
            continue  # more partial-fill emails still to come
        ids = [p.id for p in pv]
        for (tid,) in db.execute(select(TradeFill.trade_id).where(TradeFill.execution_id.in_(ids))):
            links[tid].update(e.id for e in em)
        for e in em:  # keep the broker's open/close hint the order carried
            if not e.position_effect:
                e.position_effect = next((p.position_effect for p in pv if p.position_effect), None)
        for p in pv:
            db.delete(p)
            removed += 1
    db.flush()
    return removed


def absorb_confirmed_emails(db: Session, account_id: int, covered_day) -> int:
    """Email fills on days the activity feed already covers (<= covered_day) that no activity
    replaced (e.g. some partial-fill emails were missing when the activity arrived): give their
    earliest time to the activity rows of the same day/symbol/side that lack one, then drop them.
    Only when the activity rows account for at least the emailed quantity."""
    from app.symbols import load_aliases
    aliases = load_aliases(db)
    emails = [e for e in db.scalars(select(Execution).where(Execution.account_id == account_id,
                                                            Execution.source == SOURCE))
              if (_row_day(e) or covered_day) <= covered_day]
    if not emails:
        return 0
    days = {_row_day(e) for e in emails}
    key = lambda r: (_row_day(r), _canon(aliases, r.symbol), r.side, r.kind)  # noqa: E731
    auth = defaultdict(list)
    for r in db.scalars(select(Execution).where(Execution.account_id == account_id,
                                               Execution.source.notin_(PROVISIONAL_SOURCES | {SOURCE}))):
        if _row_day(r) in days:
            auth[key(r)].append(r)
    groups = defaultdict(list)
    for e in emails:
        groups[key(e)].append(e)
    removed = 0
    links = db.info.setdefault("trade_exec_links", defaultdict(set))
    for k, em in groups.items():
        targets = auth.get(k)
        if not targets or sum(t.quantity for t in targets) + QTY_EPS < sum(e.quantity for e in em):
            continue  # no (or not enough) broker record for it: keep the email fills
        first = min(em, key=lambda e: (e.executed_at, e.seq or 0))
        for t in targets:
            if not t.time_known:
                t.executed_at, t.time_known, t.seq = first.executed_at, True, first.seq or 0
        ids = [e.id for e in em]
        for (tid,) in db.execute(select(TradeFill.trade_id).where(TradeFill.execution_id.in_(ids))):
            links[tid].update(t.id for t in targets)
        for e in em:
            db.delete(e)
            removed += 1
    db.flush()
    return removed


def ingest_email(db: Session, payload: dict) -> dict:
    """Store + parse one email. Returns a JSON-able result; raises ValueError on bad input."""
    mid = str(payload.get("message_id") or "").strip()[:300]
    if not mid:
        raise ValueError("message_id is required")
    received = parse_received(payload.get("received_at"))
    subject = str(payload.get("subject") or "")[:500]
    body = str(payload.get("body") or "")[:MAX_BODY]
    sender = str(payload.get("from") or "")[:300]
    old = db.scalar(select(InboundEmail).where(InboundEmail.message_id == mid))
    if old is not None:
        return {"ok": True, "duplicate": True, "status": old.status, "fills": old.fills}
    parsed = tos_email.parse(subject, body, received, mid)
    acct = _pick_account(db, parsed.account_suffix) if parsed.records else None
    note = {}
    if parsed.skipped:
        note["skipped"] = parsed.skipped[:20]
    if parsed.body_time:
        note["time_from_body"] = True
    status = "fills" if parsed.records else "ignored"
    if parsed.records and acct is None:
        status, note["error"] = "error", "no account to attach the fills to"
    row = InboundEmail(message_id=mid, account_id=acct.id if acct else None, received_at=received,
                       sender=sender, subject=subject, body=body, status=status,
                       fills=len(parsed.records) if status == "fills" else 0,
                       note=json.dumps(note) if note else None)
    db.add(row)
    try:
        db.flush()
    except IntegrityError:  # the same email posted twice at once
        db.rollback()
        old = db.scalar(select(InboundEmail).where(InboundEmail.message_id == mid))
        return {"ok": True, "duplicate": True, "status": old.status if old else "?", "fills": old.fills if old else 0}
    result = {"ok": True, "duplicate": False, "status": status, "fills": row.fills,
              "skipped": len(parsed.skipped)}
    if status == "fills":
        stats = _store_fills(db, acct.id, parsed.records)
        result.update(inserted=stats["inserted"], merged=stats["merged"])
        rebuild_trades(db, [acct.id])
    db.commit()
    return result


def _store_fills(db: Session, account_id: int, records) -> dict:
    """Ingest the email's fills together with this day's earlier, still unconfirmed email fills
    so partial-fill emails can be aggregated against one broker row once they add up."""
    days = {r.trade_date for r in records}
    prior = [e for e in db.scalars(select(Execution).where(Execution.account_id == account_id,
                                                           Execution.source == SOURCE))
             if _row_day(e) in days]
    prior_trades = {}
    for e in prior:
        prior_trades[e.id] = [tid for (tid,) in db.execute(
            select(TradeFill.trade_id).where(TradeFill.execution_id == e.id))]
    prior_recs = [_row_to_record(e) for e in prior]
    old_ids = {id(r): e.id for r, e in zip(prior_recs, prior)}
    for e in prior:
        db.delete(e)
    db.flush()
    trace: dict = {}
    stats = ingest_records(db, account_id, SOURCE, prior_recs + list(records), trace=trace)
    db.flush()
    links = db.info.setdefault("trade_exec_links", defaultdict(set))
    for r in prior_recs:
        for tid in prior_trades.get(old_ids[id(r)], []):
            links[tid].update(x.id for x in trace.get(id(r), []) if x.id)
    reconcile_provisional(db, account_id, days)
    n_prior = len(prior_recs)
    return {"inserted": max(0, stats.inserted - n_prior), "merged": stats.merged}


def reconcile_after_sync(db: Session, account_ids) -> None:
    """Called by the sync run after the broker sources: drop provisional orders covered by email
    fills; fold leftover email fills into activities on days the activity feed covers."""
    for acct_id in account_ids:
        reconcile_provisional(db, acct_id)
        st = db.scalar(select(SourceState).where(SourceState.source == "snaptrade",
                                                 SourceState.account_id == acct_id))
        if st and st.synced_through:  # (absorb only acts where broker rows cover the emailed qty)
            absorb_confirmed_emails(db, acct_id, st.synced_through.date())


# ------------------------------------------------------------------------------------ settings
def discard_email(db: Session, email_id: int) -> int:
    """Remove the fills an email created that are still email-only (e.g. a mis-parsed or test
    email) and mark it discarded, so re-posting it stays a no-op. Returns fills removed."""
    row = db.get(InboundEmail, email_id)
    if row is None:
        return 0
    prefix = f"{row.message_id}#"
    rows = [e for e in db.scalars(select(Execution).where(Execution.source == SOURCE))
            if (e.external_id or "").startswith(prefix)]
    accts = {e.account_id for e in rows} | ({row.account_id} if row.account_id else set())
    for e in rows:
        db.delete(e)
    row.status, row.fills = "discarded", 0
    db.flush()
    if accts:
        rebuild_trades(db, sorted(accts))
    db.commit()
    return len(rows)


def status(db: Session) -> dict:
    live_rows = InboundEmail.status != "discarded"
    n, last, fills = db.execute(select(func.count(InboundEmail.id), func.max(InboundEmail.received_at),
                                       func.sum(InboundEmail.fills)).where(live_rows)).first()
    last_fill = db.scalar(select(func.max(InboundEmail.received_at)).where(InboundEmail.status == "fills"))
    errors = db.scalar(select(func.count(InboundEmail.id)).where(InboundEmail.status == "error")) or 0
    pending = db.scalar(select(func.count(Execution.id)).where(Execution.source == SOURCE)) or 0
    recent = list(db.scalars(select(InboundEmail).order_by(InboundEmail.received_at.desc()).limit(8)))
    return {"emails": n or 0, "last": last, "last_fill": last_fill, "fills": int(fills or 0), "errors": errors,
            "pending": pending, "recent": recent}


APPS_SCRIPT = r"""/**
 * Trading Journal: thinkorswim fill emails -> journal (runs in your own Google account).
 * Setup: run setup() once and authorize. It then runs every minute.
 */
const ENDPOINT = '__ENDPOINT__';
const TOKEN = '__TOKEN__';          // keep private: anyone with it can add fills to your journal
const SENDER = 'alerts@thinkorswim.com';
const LABEL = 'Trading Journal/Fills';
const LOOKBACK = '3d';
const FILL_RE = /\b(BOT|SOLD)\s+[+-]?\s?\d/;

function setup() {
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === 'syncFills') ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger('syncFills').timeBased().everyMinutes(1).create();
  label_();
  syncFills();
}

function syncFills() {
  var lock = LockService.getScriptLock();
  if (!lock.tryLock(5000)) return;
  try {
    var props = PropertiesService.getScriptProperties();
    var done = loadDone_(props);
    // Search by sender, not the inbox, so emails a Gmail filter already archived are found too.
    var threads = GmailApp.search('from:' + SENDER + ' newer_than:' + LOOKBACK, 0, 100);
    threads.forEach(function (thread) {
      var allOk = true, anyFill = false;
      thread.getMessages().forEach(function (msg) {
        if (msg.getFrom().toLowerCase().indexOf(SENDER) < 0) return;
        var id = msg.getId();
        if (done[id] !== undefined) { if (done[id] > 0) anyFill = true; return; }
        var body = msg.getPlainBody() || msg.getBody();
        if (!FILL_RE.test(msg.getSubject() + '\n' + body)) return;   // not a fill (code, price alert...)
        var res;
        try {
          res = UrlFetchApp.fetch(ENDPOINT, {
            method: 'post', contentType: 'application/json', muteHttpExceptions: true,
            headers: { Authorization: 'Bearer ' + TOKEN },
            payload: JSON.stringify({
              message_id: msg.getHeader('Message-ID') || id, gmail_id: id,
              received_at: msg.getDate().getTime(), subject: msg.getSubject(),
              body: body, from: msg.getFrom()
            })
          });
        } catch (e) { allOk = false; console.warn('Journal not reachable: ' + e); return; }
        var code = res.getResponseCode();
        if (code >= 200 && code < 300) {
          var fills = 0;
          try { fills = JSON.parse(res.getContentText()).fills || 0; } catch (e) {}
          done[id] = fills;
          remember_(props, id, fills);
          if (fills > 0) anyFill = true;
        } else {
          allOk = false;              // leave it in the inbox, unlabelled: retried next minute
          console.warn('Journal returned HTTP ' + code);
        }
      });
      if (allOk && anyFill) {
        thread.addLabel(label_());
        thread.markRead();
        thread.moveToArchive();
      }
    });
  } finally {
    lock.releaseLock();
  }
}

function label_() {
  var parent = GmailApp.getUserLabelByName('Trading Journal') || GmailApp.createLabel('Trading Journal');
  return GmailApp.getUserLabelByName(LABEL) || GmailApp.createLabel(LABEL);
}

// Processed Gmail message ids, kept for 5 days (one property per hour, well under 9 KB each).
function loadDone_(props) {
  var all = props.getProperties(), done = {}, cutoff = dayKey_(-5);
  Object.keys(all).forEach(function (k) {
    if (k.indexOf('done-') !== 0) return;
    if (k < cutoff) { props.deleteProperty(k); return; }
    var m = JSON.parse(all[k]);
    Object.keys(m).forEach(function (id) { done[id] = m[id]; });
  });
  return done;
}

function remember_(props, id, fills) {
  var key = dayKey_(0) + '-' + Utilities.formatDate(new Date(), 'UTC', 'HH');
  var m = JSON.parse(props.getProperty(key) || '{}');
  m[id] = fills;
  props.setProperty(key, JSON.stringify(m));
}

function dayKey_(offset) {
  var d = new Date(Date.now() + offset * 86400000);
  return 'done-' + Utilities.formatDate(d, 'UTC', 'yyyyMMdd');
}
"""


def apps_script(endpoint: str, token: str) -> str:
    return APPS_SCRIPT.replace("__ENDPOINT__", endpoint).replace("__TOKEN__", token)
