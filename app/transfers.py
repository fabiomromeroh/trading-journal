"""Pair internal IBKR transfers and move lots at cost. No network."""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict, deque
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import Account, CorporateAdjustment, Execution, Instrument

SOURCE = "ibkr_flex"
QTY_TOL = 1e-4
BDAY_WINDOW = 5
SLICE = "#xferlot"


def _raw(ex: Execution) -> dict:
    if not ex.raw:
        return {}
    try:
        data = json.loads(ex.raw)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _dump(raw: dict) -> str:
    return json.dumps(raw, default=str)


def _when(value) -> datetime | None:
    if isinstance(value, datetime):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def business_days(a, b) -> int:
    """Weekdays strictly after the earlier date, through the later date. Same day is 0."""
    if a == b:
        return 0
    start, end = (a, b) if a < b else (b, a)
    count = 0
    day = start
    while day < end:
        day += timedelta(days=1)
        if day.weekday() < 5:
            count += 1
        if count > BDAY_WINDOW + 2:
            break
    return count


def _pair_id(out_ext: str, in_ext: str) -> str:
    raw = f"p:{out_ext}:{in_ext}"
    if len(raw) <= 64:
        return raw
    return "p:" + hashlib.sha256(raw.encode()).hexdigest()[:62]


def _conid_of(ex: Execution, instruments: dict[int, Instrument]) -> str:
    raw = _raw(ex)
    xfer = raw.get("xfer") if isinstance(raw.get("xfer"), dict) else {}
    for source in (xfer, raw):
        token = str(source.get("conid") or "").strip()
        if token:
            return token
    inst = instruments.get(ex.instrument_id) if ex.instrument_id else None
    return str(inst.conid or "").strip() if inst is not None and inst.conid else ""


def _is_slice(ex: Execution) -> bool:
    return SLICE in (ex.external_id or "")


def _note(symbol: str, text: str) -> str:
    return f"{symbol}: {text}"


def _upsert_note(db: Session, account_id: int, symbol: str, code: str, note: str) -> None:
    from app.adjustments import upsert
    upsert(db, account_id, key=f"attn:{symbol}:{code}", symbol=symbol, kind="CHECK",
           status="needs_attention", source="broker", note=note)


def _clear_note(db: Session, account_id: int, symbol: str, code: str) -> None:
    db.execute(delete(CorporateAdjustment).where(
        CorporateAdjustment.account_id == account_id,
        CorporateAdjustment.external_key == f"attn:{symbol}:{code}"))


def _void(db: Session, ex: Execution) -> None:
    """Drop a transfer that must not open a $0 lot, and remember its id so a repeat import skips it.

    A rebuild deletes these markers with the other corporate rows and imports the transfer again.
    """
    from app.adjustments import upsert
    ext = (ex.external_id or "")[:180]
    upsert(db, ex.account_id, key=f"xfer-void:{ext}", symbol=(ex.symbol or "?")[:64],
           kind="CHECK", status="applied", source="broker",
           note=f"{ex.symbol or '?'}: transfer was not booked.")
    db.delete(ex)


def _bookable(ex: Execution) -> bool:
    if (ex.asset_type or "") == "OPTION" or " " in (ex.symbol or ""):
        return False
    return True


def _reset_primary(ex: Execution) -> dict | None:
    """Put a transfer execution back to the imported row. Returns the xfer dict."""
    raw = _raw(ex)
    xfer = raw.get("xfer") if isinstance(raw.get("xfer"), dict) else None
    if not xfer:
        return None
    qty = abs(float(xfer.get("qty") or 0.0))
    if qty <= 1e-12:
        return None
    cost = xfer.get("cost")
    cost = float(cost) if cost not in (None, "") else None
    when = _when(xfer.get("when")) or ex.executed_at
    direction = (xfer.get("direction") or "").upper()
    ex.quantity = round(qty, 8)
    ex.price = round(cost / qty, 8) if cost else 0.0
    ex.executed_at = when
    ex.side = "BUY" if direction == "IN" else "SELL"
    ex.position_effect = "OPEN" if direction == "IN" else "CLOSE"
    ex.kind = "TRANSFER"
    ex.transfer_pair_id = None
    ex.fees = ex.fees or 0.0
    return xfer


