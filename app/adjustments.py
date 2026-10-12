"""Stored stock splits and ticker renames, plus the raw file from each sync.

A split is not a fill. The row records the ratio and the date; the trade builder multiplies
quantities and divides prices on the fills that happened before that date, so the cost basis
stays the same. A rename folds the old ticker into the new one. The same key is never applied
twice. When the ratio or the new ticker is not safe to guess, the symbol is marked
``needs_attention`` and left unchanged until the user enters the adjustment.
"""
from __future__ import annotations

import gzip
import json
import os
import re
from datetime import datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import CorporateAdjustment, Execution, Instrument, SyncPayload, utcnow

KEEP_PAYLOADS = 50
GZIP_AT = 4096
_SECRET_KEY = re.compile(r"token|secret|password|authorization|api_key|apikey|consumer", re.I)
_RENAME_WORDS = re.compile(r"rename|ticker change|symbol change|name change", re.I)
_TICKER = re.compile(r"\b([A-Z][A-Z0-9.]{0,7})\b")
_ARROW = re.compile(
    r"\b([A-Z][A-Z0-9.]{0,7})\b\s*(?:->|→|to)\s*\b([A-Z][A-Z0-9.]{0,7})\b", re.I)
_SPLIT_TYPES = {"SPLIT", "STOCK_SPLIT", "REVERSE_SPLIT"}
_RENAME_TYPES = {"RENAME", "SYMBOL_CHANGE", "TICKER_CHANGE"}


def default_fetch(symbol: str, start: datetime, end: datetime) -> list:
    """Yahoo chart splits. Tests never call the network unless they pass their own fetch."""
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return []
    try:
        from app.prices import yahoo_split_events
        return list(yahoo_split_events(symbol, start, end) or [])
    except Exception:  # noqa: BLE001 — a quote miss must not abort the sync
        return []


def ratio_text(text: str) -> float | None:
    """``3``, ``3:1``, ``3 for 1``, ``1/10`` → new/old. 1 is not a split."""
    from app.corporate import ratio_from_text
    raw = (text or "").strip()
    if not raw:
        return None
    slash = re.fullmatch(r"(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)", raw)
    if slash:
        new, old = float(slash.group(1)), float(slash.group(2))
        if old <= 0 or new <= 0 or abs(new / old - 1.0) <= 1e-9:
            return None
        return new / old
    found = ratio_from_text(raw)
    if found is not None and abs(found - 1.0) > 1e-9:
        return found
    try:
        value = float(raw)
    except ValueError:
        return None
    if value <= 0 or abs(value - 1.0) <= 1e-9:
        return None
    return value


def _ratio_key(ratio: float) -> str:
    return f"{ratio:.8g}"


def _day_key(when: datetime) -> str:
    return when.date().isoformat()


# ---------------------------------------------------------------- persistence
def _get(db: Session, account_id: int, key: str) -> CorporateAdjustment | None:
    return db.scalar(select(CorporateAdjustment).where(
        CorporateAdjustment.account_id == account_id, CorporateAdjustment.external_key == key))


def upsert(db: Session, account_id: int, *, key: str, symbol: str, kind: str, status: str,
           source: str, ratio: float | None = None, new_symbol: str | None = None,
           when: datetime | None = None, note: str | None = None) -> tuple[CorporateAdjustment, bool]:
    """Insert the row once. A manual or already-applied row is not overwritten."""
    row = _get(db, account_id, key)
    if row is not None:
        if row.status == "needs_attention" and status == "needs_attention" and note and row.note != note:
            row.note = note
        return row, False
    row = CorporateAdjustment(
        account_id=account_id, symbol=(symbol or "").upper(), kind=kind, ratio=ratio,
        new_symbol=(new_symbol or "").upper() or None, effective_on=when, source=source,
        external_key=key[:200], status=status, note=note,
    )
    db.add(row)
    db.flush()
    return row, True


def _manual_blocks(db: Session, account_id: int, symbol: str, kind: str) -> bool:
    return db.scalar(select(CorporateAdjustment.id).where(
        CorporateAdjustment.account_id == account_id,
        CorporateAdjustment.symbol == symbol.upper(),
        CorporateAdjustment.kind == kind,
        CorporateAdjustment.source == "manual",
        CorporateAdjustment.status == "applied",
    ).limit(1)) is not None


def _split_covered(rows, when: datetime, ratio: float) -> bool:
    for e in rows:
        if e.kind != "SPLIT" or e.position_effect != "RATIO" or not e.quantity:
            continue
        if abs((e.executed_at - when).total_seconds()) < 7 * 86400 and abs(float(e.quantity) - ratio) / ratio < 0.02:
            return True
    return False


