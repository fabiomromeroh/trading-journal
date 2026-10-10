"""Broker catalogue shown in Settings > Brokers: which brokers can be connected and in what state."""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session


@dataclass
class BrokerCard:
    key: str
    name: str
    how: str
    currency: str
    state: str          # connected | needs_login | not_connected | unavailable
    detail: str = ""
    last_sync: str | None = None
    form: str | None = None   # which connect form the template renders


def cards(db: Session) -> list[BrokerCard]:
    from app.sources import all_sources
    out: list[BrokerCard] = []
    by_key = {s.key: s for s in all_sources()}
    snap, schwab = by_key.get("snaptrade"), by_key.get("schwab_api")
    state, detail = "not_connected", "Connect from the Automated data sources card below, or import CSV / thinkorswim statements."
    for src in (snap, schwab):
        if src is not None and src.is_configured():
            st = src.status(db)
            state = "connected" if st.ready else "needs_login"
            detail = f"{src.name}: {st.message}"
            break
    out.append(BrokerCard("schwab", "Schwab", "SnapTrade (daily) · Schwab API · thinkorswim email/CSV", "USD", state, detail))
    try:
        from app.sources.trading212 import card as t212_card
        out.append(t212_card(db))
    except ImportError:
        out.append(BrokerCard("t212", "Trading 212", "Official API key + secret (read-only)", "EUR", "unavailable",
                              "Adapter not installed in this build."))
    try:
        from app.sources.ibkr_flex import card as ibkr_card
        out.append(ibkr_card(db))
    except ImportError:
        out.append(BrokerCard("ibkr", "Interactive Brokers", "Flex Web Service (token + query id)", "USD", "unavailable",
                              "Planned after Trading 212; CSV import works today."))
    return out
