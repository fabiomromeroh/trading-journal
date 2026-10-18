"""Rename pairs proved by the broker's own identifiers, with a real effective date.

A pair is stored only when something already says the two tickers are the same security
(one instrument, a shared ISIN / CUSIP / FIGI / symbol id, or an alias the user or the
built-in list already trusts) and a date comes from that list, a verified listing change,
or an SEC filing that states the effective date. Nothing here invents a fill.
"""
from __future__ import annotations

import json
import os
import re
from collections import defaultdict
from datetime import datetime
from urllib.request import Request, urlopen

from app.symbols import BUILTIN, canonical_ticker

# (old, new) -> (YYYY-MM-DD, source). Trading dates, not the SEC period_ending the day before.
# Used only after an identifier already proved the two tickers are one security.
VERIFIED = {
    "SNII": ("RGTI", "2022-03-02", "nasdaq"),   # Rigetti, Nasdaq/MIAX memo; trading began 2022-03-02
    "XPOA": ("QBTS", "2022-08-08", "nasdaq"),   # DPCM / D-Wave, symbol change 2022-08-08
    "DMYI": ("IONQ", "2021-10-01", "nasdaq"),   # dMY / IonQ, trading began 2021-10-01
}
_CACHE = "symbol_change_cache"
_CACHE_V = 1
_TICKER = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$")
_ISIN = re.compile(r"^[A-Z]{2}[A-Z0-9]{10}$")
_CUSIP = re.compile(r"^[A-Z0-9]{9}$")
_FIGI = re.compile(r"^[A-Z0-9]{12}$")
_ID_KEYS = {
    "cusip": "cusip", "isin": "isin", "figi": "figi", "figi_code": "figi",
    "symbol_id": "symbol_id", "instrumentid": "instrument_id",
}
_MONTHS = {n: i for i, n in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August",
     "September", "October", "November", "December"], 1)}
_CHANGE = re.compile(
    r"(?:ticker|trading)\s+symbol\b.{0,80}?\b([A-Z][A-Z0-9.]{0,7})\b\s+"
    r"(?:to|into|will be|changed to)\s+\b([A-Z][A-Z0-9.]{0,7})\b",
    re.I | re.S)
_EFFECTIVE = re.compile(
    r"effective(?:\s+date)?(?:\s+of)?(?:\s+on)?\s+"
    r"((?:January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+\d{1,2},\s+\d{4}|\d{4}-\d{2}-\d{2})",
    re.I)


class RenamePair:
    def __init__(self, old: str, new: str, *, proven: bool, via: str, isin_conflict: bool,
                 already_aliased: bool, both_fills: bool):
        self.old, self.new = old, new
        self.proven, self.via = proven, via
        self.isin_conflict, self.already_aliased = isin_conflict, already_aliased
        self.both_fills = both_fills


class AmbiguousRename:
    def __init__(self, symbol: str, note: str):
        self.symbol, self.note = symbol, note


def _ok(symbol: str | None) -> str | None:
    s = (symbol or "").upper().strip()
    return s if s and " " not in s and _TICKER.match(s) else None


def _price_base(symbol: str | None) -> str | None:
    raw = (symbol or "").upper().strip()
    return _ok(raw.split(".")[0]) if raw else None


def _valid_id(kind: str, value: str) -> bool:
    v = (value or "").upper().strip()
    if kind == "isin":
        return _ISIN.match(v) is not None
    if kind == "cusip":
        return _CUSIP.match(v) is not None
    if kind == "figi":
        return _FIGI.match(v) is not None
    if kind == "symbol_id":
        return len(v) >= 12
    if kind == "instrument_id":
        return len(v) >= 6
    return False


