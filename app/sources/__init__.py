"""Pluggable automated data sources.

A data source pulls executions from a broker/aggregator when you click "Sync now" (or run
`python -m app.sync`). There is deliberately no scheduled sync. CSV import is a separate manual path
in app/importers. To add a source, implement DataSource and add it to `all_sources()`.
"""
from __future__ import annotations

from app.sources.base import DataSource, SourceStatus, SyncContext, SourceResult


def all_sources() -> list[DataSource]:
    from app.sources.schwab_api import SchwabApiSource
    from app.sources.snaptrade import SnapTradeSource
    from app.sources.trading212 import Trading212Source
    from app.sources.ibkr_flex import IbkrFlexSource
    # the two Schwab sources first (unchanged behaviour); broker adapters report "not configured" until a key is saved
    return [SnapTradeSource(), SchwabApiSource(), Trading212Source(), IbkrFlexSource()]


def enabled_sources() -> list[DataSource]:
    return [s for s in all_sources() if s.is_configured()]


__all__ = ["DataSource", "SourceStatus", "SyncContext", "SourceResult", "all_sources", "enabled_sources"]