def _lots_for(execs: list[Execution], instruments: dict[int, Instrument], conid: str, account_id: int):
    """Non-transfer fills for one account and conid, in time order."""
    rows = []
    for ex in execs:
        if ex.account_id != account_id or ex.kind == "TRANSFER" or _is_slice(ex):
            continue
        if (ex.asset_type or "") == "OPTION" or " " in (ex.symbol or ""):
            continue
        if _conid_of(ex, instruments) != conid:
            continue
        rows.append(ex)
    rows.sort(key=lambda e: (e.executed_at, e.id or 0))
    return rows


def _consume(lots: deque, qty: float, when: datetime) -> list[tuple[float, float, datetime]] | None:
    """FIFO lots available at ``when``. None when they do not cover ``qty`` (lots stay put)."""
    snapshot = [list(lot) for lot in lots]
    taken: list[tuple[float, float, datetime]] = []
    left = qty
    for lot in snapshot:
        if left <= 1e-8:
            break
        if lot[3] > when:
            continue
        use = min(lot[0], left)
        if use <= 1e-12:
            continue
        taken.append((use, lot[1], lot[2]))
        lot[0] -= use
        left -= use
    if left > QTY_TOL:
        return None
    lots.clear()
    for lot in snapshot:
        if lot[0] > 1e-8:
            lots.append(lot)
    return taken


def _weighted(taken: list[tuple[float, float, datetime]], fallback: float | None, qty: float) -> float | None:
    """Exit price that realises nothing. Source-lot cost wins; IBKR cost fills a gap."""
    if qty <= 1e-12:
        return None
    if taken:
        covered = sum(part[0] for part in taken)
        cost = sum(part[0] * part[1] for part in taken)
        if covered > 1e-12 and abs(covered - qty) <= QTY_TOL:
            return cost / covered
        if fallback and covered > 1e-12:
            return (cost + (fallback / qty) * (qty - covered)) / qty
        return None
    if fallback:
        return fallback / qty
    return None


def _apply_trades(lots: deque, trades: list[Execution], until: datetime, cursor: list[int]) -> None:
    while cursor[0] < len(trades) and trades[cursor[0]].executed_at <= until:
        ex = trades[cursor[0]]
        cursor[0] += 1
        qty = abs(float(ex.quantity or 0.0))
        if qty <= 1e-12:
            continue
        if ex.kind == "SPLIT" and ex.position_effect == "RATIO" and abs(qty - 1.0) > 1e-6:
            for lot in lots:
                lot[0] *= qty
                if qty:
                    lot[1] /= qty
            continue
        if ex.side == "BUY":
            lots.append([qty, float(ex.price or 0.0), ex.executed_at, ex.executed_at])
        elif ex.side == "SELL":
            held = sum(lot[0] for lot in lots if lot[3] <= ex.executed_at)
            if held > 1e-12:
                _consume(lots, min(qty, held), ex.executed_at)


