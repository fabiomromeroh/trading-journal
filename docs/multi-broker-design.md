# Multi-broker design: Trading 212, Interactive Brokers, and "any broker"

Status: **Phase 0 + A + B built** (2026-10-11). Phase 0 golden-master harness and Phase A schema /
account switcher / USD·EUR display currency are on `main`. Phase B (Trading 212 official API adapter,
Settings connect card, Yahoo/ISIN mapping, GBX scale, US-only auto-stop) is in this tree. Phase C
(IBKR Flex) is not built; the Brokers card still shows it as coming soon. All external facts below
were read from the official pages linked in each section on 2026-10-11; items marked **(verify)**
must be checked against a real sample from the user's account (no live T212 key has been used yet).

Goal: track the user's other investments (IBKR, Trading 212, later more) in the same journal **without changing any
Schwab / SnapTrade / thinkorswim-email / CSV behaviour**.

---------------------------------------------------------------------------------------------------------------------

## 1. Research

### 1a. Trading 212 public API (direct)  -> best Trading 212 path
Docs: <https://docs.trading212.com/api> (Beta, v0).

| Topic | Finding |
|---|---|
| Accounts covered | **Invest and Stocks & Shares ISA only.** Not CFD, not SIPP ([API intro](https://docs.trading212.com/api), [help centre](https://helpcentre.trading212.com/hc/en-us/articles/14584770928157-Trading-212-API-key)). |
| Auth | HTTP Basic: API key = user, API secret = password ([Authentication](https://docs.trading212.com/api/section/authentication)). One key pair per account (live and demo are separate hosts: `live.trading212.com/api/v0`, `demo.trading212.com/api/v0`). Optional IP allow-list on the key. |
| Read-only | Yes: the key has per-scope permissions (account data, portfolio, history, history-orders, orders-read, orders-execute...). A journal key needs **no** "Orders - Execute" ([help centre](https://helpcentre.trading212.com/hc/en-us/articles/14584770928157-Trading-212-API-key), [SnapTrade's T212 page lists the scopes](https://support.snaptrade.com/Trading212-bd4feaa69a1c829db6cc015ee0d3b26b)). A missing scope returns 403 "Scope(history:orders) missing". |
| Executions | `GET /api/v0/equity/history/orders` ([ref](https://docs.trading212.com/api/historical-events/orders_1)). Each item = `{order, fill}`. `fill`: `id`, **`filledAt` (ISO timestamp)**, `price`, `quantity` (fractional ok), `type` (`TRADE`, `STOCK_SPLIT`, `SPIN_OFF`, `STOCK_ACQUISITION`... ), `tradingMethod` (TOTV/OTC), `walletImpact{currency, fxRate, netValue, realisedProfitLoss, taxes[]}`. `order`: `createdAt`, `ticker` (e.g. `AAPL_US_EQ`), `instrument{ticker, name, isin, currency}`, `extendedHours`, `initiatedFrom`. So: real execution timestamps, ISIN, instrument currency, fees/taxes broken out (commission turnover, FX conversion fee, stamp duty, FINRA fee, transaction fee...). |
| Other endpoints | positions `GET /equity/positions` (1 req/s), account summary (1 req/5 s), instruments metadata (1 req/50 s), transactions (cash movements: deposit/withdraw/fee/interest), dividends, async CSV export. |
| Rate limits | History endpoints **20 req/min**, page size max 50, cursor pagination via `nextPagePath`; limits are per account regardless of key/IP; `x-ratelimit-*` headers ([General info](https://docs.trading212.com/api/section/general-information)). 1,000 orders/min of backfill: plenty. |
| Currency | "Multi-currency accounts are not supported through the API: account, position and result values are in the **primary account currency**." `walletImpact.fxRate` converts instrument currency -> account currency. |
| Gotchas | Beta (schema may change); `STOCK_SPLIT`/`SPIN_OFF` fills are not trades and must not become executions; "value" (notional) orders may have no quantity; one key per account (ISA and Invest = two accounts, two keys). |

### 1b. Interactive Brokers (retail individual account)
| Option | Verdict |
|---|---|
| **Flex Web Service + Flex Queries** | **Recommended.** Free, token + query id, read-only by nature. Two-step HTTPS: `SendRequest?t=TOKEN&q=QUERYID&v=3` -> ReferenceCode -> `GetStatement` ([intro](https://www.interactivebrokers.com/docs/web-api/flex-web-service/using-flex-web-service.md), [SendRequest](https://www.interactivebrokers.com/docs/web-api/api-reference/send-request.md)). Token created in Client Portal > Reporting > Flex Queries, lifetime 6 h to 1 year, optional IP restriction ([token page](https://www.interactivebrokers.com/docs/web-api/flex-web-service/client-portal-configuration/enable-and-create-access-token.md)). Max 365 days per request (`p`/`fd`+`td`). Limits: **1 req/s, 10 req/min per token** (error 1018, [error codes](https://www.interactivebrokers.com/docs/web-api/flex-web-service/error-codes.md)). Needs a User-Agent header. Saved queries cover 4 prior calendar years + current ([Flex Queries](https://www.ibkrguides.com/clientportal/performanceandstatements/flex.htm)). |
| Trades fields | Trades section, level "Executions": Trade ID, **Trade Date + Trade Time**, IB Execution ID, IB Order ID, Settle Date, currency, FX rate to base, commission, open/close indicator, ISIN/conid/exchange are selectable columns ([Trades - Flex](https://ibkrguides.com/reportingreference/reportguide/tradesfq.htm)). |
| Freshness | **Activity Flex** = end-of-day statement (updated after close/overnight). **Trade Confirmation Flex** = intraday execution report; one third-party source says ~10 min after execution **(verify; IBKR's guide pages do not state a delay)**. So "near-real-time" is possible with a Trade Confirmation query, not guaranteed. |
| Client Portal Web API | **Not suitable.** Individual accounts must run the Client Portal Gateway locally and log in interactively; OAuth 2.0 is for advisor/institutional structures ([Gateway lesson](https://www.interactivebrokers.com/campus/trading-lessons/launching-and-authenticating-the-gateway/), [OAuth state discussion](https://github.com/paulfryer/ibkr/issues/2)). A Render free web service cannot hold that session. Same for TWS/IB Gateway API. |
| IBKR via SnapTrade | Supported, **read-only**; data delayed up to one business day (~2 AM ET), activation 24-72 h; transactions cached once a day, **no intraday trade activity** ([SnapTrade IBKR page](https://support.snaptrade.com/end-user-guide-for-connecting-interactive-brokers), [syncing](https://docs.snaptrade.com/docs/syncing)). Works, but worse than Flex for a journal. |
| Activity Statement CSV | Manual download, always works, no setup; good fallback and for history beyond Flex limits. |

### 1c. SnapTrade (already used by the app for Schwab)
* Personal plan: free, no card, **up to 20 brokerage connections** ([snaptrade.com/personal](https://snaptrade.com/personal)); the dev docs still say the free build plan has 5 ([broker access guide](https://docs.snaptrade.com/docs/broker-access-guide)). Pricing page: Personal/OAuth users free "for a limited time" ([pricing](https://snaptrade.com/pricing)) -> **commercial risk: SnapTrade Personal can start charging; do not make it the only path.**
* Both brokers are "available immediately": Interactive Brokers (read-only), Trading212 (read+trade) and Trading212 Practice ([access guide](https://docs.snaptrade.com/docs/broker-access-guide)).
* Trading 212 via SnapTrade: Invest and UK ISA; transactions come from Trading 212's CSV-export feature (user gets a "CSV ready" notification ~daily); notional orders in a foreign currency have **no quantity** ([T212 page](https://support.snaptrade.com/Trading212-bd4feaa69a1c829db6cc015ee0d3b26b)). The direct API is strictly better (real fills, timestamps, fees).
* Transactions are cached, updated once a day, delayed one day; intraday = compare with orders ([syncing](https://docs.snaptrade.com/docs/syncing)).

### 1d. Other free aggregators
None worth it. Plaid Investments: EU/UK customers need a Custom plan, free trial is US/CA only ([Plaid billing](https://plaid.com/docs/account/billing/)). MX/Finicity/Yodlee are enterprise-priced. **Direct broker APIs + CSV beat aggregators for a single-user journal.** FX rates: ECB daily reference rates are free to reuse with attribution ([ECB](https://www.ecb.europa.eu/stats/policy_and_exchange_rates/euro_reference_exchange_rates/html/index.en.html), [reuse policy](https://www.ecb.europa.eu/stats/ecb_statistics/governance_and_quality_framework/html/usage_policy.en.html), keyless mirror [Frankfurter](https://frankfurter.dev/providers/ecb/)); IBKR Flex already contains the FX rate used per trade, and T212 fills carry `fxRate`, so ECB is only needed for display-currency conversion.

### 1e. Cross-cutting domain issues (what breaks if we do nothing)
| Issue | Today's assumption | Needed |
|---|---|---|
| Currency | `money()` prints `$`; P&L, R, risk are summed across accounts as dollars. | `Account.currency`; per-execution instrument currency + FX to account currency; never add unlike currencies without conversion. |
| Time zone / sessions | NY time everywhere: entry day, 09:30 session, ET trade_date in matching, 5-min stop rule, hour/weekday charts, `display_tz` setting. | Per-account/market tz + session calendar (LSE 08:00-16:30 London, Xetra 09:00-17:30 Frankfurt, US 09:30-16:00 NY). Trade date = **exchange-local** date. |
| Symbols | US tickers; Yahoo symbol = ticker. | Instrument table: ISIN, broker ids (T212 `AAPL_US_EQ`, IBKR conid), exchange, price symbol (Yahoo `.L`, `.DE`, `.AS`...). **LSE quotes are in pence (GBX)** while T212/IBKR may report GBP: needs a price scale **(verify on a real LSE trade)**. |
| Fees | One `fees` number. | T212 taxes list (stamp duty, FX fee...) and IBKR commission + regulatory; keep summed into `fees` plus raw detail. |
| Fractional shares | Quantities are floats already. | Check rounding in matching (`quantity:.4f` in `match_key`) -> T212 allows more decimals. |
| Corporate actions | Alias table for renames only. | T212 `STOCK_SPLIT/SPIN_OFF` fills, IBKR corporate-action rows: ignore as trades, adjust cost basis/position (Phase 4). |
| CFDs | None. | Out of scope (T212 API excludes CFD; IBKR CFDs exist as asset class: skip in v1 with a visible "skipped" count). |
| Settlement | Trade date only. | Store `settle_date` (nullable); journal stays on trade date. |
| UCITS ETFs | n/a | Just instruments with ISIN; US persons cannot buy them but the user is EU/UK-based; no special handling beyond symbol mapping. |

---------------------------------------------------------------------------------------------------------------------

## 2. Current architecture (what we must not break)
* `app/sources/base.py`: `DataSource` (`is_configured/refresh/status/sync`) -> `app/sources/__init__.py::all_sources()` = SnapTrade, Schwab API. `app/sync.py::execute_run` loops sources, then rebuilds trades.
* Normalised record: `app/instruments.py::ExecRecord` (naive UTC `executed_at`, `trade_date` = **ET** date, `match_key()` = `ET date|symbol|side|qty|price|kind`). `STOCK|OPTION` only.
* Persistence: `app/services.py::ingest_records(db, account_id, source, records)` with source-quality ranking (`SOURCE_QUALITY`), dedupe (`uq_exec_source_ext`, `match_key` across sources), `rebuild_trades` -> `Trade` + `TradeFill` (FIFO round trips, journal carried over by `_carry_journal`).
* `Account(name, broker, account_number_masked, external_ref, is_demo)` has **no currency/timezone/market**. Account filter already exists (`Filters.account_id`, `?account=`).
* Prices: `app/prices.py` provider chain (Schwab/Polygon/Yahoo) keyed by `trade.underlying`; ET sessions hard-coded (`ET`), stop rule in `app/stops.py` uses NY 09:30.
* Display: `display_tz` setting (default New York), `$` formatting, `summarize(trades, tz)`.
* Secrets: Fernet via `TOKEN_ENCRYPTION_KEY` (`email_sync.py` pattern, `oauth_tokens` table).

---------------------------------------------------------------------------------------------------------------------

## 3. Design

### 3.1 Principle: additive, default = today's behaviour
Every new column is nullable/defaulted so that **existing rows mean exactly what they mean now** (`currency='USD'`, `timezone='America/New_York'`, `market='US'`). Code branches only when an account is non-default. Schwab/SnapTrade/ToS/CSV code paths are not edited in Phase 0-1 except through those defaults.

### 3.2 Schema (Alembic 0006+, SQLite and Postgres safe, all `server_default`)
```
accounts      + currency varchar(3) default 'USD'     -- account/base currency (T212: primary currency)
              + timezone varchar(40) default 'America/New_York'   -- display + "trading day" tz of this account
              + market varchar(8) default 'US'        -- default market for symbol mapping / sessions
              + kind varchar(16) null                 -- invest | isa | margin | cash | cfd
              + source_key varchar(30) null           -- which adapter owns it (t212, ibkr_flex, schwab_api, snaptrade...)
executions    + currency varchar(3) null              -- instrument/trade currency (null = account currency)
              + fx_to_account float null              -- rate instrument ccy -> account ccy at fill (null = 1)
              + instrument_id int null FK instruments
              + settle_date date null
trades        + currency varchar(3) null              -- copied from account at build time
              + instrument_id int null
instruments   id, isin, conid, broker_symbol (e.g. AAPL_US_EQ), symbol, exchange (MIC), currency, asset_type, multiplier,
              price_symbol (Yahoo "VUSA.L"), price_scale (1 or 0.01 for GBX), tz, session_open, session_close, UNIQUE(isin, exchange)
fx_rates      date, base, quote, rate  (ECB daily, cached; only for display conversion)
```
`TradeFill` is unchanged. `instruments` is optional: Schwab rows keep `instrument_id NULL` and behave as today.

### 3.3 Generic Source adapter interface
Keep `DataSource` as-is (so Schwab/SnapTrade don't change) and add a thin helper base for broker-API adapters:
```python
class BrokerAdapter(DataSource):                 # app/sources/broker_base.py
    key, name, source_key                        # 't212', 'ibkr_flex'
    def credentials_form(self) -> list[Field]    # what Settings asks for (key, secret / token, query id, label, currency)
    def discover_accounts(self, creds) -> list[AccountInfo]   # id, name, currency, kind, tz, market
    def fetch_executions(self, acct, since) -> Iterable[RawFill]   # paging/rate limit handled inside
    def to_record(self, raw, acct) -> ExecRecord | Skip(reason)    # PURE function: unit-testable on fixtures
    def fetch_positions(self, acct) -> list[Position]              # optional, for reconcile + live unrealized
```
`ExecRecord` gains optional `currency, fx_to_account, isin, exchange, settle_date, instrument_key` (defaults None -> old behaviour). `trade_date` becomes "exchange-local date" (for US it is still ET, so `match_key` is byte-identical for existing sources). `BrokerAdapter.sync()` = fetch -> `to_record` -> `ingest_records` (same call every source uses) -> Settings shows "skipped: 12 splits, 3 CFD".
Registration: `all_sources()` returns the existing two first, then adapters (T212 / IBKR) **only if configured**, so an unconfigured install is identical.
Credentials: Settings form -> Fernet-encrypted in `app_state` (same pattern as `tos_email:token`); never logged; read-only keys only (UI states which scopes to tick).

### 3.4 Accounts, filters, combined view
* One `Account` per broker account (T212 Invest, T212 ISA, IBKR U1234567...). `broker`/`source_key` identify the adapter; name editable.
* Existing account filter stays. Add "All accounts" behaviour rule: **metrics are computed per currency**. Combined view groups by currency (USD block, EUR block, GBP block) unless the user picks a display currency in Settings (default: none = grouped). Display-currency conversion (Phase 4) converts per-trade net P&L at the trade's close date with ECB rates; R multiples, win rates, MFE/MAE efficiency are currency-free and can be combined as is.
* Dashboard/Reports/AI review/Coach get an optional account selector default "all"; with mixed currencies they show per-currency tiles instead of a wrong sum.
* Journal data (setups, tags, mistakes, notes) is account-independent and works across brokers unchanged.

### 3.5 De-duplication across sources
* Within one adapter: `(account_id, source, external_id)` unique already exists. T212 `fill.id`, IBKR `IBExecID`/`TradeID` are stable ids.
* Across adapters (e.g. IBKR Flex + IBKR via SnapTrade + CSV for the same account): reuse `match_key`, but build the date part from the **account/exchange-local** date (US accounts: unchanged ET). Add `currency`+`isin` to the key only for non-US records, so existing keys do not change. Add `ibkr_flex: 3`, `t212: 3`, `ibkr_csv: 1`, `t212_csv: 1` to `SOURCE_QUALITY`; API sources `REFRESHABLE`.
* Never merge across different accounts.

### 3.6 Symbols, prices and sessions per market
* `instruments` resolves broker id -> (symbol, exchange, ISIN, price_symbol). T212: `AAPL_US_EQ` -> AAPL/US; `VUSA_EQ`/ISIN + instrument currency -> exchange via the instruments endpoint. IBKR: conid/ISIN/exchange from the Flex columns. Unknown mapping -> keep trade, flag "no price symbol: set it" (editable in a small Instruments table in Settings, same spirit as the existing symbol-alias editor).
* Yahoo symbol suffixes by exchange (`.L`, `.DE`, `.AS`, `.PA`, `.MI`...) from a static MIC->suffix map **(verify per symbol against Yahoo)**; `price_scale` for GBX.
* `prices.py` gets `trade.price_symbol or trade.underlying` and `session(trade)` instead of the hard-coded NY 09:30/16:00; `stops.py` uses that session + `account.timezone`; non-US stock trades older than Yahoo's intraday window already fall back to daily. For markets without reliable intraday data the stop rule degrades to daily-flagged-approx (existing mechanism).
* Chart, hour-of-day and weekday reports use the account tz.

### 3.7 Keeping Schwab byte-for-byte
1. **Phase 0 (before any feature code):** add golden-master tests: run the committed fixtures (`tests/fixtures/schwab_transactions.csv`, `snaptrade_activities.json`, `tos_statement.csv`, `tos_email_fill.html`) through ingest -> `rebuild_trades` -> `metrics.summarize` / reports and snapshot the full JSON (trades, fills, stats, rendered stat bar HTML for a few trades). The snapshot is committed; CI fails on any diff.
2. Migration backfills nothing: defaults do it. A migration test restores the real-data backup copy and asserts identical snapshots before/after `alembic upgrade`.
3. New code paths are gated by `account.market != 'US' or account.currency != 'USD'` (or `instrument_id is not None`); the old path is the `else`.
4. Live release procedure as before: pre-deploy export, deploy, verify counts of trades/executions/net P&L identical on Neon via SQL.

### 3.8 Multi-currency P&L (Phase 4 detail)
Trade P&L stays in the **account currency** (T212: `walletImpact.netValue/realisedProfitLoss` are already account currency; IBKR: `fxRateToBase`). Per-currency totals everywhere; optional display currency converts at close-date ECB rate (rates cached in `fx_rates`). Fees in account currency. R/risk use instrument-currency prices (ratios), so R is currency independent.

---------------------------------------------------------------------------------------------------------------------

## 4. Phased rollout and effort
(One developer-day = ~6 focused hours; includes tests and a local headless-browser check; excludes waiting for the user's data.)

| Phase | Scope | Effort | Done when |
|---|---|---|---|
| 0 | Golden-master regression tests on all existing fixtures + real-data snapshot harness | 1 d | Suite green before any change; snapshot committed |
| 1a | Schema 0006 (account currency/tz/market/kind, execution currency/fx, instruments, fx_rates), `ExecRecord` optional fields, per-currency money formatting, account selector semantics | 1.5-2 d | Existing data renders identically; USD accounts unchanged |
| 1b | **Trading 212 adapter**: credentials UI, discover account (summary), history/orders paging + 20/min throttle, `to_record` (TRADE only; skip splits/spin-offs with counts), fees from `taxes[]`, instruments mapping (ISIN, `_US_EQ`/LSE/Xetra), positions for reconcile, tests on recorded JSON fixtures (user's real key tested once, locally, results redacted) | 3-4 d | T212 trades appear with real fill times, fees, currency; re-sync is idempotent |
| 2 | **IBKR Flex adapter**: token + query id UI, 2-step fetch with 1/s,10/min limit and retry on 1019/1001, XML parser (Executions level), multi-currency + FX rate, option rows (OCC-like mapping -> existing OPTION model), open/close indicator, Activity (daily) and optional Trade Confirmation (intraday) query ids, query-template instructions with exact field list | 3-4 d | Matches the user's Activity Statement totals |
| 3 | Generic CSV importer (column-mapping UI + saved mappings per broker; IBKR Activity Statement CSV and T212 history CSV presets) on the existing import/preview/undo flow | 2-3 d | A CSV from any broker imports through the same preview |
| 4 | Multi-currency reporting (ECB `fx_rates`, display currency, per-currency tiles), market tz/sessions in charts + stop rule, GBX scale, corporate-action handling (splits adjust position/basis), settle date | 3-5 d | Combined view is correct in EUR/GBP/USD; non-US charts use local sessions |
| Total | | **~14-20 d** (T212 + IBKR usable after Phase 1+2 = ~9-11 d) | |

Risks: T212 API is Beta; IBKR Flex field names depend on the user's query (we ship an exact template); Yahoo coverage for EU symbols and intraday history limits; LSE pence scaling; IBKR Flex "intraday" delay unverified.

Fallback plan: if a broker API fails or changes, the CSV importer (Phase 3) keeps working; SnapTrade stays available for both brokers as a second source (read-only, 1-day delay) via the existing adapter.

---------------------------------------------------------------------------------------------------------------------

## 5. Recommendation
1. **Trading 212 via its official API** (not SnapTrade): real fills with timestamps, fees, ISIN, read-only key, free, no card.
2. **IBKR via Flex Web Service** (not SnapTrade, not Client Portal API): free, executions with date + time, multi-currency + FX, read-only token. Start with an Activity Flex query (daily); add a Trade Confirmation query if you want same-day trades. SnapTrade IBKR only as a backup.
3. Build order: Phase 0 -> 1 -> T212 (the simplest and highest-confidence) -> IBKR Flex -> CSV -> multi-currency polish.

## 6. What the user must provide (nothing is needed until you say go)
**Trading 212**
1. Which accounts: Invest, Stocks ISA (CFD and SIPP are not available through the API), plus whether to include a Practice account for testing.
2. For each account, an API key + secret created in the T212 app (Settings > API (Beta) > Generate): tick **Account data, Portfolio, History, History - Orders, Metadata**; leave **Orders - Execute** and any trading scopes **off**; optionally restrict to the app's IP. (One key pair per account; keys go only into the app's Settings, never in chat/email.)
3. Account currency (GBP/EUR) is read from the account; confirm which you want as display currency.

**Interactive Brokers**
1. Account type (individual, margin/cash, any sub-accounts) and base currency; which asset classes you trade there (stocks/ETFs only, or options/futures too).
2. In Client Portal > Performance & Reports > Flex Queries: create an **Activity Flex Query** with Trades > level "Executions" and the fields we list (Trade ID, IB Execution ID, Date/Time, Symbol, ISIN, conid, Exchange, Buy/Sell, Open/Close, Quantity, Trade Price, Currency, FX Rate To Base, Commission, Taxes, Multiplier, Put/Call, Strike, Expiry, Settle Date), XML output, period "last 365 days"; optional second **Trade Confirmation Flex Query** for same-day trades.
3. Enable Flex Web Service and generate a **token** (suggest 1-year lifetime; IP-restrict if you like) and give us the **Query ID(s)**. Entered in Settings only.

**Preferences**
* Display currency: per-account (recommended default) or one combined currency (EUR / GBP / USD)?
* Time zone for non-US accounts' charts/reports (default: market local; app default stays New York for Schwab).
* Should non-US trades share the same Setup/Tag/Mistake lists and Coach/AI review (default yes), and do you want a combined dashboard or one dashboard per account?
* Priority: Trading 212 first, or IBKR first?
