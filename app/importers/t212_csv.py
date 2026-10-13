"""Trading 212 history CSV (History → Export).

Columns vary by year and account currency. The header is recognised when it has Action, Time,
ISIN, Ticker and a shares column. Rows are deduped by the broker ID (``external_id``). Buys and
sells are trades; splits, spin-offs, distributions, conversions and dividend reinvestments are
corporate actions. Cash dividends, deposits, interest and card rows are skipped (they do not
change the share count). Prices in GBX are stored as pounds.
"""
from __future__ import annotations

import csv
import io
import re
from datetime import timezone
from zoneinfo import ZoneInfo

from app.corporate import classify_csv_action, parse_csv_time, ratio_from_text
from app.importers.base import ParseResult, parse_money
from app.instruments import ExecRecord
from app.markets import exchange_tz, parse_t212_ticker

FORMAT = "t212_csv"
_SHARES = re.compile(r"no\.?\s*of\s*shares|shares", re.I)


def _headers(row: list[str]) -> list[str]:
    return [re.sub(r"\s+", " ", (c or "").strip().strip('"').lower()) for c in row]


def detect(text: str) -> bool:
    sample = text.lstrip("\ufeff")
    for row in list(csv.reader(io.StringIO(sample)))[:8]:
        h = _headers(row)
        if "action" in h and "isin" in h and "ticker" in h and any(_SHARES.search(c) for c in h):
            return True
    return False


def _col(header: list[str], *names: str) -> int | None:
    for n in names:
        if n in header:
            return header.index(n)
    for i, h in enumerate(header):
        for n in names:
            if h.startswith(n):
                return i
    return None


def _cell(row: list[str], idx: int | None) -> str:
    if idx is None or idx >= len(row):
        return ""
    return (row[idx] or "").strip()


def _csv_fx(gross_instr: float, rate: float | None, total: float | None, pence: bool) -> float | None:
    """Pick the instrument→account rate. A GBX export often quotes pence per account-currency unit."""
    if not rate or rate <= 0:
        return None
    scales = (1.0, 0.01) if pence else (1.0,)
    net = abs(total or 0.0)
    if not net or not gross_instr:
        quoted = rate * (0.01 if pence and abs(rate) >= 20 else 1.0)
        return (1.0 / quoted) if quoted else None
    best, best_err = None, None
    for scale in scales:
        quoted = rate * scale
        if not quoted:
            continue
        for cand in (quoted, 1.0 / quoted):
            err = abs(net - abs(gross_instr) * cand)
            if best is None or err < best_err:
                best, best_err = cand, err
    return best


