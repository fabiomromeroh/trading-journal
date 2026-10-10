"""Journal dropdown options (setups, tags, mistakes) and the configurable note questions.

Options live in ``journal_options`` so they survive when no trade uses them. Removing an option only takes it off
the list: trades that already carry it keep it (history is never rewritten). Typing a new value in a dropdown
adds it to the list automatically. The first read seeds the lists (mistakes: a sensible default set; setups and
tags: whatever the existing trades already use)."""
from __future__ import annotations

import json
import secrets

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import JournalOption, Tag, Trade, TradeMistake
from app.services import get_state, set_state

KINDS = ("setup", "tag", "mistake")
SEED_STATE = "opts:seeded"
QUESTIONS_STATE = "journal:questions"
DEFAULT_MISTAKES = ["Chased entry", "Oversized", "No stop", "Moved stop", "Sold too early", "Held too long",
                    "Ignored plan", "FOMO", "Revenge trade", "Poor setup quality", "Averaged down", "Early entry"]
DEFAULT_QUESTIONS = [
    {"id": "thesis", "label": "Why did I take this trade? (thesis / setup)", "type": "text"},
    {"id": "well", "label": "What went well?", "type": "text"},
    {"id": "wrong", "label": "What went wrong?", "type": "text"},
    {"id": "plan", "label": "Did I follow my plan?", "type": "plan"},
    {"id": "lesson", "label": "What would I do differently? (lesson)", "type": "text"},
    {"id": "emotions", "label": "Emotions / state of mind", "type": "text"},
]
GRADES = ["A", "B", "C", "D", "F"]
PLAN_CHOICES = [("yes", "Yes"), ("partly", "Partly"), ("no", "No")]


def clean(name: str | None) -> str:
    return " ".join((name or "").split())[:60]


def ensure_seeded(db: Session) -> None:
    try:
        done = set(json.loads(get_state(db, SEED_STATE) or "[]"))
    except ValueError:
        done = set()
    if set(KINDS) <= done:
        return
    for kind in KINDS:
        if kind in done:
            continue
        if kind == "mistake":
            seeds = list(DEFAULT_MISTAKES)
        elif kind == "setup":
            seeds = sorted({s for s in db.scalars(select(Trade.setup).where(Trade.setup.is_not(None))) if s})
        else:
            seeds = sorted(db.scalars(select(Tag.name)))
        have = {n.lower() for n in db.scalars(select(JournalOption.name).where(JournalOption.kind == kind))}
        for i, n in enumerate(seeds):
            if n.lower() not in have:
                db.add(JournalOption(kind=kind, name=n, position=i))
        done.add(kind)
    set_state(db, SEED_STATE, json.dumps(sorted(done)))
    db.commit()


def names(db: Session, kind: str) -> list[str]:
    ensure_seeded(db)
    return list(db.scalars(select(JournalOption.name).where(JournalOption.kind == kind)
                           .order_by(JournalOption.position, func.lower(JournalOption.name))))


def add(db: Session, kind: str, name: str) -> str | None:
    """Add to the list (case-insensitive: returns the existing spelling when it is already there)."""
    name = clean(name)
    if kind not in KINDS or not name:
        return None
    ensure_seeded(db)
    row = db.scalar(select(JournalOption).where(JournalOption.kind == kind, func.lower(JournalOption.name) == name.lower()))
    if row:
        return row.name
    top = db.scalar(select(func.max(JournalOption.position)).where(JournalOption.kind == kind))
    db.add(JournalOption(kind=kind, name=name, position=(top or 0) + 1))
    db.flush()
    return name


def remove(db: Session, kind: str, name: str) -> bool:
    ensure_seeded(db)
    row = db.scalar(select(JournalOption).where(JournalOption.kind == kind, JournalOption.name == name))
    if not row:
        return False
    db.delete(row)
    db.flush()
    return True


def usage(db: Session, kind: str) -> dict[str, int]:
    if kind == "setup":
        rows = db.execute(select(Trade.setup, func.count()).where(Trade.setup.is_not(None)).group_by(Trade.setup))
    elif kind == "tag":
        from app.models import trade_tags
        rows = db.execute(select(Tag.name, func.count()).join(trade_tags, trade_tags.c.tag_id == Tag.id).group_by(Tag.name))
    else:
        rows = db.execute(select(TradeMistake.name, func.count()).group_by(TradeMistake.name))
    return {n: c for n, c in rows if n}


def rename(db: Session, kind: str, old: str, new: str) -> str | None:
    """Rename on the list AND on every trade that uses it (merges when `new` already exists)."""
    new = clean(new)
    if kind not in KINDS or not new or old == new:
        return None
    ensure_seeded(db)
    src = db.scalar(select(JournalOption).where(JournalOption.kind == kind, JournalOption.name == old))
    dup = db.scalar(select(JournalOption).where(JournalOption.kind == kind, func.lower(JournalOption.name) == new.lower(),
                                                JournalOption.name != old))
    if dup:
        new = dup.name
        if src:
            db.delete(src)
    elif src:
        src.name = new
    else:
        db.add(JournalOption(kind=kind, name=new, position=0))
    db.flush()
    if kind == "setup":
        for t in db.scalars(select(Trade).where(Trade.setup == old)):
            t.setup = new
    elif kind == "tag":
        tag = db.scalar(select(Tag).where(Tag.name == old))
        other = db.scalar(select(Tag).where(func.lower(Tag.name) == new.lower(), Tag.name != old))
        if tag and other:
            for t in db.scalars(select(Trade).where(Trade.tags.any(Tag.id == tag.id))):
                t.tags = [x for x in t.tags if x.id != tag.id] + ([other] if other not in t.tags else [])
            db.flush()
            db.delete(tag)
        elif tag:
            tag.name = new
    else:
        for m in list(db.scalars(select(TradeMistake).where(TradeMistake.name == old))):
            exists = db.get(TradeMistake, (m.trade_id, new))
            db.delete(m)
            if not exists:
                db.flush()
                db.add(TradeMistake(trade_id=m.trade_id, name=new))
    db.flush()
    return new


# ------------------------------------------------------------------------------------ questions
def get_questions(db: Session) -> list[dict]:
    try:
        qs = json.loads(get_state(db, QUESTIONS_STATE) or "null")
    except ValueError:
        qs = None
    if not isinstance(qs, list):
        return [dict(q) for q in DEFAULT_QUESTIONS]
    return [q for q in qs if isinstance(q, dict) and q.get("id") and q.get("label")]


def save_questions(db: Session, raw) -> list[dict]:
    out, seen = [], set()
    for q in raw if isinstance(raw, list) else []:
        if not isinstance(q, dict):
            continue
        label = " ".join(str(q.get("label", "")).split())[:140]
        if not label:
            continue
        qid = str(q.get("id") or "")[:20]
        if not qid or qid in seen:
            qid = "q" + secrets.token_hex(3)
        seen.add(qid)
        out.append({"id": qid, "label": label, "type": "plan" if q.get("type") == "plan" else "text"})
    set_state(db, QUESTIONS_STATE, json.dumps(out))
    db.commit()
    return out