def _add_split_execution(db: Session, account_id: int, symbol: str, when: datetime, ratio: float,
                         key: str, note: str, rows: list) -> Execution | None:
    """One SPLIT execution for this ratio and date. A broker or Yahoo row already stored counts."""
    if _split_covered(rows, when, ratio):
        return None
    ext = f"ysplit:{symbol}:{_day_key(when)}"[:200]
    existing = db.scalar(select(Execution).where(
        Execution.account_id == account_id, Execution.external_id == ext))
    if existing is not None:
        return None
    sib = next((e for e in rows if e.symbol and e.kind != "SPLIT"), None)
    row = Execution(
        account_id=account_id, source="adjustment", external_id=ext, symbol=symbol, underlying=symbol,
        asset_type="STOCK", side="BUY", quantity=float(ratio), price=0.0, fees=0.0,
        executed_at=when, time_known=True, currency=getattr(sib, "currency", None),
        instrument_id=getattr(sib, "instrument_id", None), position_effect="RATIO", kind="SPLIT",
        multiplier=1.0, description=(note or f"Split ×{_ratio_key(ratio)}")[:300],
        match_key=f"{_day_key(when)}|{symbol}|BUY|{ratio:.6f}|0|SPLIT",
        raw=json.dumps({"adjustment": True, "key": key, "ratio": ratio, "note": note}),
    )
    db.add(row)
    db.flush()
    rows.append(row)
    return row


def materialize(db: Session, account_id: int) -> list[str]:
    """Make sure each applied row has its split execution or rename alias. Safe to call again."""
    from app.symbols import save_auto
    lines = []
    rows_by_symbol: dict[str, list] = {}
    execs = list(db.scalars(select(Execution).where(Execution.account_id == account_id)))
    for e in execs:
        rows_by_symbol.setdefault((e.symbol or "").upper(), []).append(e)
    applied = list(db.scalars(select(CorporateAdjustment).where(
        CorporateAdjustment.account_id == account_id, CorporateAdjustment.status == "applied")))
    renames: dict[str, tuple[str, str]] = {}
    for adj in applied:
        sym = (adj.symbol or "").upper()
        if adj.kind == "SPLIT" and adj.ratio and adj.effective_on and abs(adj.ratio - 1.0) > 1e-9:
            bucket = rows_by_symbol.setdefault(sym, [])
            made = _add_split_execution(
                db, account_id, sym, adj.effective_on, float(adj.ratio), adj.external_key,
                adj.note or f"Split ×{_ratio_key(adj.ratio)}", bucket)
            if made is not None:
                lines.append(f"{sym}: split ×{_ratio_key(adj.ratio)} on {_day_key(adj.effective_on)}")
        elif adj.kind == "RENAME" and adj.new_symbol and adj.new_symbol.upper() != sym:
            renames[sym] = (adj.new_symbol.upper(), adj.note or "ticker change")
    if renames:
        save_auto(db, renames)
    return lines


def remember_split(db: Session, account_id: int, symbol: str, when: datetime, ratio: float, *,
                   source: str, external_key: str, note: str | None = None) -> None:
    """Record a split the broker or a listing import already stored as an execution."""
    upsert(db, account_id, key=external_key, symbol=symbol.upper(), kind="SPLIT", status="applied",
           source=source, ratio=float(ratio), when=when, note=note)


# ---------------------------------------------------------------- detection
def _flag(db: Session, account_id: int, symbol: str, code: str, note: str) -> bool:
    _, created = upsert(
        db, account_id, key=f"attn:{symbol.upper()}:{code}", symbol=symbol.upper(), kind="CHECK",
        status="needs_attention", source="broker", note=note)
    return created


def _clear_flag(db: Session, account_id: int, symbol: str, code: str) -> None:
    db.execute(delete(CorporateAdjustment).where(
        CorporateAdjustment.account_id == account_id,
        CorporateAdjustment.external_key == f"attn:{symbol.upper()}:{code}"))


def _held_rows(execs) -> dict[str, list]:
    groups: dict[str, list] = {}
    for e in execs:
        if not _is_stock(e):
            continue
        groups.setdefault((e.symbol or "").upper(), []).append(e)
    return groups


def _is_stock(e) -> bool:
    return (getattr(e, "asset_type", None) or "STOCK") != "OPTION" and " " not in (getattr(e, "symbol", None) or "")