def _write_in(db: Session, primary: Execution, taken: list[tuple[float, float, datetime]],
              pair: str, transfer_when: datetime, ibkr_cost: float | None) -> list[Execution]:
    """Open the destination at the source lots' original dates and costs."""
    if not taken:
        qty = abs(float(primary.quantity or 0.0))
        if ibkr_cost and qty:
            primary.side = "BUY"
            primary.position_effect = "OPEN"
            primary.kind = "TRANSFER"
            primary.price = round(ibkr_cost / qty, 8)
            primary.executed_at = transfer_when
            primary.transfer_pair_id = pair
            return [primary]
        return []
    pieces = sorted(taken, key=lambda item: (item[2], item[1]))
    first_qty, first_px, first_at = pieces[0]
    primary.side = "BUY"
    primary.position_effect = "OPEN"
    primary.kind = "TRANSFER"
    primary.quantity = round(first_qty, 8)
    primary.price = round(first_px, 8)
    primary.executed_at = first_at
    primary.transfer_pair_id = pair
    primary.fees = 0.0
    made = [primary]
    for index, (qty, price, opened) in enumerate(pieces[1:], start=1):
        ext = f"{primary.external_id}{SLICE}{index}"[:200]
        row = Execution(
            account_id=primary.account_id, source=primary.source, external_id=ext,
            match_key=f"xfer|{pair}|{index}|{opened.isoformat()}|{qty:.4f}|{price:.4f}",
            symbol=primary.symbol, underlying=primary.underlying or primary.symbol,
            asset_type="STOCK", side="BUY", quantity=round(qty, 8), price=round(price, 8),
            fees=0.0, executed_at=opened, time_known=primary.time_known, seq=index,
            position_effect="OPEN", kind="TRANSFER", multiplier=1.0,
            description=primary.description, currency=primary.currency,
            instrument_id=primary.instrument_id, transfer_pair_id=pair,
            raw=_dump({"xfer_slice": True, "pair": pair, "opened": opened.isoformat()}),
        )
        db.add(row)
        made.append(row)
    return made


