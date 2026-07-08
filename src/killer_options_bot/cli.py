"""Command-line interface for the Killer Options Bot."""

from __future__ import annotations

import argparse
import sys
from datetime import date

from killer_options_bot.brokers.base import MarketData
from killer_options_bot.brokers.mock import MockMarketData
from killer_options_bot.config import Config, load_config
from killer_options_bot.models import Candidate
from killer_options_bot.paper import PaperEngine
from killer_options_bot.scanner import Scanner
from killer_options_bot.storage import get_storage


def _parse_as_of(args: argparse.Namespace) -> date | None:
    value = getattr(args, "as_of", None)
    if not value:
        return None
    return date.fromisoformat(value)


def _build_data_source(
    source: str, config: Config, as_of: date | None = None
) -> MarketData:
    if source == "mock":
        return MockMarketData(as_of=as_of)
    if source == "tradier":
        # Imported lazily so the mock path never needs the network stack.
        from killer_options_bot.brokers.tradier import TradierMarketData

        return TradierMarketData(
            api_token=config.tradier.api_token or "",
            base_url=config.tradier.base_url,
        )
    raise ValueError(f"Unknown source: {source}")


def _print_candidate(candidate: Candidate, as_of: date | None = None) -> None:
    c = candidate.contract
    verdict = "ALLOW" if candidate.decision.allowed else "REJECT"
    line = (
        f"[{verdict}] {c.underlying} {candidate.side.value.upper()} "
        f"{c.strike:g} exp {c.expiration} ({c.dte(as_of)}DTE) "
        f"mid ${c.mid:.2f} cost ${c.cost:.2f} "
        f"delta {c.delta:+.2f} spread {c.spread_pct:.0%} "
        f"vol {c.volume} oi {c.open_interest}"
    )
    print(line)
    print(f"        signal: {candidate.signal_note}")
    if candidate.decision.allowed:
        print(f"        -> would risk max ${candidate.max_loss:.2f}")
    else:
        for reason in candidate.decision.reasons:
            print(f"        x {reason}")