def _as_view(e, symbol: str):
    """Same fill, read as ``symbol``, so a renamed ticker shares one open quantity."""
    return SimpleNamespace(
        id=e.id, account_id=e.account_id, external_id=e.external_id, symbol=symbol,
        side=e.side, quantity=e.quantity, price=e.price, executed_at=e.executed_at,
        fees=e.fees or 0.0, multiplier=e.multiplier or 1.0, position_effect=e.position_effect,
        kind=e.kind or "TRADE", time_known=bool(e.time_known), seq=e.seq or 0,
        instrument_id=e.instrument_id, source=getattr(e, "source", None),
        description=getattr(e, "description", None), asset_type=getattr(e, "asset_type", "STOCK"),
        raw=getattr(e, "raw", None))


def _canonical_groups(execs, aliases) -> tuple[dict[str, list], dict[str, set[str]]]:
    from app.symbols import canonical_symbol
    groups: dict[str, list] = {}
    members: dict[str, set[str]] = {}
    for e in execs:
        if not _is_stock(e):
            continue
        raw = (e.symbol or "").upper()
        canon = canonical_symbol(raw, aliases).upper()
        groups.setdefault(canon, []).append(_as_view(e, canon))
        members.setdefault(canon, set()).add(raw)
    return groups, members


def _remap_positions(positions: dict[str, float] | None, aliases) -> dict[str, float] | None:
    """One broker quantity per current ticker. The larger figure wins when both names were reported."""
    if positions is None:
        return None
    from app.symbols import canonical_symbol
    out: dict[str, float] = {}
    for key, qty in positions.items():
        canon = canonical_symbol((key or "").upper(), aliases).upper()
        if not canon or canon == "?" or " " in canon:
            continue
        out[canon] = max(out.get(canon, 0.0), float(qty))
    return out


def _apply_identity_renames(db: Session, account_id: int, execs) -> list[str]:
    """Store a rename when the broker identifiers and a real effective date agree."""
    from app.renames import lookup_rename, detect_pairs
    lines = []
    pairs, ambiguous = detect_pairs(db, execs)
    for item in ambiguous:
        if _flag(db, account_id, item.symbol, "rename", item.note):
            lines.append(item.note)
    for pair in pairs:
        if pair.isin_conflict or _manual_blocks(db, account_id, pair.old, "RENAME"):
            continue
        found = lookup_rename(pair.old, pair.new, proven=pair.proven, db=db)
        if found is None:
            # An alias already merges the fills. A catalogue name with no fills and no date is not a guess.
            if pair.already_aliased or not pair.both_fills:
                continue
            note = (f"{pair.old}: same security as {pair.new} ({pair.via}), but no effective date "
                    f"was found. Enter the date to merge them.")
            if _flag(db, account_id, pair.old, "rename", note):
                lines.append(note)
            continue
        when, source = found
        _, created = upsert(
            db, account_id, key=f"rename:{pair.old}:{pair.new}", symbol=pair.old, kind="RENAME",
            status="applied", source=source[:20], new_symbol=pair.new, when=when,
            note=f"{pair.old} renamed to {pair.new} ({source})")
        if created:
            lines.append(f"{pair.old} → {pair.new}")
        _clear_flag(db, account_id, pair.old, "rename")
    return lines


def _buy_totals(execs, aliases) -> dict[str, float]:
    from app.symbols import canonical_symbol
    totals: dict[str, float] = {}
    for e in execs:
        if str(getattr(e, "external_id", "") or "").startswith("recon:"):
            continue
        if not _is_stock(e) or (getattr(e, "kind", None) or "TRADE") != "TRADE" or e.side != "BUY":
            continue
        key = canonical_symbol((e.symbol or "").upper(), aliases).upper()
        totals[key] = totals.get(key, 0.0) + abs(e.quantity or 0.0)
    return totals


def _price_symbol(db: Session, rows) -> str:
    iid = next((e.instrument_id for e in rows if e.instrument_id), None)
    if iid:
        inst = db.get(Instrument, iid)
        if inst is not None and inst.price_symbol:
            return inst.price_symbol
    return (rows[0].symbol if rows else "") or ""


def _open_qty(rows) -> float:
    from app.corporate import open_state
    if not rows:
        return 0.0
    qty, _avg = open_state(rows)
    return qty


def _virtual(symbol: str, when: datetime, ratio: float, account_id: int):
    return SimpleNamespace(
        id=0, account_id=account_id, external_id="virtual", symbol=symbol, side="BUY",
        quantity=float(ratio), price=0.0, executed_at=when, fees=0.0, multiplier=1.0,
        position_effect="RATIO", kind="SPLIT", time_known=True, seq=0)