def parse(text: str, **_kw) -> ParseResult:
    sample = text.lstrip("\ufeff")
    rows = list(csv.reader(io.StringIO(sample)))
    header_i = None
    header: list[str] = []
    for i, row in enumerate(rows[:8]):
        h = _headers(row)
        if "action" in h and "ticker" in h:
            header_i, header = i, h
            break
    if header_i is None:
        raise ValueError("Trading 212 CSV has no Action / Ticker header.")
    ix = {
        "action": _col(header, "action"),
        "time": _col(header, "time"),
        "isin": _col(header, "isin"),
        "ticker": _col(header, "ticker"),
        "name": _col(header, "name"),
        "shares": _col(header, "no. of shares", "no. of share"),
        "price": _col(header, "price / share", "price/share"),
        "ccy": _col(header, "currency (price / share)", "currency (price/share)", "currency"),
        "fx": _col(header, "exchange rate"),
        "total": _col(header, "total"),
        "stamp": _col(header, "stamp duty reserve tax", "stamp duty"),
        "fxfee": _col(header, "currency conversion fee"),
        "notes": _col(header, "notes"),
        "id": _col(header, "id"),
    }
    res = ParseResult(format=FORMAT, rows_total=max(0, len(rows) - header_i - 1))
    seen: set[str] = set()
    for row in rows[header_i + 1:]:
        if not any(c.strip() for c in row):
            continue
        action = _cell(row, ix["action"])
        mapped = classify_csv_action(action)
        if mapped is None:
            a = " ".join((action or "").lower().split())
            if a.startswith("dividend") and "reinvest" not in a:
                from app.dividends import DividendIn, scale_gbx
                ext = _cell(row, ix["id"])
                when = parse_csv_time(_cell(row, ix["time"]))
                ticker = _cell(row, ix["ticker"]).upper()
                total = parse_money(_cell(row, ix["total"]))
                if not ext or when is None or not ticker or total is None:
                    res.skipped[action or "blank"] += 1
                    continue
                if ext in seen:
                    res.skipped["duplicate id"] += 1
                    continue
                seen.add(ext)
                ccy = (_cell(row, ix["ccy"]) or "").upper() or None
                gps, instr_ccy = scale_gbx(parse_money(_cell(row, ix["price"])), ccy)
                amt, acct_ccy = scale_gbx(total, ccy if ccy == "GBX" else None)
                # CSV Total is the credited cash. If the price currency is GBX, Total is still usually
                # the account currency — keep it as-is unless the Total itself was tagged GBX.
                if ccy != "GBX":
                    amt, acct_ccy = total, ccy
                if "_" in ticker:
                    info = parse_t212_ticker(ticker, _cell(row, ix["isin"]).upper() or None)
                    sym = info.symbol or ticker
                else:
                    sym = ticker
                res.dividends.append(DividendIn(
                    external_id=ext, symbol=sym,
                    paid_on=when, amount=float(amt), currency=acct_ccy or instr_ccy,
                    amount_account=float(amt), account_currency=acct_ccy or instr_ccy,
                    quantity=parse_money(_cell(row, ix["shares"])), gross_per_share=gps,
                    kind=action, ticker=ticker, raw={"action": action, "ccy": ccy},
                ))
                continue
            if "interest" in a:  # "Interest on cash", "Lending interest": real cash the broker paid
                ext = _cell(row, ix["id"])
                when = parse_csv_time(_cell(row, ix["time"]))
                total = parse_money(_cell(row, ix["total"]))
                if ext and when is not None and total:
                    res.cash_flows.append({"external_id": "csv:" + ext, "occurred_at": when, "amount": float(total),
                                           "currency": (_cell(row, _col(header, "currency (total)")) or "").upper() or None,
                                           "kind": "interest", "description": action[:200]})
                    continue
            res.skipped[action or "blank"] += 1
            continue
        kind, side = mapped
        ext = _cell(row, ix["id"])
        if not ext:
            res.skipped["no id"] += 1
            continue
        if ext in seen:
            res.skipped["duplicate id"] += 1
            continue
        seen.add(ext)
        when = parse_csv_time(_cell(row, ix["time"]))
        if when is None:
            res.skipped["no time"] += 1
            continue
        ticker = _cell(row, ix["ticker"]).upper()
        isin = _cell(row, ix["isin"]).upper() or None
        # A CSV ticker is the symbol (AAPL, VUSA). A broker id (AAPL_US_EQ, VUSAl_EQ) still parses.
        if "_" in ticker:
            info = parse_t212_ticker(ticker, isin)
            sym, exch = info.symbol or ticker, info.exchange
        else:
            sym, exch = ticker, None
        from app.markets import ISIN_PREFIX_TO_EXCHANGE
        exch = exch or (ISIN_PREFIX_TO_EXCHANGE.get((isin or "")[:2]) if isin else None)
        shares = parse_money(_cell(row, ix["shares"]))
        price = parse_money(_cell(row, ix["price"])) or 0.0
        ccy = (_cell(row, ix["ccy"]) or "").upper() or None
        notes = _cell(row, ix["notes"])
        name = _cell(row, ix["name"])
        ratio = ratio_from_text(action, notes) if kind == "SPLIT" else None
        effect = None
        if kind == "SPLIT":
            if ratio is not None:
                shares, price, effect = ratio, 0.0, "RATIO"
            elif not shares:
                res.skipped["split without shares"] += 1
                continue
        if shares is None or abs(shares) == 0:
            res.skipped[action or kind] += 1
            continue
        pence = ccy == "GBX"
        if pence:
            price, ccy = price / 100.0, "GBP"
        total = parse_money(_cell(row, ix["total"]))
        fx = _csv_fx(abs(shares) * price, parse_money(_cell(row, ix["fx"])), total, pence)
        stamp = abs(parse_money(_cell(row, ix["stamp"])) or 0.0)
        fxfee = abs(parse_money(_cell(row, ix["fxfee"])) or 0.0)
        fees = stamp + fxfee
        if fx:
            fees = fees / fx
        tz = exchange_tz(exch)
        trade_date = when.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(tz)).date()
        res.records.append(ExecRecord(
            external_id=ext, symbol=sym, underlying=sym, asset_type="STOCK", side=side,
            quantity=round(abs(shares), 8), price=price, executed_at=when, fees=round(fees, 6),
            time_known=True, position_effect=effect, kind=kind, description=name or action,
            raw={"action": action, "isin": isin, "ticker": ticker, "notes": notes, "ccy": ccy},
            trade_date=trade_date, currency=ccy, fx_to_account=fx,
        ))
    return res