def cmd_scan(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    storage = get_storage(config)
    as_of = _parse_as_of(args)
    data = _build_data_source(args.source, config, as_of)
    scanner = Scanner(config, data, storage, as_of=as_of)

    print(
        f"Scanning {len(config.watchlist)} symbols "
        f"(account ${config.account_value:.2f}, "
        f"max risk/trade ${config.account_value * config.risk.max_trade_risk_pct:.2f}, "
        f"source={args.source})\n"
    )
    candidates = scanner.scan()
    if not candidates:
        print("No signals fired. The picky bot says: no trade today.")
        return 0

    allowed = [c for c in candidates if c.decision.allowed]
    for candidate in candidates:
        _print_candidate(candidate, as_of=scanner.as_of)

    opened = 0
    if getattr(args, "paper", False) and allowed:
        engine = PaperEngine(
            config, data, storage, as_of=as_of, cost_model=config.cost_model()
        )
        for candidate in allowed:
            position = engine.open_from_candidate(candidate)
            if position is not None:
                opened += 1
                print(
                    f"\n  opened paper position #{position.id}: "
                    f"{position.quantity}x {position.option_symbol} "
                    f"@ ${position.entry_price:.2f} "
                    f"(debit ${position.entry_cost:.2f})"
                )

    tail = "Nothing was ordered (scan mode)."
    if getattr(args, "paper", False):
        tail = f"Opened {opened} paper position(s)."
    print(f"\n{len(allowed)} allowed / {len(candidates)} evaluated. {tail}")
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    storage = get_storage(config)
    rows = storage.recent_candidates(limit=args.limit)
    if not rows:
        print("No candidates logged yet. Run: killer-options-bot scan")
        return 0
    for r in rows:
        verdict = "ALLOW" if r["allowed"] else "REJECT"
        print(
            f"#{r['id']} [{verdict}] {r['created_at']} "
            f"{r['underlying']} {r['side'].upper()} {r['strike']:g} "
            f"{r['dte']}DTE mid ${r['mid']:.2f} cost ${r['cost']:.2f}"
        )
        if not r["allowed"] and r["reasons"]:
            print(f"        x {r['reasons']}")
    return 0


def cmd_manage(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    storage = get_storage(config)
    as_of = _parse_as_of(args)
    data = _build_data_source(args.source, config, as_of)
    engine = PaperEngine(
        config, data, storage, as_of=as_of, cost_model=config.cost_model()
    )

    positions = storage.open_positions()
    if not positions:
        print("No open paper positions.")
        return 0

    results = engine.manage_all()
    closed = 0
    for r in results:
        p = r.position
        price_txt = f"${r.price:.2f}" if r.price is not None else "n/a"
        if r.closed:
            closed += 1
            pl = p.value_at(r.price or 0.0) - p.entry_cost
            print(
                f"CLOSED #{p.id} {p.option_symbol} @ {price_txt} "
                f"({r.reason}) P/L ${pl:+.2f}"
            )
        else:
            pl = p.unrealized_pl(r.price) if r.price is not None else 0.0
            print(
                f"HOLD   #{p.id} {p.option_symbol} @ {price_txt} "
                f"unreal ${pl:+.2f} "
                f"({p.holding_days(engine.as_of)}d held, "
                f"{p.dte(engine.as_of)}DTE)"
            )
    print(f"\nManaged {len(results)} position(s). Closed {closed}.")
    return 0


def cmd_positions(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    storage = get_storage(config)
    as_of = _parse_as_of(args)
    data = _build_data_source(args.source, config, as_of)
    engine = PaperEngine(
        config, data, storage, as_of=as_of, cost_model=config.cost_model()
    )

    positions = storage.open_positions()
    if not positions:
        print("No open paper positions.")
        return 0
    for p in positions:
        price = engine.mark_to_market(p)
        if price is None:
            print(
                f"#{p.id} {p.option_symbol} entry ${p.entry_price:.2f} "
                f"(debit ${p.entry_cost:.2f}) - no market price"
            )
            continue
        print(
            f"#{p.id} {p.underlying} {p.side.value.upper()} {p.strike:g} "
            f"entry ${p.entry_price:.2f} mark ${price:.2f} "
            f"unreal ${p.unrealized_pl(price):+.2f} "
            f"({p.pl_pct(price):+.0%}) "
            f"{p.holding_days(engine.as_of)}d held {p.dte(engine.as_of)}DTE"
        )
    return 0


def cmd_pnl(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    storage = get_storage(config)
    as_of = _parse_as_of(args)
    data = _build_data_source(args.source, config, as_of)
    engine = PaperEngine(
        config, data, storage, as_of=as_of, cost_model=config.cost_model()
    )

    closed = storage.closed_positions()
    open_positions = storage.open_positions()

    realized = engine.realized_pl_total()
    wins = [p for p in closed if (p.realized_pl() or 0) > 0]
    losses = [p for p in closed if (p.realized_pl() or 0) <= 0]

    unrealized = 0.0
    for p in open_positions:
        price = engine.mark_to_market(p)
        if price is not None:
            unrealized += p.unrealized_pl(price)

    print("Paper trading P/L summary")
    print("-------------------------")
    print(f"Closed trades : {len(closed)}")
    if closed:
        win_rate = len(wins) / len(closed)
        avg_win = (
            sum(p.realized_pl() or 0 for p in wins) / len(wins) if wins else 0.0
        )
        avg_loss = (
            sum(p.realized_pl() or 0 for p in losses) / len(losses)
            if losses
            else 0.0
        )
        print(f"Win rate      : {win_rate:.0%} ({len(wins)}W / {len(losses)}L)")
        print(f"Avg win       : ${avg_win:+.2f}")
        print(f"Avg loss      : ${avg_loss:+.2f}")
    print(f"Realized P/L  : ${realized:+.2f}")
    print(f"Open positions: {len(open_positions)}")
    print(f"Unrealized P/L: ${unrealized:+.2f}")
    print(f"Total P/L     : ${realized + unrealized:+.2f}")

    if closed:
        from killer_options_bot.web import _trade_stats

        print()
        print("Trade stats (R = multiple of risk; a full stop-out is -1R)")
        print("-" * 57)
        header = (
            f"{'Type':<10} {'W':>3} {'L':>3} {'Total':>5} {'Win%':>5} "
            f"{'AvgW':>7} {'AvgL':>7} {'R:R':>5} {'TotalR':>8}"
        )
        print(header)
        for row in _trade_stats(closed):
            print(
                f"{row['type']:<10} {row['wins']:>3} {row['losses']:>3} "
                f"{row['total']:>5} {row['win_rate']:>4.0%} "
                f"{row['avg_winner']:>+6.2f}R {row['avg_loser']:>+6.2f}R "
                f"{row['risk_reward']:>5.2f} {row['total_r']:>+7.2f}R"
            )
    return 0


def cmd_withdraw(args: argparse.Namespace) -> int:
    from killer_options_bot.withdraw import advise_from_storage

    config = load_config(args.config)
    storage = get_storage(config)

    if not config.withdraw.enabled:
        print(
            "Withdrawal advisor is disabled. Set withdraw.enabled: true in "
            "config.yaml to use it."
        )
        return 0

    advice = advise_from_storage(config.withdraw, storage)
    print("Withdrawal advisor (advisory only \u2014 no money is moved)")
    print("-----------------------------------------------------")
    print(f"Starting capital : ${advice.starting_capital:,.2f}")
    print(f"Equity (banked)  : ${advice.equity:,.2f}")
    print(f"Peak equity      : ${advice.peak_equity:,.2f}")
    print(f"Gain             : ${advice.gain:+,.2f}")
    print(f"Drawdown         : {advice.drawdown_pct:.0%}")
    print()
    if not advice.recommendations:
        print("No action suggested right now \u2014 keep it all working.")
        return 0

    labels = {
        "profit_skim": "Skim profits",
        "milestone": "Milestone reached",
        "tax_reserve": "Tax reserve",
        "drawdown_defense": "De-risk (drawdown)",
    }
    print("Recommendations:")
    for r in advice.recommendations:
        label = labels.get(r.kind, r.kind)
        print(f"  \u2022 {label}: ${r.amount:,.2f}")
        print(f"    {r.reason}")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    from killer_options_bot.export import positions_to_csv

    config = load_config(args.config)
    storage = get_storage(config)
    positions = storage.all_positions(limit=100000)
    csv_text = positions_to_csv(positions)

    if args.output and args.output != "-":
        with open(args.output, "w", encoding="utf-8", newline="") as fh:
            fh.write(csv_text)
        print(f"Wrote {len(positions)} position(s) to {args.output}")
    else:
        sys.stdout.write(csv_text)
    return 0


def cmd_live(args: argparse.Namespace) -> int:
    """Scan and place LIVE option orders behind every guardrail.

    Defaults to a non-transmitting dry run. Real orders are only sent when
    --i-understand-live is passed AND config live.enabled is true AND the
    kill-switch file is absent AND loss lockouts are clear.
    """
    import os

    from killer_options_bot.live import LiveEngine, LiveGuardError

    config = load_config(args.config)
    if config.trading_mode != "live":
        print(
            "Refusing: set mode.trading_mode: live in your config to use "
            "the live command.",
            file=sys.stderr,
        )
        return 1

    storage = get_storage(config)
    as_of = _parse_as_of(args)
    data = _build_data_source(args.source, config, as_of)

    # Choose a broker. mock is safe/offline; tradier hits the real API.
    if args.broker == "mock":
        from killer_options_bot.brokers.mock import MockBroker

        broker = MockBroker()
    else:
        from killer_options_bot.brokers.tradier import TradierBroker

        broker = TradierBroker(
            api_token=config.tradier.api_token or "",
            account_id=os.getenv("TRADIER_ACCOUNT_ID", ""),
            base_url=config.tradier.base_url,
        )

    confirm = bool(args.i_understand_live)
    mode_txt = "LIVE (transmitting)" if confirm else "dry-run (preview only)"
    print(f"Live command: {mode_txt}, broker={args.broker}")
    if config.live.kill_switch_file.exists():
        print(
            f"Kill switch present ({config.live.kill_switch_file}). "
            "No orders will be placed. Remove it to enable live orders."
        )

    scanner = Scanner(config, data, storage, as_of=as_of)
    candidates = scanner.scan()
    allowed = [c for c in candidates if c.decision.allowed]
    if not allowed:
        print("No allowed candidates. The picky bot says: no trade.")
        return 0

    engine = LiveEngine(config, data, storage, broker, as_of=as_of)
    placed = 0
    for candidate in allowed:
        try:
            result = engine.open_from_candidate(
                candidate, quantity=1, confirm_live=confirm
            )
        except LiveGuardError as exc:
            print(f"  BLOCKED: {exc}")
            break
        symbol = candidate.contract.symbol
        if result.accepted and result.position is not None:
            placed += 1
            order_id = result.order.order_id if result.order else "?"
            print(f"  LIVE #{result.position.id} {symbol} order={order_id}")
        elif result.accepted:
            print(f"  {symbol}: {result.reason}")
        else:
            print(f"  {symbol}: not opened ({result.reason})")

    print(f"\nDone. {placed} live position(s) opened.")
    return 0


def cmd_backtest(args: argparse.Namespace) -> int:
    from killer_options_bot.backtest import Backtester
    from killer_options_bot.models import CostModel

    config = load_config(args.config)
    try:
        start = date.fromisoformat(args.start)
        end = date.fromisoformat(args.end)
    except ValueError as exc:
        print(f"Invalid date: {exc}", file=sys.stderr)
        return 1
    if end < start:
        print("--end must be on or after --start", file=sys.stderr)
        return 1

    if args.no_costs:
        cost_model = CostModel.free()
    else:
        cost_model = CostModel(
            commission_per_contract=args.commission,
            slippage_frac=args.slippage,
        )

    bt = Backtester(
        config, start, end, step_days=args.step, cost_model=cost_model
    )
    stats = bt.run()

    if args.no_costs:
        cost_txt = "costs OFF (fills at mid)"
    else:
        cost_txt = (
            f"costs ON (${args.commission:.2f}/contract, "
            f"{args.slippage:.0%} of spread)"
        )
    print(
        f"Backtest {stats.start} -> {stats.end} "
        f"(step {args.step}d, account ${config.account_value:.2f})"
    )
    print(cost_txt)
    print("=" * 46)
    if stats.num_trades == 0:
        print("No trades were taken. The picky bot stayed flat.")
        return 0

    pf = stats.profit_factor
    pf_txt = "inf" if pf == float("inf") else f"{pf:.2f}"
    avg_hold = sum(t.holding_days for t in stats.trades) / stats.num_trades

    print(f"Trades        : {stats.num_trades}")
    print(
        f"Win rate      : {stats.win_rate:.0%} "
        f"({len(stats.wins)}W / {len(stats.losses)}L)"
    )
    print(f"Total P/L     : ${stats.total_pl:+.2f}")
    print(f"Expectancy    : ${stats.expectancy:+.2f} / trade")
    print(f"Avg win       : ${stats.avg_win:+.2f}")
    print(f"Avg loss      : ${stats.avg_loss:+.2f}")
    print(f"Profit factor : {pf_txt}")
    print(f"Max drawdown  : ${stats.max_drawdown:.2f}")
    print(f"P/L std       : ${stats.pl_std:.2f} / trade")
    print(f"t-stat        : {stats.t_stat:+.2f}  (|t|>=2 & N>=100 = credible)")
    print(f"Avg hold      : {avg_hold:.1f} days")

    if args.verbose:
        print("\nTrade log:")
        for t in sorted(stats.trades, key=lambda r: r.entry_date):
            print(
                f"  {t.entry_date} -> {t.exit_date} "
                f"{t.underlying} {t.side.upper()} "
                f"${t.entry_price:.2f}->${t.exit_price:.2f} "
                f"P/L ${t.pl:+.2f} ({t.pl_pct:+.0%}) [{t.reason}]"
            )
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import os

    from killer_options_bot.web import serve

    # Fail fast if the config is invalid before starting the server.
    config = load_config(args.config)

    # Env fallbacks make container/PaaS deployment (e.g. Railway) simple:
    # PORT, KOB_AUTH_USER, KOB_AUTH_PASS, KOB_SOURCE.
    host = args.host
    port = args.port if args.port is not None else int(os.getenv("PORT", "8787"))
    auth_user = args.auth_user or os.getenv("KOB_AUTH_USER")

    # Data source: --source, else KOB_SOURCE env, else mock. This lets a deploy
    # flip mock -> tradier by setting one env var (no code change / redeploy).
    source = args.source or os.getenv("KOB_SOURCE", "mock")
    if source not in {"mock", "tradier"}:
        print(
            f"Error: unknown source '{source}'. Use 'mock' or 'tradier' "
            "(via --source or the KOB_SOURCE environment variable).",
            file=sys.stderr,
        )
        return 1
    if source == "tradier" and not config.tradier.api_token:
        print(
            "Error: source 'tradier' requires a token. Set TRADIER_API_TOKEN "
            "(and optionally TRADIER_BASE_URL) in the environment.",
            file=sys.stderr,
        )
        return 1

    # Auth: username with password from --auth-pass or KOB_AUTH_PASS env.
    auth = None
    if auth_user:
        password = args.auth_pass or os.getenv("KOB_AUTH_PASS")
        if not password:
            print(
                "Error: auth user requires a password via --auth-pass or "
                "the KOB_AUTH_PASS environment variable.",
                file=sys.stderr,
            )
            return 1
        auth = (auth_user, password)

    if auth is None and host not in {"127.0.0.1", "localhost", "::1"}:
        print(
            "Refusing to bind a non-local host without auth. "
            "Add --auth-user (and a password) to enable Basic Auth.",
            file=sys.stderr,
        )
        return 1

    # Optional embedded run loop. Enabled by the --run flag; the KOB_RUN env
    # var overrides it when set (so a deploy whose start command includes --run
    # can still be turned off with KOB_RUN=0 without a redeploy). Lets one
    # process (e.g. a single Railway service) both trade and serve the UI.
    run_env = os.getenv("KOB_RUN")
    if run_env is not None:
        run_loop = run_env.strip().lower() in {"1", "true", "yes", "on"}
    else:
        run_loop = bool(args.run)

    serve(
        config_path=args.config,
        source=source,
        host=host,
        port=port,
        auth=auth,
        run_loop=run_loop,
        run_tick=int(args.run_tick),
        run_paper=not args.no_paper,
        run_ignore_market_hours=bool(args.ignore_market_hours),
    )
    return 0


def _resolve_run_source(args: argparse.Namespace, config: Config) -> str:
    """Resolve and validate the data source for the run loop (like serve)."""
    import os

    source = args.source or os.getenv("KOB_SOURCE", "mock")
    if source not in {"mock", "tradier"}:
        raise ValueError(
            f"unknown source '{source}'. Use 'mock' or 'tradier' "
            "(via --source or the KOB_SOURCE environment variable)."
        )
    if source == "tradier" and not config.tradier.api_token:
        raise ValueError(
            "source 'tradier' requires a token. Set TRADIER_API_TOKEN "
            "(and optionally TRADIER_BASE_URL) in the environment."
        )
    return source


def cmd_run(args: argparse.Namespace) -> int:
    """Run continuously, acting only while the US market is open.

    Every tick it manages open positions (exit checks), and it scans each
    strategy for new entries on that strategy's own ``scan_interval_minutes``.
    Outside market hours it sleeps until the next open (unless
    --ignore-market-hours). Ctrl-C exits cleanly.
    """
    config = load_config(args.config)
    try:
        source = _resolve_run_source(args, config)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    try:
        run_loop(
            config_path=args.config,
            source=source,
            tick=int(args.tick),
            paper=not args.no_paper,
            ignore_market_hours=bool(args.ignore_market_hours),
            once=bool(args.once),
        )
    except KeyboardInterrupt:
        print("\nStopped by user.")
    return 0


def run_loop(
    config_path: str,
    source: str,
    tick: int = 60,
    paper: bool = True,
    ignore_market_hours: bool = False,
    once: bool = False,
    stop_event=None,
    log=print,
) -> int:
    """The scan/manage loop shared by the `run` command and `serve --run`.

    ``stop_event`` (a threading.Event) lets a host thread request a clean stop;
    ``log`` lets the caller redirect output. Returns the number of active
    (market-open) cycles completed.
    """
    import time as _time
    from datetime import datetime

    from killer_options_bot import market

    config = load_config(config_path)
    storage = get_storage(config)
    tick = max(1, int(tick))
    strategies = list(config.active_strategies)

    log(
        f"Run loop starting: source={source}, tick={tick}s, "
        f"paper={'on' if paper else 'off'}, "
        f"strategies={[s.name for s in strategies]}"
    )
    log(
        "Entry scan cadence per strategy: "
        + ", ".join(f"{s.name}={s.scan_interval_minutes}m" for s in strategies)
    )
    if ignore_market_hours:
        log("WARNING: ignore_market_hours set; trading clock is bypassed.")

    def _stopped() -> bool:
        return stop_event is not None and stop_event.is_set()

    def _sleep(seconds: float) -> None:
        # Interruptible sleep so a stop_event ends the wait promptly.
        if stop_event is not None:
            stop_event.wait(seconds)
        else:
            _time.sleep(seconds)

    # Next time (monotonic seconds) each strategy is due to scan; scan on the
    # first open tick, then space subsequent scans by the strategy interval.
    next_scan: dict[str, float] = {s.name: 0.0 for s in strategies}
    cycles = 0

    while not _stopped():
        now_mono = _time.monotonic()
        open_now = ignore_market_hours or market.is_market_open()

        if not open_now:
            wait = (
                tick
                if ignore_market_hours
                else min(max(market.seconds_until_open(), tick), 3600.0)
            )
            nxt = market.next_open().strftime("%Y-%m-%d %H:%M %Z")
            log(
                f"[{datetime.now().strftime('%H:%M:%S')}] market closed; "
                f"next open {nxt} (sleeping {int(wait)}s)"
            )
            if once:
                break
            _sleep(wait)
            continue

        as_of = date.today()
        data = _build_data_source(source, config, as_of=None)
        engine = PaperEngine(
            config, data, storage, as_of=as_of, cost_model=config.cost_model()
        )
        scanner = Scanner(config, data, storage, as_of=as_of)
        stamp = datetime.now().strftime("%H:%M:%S")

        # 1) Always manage exits first (capital protection).
        results = engine.manage_all()
        closed = sum(1 for r in results if r.closed)
        if results:
            log(f"[{stamp}] managed {len(results)} position(s), closed {closed}")

        # 2) Scan each strategy that is due for new entries.
        opened = 0
        scanned = []
        for strategy in strategies:
            if now_mono < next_scan[strategy.name]:
                continue
            scanned.append(strategy.name)
            next_scan[strategy.name] = (
                now_mono + strategy.scan_interval_minutes * 60
            )
            for candidate in scanner.scan_strategy(strategy):
                if not candidate.decision.allowed:
                    continue
                if not paper:
                    continue
                position = engine.open_from_candidate(candidate)
                if position is not None:
                    opened += 1
                    log(
                        f"[{stamp}] opened #{position.id} "
                        f"[{strategy.name}] {position.quantity}x "
                        f"{position.option_symbol} @ "
                        f"${position.entry_price:.2f}"
                    )
        if scanned and opened == 0:
            log(f"[{stamp}] scanned {scanned}: no new entries")

        cycles += 1
        if once:
            break
        _sleep(tick)

    log(f"Run loop finished after {cycles} active cycle(s).")
    return cycles




def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="killer-options-bot",
        description="Small-account, paper-first options scanner (no live orders).",
    )
    parser.add_argument(
        "--config", default="config.yaml", help="Path to config.yaml"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_scan = sub.add_parser("scan", help="Scan the watchlist and log candidates")
    p_scan.add_argument(
        "--source",
        choices=["mock", "tradier"],
        default="mock",
        help="Market data source (default: mock, no network needed)",
    )
    p_scan.add_argument(
        "--paper",
        action="store_true",
        help="Also open paper positions for allowed candidates",
    )
    p_scan.add_argument(
        "--as-of",
        help="Simulate on a date (YYYY-MM-DD); mock source only",
    )
    p_scan.set_defaults(func=cmd_scan)

    p_hist = sub.add_parser("history", help="Show recently logged candidates")
    p_hist.add_argument("--limit", type=int, default=20)
    p_hist.set_defaults(func=cmd_history)

    p_manage = sub.add_parser(
        "manage", help="Re-price open paper positions and apply exit rules"
    )
    p_manage.add_argument(
        "--source", choices=["mock", "tradier"], default="mock"
    )
    p_manage.add_argument("--as-of", help="Simulate on a date (YYYY-MM-DD)")
    p_manage.set_defaults(func=cmd_manage)

    p_pos = sub.add_parser("positions", help="Show open paper positions (marked)")
    p_pos.add_argument("--source", choices=["mock", "tradier"], default="mock")
    p_pos.add_argument("--as-of", help="Simulate on a date (YYYY-MM-DD)")
    p_pos.set_defaults(func=cmd_positions)

    p_pnl = sub.add_parser("pnl", help="Show paper trading P/L summary")
    p_pnl.add_argument("--source", choices=["mock", "tradier"], default="mock")
    p_pnl.add_argument("--as-of", help="Simulate on a date (YYYY-MM-DD)")
    p_pnl.set_defaults(func=cmd_pnl)

    p_wd = sub.add_parser(
        "withdraw",
        help="Show withdrawal recommendations (advisory only, no money moved)",
    )
    p_wd.set_defaults(func=cmd_withdraw)

    p_export = sub.add_parser(
        "export", help="Export all positions as CSV (stdout or --output file)"
    )
    p_export.add_argument(
        "--output",
        "-o",
        default="-",
        help="Output file path, or '-' for stdout (default)",
    )
    p_export.set_defaults(func=cmd_export)

    p_live = sub.add_parser(
        "live",
        help="Place LIVE orders behind guardrails (DRY-RUN by default)",
    )
    p_live.add_argument(
        "--source", choices=["mock", "tradier"], default="tradier"
    )
    p_live.add_argument(
        "--broker",
        choices=["mock", "tradier"],
        default="mock",
        help="Order broker (mock is offline/safe; default: mock)",
    )
    p_live.add_argument("--as-of", help="Simulate on a date (YYYY-MM-DD)")
    p_live.add_argument(
        "--i-understand-live",
        action="store_true",
        help="Actually transmit orders. Without this flag, runs a dry run.",
    )
    p_live.set_defaults(func=cmd_live)

    p_bt = sub.add_parser(
        "backtest",
        help="Step dates over a range to generate paper stats (mock only)",
    )
    p_bt.add_argument("--start", required=True, help="Start date (YYYY-MM-DD)")
    p_bt.add_argument("--end", required=True, help="End date (YYYY-MM-DD)")
    p_bt.add_argument(
        "--step", type=int, default=1, help="Days between steps (default 1)"
    )
    p_bt.add_argument(
        "--commission",
        type=float,
        default=0.65,
        help="Commission per contract, charged on entry and exit (default 0.65)",
    )
    p_bt.add_argument(
        "--slippage",
        type=float,
        default=1.0,
        help="Fraction of the half-spread crossed on each fill "
        "(1.0 = full bid/ask, 0.0 = mid; default 1.0)",
    )
    p_bt.add_argument(
        "--no-costs",
        action="store_true",
        help="Disable costs entirely (fill at mid, no commission)",
    )
    p_bt.add_argument(
        "--verbose", action="store_true", help="Print the full trade log"
    )
    p_bt.set_defaults(func=cmd_backtest)

    p_serve = sub.add_parser(
        "serve", help="Run the local web dashboard (127.0.0.1, no auth)"
    )
    p_serve.add_argument(
        "--source",
        choices=["mock", "tradier"],
        default=None,
        help="Data source (default: KOB_SOURCE env or 'mock')",
    )
    p_serve.add_argument("--host", default="127.0.0.1", help="Bind host")
    p_serve.add_argument(
        "--port",
        type=int,
        default=None,
        help="Bind port (default: PORT env or 8787)",
    )
    p_serve.add_argument(
        "--auth-user",
        help="Enable HTTP Basic Auth with this username",
    )
    p_serve.add_argument(
        "--auth-pass",
        help="Basic Auth password (or set KOB_AUTH_PASS env var)",
    )
    p_serve.add_argument(
        "--run",
        action="store_true",
        help="Also run the automated scan/manage loop in a background thread "
        "(or set KOB_RUN=1)",
    )
    p_serve.add_argument(
        "--run-tick",
        type=int,
        default=60,
        help="Loop cycle seconds when --run is set (default 60)",
    )
    p_serve.add_argument(
        "--no-paper",
        action="store_true",
        help="With --run, scan/manage only; do not open paper positions",
    )
    p_serve.add_argument(
        "--ignore-market-hours",
        action="store_true",
        help="With --run, bypass the market-open check (testing only)",
    )
    p_serve.set_defaults(func=cmd_serve)

    p_run = sub.add_parser(
        "run",
        help="Run continuously during market hours (scan + manage on a loop)",
    )
    p_run.add_argument(
        "--source",
        choices=["mock", "tradier"],
        default=None,
        help="Data source (default: KOB_SOURCE env or 'mock')",
    )
    p_run.add_argument(
        "--tick",
        type=int,
        default=60,
        help="Seconds between loop cycles; exits are checked every cycle "
        "(default 60)",
    )
    p_run.add_argument(
        "--no-paper",
        action="store_true",
        help="Scan/manage only; do not open paper positions on signals",
    )
    p_run.add_argument(
        "--ignore-market-hours",
        action="store_true",
        help="Bypass the market-open check and always act (testing only)",
    )
    p_run.add_argument(
        "--once",
        action="store_true",
        help="Run a single cycle then exit (useful for cron/testing)",
    )
    p_run.set_defaults(func=cmd_run)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])
    try:
        return args.func(args)
    except Exception as exc:  # top-level guard for a friendly CLI
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