def events_from_activities(activities: list[dict] | None) -> list[dict]:
    """Split and rename hints on SnapTrade activities. Unknown ratios are not guessed."""
    out = []
    for a in activities or []:
        if not isinstance(a, dict):
            continue
        t = (a.get("type") or "").upper()
        desc = a.get("description") or ""
        sym = ""
        raw_sym = a.get("symbol")
        if isinstance(raw_sym, dict):
            sym = (raw_sym.get("symbol") or raw_sym.get("raw_symbol") or "").upper()
        elif isinstance(raw_sym, str):
            sym = raw_sym.upper()
        when = _parse_when(a.get("trade_date") or a.get("settlement_date"))
        ext = str(a.get("external_reference_id") or a.get("id") or "")
        blob = f"{t} {desc}"
        if t in _SPLIT_TYPES or re.search(r"\bstock split\b|\breverse split\b", blob, re.I):
            ratio = ratio_text(desc)
            if ratio and sym and when:
                out.append({"kind": "SPLIT", "symbol": sym, "ratio": ratio, "when": when,
                            "key": f"broker:{ext or sym}:{_day_key(when)}:{_ratio_key(ratio)}"})
            elif sym:
                out.append({"kind": "ATTENTION", "symbol": sym, "code": "ratio",
                            "note": f"{sym}: split in the broker history has no usable ratio."})
        parsed = _rename_pair(blob)
        if t in _RENAME_TYPES or (_RENAME_WORDS.search(blob) and parsed):
            old, new = parsed if parsed else (sym, "")
            if old and new and old != new and when:
                out.append({"kind": "RENAME", "symbol": old, "new_symbol": new, "when": when,
                            "key": f"rename:{old}:{new}:{_day_key(when)}"})
            elif sym:
                out.append({"kind": "ATTENTION", "symbol": sym or old or "?", "code": "rename",
                            "note": f"{sym or 'A ticker'}: rename in the broker history is ambiguous."})
    return out


def events_from_schwab(txs: list[dict] | None) -> list[dict]:
    out = []
    for tx in txs or []:
        if not isinstance(tx, dict):
            continue
        desc = tx.get("description") or ""
        t = (tx.get("type") or "").upper()
        when = _parse_when(tx.get("time") or tx.get("tradeDate"))
        sym = ""
        for it in tx.get("transferItems") or []:
            inst = it.get("instrument") or {}
            if inst.get("assetType") in ("EQUITY", "COLLECTIVE_INVESTMENT", "ETF") and inst.get("symbol"):
                sym = str(inst["symbol"]).upper()
                break
        blob = f"{t} {desc}"
        ext = str(tx.get("activityId") or "")
        if re.search(r"\bstock split\b|\breverse split\b", blob, re.I) or t in _SPLIT_TYPES:
            ratio = ratio_text(desc)
            if ratio and sym and when:
                out.append({"kind": "SPLIT", "symbol": sym, "ratio": ratio, "when": when,
                            "key": f"broker:{ext or sym}:{_day_key(when)}:{_ratio_key(ratio)}"})
            elif sym:
                out.append({"kind": "ATTENTION", "symbol": sym, "code": "ratio",
                            "note": f"{sym}: split in the broker history has no usable ratio."})
        parsed = _rename_pair(blob)
        if _RENAME_WORDS.search(blob) and parsed:
            old, new = parsed
            if old != new and when:
                out.append({"kind": "RENAME", "symbol": old, "new_symbol": new, "when": when,
                            "key": f"rename:{old}:{new}:{_day_key(when)}"})
        elif _RENAME_WORDS.search(blob) and sym:
            out.append({"kind": "ATTENTION", "symbol": sym, "code": "rename",
                        "note": f"{sym}: rename in the broker history is ambiguous."})
    return out


def _parse_when(value) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if not value:
        return None
    text = str(value).replace("Z", "")
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:19] if "T" in text else text[:10], fmt).replace(tzinfo=None)
        except ValueError:
            continue
    try:
        d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is not None:
        from datetime import timezone
        d = d.astimezone(timezone.utc).replace(tzinfo=None)
    return d


def _rename_pair(text: str) -> tuple[str, str] | None:
    if not text or not _RENAME_WORDS.search(text):
        return None
    m = _ARROW.search(text.upper())
    if not m:
        tickers = _TICKER.findall(text.upper())
        # drop the words that are not tickers by requiring two tokens after the phrase
        if len(tickers) >= 2:
            return tickers[0], tickers[1]
        return None
    return m.group(1).upper(), m.group(2).upper()


