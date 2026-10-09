"""Win / loss / break-even classification of a closed trade (or a day) by net P&L, with a
user-configurable break-even range in dollars (Settings > Break-even range; default 0 to 0, i.e.
only exactly $0.00 is BE).

    BE   : lo <= net <= hi
    WIN  : net >  hi
    LOSS : net <  lo

Convention (TraderSync-style): BE trades are excluded from the win and loss counts, so
Win % = wins / (wins + losses); gross profit / gross loss, profit factor, avg win / avg loss only use
wins / losses; BE trades' P&L still counts in net P&L, expectancy and every total.

The range is a process-wide setting (single-user app) loaded from app_state at the start of every
request (app.db.get_db), so a change applies immediately everywhere without any migration.
"""
from __future__ import annotations

import json

STATE_KEY = "be_range"
EPS = 1e-9
_range = (0.0, 0.0)


def get() -> tuple[float, float]:
    return _range


def set_range(lo: float = 0.0, hi: float = 0.0) -> tuple[float, float]:
    global _range
    lo, hi = float(lo), float(hi)
    if lo > 0 or hi < 0 or lo > hi:
        raise ValueError("The break-even range must include $0 (lower ≤ 0 ≤ upper).")
    _range = (round(lo, 2), round(hi, 2))
    return _range


def load(db) -> tuple[float, float]:
    """Read the stored range (falls back to 0/0) and make it current."""
    from app.services import get_state
    try:
        raw = get_state(db, STATE_KEY)
        lo, hi = json.loads(raw) if raw else (0.0, 0.0)
        return set_range(lo, hi)
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        return set_range(0.0, 0.0)


def save(db, lo: float, hi: float) -> tuple[float, float]:
    from app.services import set_state
    r = set_range(lo, hi)
    set_state(db, STATE_KEY, json.dumps(list(r)))
    return r


def classify(pnl: float | None) -> str | None:
    if pnl is None:
        return None
    lo, hi = _range
    if pnl > hi + EPS:
        return "win"
    if pnl < lo - EPS:
        return "loss"
    return "be"


def is_win(pnl) -> bool:
    return classify(pnl) == "win"


def is_loss(pnl) -> bool:
    return classify(pnl) == "loss"


def is_be(pnl) -> bool:
    return classify(pnl) == "be"


def label(t) -> str:
    """Badge text for a trade: OPEN / WIN / LOSS / BE."""
    if getattr(t, "status", "CLOSED") == "OPEN":
        return "OPEN"
    return {"win": "WIN", "loss": "LOSS", "be": "BE"}[classify(t.net_pnl)]


def describe() -> str:
    lo, hi = _range
    if lo == 0 and hi == 0:
        return "BE = net exactly $0.00"
    return f"BE = net between {_m(lo)} and {_m(hi)}"


def _m(v):
    return ("-$" if v < 0 else "+$" if v > 0 else "$") + f"{abs(v):,.2f}"


def sql_win(col):
    return col > _range[1] + EPS


def sql_loss(col):
    return col < _range[0] - EPS


def sql_be(col):
    return col.between(_range[0] - EPS, _range[1] + EPS)
