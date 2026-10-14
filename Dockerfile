# Killer Options Bot — container image for Railway / any Docker host.
# Runs the paper dashboard. Live trading remains disabled unless you opt in
# via config and environment (see README).

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install dependencies first for better layer caching.
COPY pyproject.toml README.md ./
COPY src ./src

# Install the package plus the optional Postgres backend (for Supabase).
RUN pip install --upgrade pip && \
    pip install ".[postgres]"

# Persisted config lives in the repo; override values via env / a mounted file.
COPY config.yaml ./config.yaml

# Railway provides PORT. Bind to all interfaces; auth is required for non-local
# hosts (KOB_AUTH_USER / KOB_AUTH_PASS). DATABASE_URL selects Postgres. The data
# source defaults to mock; set KOB_SOURCE=tradier (+ TRADIER_API_TOKEN) to use
# real market data. --run also starts the automated scan/manage loop in the
# same process (market-hours aware); disable it with KOB_RUN=0 if you only want
# the dashboard.
# --run-tick 30 (default is 60s): exits/trails are polled, not real broker
# stop orders, so each tick is a window where a cheap, gamma-heavy short-dated
# option can gap past a configured stop/trail before it's caught. Halving the
# poll interval halves that slippage window (found 2026-10-13: a 25% stop on
# a sub-$1 contract was seen overshooting to -41% between two 60s ticks).
EXPOSE 8787
CMD ["killer-options-bot", "serve", "--host", "0.0.0.0", "--run", "--run-tick", "30"]
