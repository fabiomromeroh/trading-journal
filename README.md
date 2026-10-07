# Trading Journal (Schwab edition)

A private, single-user trading journal in the spirit of TraderSync, built for a Charles Schwab account.
You import your Schwab history, it groups fills into round-trip trades, and you get performance stats,
per-trade pages with a price chart, and journal notes, tags, setups and ratings.

![Dashboard](screenshots/01-dashboard.png)

## Features
- **CSV import (the main way to get data in)**: drag and drop, preview (new / merge / duplicate), then
  commit, with an import history and undo. The file format is detected automatically:
  - **Schwab.com Transactions export** (Accounts → History → Transactions → Export → CSV). Includes fees,
    expirations and assignments, but no execution times.
  - **thinkorswim Account Statement** (Monitor → Account Statement → Export to file). Has exact fill
    times but no fees.
  - Importing both is safe. A fill that appears in both files is merged into one: thinkorswim adds the
    time and Schwab.com adds the fees. Re-importing overlapping date ranges creates no duplicates.
- **Trade builder**: FIFO matching per account and symbol, scaling in and out, partial closes, long and
  short, flips (one fill that closes a position and opens the opposite one is split, with fees split
  pro rata), options at a 100× multiplier. Expirations, assignments and exercises close positions. An
  option still open after its expiry is closed at $0 automatically. A closing fill with no matching
  open position (because it opened before your imported history) is reported in Settings instead of
  being turned into a fake short.
- **Dashboard**:
  - Net and gross P&L, fees, win rate, profit factor, expectancy, average win and loss, largest win and
    loss, average hold time, max drawdown, streaks, best and worst day.
  - Equity curve, daily P&L, and a P&L calendar heatmap.
  - P&L by symbol, by weekday, by hour of entry and by holding time.
  - Long vs short, stocks vs options, by setup and by tag.
  - Date-range and account filters.
- **Trades list**: sortable and filterable by symbol, side, status, outcome, asset type, setup and tag.
  Paginated.
- **Trade detail page**:
  - Executions table, P&L, fees, return %, hold time.
  - MFE/MAE (max favourable / adverse excursion) from price bars.
  - Candlestick chart with entry and exit markers (TradingView lightweight-charts).
  - Journal: notes, tags, setup, 1–5 star rating. These are kept when trades are rebuilt.
- **Sync engine with pluggable data sources** (`app/sources/`). The "Sync now" button and
  `python -m app.sync` (for a cron job) run every enabled source and then rebuild trades. It ships with
  an optional **Schwab Trader API** source, which is off unless its env vars are set.
- **Sample-data mode**: always labelled "Sample data", kept in its own account, and removable with one
  click in Settings.
- **Single-user login**: password from `APP_PASSWORD`, signed session cookie, simple brute-force
  throttle.

## Run locally
```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env            # set APP_PASSWORD
python -m app.migrate           # create / upgrade the SQLite schema
python -m app.seed_demo         # optional sample data (remove with --clear or the Settings button)
uvicorn app.main:app --reload   # http://127.0.0.1:8000
pytest -q                       # tests
```

## Deploy on Render
`render.yaml` defines a free Python web service and a free Postgres database.
- Start command: `alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port $PORT ...`
- Health check: `/healthz`
- Required env vars:
  - `APP_PASSWORD`: your login.
  - `SECRET_KEY`: random value.
  - `DATABASE_URL`: the Postgres internal URL. `postgres://` URLs are converted automatically.
  - `COOKIE_SECURE=true`.
- Optional env vars: `TOKEN_ENCRYPTION_KEY`, `DISPLAY_TZ`, `PRICE_PROVIDER`, `POLYGON_API_KEY`,
  `SCHWAB_*`, `SMTP_*`.
- **Render free Postgres expires 30 days after creation.** Upgrade the database (or export your data)
  before then.