def _events_from_execs(execs) -> list[dict]:
    out = []
    seen_rename: dict[str, set[str]] = {}
    for e in execs:
        try:
            raw = json.loads(e.raw or "{}")
        except ValueError:
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        text = " ".join(str(raw.get(k) or "") for k in ("notes", "action", "description"))
        text = f"{text} {e.description or ''}"
        parsed = _rename_pair(text)
        if parsed and e.executed_at:
            old, new = parsed
            seen_rename.setdefault(old, set()).add(new)
            out.append({"kind": "RENAME", "symbol": old, "new_symbol": new, "when": e.executed_at,
                        "key": f"rename:{old}:{new}:{_day_key(e.executed_at)}"})
    ambiguous = []
    for old, news in seen_rename.items():
        if len(news) > 1:
            ambiguous.append({"kind": "ATTENTION", "symbol": old, "code": "rename",
                              "note": f"{old}: more than one new ticker ({', '.join(sorted(news))})."})
    if ambiguous:
        # drop the guessed renames; the user has to choose
        out = [e for e in out if not (e["kind"] == "RENAME" and e["symbol"] in {a["symbol"] for a in ambiguous})]
        out.extend(ambiguous)
    return out


def detect_and_apply(db: Session, account_id: int, *, fetch=None, positions: dict[str, float] | None = None,
                     broker_events: list[dict] | None = None, symbols: set[str] | None = None) -> list[str]:
    """Find splits and renames for this account and store them. Idempotent."""
    from app.corporate import select_listing_splits
    fetch = fetch or default_fetch
    lines = materialize(db, account_id)
    execs = list(db.scalars(select(Execution).where(Execution.account_id == account_id)))
    for e in execs:
        # A split the broker already stored is kept as an adjustment too, without a second execution.
        if (e.kind == "SPLIT" and e.position_effect == "RATIO" and e.quantity
                and abs(float(e.quantity) - 1.0) > 1e-6
                and not str(e.external_id or "").startswith(("recon:", "ysplit:"))
                and e.source != "adjustment"):
            remember_split(db, account_id, e.symbol, e.executed_at, float(e.quantity), source="broker",
                           external_key=f"broker:{e.source}:{e.external_id}",
                           note=e.description or f"Split ×{_ratio_key(float(e.quantity))}")
    groups = _held_rows(execs)
    events = list(broker_events or []) + _events_from_execs(execs)
    # Ambiguous renames: the same old ticker pointed at two new ones in one batch.
    rename_targets: dict[str, set[str]] = {}
    for ev in events:
        if ev.get("kind") == "RENAME" and ev.get("symbol") and ev.get("new_symbol"):
            rename_targets.setdefault(ev["symbol"].upper(), set()).add(ev["new_symbol"].upper())
    ambiguous = {old for old, news in rename_targets.items() if len(news) > 1}

    for ev in events:
        kind = ev.get("kind")
        sym = (ev.get("symbol") or "").upper()
        if not sym or sym == "?":
            continue
        if kind == "ATTENTION":
            if _flag(db, account_id, sym, ev.get("code") or "check", ev.get("note") or f"{sym} needs attention"):
                lines.append(ev.get("note") or sym)
            continue
        if kind == "RENAME":
            if sym in ambiguous:
                note = f"{sym}: more than one new ticker ({', '.join(sorted(rename_targets[sym]))})."
                if _flag(db, account_id, sym, "rename", note):
                    lines.append(note)
                continue
            if _manual_blocks(db, account_id, sym, "RENAME"):
                continue
            new = (ev.get("new_symbol") or "").upper()
            when = ev.get("when") or utcnow()
            _, created = upsert(
                db, account_id, key=ev.get("key") or f"rename:{sym}:{new}:{_day_key(when)}",
                symbol=sym, kind="RENAME", status="applied", source="broker", new_symbol=new, when=when,
                note=f"{sym} renamed to {new}")
            if created:
                lines.append(f"{sym} → {new}")
                _clear_flag(db, account_id, sym, "rename")
            continue
        if kind == "SPLIT" and ev.get("ratio") and ev.get("when"):
            if _manual_blocks(db, account_id, sym, "SPLIT"):
                continue
            ratio = float(ev["ratio"])
            when = ev["when"]
            key = ev.get("key") or f"broker:{sym}:{_day_key(when)}:{_ratio_key(ratio)}"
            _, created = upsert(
                db, account_id, key=key, symbol=sym, kind="SPLIT", status="applied", source="broker",
                ratio=ratio, when=when, note=f"Split ×{_ratio_key(ratio)} (broker)")
            bucket = groups.setdefault(sym, [])
            made = _add_split_execution(db, account_id, sym, when, ratio, key, f"Split ×{_ratio_key(ratio)}", bucket)
            if created or made is not None:
                lines.append(f"{sym}: split ×{_ratio_key(ratio)} on {_day_key(when)}")
                _clear_flag(db, account_id, sym, "ratio")

    lines.extend(_apply_identity_renames(db, account_id, execs))
    lines.extend(materialize(db, account_id))
    from app.symbols import canonical_symbol, load_aliases
    aliases = load_aliases(db)
    positions = _remap_positions(positions, aliases)
    execs = list(db.scalars(select(Execution).where(Execution.account_id == account_id)))
    groups, members = _canonical_groups(execs, aliases)
    wanted = ({canonical_symbol(s, aliases).upper() for s in symbols} if symbols is not None else None)
    for sym, rows in groups.items():
        if wanted is not None and sym not in wanted:
            continue
        if not rows or any(_manual_blocks(db, account_id, raw, "SPLIT") for raw in members.get(sym, {sym})):
            continue
        start = min(e.executed_at for e in rows) - timedelta(days=7)
        end = max(e.executed_at for e in rows) + timedelta(days=2)
        if _open_qty(rows) > 1e-6:
            end = max(end, utcnow() + timedelta(days=1))
        try:
            found = list(fetch(_price_symbol(db, rows), start, end) or [])
        except Exception:  # noqa: BLE001
            found = []
        # Already-adjusted history: a broker position that already matches is left alone.
        journal = _open_qty(rows)
        broker = None if positions is None else float(positions.get(sym, 0.0))
        keep = select_listing_splits(rows, found)
        for when, ratio in keep:
            if broker is not None and abs(journal - broker) <= _tol(journal, broker):
                break
            trial = _open_qty(rows + [_virtual(sym, when, ratio, account_id)])
            if broker is not None and abs(trial - broker) >= abs(journal - broker) - 1e-9:
                # The listing split does not explain the broker quantity. Don't apply it.
                if abs(journal - broker) > _tol(journal, broker):
                    note = (f"{sym}: a {_ratio_key(ratio)}-for-1 on {_day_key(when)} does not match "
                            f"the broker ({_fmt(journal)} in the journal, {_fmt(broker)} at the broker). "
                            f"Suggest: split {_ratio_key(ratio)} on {_day_key(when)} (yahoo).")
                    if _flag(db, account_id, sym, "ratio", note):
                        lines.append(note)
                continue
            key = f"yahoo:{sym}:{_day_key(when)}:{_ratio_key(ratio)}"
            _, created = upsert(
                db, account_id, key=key, symbol=sym, kind="SPLIT", status="applied", source="yahoo",
                ratio=float(ratio), when=when, note=f"Split ×{_ratio_key(ratio)} ({_price_symbol(db, rows)})")
            made = _add_split_execution(
                db, account_id, sym, when, float(ratio), key, f"Split ×{_ratio_key(ratio)}", rows)
            if created or made is not None:
                lines.append(f"{sym}: split ×{_ratio_key(ratio)} on {_day_key(when)}")
                journal = trial
                for raw in members.get(sym, {sym}):
                    _clear_flag(db, account_id, raw, "ratio")

    if positions is not None:
        from app.renames import explain_gap
        execs = list(db.scalars(select(Execution).where(Execution.account_id == account_id)))
        aliases = load_aliases(db)
        groups, members = _canonical_groups(execs, aliases)
        bought = _buy_totals(execs, aliases)
        keys = set(groups) | set(positions)
        for sym in keys:
            if not sym or sym == "?" or " " in sym:
                continue
            rows = groups.get(sym, [])
            journal = _open_qty(rows)
            broker = float(positions.get(sym, 0.0))
            raws = members.get(sym, set()) | {sym}
            if abs(journal - broker) <= _tol(journal, broker):
                for raw in raws:
                    _clear_flag(db, account_id, raw, "qty")
                continue
            raw_groups = _held_rows(execs)
            comps = [(raw, _open_qty(raw_groups.get(raw, []))) for raw in sorted(members.get(sym) or {sym})]
            others = {k: v for k, v in bought.items() if k != sym}
            note = explain_gap(sym, journal, broker, comps, others)
            for raw in raws:
                if raw != sym:
                    _clear_flag(db, account_id, raw, "qty")
            if _flag(db, account_id, sym, "qty", note):
                lines.append(note)
    db.flush()
    return lines