def ids_from_raw(raw) -> list[tuple[str, str]]:
    """CUSIP / ISIN / FIGI / symbol id / Schwab instrumentId. Activity ids are not identifiers."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "{}")
        except ValueError:
            return []
    found: list[tuple[str, str]] = []

    def walk(obj, depth: int) -> None:
        if depth > 8:
            return
        if isinstance(obj, dict):
            for k, v in obj.items():
                kind = _ID_KEYS.get(str(k).lower())
                if kind and isinstance(v, (str, int)) and not isinstance(v, bool):
                    text = str(v).upper().strip()
                    if _valid_id(kind, text):
                        found.append((kind, text))
                else:
                    walk(v, depth + 1)
        elif isinstance(obj, list):
            for v in obj:
                walk(v, depth + 1)

    walk(raw, 0)
    return found


def _day(value: str) -> datetime:
    y, m, d = (int(p) for p in value.split("-"))
    return datetime(y, m, d, 12, 0)


def date_from_filing(text: str, old: str, new: str) -> datetime | None:
    """Effective date from filing text that says the ticker changed from old to new."""
    if not text:
        return None
    old, new = old.upper(), new.upper()
    for m in _CHANGE.finditer(text):
        a, b = m.group(1).upper(), m.group(2).upper()
        if (a, b) != (old, new):
            continue
        window = text[m.start():m.end() + 240]
        em = _EFFECTIVE.search(window)
        if not em:
            continue
        raw = em.group(1)
        try:
            if re.match(r"\d{4}-\d{2}-\d{2}", raw):
                return _day(raw[:10])
            month, rest = raw.split(" ", 1)
            day_s, year_s = rest.replace(",", "").split()
            return datetime(int(year_s), _MONTHS[month.title()], int(day_s), 12, 0)
        except (KeyError, ValueError):
            continue
    return None


def _sec_date(old: str, new: str) -> datetime | None:
    """One SEC full-text search, then the filing, only when it states an effective date."""
    try:
        from urllib.parse import urlencode
        q = urlencode({"q": f'"{old}" "{new}" "ticker symbol"', "forms": "8-K", "dateRange": "custom",
                       "startdt": "1994-01-01", "enddt": "2035-01-01"})
        req = Request(
            "https://efts.sec.gov/LATEST/search-index?" + q,
            headers={"User-Agent": "trading-journal research (personal journal)"})
        with urlopen(req, timeout=8) as resp:  # noqa: S310 — public SEC search, no secrets
            payload = json.loads(resp.read(400_000).decode("utf-8", "replace"))
        hits = ((payload.get("hits") or {}).get("hits") or [])[:2]
    except Exception:  # noqa: BLE001 — a lookup miss must not abort the sync
        return None
    for hit in hits:
        src = hit.get("_source") or {}
        adsh = str(src.get("adsh") or "")
        ciks = src.get("ciks") or []
        if not adsh or not ciks:
            continue
        cik = str(ciks[0]).lstrip("0") or "0"
        url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{adsh.replace('-', '')}/{adsh}.txt"
        try:
            req = Request(url, headers={"User-Agent": "trading-journal research (personal journal)"})
            with urlopen(req, timeout=8) as resp:  # noqa: S310
                text = resp.read(400_000).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            continue
        found = date_from_filing(text, old, new)
        if found is not None:
            return found
    return None


def _cache_get(db, old: str, new: str):
    if db is None:
        return None
    from app.services import get_state
    try:
        blob = json.loads(get_state(db, _CACHE) or "{}")
    except ValueError:
        return None
    if blob.get("v") != _CACHE_V:
        return None
    key = f"{old}>{new}"
    if key not in blob:
        return None
    day = (blob.get(key) or {}).get("date")
    if not day:
        return False  # cached miss
    try:
        return _day(day), "sec"
    except ValueError:
        return False


def _cache_put(db, old: str, new: str, when: datetime | None) -> None:
    if db is None:
        return
    from app.services import get_state, set_state
    try:
        blob = json.loads(get_state(db, _CACHE) or "{}")
    except ValueError:
        blob = {}
    if blob.get("v") != _CACHE_V:
        blob = {"v": _CACHE_V}
    blob[f"{old}>{new}"] = {"date": when.date().isoformat() if when else None}
    set_state(db, _CACHE, json.dumps(blob))


def lookup_rename(old: str, new: str, *, proven: bool, db=None) -> tuple[datetime, str] | None:
    """(effective datetime, source) or None. Never today's date and never a fill date."""
    old, new = (old or "").upper(), (new or "").upper()
    hit = BUILTIN.get(old)
    if hit and hit[0] == new:
        return _day(hit[1]), "builtin"
    row = VERIFIED.get(old)
    if proven and row and row[0] == new:
        return _day(row[1]), row[2]
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return None
    cached = _cache_get(db, old, new)
    if cached is False:
        return None
    if cached:
        return cached
    found = _sec_date(old, new)
    _cache_put(db, old, new, found)
    if found is not None:
        return found, "sec"
    return None


