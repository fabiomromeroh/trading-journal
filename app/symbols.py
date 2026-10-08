"""Ticker renames (symbol changes with the same security / CUSIP).

Brokers report history differently after a rename: SnapTrade and the Schwab.com export keep the
ticker that was traded at the time (SATS), while thinkorswim rewrites history with the current one
(ECHO). Executions keep the symbol their source reported; trades and cross-source matching use the
canonical (current) ticker from this alias table, so the same fills are recognised and a position
continues across a rename.

Sources of aliases, later ones win: built-in list < auto-detected (from imports) < Settings.
A Settings entry with an empty target ("SATS=") disables a built-in or detected alias.
"""
from __future__ import annotations

import json
import re

# old ticker -> (new ticker, effective date, note). Only verified renames.
BUILTIN = {
    "SATS": ("ECHO", "2026-06-24", "EchoStar: Nasdaq ticker SATS -> ECHO, CUSIP unchanged"),
    "FB": ("META", "2022-06-09", "Meta Platforms: FB -> META"),
}
USER_STATE = "symbol_aliases"          # JSON {"OLD": "NEW" | ""}
AUTO_STATE = "symbol_aliases_auto"     # JSON {"OLD": {"to": "NEW", "evidence": "..."}}
_TICKER = re.compile(r"^[A-Z][A-Z0-9.\-/]{0,9}$")


def parse_alias_text(text: str) -> dict[str, str]:
    """'OLD=NEW' / 'OLD -> NEW' / 'OLD NEW' per line; '#' comments; 'OLD=' disables."""
    out: dict[str, str] = {}
    for line in (text or "").splitlines():
        line = line.split("#", 1)[0].strip().upper()
        if not line:
            continue
        parts = [p for p in re.split(r"\s*(?:->|=>|=|,|\s)\s*", line) if p != ""]
        if not parts or len(parts) > 2 or not _TICKER.match(parts[0]):
            raise ValueError(f"Can't read alias line: {line!r} (use OLD=NEW)")
        new = parts[1] if len(parts) > 1 else ""
        if new and not _TICKER.match(new):
            raise ValueError(f"Can't read alias line: {line!r} (use OLD=NEW)")
        if new == parts[0]:
            continue
        out[parts[0]] = new
    return out


def load_aliases(db) -> dict[str, str]:
    from app.services import get_state
    table = {old: new for old, (new, _d, _n) in BUILTIN.items()}
    try:
        for old, v in json.loads(get_state(db, AUTO_STATE) or "{}").items():
            table[old] = v["to"] if isinstance(v, dict) else v
    except ValueError:
        pass
    try:
        for old, new in json.loads(get_state(db, USER_STATE) or "{}").items():
            if new:
                table[old] = new
            else:
                table.pop(old, None)
    except ValueError:
        pass
    return table


def canonical_ticker(t: str | None, aliases: dict[str, str]) -> str | None:
    seen = set()
    while t in aliases and t not in seen and aliases[t]:
        seen.add(t)
        t = aliases[t]
    return t


def canonical_symbol(symbol: str, aliases: dict[str, str]) -> str:
    """Stock 'SATS' -> 'ECHO'; option 'SATS 2026-09-18 30C' -> 'ECHO 2026-09-18 30C'."""
    if not aliases or not symbol:
        return symbol
    head, sep, rest = symbol.partition(" ")
    new = canonical_ticker(head, aliases)
    return f"{new}{sep}{rest}" if new != head else symbol


def symbols_for(canon: set[str], aliases: dict[str, str]) -> set[str]:
    """All tickers whose canonical form is in canon (for querying stored executions)."""
    out = set(canon)
    for old in aliases:
        if canonical_ticker(old, aliases) in canon:
            out.add(old)
    return out


def save_auto(db, detected: dict[str, tuple[str, str]]) -> None:
    from app.services import get_state, set_state
    cur = json.loads(get_state(db, AUTO_STATE) or "{}")
    for old, (new, evidence) in detected.items():
        cur[old] = {"to": new, "evidence": evidence}
    set_state(db, AUTO_STATE, json.dumps(cur))


def describe(db) -> list[dict]:
    """Rows for the Settings page."""
    from app.services import get_state
    user = json.loads(get_state(db, USER_STATE) or "{}")
    auto = json.loads(get_state(db, AUTO_STATE) or "{}")
    rows = []
    for old, (new, eff, note) in BUILTIN.items():
        rows.append({"old": old, "new": new, "origin": "built-in", "note": f"{note} (from {eff})",
                     "disabled": old in user and not user[old]})
    for old, v in auto.items():
        rows.append({"old": old, "new": v.get("to"), "origin": "detected", "note": v.get("evidence", ""),
                     "disabled": old in user and not user[old]})
    for old, new in user.items():
        if new:
            rows.append({"old": old, "new": new, "origin": "settings", "note": "", "disabled": False})
    return rows