def _tol(journal: float, broker: float) -> float:
    return max(1e-4, 1e-4 * max(abs(journal), abs(broker), 1.0))


def _fmt(n: float) -> str:
    return f"{n:.8g}"


def save_manual(db: Session, account_id: int, kind: str, symbol: str, when: datetime,
                ratio: float | None = None, new_symbol: str | None = None) -> str:
    """User-entered split or rename. Replaces a needs-attention flag on that ticker."""
    sym = (symbol or "").strip().upper()
    if not sym or not re.fullmatch(r"[A-Z][A-Z0-9.]{0,7}", sym):
        raise ValueError("Enter a ticker such as WMT.")
    if when is None:
        raise ValueError("Enter the date.")
    if kind == "rename":
        new = (new_symbol or "").strip().upper()
        if not new or not re.fullmatch(r"[A-Z][A-Z0-9.]{0,7}", new) or new == sym:
            raise ValueError("Enter the new ticker.")
        key = f"manual:{sym}:{_day_key(when)}:rename:{new}"
        upsert(db, account_id, key=key, symbol=sym, kind="RENAME", status="applied", source="manual",
               new_symbol=new, when=when, note=f"{sym} renamed to {new}")
        _clear_flag(db, account_id, sym, "rename")
        _clear_flag(db, account_id, sym, "qty")
        materialize(db, account_id)
        return f"Saved rename {sym} → {new}."
    if ratio is None or ratio <= 0 or abs(ratio - 1.0) <= 1e-9:
        raise ValueError("Enter a split ratio such as 3 or 1:10.")
    key = f"manual:{sym}:{_day_key(when)}:split:{_ratio_key(ratio)}"
    upsert(db, account_id, key=key, symbol=sym, kind="SPLIT", status="applied", source="manual",
           ratio=float(ratio), when=when, note=f"Split ×{_ratio_key(ratio)} (entered by you)")
    for code in ("ratio", "qty"):
        _clear_flag(db, account_id, sym, code)
    materialize(db, account_id)
    return f"Saved {_ratio_key(ratio)}-for-1 split of {sym} on {_day_key(when)}."