def detect_pairs(db, execs) -> tuple[list[RenamePair], list[AmbiguousRename]]:
    """Pairs and ambiguous groups for one account's executions. Does not write."""
    from app.markets import parse_t212_ticker
    from app.models import Instrument
    from app.services import get_state
    from app.symbols import USER_STATE, load_aliases
    from sqlalchemy import select

    aliases = load_aliases(db)
    try:
        user = json.loads(get_state(db, USER_STATE) or "{}")
    except ValueError:
        user = {}
    inst_ids = {e.instrument_id for e in execs if getattr(e, "instrument_id", None)}
    instruments = {}
    if inst_ids:
        instruments = {i.id: i for i in db.scalars(select(Instrument).where(Instrument.id.in_(inst_ids)))}

    def tickers_of(inst) -> set[str]:
        out = set()
        for candidate in (
            _ok(getattr(inst, "symbol", None)),
            _price_base(getattr(inst, "price_symbol", None)),
        ):
            if candidate:
                out.add(candidate)
        broker = getattr(inst, "broker_symbol", None)
        if broker:
            base = _ok(parse_t212_ticker(broker, getattr(inst, "isin", None)).symbol)
            if base:
                out.add(base)
        return out

    groups: dict[tuple[str, str], set[str]] = defaultdict(set)
    symbol_isins: dict[str, set[str]] = defaultdict(set)
    symbol_conids: dict[str, set[str]] = defaultdict(set)
    hints: set[tuple[str, str]] = set()
    fill_symbols: set[str] = set()

    def note_conid(sym: str | None, conid) -> None:
        token = str(conid or "").strip()
        ok = _ok(sym)
        if ok and token:
            symbol_conids[ok].add(token)

    def add_hint(inst) -> None:
        old = _ok(getattr(inst, "symbol", None))
        new = _price_base(getattr(inst, "price_symbol", None))
        broker = getattr(inst, "broker_symbol", None)
        base = _ok(parse_t212_ticker(broker, getattr(inst, "isin", None)).symbol) if broker else None
        for src in (old, base):
            if src and new and src != new:
                hints.add((src, new))

    for inst in instruments.values():
        add_hint(inst)
        names = tickers_of(inst)
        for name in names:
            note_conid(name, getattr(inst, "conid", None))
        if getattr(inst, "isin", None) and _valid_id("isin", inst.isin):
            groups[("isin", inst.isin.upper())] |= names
            for s in names:
                symbol_isins[s].add(inst.isin.upper())
        groups[("instrument", str(inst.id))] |= names

    for e in execs:
        if str(getattr(e, "external_id", "") or "").startswith("recon:"):
            continue
        if (getattr(e, "asset_type", None) or "STOCK") == "OPTION" or " " in (getattr(e, "symbol", None) or ""):
            continue
        sym = _ok(getattr(e, "symbol", None))
        if not sym:
            continue
        if (getattr(e, "kind", None) or "TRADE") == "TRADE":
            fill_symbols.add(sym)
        inst = instruments.get(getattr(e, "instrument_id", None))
        names = {sym}
        raw = getattr(e, "raw", None)
        if isinstance(raw, str):
            try:
                raw = json.loads(raw or "{}")
            except ValueError:
                raw = {}
        if isinstance(raw, dict):
            note_conid(sym, raw.get("conid") or raw.get("conId"))
            xfer = raw.get("xfer")
            if isinstance(xfer, dict):
                note_conid(sym, xfer.get("conid"))
        if inst is not None:
            names |= tickers_of(inst)
            if getattr(inst, "isin", None) and _valid_id("isin", inst.isin):
                groups[("isin", inst.isin.upper())] |= names
                for s in names:
                    symbol_isins[s].add(inst.isin.upper())
            groups[("instrument", str(inst.id))] |= names
        for kind, value in ids_from_raw(getattr(e, "raw", None)):
            groups[(kind, value)] |= names

    label = {"isin": "ISIN", "cusip": "CUSIP", "figi": "FIGI", "symbol_id": "symbol id",
             "instrument_id": "broker id", "instrument": "broker instrument"}
    best: dict[tuple[str, str], RenamePair] = {}
    ambiguous: list[AmbiguousRename] = []
    blocked: set[str] = set()

    def keep(pair: RenamePair) -> None:
        if pair.old in user and not user[pair.old]:
            return
        prev = best.get((pair.old, pair.new))
        if prev is None or (pair.proven and not prev.proven):
            best[(pair.old, pair.new)] = pair

    for (kind, value), names in groups.items():
        names = {s for s in names if _ok(s)}
        if len(names) < 2:
            continue
        via = f"{label.get(kind, kind)} {value}" if kind != "instrument" else "broker instrument"
        conid_sets = [symbol_conids.get(s) or set() for s in names]
        if all(conid_sets) and all(conid_sets[i].isdisjoint(conid_sets[j])
                                   for i in range(len(conid_sets)) for j in range(i + 1, len(conid_sets))):
            continue
        if len(names) > 2:
            shown = ", ".join(sorted(names))
            for s in sorted(names):
                blocked.add(s)
                ambiguous.append(AmbiguousRename(
                    s, f"{s}: more than one new ticker ({shown})."))
            continue
        a, b = sorted(names)
        left, right = symbol_conids.get(a) or set(), symbol_conids.get(b) or set()
        if left and right and left.isdisjoint(right):
            continue
        way = _direction(a, b, aliases, hints)
        if way is None:
            blocked.update((a, b))
            ambiguous.append(AmbiguousRename(a, f"{a}: rename between {a} and {b} is ambiguous."))
            continue
        old, new = way
        isins_old, isins_new = symbol_isins.get(old) or set(), symbol_isins.get(new) or set()
        keep(RenamePair(
            old, new, proven=True, via=via,
            isin_conflict=bool(isins_old and isins_new and isins_old.isdisjoint(isins_new)),
            already_aliased=canonical_ticker(old, aliases) == new,
            both_fills=old in fill_symbols and new in fill_symbols))

    for old, new in list(aliases.items()):
        dest = canonical_ticker(old, aliases)
        if not dest or dest == old or old not in fill_symbols or dest not in fill_symbols:
            continue
        if old in blocked or dest in blocked:
            continue
        left, right = symbol_isins.get(old) or set(), symbol_isins.get(dest) or set()
        keep(RenamePair(
            old, dest, proven=False, via="alias",
            isin_conflict=bool(left and right and left.isdisjoint(right)),
            already_aliased=True, both_fills=True))
    return list(best.values()), ambiguous


