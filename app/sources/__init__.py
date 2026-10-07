"""Pluggable automated data sources.

A data source pulls executions from a broker/aggregator on a schedule ("Sync now" button and the
cron job). CSV import is a manual path and lives in app/importers. To add a source (e.g. SnapTrade),
implement DataSource and add it to `all_sources()`.
"""
from __future__ import annotations

from app.sources.base import DataSource, SourceStatus, SyncContext, SourceResult


def all_sources() -> list[DataSource]:
    from app.sources.schwab_api import SchwabApiSource
    return [SchwabApiSource()]


def enabled_sources() -> list[DataSource]:
    return [s for s in all_sources() if s.is_configured()]


__all__ = ["DataSource", "SourceStatus", "SyncContext", "SourceResult", "all_sources", "enabled_sources"]