- Free web instances sleep after about 15 minutes idle, so the first request after that is slow.
- The cron job is in `render.yaml` but commented out. Enable it once an automated source exists (Render
  cron jobs are paid). Its schedule is `30 16,21 * * 1-5` UTC, which is US midday and after the close.

## Price charts
`PRICE_PROVIDER=auto` tries, in order:
1. Schwab market data (if the Schwab API source is connected).
2. Polygon.io (if `POLYGON_API_KEY` is set).
3. Yahoo Finance's public chart endpoint. This is unofficial, best effort and may break.

Set `PRICE_PROVIDER=none` to turn charts off. Option trades are charted on the underlying stock.
Intraday trades use 5-minute bars. Multi-day trades use daily bars, so their MFE/MAE is approximate.

## Optional: Schwab Trader API source (US retail accounts only)
Schwab One International accounts cannot get Trader API apps, so for those accounts CSV import is the
way in. If you have a US account:
1. Create an account at https://developer.schwab.com and create an app with the API product
   **Accounts and Trading Production**.
2. Add callback URLs separated by commas (they must be HTTPS):
   `https://127.0.0.1:8182,https://<your-service>.onrender.com/auth/schwab/callback`
3. Wait for the status to change from "Approved – Pending" to **"Ready For Use"** (can take a few days).
4. Set these env vars:
   - `SCHWAB_APP_KEY` and `SCHWAB_APP_SECRET`
   - `SCHWAB_CALLBACK_URL`, matching one registered URL exactly, trailing slash included
   - `TOKEN_ENCRYPTION_KEY`
5. Click Settings → **Connect Schwab**. If you use the `127.0.0.1` callback, paste the URL you were
   redirected to into the form on that page.
6. Access tokens refresh automatically every 30 minutes. **Refresh tokens expire 7 days after you log
   in, and Schwab offers no way to extend them**, so the app shows a banner when less than 24 hours are
   left and you need to reconnect weekly.

The first sync walks back in 90-day chunks until the API refuses a date range or
`SCHWAB_MAX_LOOKBACK_DAYS` is reached. Each later sync covers from the last successful sync (minus 3
days of overlap) to now. Fills are deduplicated by Schwab `activityId`.

## Adding another automated source
Subclass `app.sources.base.DataSource` (`is_configured`, `status`, `sync`), store fills with
`app.services.ingest_records(db, account_id, "<source>", records)`, and register the class in
`app.sources.all_sources()`. The "Sync now" button, the cron command, the status banners and the sync
history pick it up automatically.

## Layout
```
app/
  main.py            FastAPI app, auth middleware
  models.py          SQLAlchemy models (accounts, executions, trades, fills, tags, imports, sync runs, tokens)
  importers/         schwab_csv.py, tos_statement.py
  sources/           base.py (DataSource interface), schwab_api.py
  trade_builder.py   pure FIFO round-trip builder (heavily unit-tested)
  services.py        ingest with cross-source dedupe/merge, trade rebuild
  stats.py           dashboard metrics
  prices.py          chart data providers, MFE/MAE
  sync.py            sync engine + CLI (python -m app.sync)
  seed_demo.py       sample data
alembic/             migrations
tests/               pytest suite
```

## Known limitations
- Schwab.com CSV rows have only a date, no time. Hold time for trades imported only from that file is
  day-level, and they are left out of hour-of-day stats. Importing a thinkorswim statement adds the
  exact times.
- Two same-day round trips in one symbol, imported from the date-only CSV, are merged into a single
  trade. The total P&L is still correct.
- Multi-leg spreads are journaled one leg per trade; there is no grouping into one spread trade yet.
  Stock splits and symbol changes are not adjusted.
- Expirations inferred for thinkorswim-only data are booked at $0. If the option was actually assigned,
  importing the Schwab.com CSV books it correctly.
- The Schwab API parsing (sign conventions, `RECEIVE_AND_DELIVER` descriptions) follows the public docs
  but hasn't been checked against a live account.