def attention_rows(db: Session) -> list[CorporateAdjustment]:
    return list(db.scalars(select(CorporateAdjustment).where(
        CorporateAdjustment.status == "needs_attention").order_by(
            CorporateAdjustment.symbol, CorporateAdjustment.id)))


_SUGGEST_RENAME = re.compile(
    r"Suggest: rename ([A-Z0-9.]+) -> ([A-Z0-9.]+) on (\d{4}-\d{2}-\d{2}) \(([^)]+)\)")
_SUGGEST_SPLIT = re.compile(r"Suggest: split (\S+) on (\d{4}-\d{2}-\d{2}) \(([^)]+)\)")
_BROKER_QTY = re.compile(r"\(([-0-9.eE+]+) in the journal, ([-0-9.eE+]+) at the broker\)")


def _candidate(note: str | None, symbol: str) -> dict | None:
    m = _SUGGEST_RENAME.search(note or "")
    if m:
        return {"kind": "rename", "symbol": m.group(1), "new_symbol": m.group(2), "ratio": "",
                "when": m.group(3), "source": m.group(4)}
    m = _SUGGEST_SPLIT.search(note or "")
    if m:
        return {"kind": "split", "symbol": symbol, "new_symbol": "", "ratio": m.group(1),
                "when": m.group(2), "source": m.group(3)}
    return None


def unresolved_attention(db: Session) -> list:
    """Flags still true after aliases, renames and splits. Reading this does not write.

    Quantity flags on the old and new ticker are one row. When the merged journal quantity
    matches the broker figure in the note, the row is left out (the sync deletes it later).
    """
    from app.renames import explain_gap
    from app.symbols import canonical_symbol, load_aliases

    rows = attention_rows(db)
    if not rows:
        return []
    aliases = load_aliases(db)
    qty, other = [], []
    for row in rows:
        code = (row.external_key or "").rsplit(":", 1)[-1]
        (qty if code == "qty" else other).append(row)
    by_account: dict[int, list] = {}
    if qty:
        ids = {r.account_id for r in qty}
        for aid in ids:
            by_account[aid] = list(db.scalars(select(Execution).where(Execution.account_id == aid)))
    grouped: dict[tuple[int, str], list] = {}
    for row in qty:
        canon = canonical_symbol(row.symbol or "", aliases).upper()
        grouped.setdefault((row.account_id, canon), []).append(row)
    shown = []
    for (aid, canon), flags in grouped.items():
        execs = by_account.get(aid, [])
        groups, members = _canonical_groups(execs, aliases)
        journal = _open_qty(groups.get(canon, []))
        brokers = []
        notes = []
        for flag in flags:
            notes.append(flag.note or "")
            m = _BROKER_QTY.search(flag.note or "")
            if m:
                brokers.append(float(m.group(2)))
        if brokers and abs(journal - max(brokers)) <= _tol(journal, max(brokers)):
            continue
        broker = max(brokers) if brokers else 0.0
        raws = members.get(canon) or {canon}
        raw_groups = _held_rows(execs)
        comps = [(raw, _open_qty(raw_groups.get(raw, []))) for raw in sorted(raws)]
        bought = _buy_totals(execs, aliases)
        others = {k: v for k, v in bought.items() if k != canon}
        note = explain_gap(canon, journal, broker, comps, others) if brokers else (flags[0].note or "")
        blob = " ".join(notes)
        extra = ""
        cand = _candidate(blob, canon)
        if cand and "Suggest:" not in note:
            if cand["kind"] == "rename":
                extra = (f" Suggest: rename {cand['symbol']} -> {cand['new_symbol']} "
                         f"on {cand['when']} ({cand['source']}).")
            else:
                extra = f" Suggest: split {cand['ratio']} on {cand['when']} ({cand['source']})."
        shown.append(SimpleNamespace(
            note=note + extra, symbol=canon, account_id=aid, candidate=cand, also=sorted(raws),
            kind="CHECK", status="needs_attention"))
    for row in other:
        cand = _candidate(row.note, row.symbol)
        shown.append(SimpleNamespace(
            note=row.note, symbol=row.symbol, account_id=row.account_id, candidate=cand, also=[row.symbol],
            kind=row.kind, status=row.status))
    shown.sort(key=lambda r: (r.symbol or "", r.account_id or 0))
    return shown


