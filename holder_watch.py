#!/usr/bin/env python3
"""Read-only crypto monitor: a Solana token's top holders, price and liquidity, and the trades of
wallets you choose on Solana, Ethereum, Base, BNB Chain and Arbitrum (built-in traders, or a token's
most profitable traders), with alerts on Telegram.

It never asks for, stores or uses a seed phrase or private key, and never signs or sends a
transaction. Alerts report which of your rules fired and the data behind it.

    python holder_watch.py setup             guided setup: Telegram, your chains, built-in traders, a token's top traders
    python holder_watch.py presets           the built-in traders per chain (presets add|remove|refresh CHAIN...)
    python holder_watch.py discover [TOKEN]  rank a token's traders by profit and watch the ones you pick
    python holder_watch.py scorecard WALLET  a wallet's results over its last 30 days
    python holder_watch.py traders           list the watched traders (traders add|remove ADDRESS)
    python holder_watch.py telegram          connect a Telegram bot and chat, send a test message
    python holder_watch.py                   run the monitoring loop
    python holder_watch.py --once            run a single cycle and exit (exit code 1 if it had errors)
    python holder_watch.py --list            print the current top holders once
    python holder_watch.py --test-alert      send a test alert
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

from watcher import alerts, wizard
from watcher.chains import CHAINS, EVM_CHAINS
from watcher.config import ConfigError, load_config, load_dotenv
from watcher.evm import client as evm_client
from watcher.listing import list_holders
from watcher.monitor import Monitor, run_forever
from watcher.rpc import HttpClient, SolanaRPC
from watcher.state import StateStore, TraderStateStore
from watcher.traders import TraderWatch
from watcher.util import Redactor, short

DEFAULT_CONFIG = Path(__file__).resolve().with_name("config.json")
COMMANDS_WITHOUT_CONFIG = ("setup", "telegram")  # they can fix .env first, and setup loads config.json itself


def _at_least(minimum, kind=int):
    def parse(text):
        try:
            value = kind(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"expected a number, got {text!r}") from None
        if value < minimum:
            raise argparse.ArgumentTypeError(f"must be at least {minimum}")
        return value
    return parse


def parse_args(argv=None):
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=argparse.SUPPRESS,
                        help="path to config.json (default: next to this script); .env is read from the same folder")
    common.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS, help="debug logging")
    parser = argparse.ArgumentParser(parents=[common], description="Read-only token monitor and trader watch for "
                                     "Solana, Ethereum, Base, BNB Chain and Arbitrum, with Telegram alerts. Run 'setup' "
                                     "first.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--list", action="store_true", help="print the current top holders once and exit")
    mode.add_argument("--test-alert", action="store_true", help="send a test alert and exit")
    mode.add_argument("--once", action="store_true", help="run a single monitoring cycle and exit")
    chains = tuple(CHAINS)
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")
    commands.add_parser("setup", parents=[common],
                        help="guided setup: Telegram, your chains, built-in traders, a token's most profitable traders")
    commands.add_parser("telegram", parents=[common], help="connect a Telegram bot and chat, and send a test message")
    presets = commands.add_parser("presets", parents=[common], help="the built-in traders of each chain")
    presets.add_argument("action", nargs="?", choices=("list", "add", "remove", "refresh", "renew"), default="list",
                         help="renew: rebuild the lists you watch and watch the new ones (run monthly by a timer)")
    presets.add_argument("chains", nargs="*", metavar="CHAIN", help=f"one or more of: {', '.join(chains)}")
    presets.add_argument("--top", type=_at_least(1), metavar="N", help="refresh: how many to keep (5)")
    presets.add_argument("--days", type=_at_least(1), metavar="D", help="refresh: days of history scored (30)")
    presets.add_argument("--add", action="store_true", help="refresh: also watch the new list")
    presets.add_argument("--build", action="store_true",
                         help="add: first build the lists that are missing (needs the API keys; minutes per chain)")
    discover = commands.add_parser("discover", parents=[common], help="rank a token's traders by profit")
    discover.add_argument("token", nargs="?", help="token address, or a name or symbol to search for")
    discover.add_argument("--chain", choices=chains, help="the token's chain (an 0x address can be on several)")
    discover.add_argument("--watch", type=_at_least(0), metavar="N", help="add the top N to the watch list without asking")
    discover.add_argument("--json", action="store_true", help="print the ranking as JSON")
    discover.add_argument("--scan", type=_at_least(50), metavar="N", help="Solana: recent pool transactions to scan")
    discover.add_argument("--hours", type=_at_least(1, float), metavar="H", help="Solana: scan no further back than this")
    discover.add_argument("--candidates", type=_at_least(1), metavar="N", help="wallets whose full history is read")
    discover.add_argument("--show", type=_at_least(1), metavar="N", help="ranked wallets to print")
    scorecard = commands.add_parser("scorecard", parents=[common], help="a wallet's results over its last days")
    scorecard.add_argument("address", help="wallet address")
    scorecard.add_argument("--chain", choices=chains, help="the wallet's chain (needed for an 0x address)")
    scorecard.add_argument("--days", type=_at_least(1), metavar="D", help="days of history (30)")
    traders = commands.add_parser("traders", parents=[common], help="list, add, change or remove watched traders")
    traders.add_argument("action", nargs="?", choices=("list", "add", "remove"), default="list")
    traders.add_argument("address", nargs="?", help="wallet address, for add (also changes a watched one) and remove")
    traders.add_argument("--chain", choices=chains, help="the wallet's chain (needed for an 0x address)")
    traders.add_argument("--label", help="name shown in alerts")
    traders.add_argument("--tokens", choices=("any", "source", "major"),
                         help="this wallet's trades in any token, only its discovery token, or only major tokens")
    traders.add_argument("--min-usd", type=_at_least(0, float), metavar="USD", help="smallest trade to alert for it")
    args = parser.parse_args(argv)
    if args.command and (args.list or args.test_alert or args.once):
        parser.error(f"--list, --test-alert and --once can't be combined with {args.command}")
    if args.command == "traders" and args.action != "list" and not args.address:
        parser.error(f"traders {args.action} needs a wallet address")
    args.config = getattr(args, "config", str(DEFAULT_CONFIG))
    args.verbose = getattr(args, "verbose", False)
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    for stream in (sys.stdout, sys.stderr):  # Windows consoles/pipes may default to a legacy code page
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    config_path = Path(args.config).resolve()
    env_path = config_path.parent / ".env"
    load_dotenv(env_path)
    try:
        cfg = load_config(config_path, require_mint=False)
        if args.command is None and cfg.mint is None and (args.list or not cfg.traders.wallets):
            load_config(config_path)  # running needs a token to monitor or traders to watch: report the mint
    except ConfigError as exc:
        if args.command not in COMMANDS_WITHOUT_CONFIG:
            print(exc, file=sys.stderr)
            return 2
        cfg = None

    redactor = Redactor(cfg.secrets if cfg else ())
    log_file = cfg.log_file if cfg else config_path.parent / "logs" / "holder_watch.log"
    alerts.setup_logging(log_file, redactor, verbose=args.verbose,
                         console_level=logging.WARNING if (args.list or args.command) else None)
    for note in cfg.notes if cfg else ():
        logging.getLogger("holder_watch").warning("config.json: %s", note)
    if args.command:
        return run_command(args, config_path, env_path)

    http = HttpClient(redact=redactor)
    rpc = SolanaRPC(cfg.rpc_url, http, public=cfg.uses_public_rpc)
    telegram = alerts.TelegramSender(cfg.telegram_token, cfg.telegram_chat_id, http) if cfg.telegram_token else None
    notifier = alerts.Notifier(telegram)

    if args.list:
        return list_holders(cfg, rpc, http)
    if args.test_alert:
        return send_test_alert(cfg, notifier)
    monitor = Monitor(cfg, rpc, http, notifier, StateStore(cfg.state_file)) if cfg.mint else None
    traders = None
    if cfg.traders.wallets:
        traders = TraderWatch(cfg, rpc, http, notifier, TraderStateStore(cfg.trader_state_file),
                              evm=evm_clients(cfg, http), signal_log=cfg.signal_log)
        traders.owns_heartbeat = monitor is None
    if args.once:
        ok = True
        if monitor is not None:
            monitor.trader_watch = traders
            ok = monitor.run_cycle().ok
        if traders is not None:
            ok = traders.run_cycle().ok and ok
        return 0 if ok else 1
    return run_forever(monitor, traders)


def evm_clients(cfg, http) -> dict:
    """An RPC per EVM chain that has watched wallets and can be read (a key, a URL, or its public RPC);
    the trader watch reports the rest."""
    watched = {wallet.chain for wallet in cfg.traders.wallets}
    clients = {}
    for chain_id in EVM_CHAINS:
        rpc = evm_client(CHAINS[chain_id], http) if chain_id in watched else None
        if rpc is not None:
            clients[chain_id] = rpc
    return clients


def run_command(args, config_path: Path, env_path: Path) -> int:
    console = wizard.Console()
    try:
        if args.command == "setup":
            return wizard.setup_command(config_path, env_path, console)
        if args.command == "telegram":
            return wizard.telegram_command(env_path, console)
        if args.command == "discover":
            return wizard.discover_command(config_path, args, console, can_prompt=sys.stdin.isatty())
        if args.command == "presets":
            return wizard.presets_command(config_path, args, console)
        if args.command == "scorecard":
            return wizard.scorecard_command(config_path, args, console)
        return wizard.traders_command(config_path, args, console)
    except ConfigError as exc:
        console.say(str(exc))
        return 2
    except KeyboardInterrupt:
        console.say("\nStopped. Changes already confirmed above were saved; nothing else was changed.")
        return 130
    except EOFError:
        console.say("\nThis command asks questions: run it in a terminal, or pass the answers as options (--help).")
        return 2


def send_test_alert(cfg, notifier) -> int:
    symbol = short(cfg.mint) if cfg.mint else "holder_watch"
    try:  # peek at the last known symbol without modifying state.json
        symbol = json.loads(cfg.state_file.read_text(encoding="utf-8"))["last"].get("symbol") or symbol
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    delivered = notifier.send(alerts.build_test(symbol, cfg, time.time()), level=logging.INFO)
    if notifier.telegram is None:
        print("Telegram is not configured (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env), so the test "
              "alert went to the console and log file only. To connect it: python holder_watch.py telegram")
        return 0
    print("Test alert sent to Telegram." if delivered else "Telegram delivery failed; see the error above.")
    return 0 if delivered else 1


if __name__ == "__main__":
    sys.exit(main())
