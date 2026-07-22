"""Tradier REST adapter (read-only: quotes, history, option chains).

Uses the sandbox by default. Requires a token in the TRADIER_API_TOKEN
environment variable. This adapter never places orders.

API reference: https://documentation.tradier.com/brokerage-api
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta

import requests

from killer_options_bot.brokers.base import OrderResult
from killer_options_bot.models import Bar, OptionContract, Quote, Side


class TradierError(RuntimeError):
    pass


class TradierMarketData:
    def __init__(
        self,
        api_token: str,
        base_url: str = "https://sandbox.tradier.com/v1",
        timeout: float = 20.0,
        retries: int = 2,
    ):
        if not api_token:
            raise TradierError(
                "Missing Tradier API token. Set TRADIER_API_TOKEN in .env."
            )
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        # Extra attempts (beyond the first) on a network-level timeout or
        # connection error. Tradier occasionally stalls on a single request
        # under load; a quick retry recovers the scan without waiting a full
        # tick for the run loop's own outer retry.
        self.retries = max(0, retries)
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {api_token}",
                "Accept": "application/json",
            }
        )

    def _get(self, path: str, params: dict) -> dict:
        url = f"{self.base_url}{path}"
        attempt = 0
        while True:
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
                break
            except (requests.Timeout, requests.ConnectionError) as exc:
                if attempt >= self.retries:
                    raise TradierError(
                        f"Tradier {path} timed out after "
                        f"{attempt + 1} attempt(s): {exc}"
                    ) from exc
                attempt += 1
                time.sleep(0.5 * attempt)  # brief backoff before retrying
        if resp.status_code != 200:
            raise TradierError(
                f"Tradier {path} returned {resp.status_code}: {resp.text[:200]}"
            )
        try:
            return resp.json()
        except ValueError as exc:  # pragma: no cover - network dependent
            raise TradierError(f"Invalid JSON from Tradier {path}") from exc

    def get_quote(self, symbol: str) -> Quote:
        # Last price.
        quotes = self._get("/markets/quotes", {"symbols": symbol})
        node = (quotes.get("quotes") or {}).get("quote")
        if isinstance(node, list):
            node = node[0] if node else {}
        last = float(node.get("last") or node.get("close") or 0.0)

        # Recent daily closes for indicators.
        end = date.today()
        start = end - timedelta(days=90)
        hist = self._get(
            "/markets/history",
            {
                "symbol": symbol,
                "interval": "daily",
                "start": start.isoformat(),
                "end": end.isoformat(),
            },
        )
        days = ((hist.get("history") or {}) or {}).get("day") or []
        if isinstance(days, dict):
            days = [days]
        closes = [float(d["close"]) for d in days if d.get("close") is not None]
        if last <= 0 and closes:
            last = closes[-1]
        return Quote(symbol=symbol, last=last, closes=closes)

    def get_intraday_closes(
        self, symbol: str, interval: str = "5min"
    ) -> list[float]:
        """Intraday bar closes for today's session, oldest first.

        Uses Tradier ``/markets/timesales`` from the session open to now, with
        ``session_filter=open`` so pre/post-market prints are excluded. Returns
        an empty list before the open or if the series is unavailable (the
        intraday signal then simply declines to trade).

        Times are computed in US/Eastern (exchange time), which is what Tradier
        interprets the ``start``/``end`` params as. Using the host clock breaks
        on any non-ET machine (e.g. a UTC container or a CST laptop), where a
        naive "now" would fall before the 09:30 session start and 400.
        """
        from killer_options_bot.market import EASTERN

        now = datetime.now(EASTERN)
        start = now.replace(hour=9, minute=30, second=0, microsecond=0)
        if now < start:
            # Before today's open: nothing to fetch, decline gracefully.
            return []
        data = self._get(
            "/markets/timesales",
            {
                "symbol": symbol,
                "interval": interval,
                "start": start.strftime("%Y-%m-%d %H:%M"),
                "end": now.strftime("%Y-%m-%d %H:%M"),
                "session_filter": "open",
            },
        )
        node = ((data.get("series") or {}) or {}).get("data") or []
        if isinstance(node, dict):
            node = [node]
        closes: list[float] = []
        for bar in node:
            price = bar.get("close")
            if price is None:
                price = bar.get("price")
            if price is not None:
                closes.append(float(price))
        return closes

    def get_intraday_bars(
        self, symbol: str, interval: str = "15min"
    ) -> list[Bar]:
        """Intraday OHLC bars for today's session, oldest first.

        Same Tradier ``/markets/timesales`` source as ``get_intraday_closes``
        but keeps each bar's full open/high/low/close so price-action signals
        (STRAT bar typing, displacement) have real ranges to work with. Returns
        an empty list before the open or if the series is unavailable.
        """
        from killer_options_bot.market import EASTERN

        now = datetime.now(EASTERN)
        start = now.replace(hour=9, minute=30, second=0, microsecond=0)
        if now < start:
            return []
        data = self._get(
            "/markets/timesales",
            {
                "symbol": symbol,
                "interval": interval,
                "start": start.strftime("%Y-%m-%d %H:%M"),
                "end": now.strftime("%Y-%m-%d %H:%M"),
                "session_filter": "open",
            },
        )
        node = ((data.get("series") or {}) or {}).get("data") or []
        if isinstance(node, dict):
            node = [node]
        return self._bars_from_nodes(node)

    def get_daily_bars(self, symbol: str, lookback: int = 5) -> list[Bar]:
        """Recent daily OHLC bars, oldest first (most recent last).

        Uses Tradier ``/markets/history`` (daily interval) and returns the last
        ``lookback`` completed sessions. Used for prior-day high/low bias.
        """
        lookback = max(1, int(lookback))
        end = date.today()
        # Pad the window generously so weekends/holidays don't starve the count.
        start = end - timedelta(days=lookback * 3 + 10)
        hist = self._get(
            "/markets/history",
            {
                "symbol": symbol,
                "interval": "daily",
                "start": start.isoformat(),
                "end": end.isoformat(),
            },
        )
        days = ((hist.get("history") or {}) or {}).get("day") or []
        if isinstance(days, dict):
            days = [days]
        bars = self._bars_from_nodes(days)
        return bars[-lookback:]

    @staticmethod
    def _bars_from_nodes(nodes: list) -> list[Bar]:
        """Convert Tradier OHLC nodes into ``Bar`` objects, skipping bad rows."""
        bars: list[Bar] = []
        for n in nodes:
            try:
                o = n.get("open")
                h = n.get("high")
                low = n.get("low")
                c = n.get("close")
                if None in (o, h, low, c):
                    continue
                bars.append(
                    Bar(
                        open=float(o),
                        high=float(h),
                        low=float(low),
                        close=float(c),
                    )
                )
            except (TypeError, ValueError, AttributeError):
                continue
        return bars

    def _expirations(self, symbol: str) -> list[date]:
        data = self._get(
            "/markets/options/expirations",
            {"symbol": symbol, "includeAllRoots": "true"},
        )
        node = (data.get("expirations") or {}).get("date") or []
        if isinstance(node, str):
            node = [node]
        result = []
        for d in node:
            try:
                result.append(datetime.strptime(d, "%Y-%m-%d").date())
            except (TypeError, ValueError):
                continue
        return result

    def get_option_chain(
        self, symbol: str, side: Side
    ) -> list[OptionContract]:
        contracts: list[OptionContract] = []
        for expiration in self._expirations(symbol):
            data = self._get(
                "/markets/options/chains",
                {
                    "symbol": symbol,
                    "expiration": expiration.isoformat(),
                    "greeks": "true",
                },
            )
            options = (data.get("options") or {}).get("option") or []
            if isinstance(options, dict):
                options = [options]
            for opt in options:
                if opt.get("option_type") != side.value:
                    continue
                greeks = opt.get("greeks") or {}
                contracts.append(
                    OptionContract(
                        symbol=opt.get("symbol", ""),
                        underlying=symbol,
                        side=side,
                        strike=float(opt.get("strike") or 0.0),
                        expiration=expiration,
                        bid=float(opt.get("bid") or 0.0),
                        ask=float(opt.get("ask") or 0.0),
                        last=float(opt.get("last") or 0.0),
                        delta=float(greeks.get("delta") or 0.0),
                        implied_volatility=float(
                            greeks.get("mid_iv") or greeks.get("smv_vol") or 0.0
                        ),
                        volume=int(opt.get("volume") or 0),
                        open_interest=int(opt.get("open_interest") or 0),
                    )
                )
        return contracts


def _underlying_from_occ(option_symbol: str) -> str:
    """Extract the underlying root from an OCC option symbol.

    OCC symbols look like ``AAPL240119C00190000`` — the root is the leading
    alphabetic characters before the 6-digit expiration date.
    """
    root = ""
    for ch in option_symbol:
        if ch.isdigit():
            break
        root += ch
    return root or option_symbol


class TradierBroker:
    """Places single-leg option LIMIT orders via Tradier.

    Requires an account id (TRADIER_ACCOUNT_ID) in addition to the API token.
    Supports Tradier's ``preview`` mode for non-transmitting dry runs. This
    class never sends market orders.
    """

    def __init__(
        self,
        api_token: str,
        account_id: str,
        base_url: str = "https://sandbox.tradier.com/v1",
        timeout: float = 10.0,
    ):
        if not api_token:
            raise TradierError(
                "Missing Tradier API token. Set TRADIER_API_TOKEN in .env."
            )
        if not account_id:
            raise TradierError(
                "Missing Tradier account id. Set TRADIER_ACCOUNT_ID in .env."
            )
        self.base_url = base_url.rstrip("/")
        self.account_id = account_id
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {api_token}",
                "Accept": "application/json",
            }
        )

    def place_option_limit(
        self,
        *,
        option_symbol: str,
        side: str,
        quantity: int,
        limit_price: float,
        dry_run: bool,
    ) -> OrderResult:
        if side not in {"buy_to_open", "buy_to_close", "sell_to_open", "sell_to_close"}:
            raise TradierError(f"Unsupported option side: {side}")
        if quantity < 1:
            raise TradierError("quantity must be >= 1")
        if limit_price <= 0:
            raise TradierError("limit_price must be positive")

        payload = {
            "class": "option",
            "symbol": _underlying_from_occ(option_symbol),
            "option_symbol": option_symbol,
            "side": side,
            "quantity": str(quantity),
            "type": "limit",  # never "market"
            "duration": "day",
            "price": f"{limit_price:.2f}",
            "preview": "true" if dry_run else "false",
        }
        url = f"{self.base_url}/accounts/{self.account_id}/orders"
        resp = self.session.post(url, data=payload, timeout=self.timeout)
        if resp.status_code not in (200, 201):
            return OrderResult(
                accepted=False,
                order_id=None,
                detail=f"HTTP {resp.status_code}: {resp.text[:200]}",
                dry_run=dry_run,
            )
        try:
            data = resp.json()
        except ValueError:  # pragma: no cover - network dependent
            return OrderResult(False, None, "invalid JSON response", dry_run)

        order = data.get("order") or {}
        if dry_run:
            status = order.get("status", "preview")
            return OrderResult(
                accepted=order.get("status") != "rejected",
                order_id=None,
                detail=f"preview: status={status}",
                dry_run=True,
            )
        order_id = order.get("id")
        status = order.get("status", "unknown")
        return OrderResult(
            accepted=order_id is not None and status != "rejected",
            order_id=str(order_id) if order_id is not None else None,
            detail=f"status={status}",
            dry_run=False,
        )