def attention_view(db: Session) -> list:
    return unresolved_attention(db)


def attention_marks(db: Session) -> set[str]:
    marks = set()
    for row in unresolved_attention(db):
        marks.add(f"{row.account_id}:{row.symbol}")
        for raw in getattr(row, "also", None) or []:
            marks.add(f"{row.account_id}:{raw}")
    return marks


def t212_positions(db: Session, positions: list[dict] | None) -> dict[str, float] | None:
    """Broker open quantity by journal symbol. None when the broker list was not fetched."""
    if positions is None:
        return None
    from app import markets
    from app.symbols import canonical_symbol, load_aliases
    aliases = load_aliases(db)
    out: dict[str, float] = {}
    for p in positions:
        ii = p.get("instrument") or {}
        ticker = ii.get("ticker") or ""
        inst = None
        if ticker:
            inst = db.scalar(select(Instrument).where(
                Instrument.source_key == "t212", Instrument.broker_symbol == ticker))
        base = (inst.symbol if inst is not None else None) or (
            markets.parse_t212_ticker(ticker, ii.get("isin")).symbol if ticker else None) or "?"
        key = canonical_symbol(base, aliases).upper()
        out[key] = out.get(key, 0.0) + float(p.get("quantity") or 0)
    return out


# ---------------------------------------------------------------- raw sync files
def _scrub(obj):
    if isinstance(obj, dict):
        return {k: ("[omitted]" if _SECRET_KEY.search(str(k)) else _scrub(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_scrub(v) for v in obj]
    return obj


def record_payload(db: Session, account_id: int, source: str, payload, *, run_id: int | None = None,
                   kind: str = "json", filename: str | None = None) -> SyncPayload | None:
    """Store the records this sync received. Large bodies are gzipped. The latest 50 are kept."""
    if payload is None:
        return None
    if kind == "csv":
        text = payload if isinstance(payload, str) else str(payload)
        raw = text.encode("utf-8", errors="replace")
        content_type = "text/csv"
        ext = "csv"
    else:
        raw = json.dumps(_scrub(payload), default=str, separators=(",", ":")).encode("utf-8")
        content_type = "application/json"
        ext = "json"
    stamp = utcnow().strftime("%Y%m%d-%H%M%S")
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", (filename or f"{source}-{account_id}-{stamp}.{ext}"))[:110]
    if not safe.lower().endswith(f".{ext}"):
        safe = f"{safe}.{ext}"[:120]
    gzipped = len(raw) >= GZIP_AT
    body = gzip.compress(raw, compresslevel=6) if gzipped else raw
    row = SyncPayload(
        account_id=account_id, sync_run_id=run_id, source=source[:30], filename=safe,
        content_type=content_type, gzipped=gzipped, nbytes=len(raw), body=body,
    )
    db.add(row)
    db.flush()
    ids = list(db.scalars(select(SyncPayload.id).where(SyncPayload.account_id == account_id)
                          .order_by(SyncPayload.id.desc())))
    extra = ids[KEEP_PAYLOADS:]
    if extra:
        db.execute(delete(SyncPayload).where(SyncPayload.id.in_(extra)))
    return row


def payload_index(db: Session, limit: int = 20) -> list:
    return list(db.execute(select(
        SyncPayload.id, SyncPayload.account_id, SyncPayload.source, SyncPayload.created_at,
        SyncPayload.filename, SyncPayload.content_type, SyncPayload.gzipped, SyncPayload.nbytes,
        SyncPayload.sync_run_id,
    ).order_by(SyncPayload.id.desc()).limit(limit)).all())


def payload_body(row: SyncPayload) -> bytes:
    if row.gzipped:
        return gzip.decompress(row.body)
    return bytes(row.body)