def pair_transfers(db: Session, account_ids: list[int] | None = None) -> set[int]:
    """Match OUT to IN across the user's IBKR accounts and move lots at cost.

    Safe to run again: slice rows are removed and each primary is reset from ``raw.xfer``.
    Returns every account whose rows were touched.
    """
    accounts = list(db.scalars(select(Account).where(Account.source_key == SOURCE)))
    # Every IBKR account is visible so an in stored earlier can match an out imported now.
    ids = [a.id for a in accounts]
    if not ids:
        return set()
    rows = list(db.scalars(select(Execution).where(
        Execution.account_id.in_(ids), Execution.kind.in_(("TRANSFER", "VOID")))))
    touched: set[int] = set()
    for ex in rows:
        if _is_slice(ex):
            db.delete(ex)
            touched.add(ex.account_id)
    db.flush()
    rows = [ex for ex in rows if not _is_slice(ex)]
    primaries: list[tuple[Execution, dict]] = []
    for ex in rows:
        if not _bookable(ex):
            continue
        xfer = _reset_primary(ex)
        if xfer is None:
            continue
        if not str(xfer.get("conid") or _conid_of(ex, {}) or "").strip() and not ex.instrument_id:
            # Still reset above; a missing conid is handled when the row is unpaired.
            pass
        primaries.append((ex, xfer))
        touched.add(ex.account_id)
    db.flush()
    if not primaries:
        return touched

    inst_ids = {ex.instrument_id for ex, _x in primaries if ex.instrument_id}
    # Trades that fund the lots may sit on instruments the transfer rows also use.
    all_execs = list(db.scalars(select(Execution).where(Execution.account_id.in_(ids))))
    inst_ids |= {ex.instrument_id for ex in all_execs if ex.instrument_id}
    instruments = {i.id: i for i in db.scalars(select(Instrument).where(Instrument.id.in_(inst_ids or [-1])))}

    outs = []
    ins = []
    for ex, xfer in primaries:
        conid = str(xfer.get("conid") or _conid_of(ex, instruments) or "")
        item = {"ex": ex, "xfer": xfer, "conid": conid, "qty": abs(float(xfer.get("qty") or ex.quantity or 0)),
                "when": _when(xfer.get("when")) or ex.executed_at, "cost": xfer.get("cost"),
                "cp": str(xfer.get("counterparty") or "")}
        if (xfer.get("direction") or "") == "OUT":
            outs.append(item)
        else:
            ins.append(item)
    used_in: set[int] = set()
    pairs: list[tuple[dict, dict]] = []
    outs.sort(key=lambda item: (item["when"], item["ex"].account_id, item["ex"].id or 0))
    for out in outs:
        best = None
        best_key = None
        for inn in ins:
            if inn["ex"].id in used_in or inn["ex"].account_id == out["ex"].account_id:
                continue
            if not out["conid"] or out["conid"] != inn["conid"]:
                continue
            if abs(out["qty"] - inn["qty"]) > QTY_TOL:
                continue
            dist = business_days(out["when"].date(), inn["when"].date())
            if dist > BDAY_WINDOW:
                continue
            out_acct = db.get(Account, out["ex"].account_id)
            in_acct = db.get(Account, inn["ex"].account_id)
            cp_hit = 0
            if out["cp"] and in_acct is not None and out["cp"] == (in_acct.external_ref or ""):
                cp_hit = 1
            if inn["cp"] and out_acct is not None and inn["cp"] == (out_acct.external_ref or ""):
                cp_hit = 1
            key = (0 if cp_hit else 1, dist, inn["ex"].account_id, inn["ex"].id or 0)
            if best_key is None or key < best_key:
                best_key = key
                best = inn
        if best is not None:
            used_in.add(best["ex"].id)
            pairs.append((out, best))
    paired_ids = {id(item["ex"]) for pair in pairs for item in pair}

    # Replay per account+conid so each transfer sees the shares held at that moment.
    books: dict[tuple[int, str], deque] = defaultdict(deque)
    trade_rows: dict[tuple[int, str], list[Execution]] = defaultdict(list)
    for ex in all_execs:
        if ex.kind in {"TRANSFER", "VOID"} or _is_slice(ex):
            continue
        conid = _conid_of(ex, instruments)
        if not conid:
            continue
        trade_rows[(ex.account_id, conid)].append(ex)
    cursors: dict[tuple[int, str], list[int]] = {}
    for key, group in trade_rows.items():
        group.sort(key=lambda e: (e.executed_at, e.id or 0))
        cursors[key] = [0]

    events = []
    for out, inn in pairs:
        events.append((out["when"], 0, out, inn))
    for item in outs + ins:
        if id(item["ex"]) in paired_ids:
            continue
        events.append((item["when"], 1 if (item["xfer"].get("direction") == "OUT") else 2, item, None))
    events.sort(key=lambda ev: (ev[0], ev[1], ev[2]["ex"].id or 0))

    problems: dict[tuple[int, str], str] = {}
    cleared: set[tuple[int, str]] = set()

    def mark(account_id: int, symbol: str, text: str) -> None:
        problems[(account_id, symbol)] = text

    def ok(account_id: int, symbol: str) -> None:
        cleared.add((account_id, symbol))

    for when, _rank, left, right in events:
        conid = left["conid"]
        symbol = (left["ex"].symbol or "").upper()
        _apply_trades(books[(left["ex"].account_id, conid)], trade_rows[(left["ex"].account_id, conid)],
                      when, cursors.setdefault((left["ex"].account_id, conid), [0]))
        if right is not None:
            _apply_trades(books[(right["ex"].account_id, conid)], trade_rows[(right["ex"].account_id, conid)],
                          when, cursors.setdefault((right["ex"].account_id, conid), [0]))
            taken = _consume(books[(left["ex"].account_id, conid)], left["qty"], when) if conid else None
            cost_total = left["cost"] if left["cost"] not in (None, "") else right["cost"]
            cost_total = float(cost_total) if cost_total not in (None, "") else None
            price = _weighted(taken or [], cost_total, left["qty"])
            if price is None:
                _void(db, left["ex"])
                _void(db, right["ex"])
                touched.add(left["ex"].account_id)
                touched.add(right["ex"].account_id)
                mark(left["ex"].account_id, symbol,
                     _note(symbol, "internal transfer has no cost basis and no source lot, so it was not booked."))
                mark(right["ex"].account_id, symbol,
                     _note(symbol, "internal transfer has no cost basis and no source lot, so it was not booked."))
                continue
            pair = _pair_id(left["ex"].external_id, right["ex"].external_id)
            left["ex"].side = "SELL"
            left["ex"].position_effect = "CLOSE"
            left["ex"].kind = "TRANSFER"
            left["ex"].quantity = round(left["qty"], 8)
            left["ex"].price = round(price, 8)
            left["ex"].executed_at = left["when"]
            left["ex"].transfer_pair_id = pair
            left["ex"].fees = 0.0
            opened = _write_in(db, right["ex"], taken or [], pair, right["when"], cost_total)
            if not opened:
                _void(db, right["ex"])
                mark(right["ex"].account_id, symbol,
                     _note(symbol, "internal transfer in has no cost basis, so no lot was opened."))
            else:
                # The shares exist at the destination only from the transfer date.
                for qty, px, opened_at in (taken or [(right["qty"], price, right["when"])]):
                    books[(right["ex"].account_id, conid)].append([qty, px, opened_at, right["when"]])
                ok(right["ex"].account_id, symbol)
            ok(left["ex"].account_id, symbol)
            touched.add(left["ex"].account_id)
            touched.add(right["ex"].account_id)
            continue

        # Unpaired.
        direction = left["xfer"].get("direction")
        cost_total = left["cost"] if left["cost"] not in (None, "") else None
        cost_total = float(cost_total) if cost_total not in (None, "") else None
        ex = left["ex"]
        if direction == "OUT":
            taken = _consume(books[(ex.account_id, conid)], left["qty"], when) if conid else None
            price = _weighted(taken or [], cost_total, left["qty"])
            if price is None:
                _void(db, ex)
                mark(ex.account_id, symbol,
                     _note(symbol, "unpaired transfer out has no cost basis, so it was not closed at $0."))
            else:
                ex.side = "SELL"
                ex.position_effect = "CLOSE"
                ex.price = round(price, 8)
                ex.quantity = round(left["qty"], 8)
                ex.executed_at = left["when"]
                ex.transfer_pair_id = None
                mark(ex.account_id, symbol,
                     _note(symbol, "unpaired transfer out was closed at cost. No matching transfer in was found."))
            touched.add(ex.account_id)
            continue
        if cost_total and left["qty"]:
            ex.side = "BUY"
            ex.position_effect = "OPEN"
            ex.price = round(cost_total / left["qty"], 8)
            ex.quantity = round(left["qty"], 8)
            ex.executed_at = left["when"]
            ex.transfer_pair_id = None
            books[(ex.account_id, conid)].append([left["qty"], ex.price, left["when"], left["when"]])
            mark(ex.account_id, symbol,
                 _note(symbol, "unpaired transfer in was opened at the IBKR cost. No matching transfer out was found."))
        else:
            _void(db, ex)
            mark(ex.account_id, symbol,
                 _note(symbol, "unpaired transfer in has no cost basis, so no $0 lot was opened."))
        touched.add(ex.account_id)

    for (account_id, symbol), text in problems.items():
        code = "xfer-cost" if ("no cost" in text or "$0" in text) else "xfer"
        other = "xfer" if code == "xfer-cost" else "xfer-cost"
        _clear_note(db, account_id, symbol, other)
        _upsert_note(db, account_id, symbol, code, text)
    for account_id, symbol in cleared:
        if (account_id, symbol) in problems:
            continue
        _clear_note(db, account_id, symbol, "xfer")
        _clear_note(db, account_id, symbol, "xfer-cost")
    db.flush()
    if account_ids is not None:
        # Still return counterparties outside the requested set; the caller rebuilds them.
        return touched
    return touched


def xfer_blob(row: dict, direction: str, qty: float, cost: float | None, when: datetime,
              counterparty: str, conid: str, txn: str) -> dict:
    return {
        "direction": direction,
        "conid": conid,
        "type": (row.get("type") or row.get("transferType") or ""),
        "counterparty": counterparty,
        "when": when.isoformat(timespec="seconds"),
        "cost": cost,
        "qty": qty,
        "txn": txn,
    }