def _direction(a: str, b: str, aliases: dict, hints: set[tuple[str, str]]) -> tuple[str, str] | None:
    a_maps = canonical_ticker(a, aliases) == b and a in aliases
    b_maps = canonical_ticker(b, aliases) == a and b in aliases
    if a_maps and not b_maps:
        return a, b
    if b_maps and not a_maps:
        return b, a
    if a_maps and b_maps:
        return None
    ab, ba = (a, b) in hints, (b, a) in hints
    if ab and not ba:
        return a, b
    if ba and not ab:
        return b, a
    return None


def _fmt(n: float) -> str:
    return f"{n:.8g}"


def explain_gap(symbol: str, journal: float, broker: float, components: list[tuple[str, float]],
                buy_totals: dict[str, float]) -> str:
    """Why the merged quantity still disagrees. No date is invented for a spin-off."""
    text = (f"{symbol}: quantity still does not match the broker "
            f"({_fmt(journal)} in the journal, {_fmt(broker)} at the broker).")
    parts = [f"{s} {_fmt(q)}" for s, q in components if abs(q) > 1e-8]
    if len(components) > 1 and parts:
        text += " Held as " + " + ".join(parts) + "."
    tol = max(1e-4, 1e-4 * max(abs(broker), 1.0))
    if journal < -1e-4 and abs(broker) <= tol:
        sold = abs(journal)
        hits = []
        for other, bought in buy_totals.items():
            if other == symbol or bought <= 0:
                continue
            n = round(bought / sold)
            rel = abs(bought - n * sold) / sold
            if 2 <= n <= 100 and rel <= 0.002:
                hits.append((rel, other, bought, int(n)))
        hits.sort()
        # An exact ratio wins. Two ratios that are similarly close are not named.
        if len(hits) == 1 or (len(hits) > 1 and hits[0][0] < 1e-6 and hits[1][0] > 1e-4):
            _, other, bought, n = hits[0]
            text += (f" Likely cause: sold {_fmt(sold)} with no opening buy in the broker history. "
                     f"That is 1 share for every {n} {other} shares bought ({_fmt(bought)}). "
                     f"Not a ticker rename.")
    return text
