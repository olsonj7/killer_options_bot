# Killer Options Bot

A **small-account, paper-first** options scanner and trade logger.

> This is educational software, not financial advice. Options are complex,
> leveraged products that can lose money quickly and require broker approval.
> **Run in paper/scan mode. Do not enable live execution until you have logged
> many paper trades and understand every line of code.**

## Design philosophy

For a small account, the best bot is a **picky bot**. It says "no trade" most of
the time. This project is built around survival first:

- Paper trading and scan-only modes by default (no live orders)
- Defined-risk, long options only in v1 (no naked options, no 0DTE)
- Hard risk guardrails enforced before any trade is logged
- Everything is logged to SQLite for later analysis

## Phases

1. **Scanner (done):** scan a watchlist, apply filters + risk rules, log
   candidate trades. No orders.
2. **Paper trading (done):** open simulated positions from allowed candidates,
   mark them to market, and apply exit rules (profit target / stop loss / max
   holding days / min DTE). Tracks realized + unrealized P&L. No real orders.
3. **Live (tiny size, done — disabled by default):** only after paper results
   look sane. Live order placement exists behind **multiple independent safety
   gates**: a `live` trading mode, an `enabled` flag, a kill-switch file,
   daily/weekly loss lockouts, a per-order contract cap, an explicit
   `--i-understand-live` confirmation, and **limit orders only**. It is a
   non-transmitting dry run unless every gate passes. See
   [Live trading](#live-trading-read-this-twice).

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e .

# Copy and edit config / credentials
cp .env.example .env
# edit config.yaml to taste

# Scan only (no positions), offline mock data, no API keys needed
python -m killer_options_bot scan --source mock

# Scan AND open paper positions for allowed candidates
python -m killer_options_bot scan --source mock --paper

# Show open paper positions marked to market
python -m killer_options_bot positions

# Re-price open positions and apply exit rules
python -m killer_options_bot manage

# Paper P&L summary (win rate, avg win/loss, realized + unrealized)
python -m killer_options_bot pnl

# Backtest: step dates over a range and print aggregate stats (mock only)
python -m killer_options_bot backtest --start 2026-01-16 --end 2026-06-30 --step 2

# Export all positions (open + closed) as CSV to stdout or a file
python -m killer_options_bot export
python -m killer_options_bot export -o positions.csv

# Local web dashboard at http://127.0.0.1:8787 (scan/manage buttons, P&L)
python -m killer_options_bot serve --source mock

# Show logged scan candidates
python -m killer_options_bot history

# Run the tests
pytest
```

### Simulating across dates (mock only)

The mock data source drifts prices deterministically with the date, so you can
replay a paper trade lifecycle with `--as-of`:

```bash
python -m killer_options_bot scan --source mock --as-of 2026-03-02 --paper
python -m killer_options_bot positions --as-of 2026-03-10
python -m killer_options_bot manage   --as-of 2026-03-25
python -m killer_options_bot pnl       --as-of 2026-03-25
```

To use real Tradier sandbox data, put your token in `.env` and run:

```bash
# Verify the token first (read-only: fetches a quote + an option chain):
python -m killer_options_bot check-tradier          # or: --symbol QQQ

python -m killer_options_bot scan --source tradier
```

## Backtesting

The `backtest` command replays the **exact same** signal, risk engine, and exit
rules across a date range, opening and managing paper positions day by day (mock
data only, since it needs historical dates). It uses an isolated throwaway
database so your real trade log is never touched.

```bash
python -m killer_options_bot backtest --start 2026-01-16 --end 2026-06-30 \
    --step 2 --verbose
```

Reported stats: number of trades, win rate, total P/L, expectancy per trade,
average win/loss, profit factor, max drawdown (on the realized-P/L curve), and
average holding days. `--verbose` prints the full trade log with the exit reason
for each trade. The weekly-cadence guardrail is enforced on simulated entry
dates, so backtests respect the same "max N trades per week" rule as live paper
trading.

> These numbers come from a deterministic **mock** price model and are for
> exercising the code path only. They are not predictive of real markets.

## How it runs (and scheduling)

The bot is a **command-line tool that runs once and exits** — each command opens
the SQLite log, does its work, and quits. It is **not** a always-on service.

For a small-account swing strategy (30-60 DTE, checked once or twice a day) you
don't need a persistent process; you need a **schedule**:

- **Local (recommended to start):** a `cron` job on macOS/Linux. Example: scan +
  open paper trades every weekday at 3:30pm ET, then apply exits:

  ```cron
  30 15 * * 1-5  cd /path/to/killer_options_bot && .venv/bin/python -m killer_options_bot scan --source tradier --paper
  35 15 * * 1-5  cd /path/to/killer_options_bot && .venv/bin/python -m killer_options_bot manage --source tradier
  ```

- **Hosted (later):** the same script on a small VM/container with cron, or a
  scheduled serverless job (GitHub Actions cron, AWS EventBridge, Render/Railway
  cron). Only worth it once you're past paper trading and want it to run without
  your laptop on. Hosting means you must secure your API keys and add auth to the
  dashboard.

### Web dashboard

`serve` starts a tiny local dashboard (Python stdlib only) showing P&L, an
**equity curve** of realized P/L, open positions, and recent candidates, with
buttons to scan or manage exits and an **inline config editor**:

```bash
python -m killer_options_bot serve --source mock          # http://127.0.0.1:8787
python -m killer_options_bot serve --source tradier --port 9000
```

- **Equity curve:** an inline SVG chart of cumulative realized P/L over closed
  trades. Once you hold open positions, a **dashed projection** segment extends
  the curve by the current mark-to-market P/L of those open positions.
- **Config editor:** visit `/config` (or the "edit config" link) to change the
  safe numeric risk / filter / exit fields. Values are range-checked and the
  whole config is re-validated before it is written back to `config.yaml`; an
  invalid edit is rejected and nothing is saved.
- **Export CSV:** the "Export CSV" link (or `GET /export.csv`) downloads all
  positions as a spreadsheet-friendly CSV.

#### Optional Basic Auth (reach it from your phone on the LAN)

By default the dashboard binds to `127.0.0.1` with no auth. To reach it from
another device, bind to your LAN address **and enable Basic Auth**:

```bash
export KOB_AUTH_PASS='choose-a-strong-password'
python -m killer_options_bot serve --source mock \
    --host 0.0.0.0 --auth-user jeff
```

The server refuses to bind a non-local host without `--auth-user`. The password
comes from `--auth-pass` or the `KOB_AUTH_PASS` environment variable (prefer the
env var so it does not land in your shell history). Basic Auth over plain HTTP
is only appropriate on a trusted LAN; put it behind HTTPS (a reverse proxy) if
you expose it more widely.

## Project layout

```
src/killer_options_bot/
  config.py        Load + validate config.yaml and .env
  models.py        Dataclasses: OptionContract, Candidate, PaperPosition, ...
  storage.py       SQLite persistence for candidates and paper positions
  indicators.py    RSI / SMA momentum signal
  risk.py          The risk engine (hard guardrails)
  scanner.py       Ties data + indicators + risk together
  paper.py         Paper engine: fills, mark-to-market, exit rules, P&L
  backtest.py      Step dates over a range and aggregate paper stats
  export.py        CSV export of positions
  live.py          Guarded live-execution scaffolding (disabled by default)
  brokers/
    base.py        MarketData + OrderBroker protocols, OrderResult
    mock.py        Deterministic offline data source + MockBroker
    tradier.py     Tradier REST adapter (quotes, chains) + TradierBroker
  web.py           Local stdlib web dashboard (serve command)
  cli.py           Command-line entry point
```

## Exit rules (defaults, see config.yaml `exits`)

- Take profit when the option is up +35% from entry
- Cut the loss when the option is down -45% from entry
- Force an exit after 21 holding days
- Exit once DTE falls to 21 or below (avoid the final expiration zone)


## Risk rules (defaults, see config.yaml)

- Max 3-5% of account risked per trade
- Reject contracts that cost too much relative to account size
- 30-60 DTE only
- Delta ~0.30-0.45
- Tight bid/ask spread
- Minimum volume + open interest
- Limit orders only, never market orders

## Hosted database (Supabase / Postgres)

By default everything is stored in a local SQLite file (`storage.db_path`). For
a hosted deployment you can point the bot at a Postgres database (e.g. a free
Supabase project) instead. Set a `DATABASE_URL` and the storage layer switches
to Postgres automatically:

```bash
pip install ".[postgres]"    # installs the optional psycopg driver

# Supabase: Project settings -> Database -> Connection string (URI).
# Use the connection pooler URI for serverless/PaaS.
export DATABASE_URL='postgresql://postgres:PASSWORD@db.PROJECT.supabase.co:5432/postgres'
python -m killer_options_bot serve --source mock
```

The schema is created automatically on first use. Both backends share the exact
same query logic; only the SQL dialect (placeholders, id column type) differs.
`DATABASE_URL` (env) takes precedence over `storage.database_url` in the YAML.

## Deploying to Railway

The repo ships a `Dockerfile` and `railway.json` so you can run the dashboard
as a small always-on service.

1. Create a Railway project from this repo (it auto-detects the Dockerfile).
2. (Optional) Add a Supabase Postgres and set `DATABASE_URL` (see above), or add
   a Railway Postgres plugin which provides `DATABASE_URL` for you.
3. Set environment variables in Railway:
   - `KOB_AUTH_USER` and `KOB_AUTH_PASS` — **required** for a public host; the
     server refuses to bind a non-local address without auth.
   - `DATABASE_URL` — if you want hosted Postgres instead of ephemeral SQLite.
   - `TRADIER_API_TOKEN` / `TRADIER_BASE_URL` — only if you use `--source tradier`.
4. Railway injects `PORT`; the start command binds `0.0.0.0` and reads `PORT`,
   `KOB_AUTH_USER`, and `KOB_AUTH_PASS` from the environment.

> A container filesystem is ephemeral. Use `DATABASE_URL` (Supabase/Postgres) so
> your trade log survives redeploys. Always set `KOB_AUTH_USER` /
> `KOB_AUTH_PASS`, and prefer to put the service behind HTTPS (Railway gives you
> a TLS domain by default).

## Live trading (read this twice)

Live execution is **off by default** and gated behind every one of these, all of
which must pass before a single real order is sent:

1. `mode.trading_mode: live` in your config.
2. `live.enabled: true` in your config.
3. The kill-switch file (`live.kill_switch_file`) must **not** exist. `touch`
   that file at any time to instantly block all new live orders.
4. Realized losses must be within `live.max_daily_loss` and `live.max_weekly_loss`
   (loss lockouts, computed from live-mode closed trades).
5. Order size must be `<= live.max_contracts_per_order`.
6. You must pass `--i-understand-live` on the command. Without it, the command
   runs a **dry run** and transmits nothing.
7. Orders are **limit orders only** (bought at the ask). Market orders are never
   sent.

Example config block:

```yaml
mode:
  trading_mode: live
live:
  enabled: true
  kill_switch_file: KILL_SWITCH
  max_daily_loss: 50      # dollars of realized loss/day before lockout
  max_weekly_loss: 100    # dollars of realized loss/week before lockout
  max_contracts_per_order: 1
```

```bash
# Dry run (no orders transmitted) against Tradier sandbox:
python -m killer_options_bot live --source tradier --broker tradier

# Offline dry run using the mock broker (safe, no network):
python -m killer_options_bot live --source mock --broker mock

# ACTUALLY transmit (only after everything above is understood):
export TRADIER_API_TOKEN='...'
export TRADIER_ACCOUNT_ID='...'
python -m killer_options_bot live --source tradier --broker tradier \
    --i-understand-live
```

Start with the Tradier **sandbox** (`TRADIER_BASE_URL` defaults to the sandbox).
Do not point at a production account until you have paper-traded extensively and
read your broker's options agreement. Positions opened live are stored with
`mode='live'` and the broker order id.

## Safety

The whole project is **paper-first**. The scanner never sends orders, paper mode
simulates fills, and live execution is disabled by default behind the multiple
independent gates described above (kill switch, loss lockouts, limit-only,
explicit opt-in). Do not enable live trading until you have read FINRA's options
guidance, obtained options approval from your broker, validated the strategy in
paper mode, and understand every line of code.
