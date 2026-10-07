"""File importers. Each returns a ParseResult of normalized ExecRecords."""
from __future__ import annotations

from app.importers.base import ParseResult, UnknownFormat
from app.importers import schwab_csv, tos_statement

IMPORTERS = {
    schwab_csv.FORMAT: schwab_csv,
    tos_statement.FORMAT: tos_statement,
}
LABELS = {
    schwab_csv.FORMAT: "Schwab.com Transactions CSV",
    tos_statement.FORMAT: "thinkorswim Account Statement CSV",
}


def decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def detect_format(text: str) -> str:
    for fmt, mod in IMPORTERS.items():
        if mod.detect(text):
            return fmt
    raise UnknownFormat(
        "Unrecognised file. Expected a Schwab.com Transactions export (Accounts > History > Export > CSV) "
        "or a thinkorswim Account Statement export.")


def parse(text: str, fmt: str | None = None, **kw) -> ParseResult:
    fmt = fmt or detect_format(text)
    return IMPORTERS[fmt].parse(text, **kw)
