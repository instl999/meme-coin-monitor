"""Interactive commands: guided setup, Telegram connection, token search, trader discovery and the
watch list. Every change to config.json or .env is validated, written atomically, and (for
config.json) backed up first. Secrets are typed hidden and never printed."""

from __future__ import annotations

import getpass
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path

from . import presets
from .alerts import Notifier, TelegramAPI, TelegramSender, build_presets_renewal, build_test, describe_traders
from .chains import CHAINS, EVM_ADDRESS, EVM_CHAINS, SOLANA
from .config import (HELIUS_RPC_URL, ConfigError, edit_config, load_config, monthly_credits, update_dotenv)
from .discover import EVM_OVERSAMPLE, DiscoveryError, discover
from .evm import check_access, client as evm_client, source as evm_source
from .gecko import Gecko
from .market import fetch_pairs, search_tokens, select_market
from .rpc import HttpClient, HttpError, RpcError, SolanaRPC
from .state import atomic_write
from .util import Redactor, clean_text, fmt_age, fmt_number, fmt_usd, is_pubkey, short, utc

log = logging.getLogger("holder_watch.wizard")

BOT_TOKEN = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")
CHAT_ID = re.compile(r"^-?\d{1,20}$")
UPDATE_TYPES = ["message", "channel_post", "my_chat_member"]
FREE_PLAN_CREDITS = 1_000_000   # Helius
ALCHEMY_FREE_CU = 30_000_000
ANKR_FREE_CREDITS = 200_000_000


class Console:
    """Terminal prompts and output. Tests use a scripted replacement."""

    def __init__(self, out=None, input_fn=input, secret_fn=getpass.getpass):
        self.out = out or sys.stdout
        self.input_fn = input_fn
        self.secret_fn = secret_fn
        self.tty = hasattr(self.out, "isatty") and self.out.isatty()
        self._stage, self._width = None, 0

    def say(self, text: str = "") -> None:
        self.end_progress()
        print(text, file=self.out, flush=True)

    def ask(self, prompt: str, default: str | None = None) -> str:
        self.end_progress()
        suffix = f" [{default}]" if default else ""
        return self.input_fn(f"{prompt}{suffix}: ").strip() or (default or "")

    def secret(self, prompt: str) -> str:
        self.end_progress()
        return self.secret_fn(f"{prompt}: ").strip()

    def confirm(self, prompt: str, default: bool = True) -> bool:
        while True:
            answer = self.ask(f"{prompt} [{'Y/n' if default else 'y/N'}]").lower()
            if not answer:
                return default
            if answer in ("y", "yes", "n", "no"):
                return answer.startswith("y")

    def progress(self, stage: str, done: int, total: int) -> None:
        if not self.tty:
            if stage != self._stage:
                print(f"  {stage} ...", file=self.out, flush=True)
            self._stage = stage
            return
        if stage != self._stage and self._width:
            print(file=self.out)
            self._width = 0
        self._stage = stage
        text = f"  {stage}: {done:,}/{total:,}" if total else f"  {stage} ..."
        print("\r" + text.ljust(self._width), end="", file=self.out, flush=True)
        self._width = max(self._width, len(text))

    def end_progress(self) -> None:
        if self._width:
            print(file=self.out, flush=True)
        self._stage, self._width = None, 0


@dataclass(frozen=True)
class TokenChoice:
    mint: str
    symbol: str | None
    name: str | None
    chain: str = "solana"


def clients(cfg, environ=None):
    """(HttpClient, {chain id: RPC}): Solana always, each EVM chain that can be read (evm.source)."""
    http = HttpClient(redact=Redactor(cfg.secrets))
    rpcs = {"solana": SolanaRPC(cfg.rpc_url, http, public=cfg.uses_public_rpc)}
    for chain_id in EVM_CHAINS:
        rpc = evm_client(CHAINS[chain_id], http, environ)
        if rpc is not None:
            rpcs[chain_id] = rpc
    return http, rpcs


NEEDS_ANKR = "needs ANKR_API_KEY in .env (free at https://www.ankr.com/rpc/, or run setup)"


def history_problem(chain, rpcs: dict) -> str | None:
    """None if a wallet's history can be read on the chain (discovery, built-in lists, scorecards)."""
    rpc = rpcs.get(chain.id)
    if chain is SOLANA:
        return None
    if rpc is None or not rpc.history:
        return f"{chain.name}: a wallet's history {NEEDS_ANKR}"
    return None


# --- Telegram -------------------------------------------------------------------------------------

def setup_telegram(env_path, http, console: Console, *, environ=None, wait_seconds: int = 120,
                   clock=time.monotonic) -> bool:
    """Connect a bot and a chat, send a test message, and save both to .env. True when done."""
    environ = os.environ if environ is None else environ
    token, me = environ.get("TELEGRAM_BOT_TOKEN"), None
    if token:
        try:
            me = TelegramAPI(token, http).call("getMe")
        except HttpError as exc:
            console.say(f"  The bot token in .env doesn't work ({exc.message}). Let's set a new one.")
        else:
            if not console.confirm(f"  Keep the bot @{me.get('username')} that is already set up?", default=True):
                me = None
    if me is None:
        token, me = _ask_bot_token(http, console)
        if me is None:
            return False
    api = TelegramAPI(token, http)
    chat = _detect_chat(api, me.get("username") or "your bot", console, wait_seconds, clock)
    if chat is None:
        answer = console.ask("  Type the chat ID by hand (a number; group IDs start with -), or press Enter to stop")
        if not CHAT_ID.match(answer):
            console.say("  Telegram was not set up." if not answer else "  That isn't a chat ID. Telegram was not set up.")
            return False
        chat = {"id": int(answer)}
    chat_id = str(chat["id"])
    try:
        api.call("sendMessage", chat_id=chat_id, text="holder_watch is connected: its alerts will arrive in this chat.")
    except HttpError as exc:
        console.say(f"  Telegram refused the test message: {exc.message}")
        if "chat not found" in exc.message or "blocked" in exc.message or exc.status == 403:
            console.say("  Open the chat with the bot, press Start (or unblock it), then run this again.")
        return False
    update_dotenv(env_path, {"TELEGRAM_BOT_TOKEN": token, "TELEGRAM_CHAT_ID": chat_id})
    environ["TELEGRAM_BOT_TOKEN"], environ["TELEGRAM_CHAT_ID"] = token, chat_id
    console.say(f"  Test message sent. Saved TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to {Path(env_path).name}.")
    return True


def _ask_bot_token(http, console: Console):
    console.say("  1. In Telegram, open @BotFather, send /newbot and follow its steps.")
    console.say("  2. Paste the token it gives you (it looks like 123456789:AAE...). It stays hidden as you paste.")
    for _ in range(3):
        token = console.secret("  Bot token")
        if not token:
            return None, None
        if not BOT_TOKEN.match(token):
            console.say("  That doesn't look like a bot token: digits, a colon, then 30+ letters, digits, - or _.")
            continue
        try:
            me = TelegramAPI(token, http).call("getMe")
        except HttpError as exc:
            console.say(f"  Telegram rejected this token: {exc.message}")
            continue
        console.say(f"  Bot @{me.get('username')} found.")
        return token, me
    return None, None


def _detect_chat(api, bot_name: str, console: Console, wait_seconds: int, clock) -> dict | None:
    try:
        pending = api.call("getUpdates", timeout=0, allowed_updates=UPDATE_TYPES) or []
    except HttpError as exc:
        if exc.status == 409:
            console.say("  This bot has a webhook, so the messages it receives can't be read here "
                        "(remove it with Telegram's deleteWebhook, or type the chat ID).")
        else:
            console.say(f"  Couldn't read the bot's messages: {exc.message}")
        return None
    offset = None
    seen = set()
    for update in reversed(pending):  # Telegram keeps messages for 24 h: offer the latest chats first
        chat = _chat_of(update)
        if chat and chat.get("id") not in seen:
            seen.add(chat["id"])
            if len(seen) <= 3 and console.confirm(f"  Use the chat with {_describe_chat(chat)}?", default=True):
                return chat
    if pending:
        offset = max(update["update_id"] for update in pending) + 1
    console.say(f"  Now send any message to @{bot_name} in Telegram (for a group: add the bot to the group "
                f"and send a message there). Waiting up to {max(1, wait_seconds // 60)} min ...")
    deadline = clock() + wait_seconds
    while clock() < deadline:
        try:
            updates = api.call("getUpdates", offset=offset, timeout=max(1, min(20, int(deadline - clock()))),
                               allowed_updates=UPDATE_TYPES) or []
        except HttpError as exc:
            console.say(f"  Couldn't read the bot's messages: {exc.message}")
            return None
        for update in updates:
            offset = update["update_id"] + 1
            chat = _chat_of(update)
            if chat and console.confirm(f"  Message received from {_describe_chat(chat)}. Use this chat?", default=True):
                return chat
    console.say("  No message arrived.")
    return None


def _chat_of(update: dict) -> dict | None:
    for key in ("message", "channel_post", "my_chat_member"):
        item = update.get(key)
        if isinstance(item, dict) and isinstance(item.get("chat"), dict) and "id" in item["chat"]:
            return item["chat"]
    return None


def _describe_chat(chat: dict) -> str:
    kind = chat.get("type", "chat")
    if kind == "private":
        name = clean_text(" ".join(filter(None, (chat.get("first_name"), chat.get("last_name")))), 60) or "a user"
        username = clean_text(chat.get("username"), 40)
        return f"{name}{f' (@{username})' if username else ''}, private chat {chat['id']}"
    return f"{kind} \"{clean_text(chat.get('title'), 60) or '?'}\" ({chat['id']})"


# --- RPC key --------------------------------------------------------------------------------------

def offer_helius_key(env_path, console: Console, *, environ=None, http=None) -> bool:
    """On the public RPC, offer to save a Helius key (discovery needs a keyed RPC to be practical)."""
    environ = os.environ if environ is None else environ
    console.say("  You're on the free public Solana RPC: it refuses the top-holder lookup and is slow for discovery.")
    console.say("  A free Helius key fixes both (1M credits a month): https://dashboard.helius.dev")
    for _ in range(3):
        key = console.secret("  Helius API key (Enter to skip)")
        if not key:
            return False
        try:
            SolanaRPC(HELIUS_RPC_URL.format(key=key), http or HttpClient(redact=Redactor([key]))).call("getSlot", [])
        except (HttpError, RpcError) as exc:
            console.say(f"  Helius rejected this key: {Redactor([key])(exc)}")
            continue
        update_dotenv(env_path, {"HELIUS_API_KEY": key})
        environ["HELIUS_API_KEY"] = key
        console.say(f"  Key works. Saved HELIUS_API_KEY to {Path(env_path).name}.")
        return True
    return False


def offer_ankr_key(env_path, console: Console, chain_ids: list, *, environ=None, http=None) -> list:
    """Ask for the Ankr key the EVM chains need for more than the keyless trader watch, check it on each
    of them (its RPC and its transfer index), then save it. Returns the chains it works on (none when
    skipped)."""
    environ = os.environ if environ is None else environ
    keyless = [CHAINS[c].name for c in chain_ids if CHAINS[c].log_span]
    console.say("  A free Ankr key (200M credits a month) covers all four EVM chains: https://www.ankr.com/rpc/ -> "
                "sign up -> copy your API key (the part after rpc.ankr.com/eth/ in any endpoint).")
    console.say("  It is needed for BNB Chain, for finding a token's traders and for building built-in lists"
                + (f"; without it {', '.join(keyless)} trades are still watched through public RPCs." if keyless else "."))
    for _ in range(3):
        key = console.secret("  Ankr API key (Enter to skip)")
        if not key:
            return []
        redact = Redactor([key])
        http_client = http or HttpClient(redact=redact)
        problems = {}
        for chain_id in chain_ids:
            problem = check_access(evm_client(CHAINS[chain_id], http_client, {"ANKR_API_KEY": key}))
            if problem:
                problems[chain_id] = redact(problem)
        working = [c for c in chain_ids if c not in problems]
        if not working:
            console.say(f"  Ankr rejected this key: {next(iter(problems.values()))}")
            continue
        update_dotenv(env_path, {"ANKR_API_KEY": key})
        environ["ANKR_API_KEY"] = key
        console.say(f"  Key works on {', '.join(CHAINS[c].name for c in working)}. Saved ANKR_API_KEY to "
                    f"{Path(env_path).name}.")
        for chain_id, problem in problems.items():
            console.say(f"  {CHAINS[chain_id].name} refused it ({problem}).")
        return working
    return []


# --- chains -----------------------------------------------------------------------------------------

def choose_chains(console: Console, default=("solana",)) -> list[str]:
    ids = list(CHAINS)
    console.say("  " + "   ".join(f"{i}) {CHAINS[c].name}" for i, c in enumerate(ids, 1)))
    suggestion = ",".join(str(ids.index(c) + 1) for c in default if c in ids)
    while True:
        picked = parse_selection(console.ask("  Which chains do you follow? Numbers like 1,3,4 or 'all'", suggestion),
                                 len(ids))
        if picked:
            return [ids[i] for i in picked]
        console.say(f"  Please pick at least one, with numbers from 1 to {len(ids)}.")


def _chain_of(address: str, chain_id: str | None) -> tuple:
    """(Chain, None) for a wallet or token address and an optional --chain, or (None, why not)."""
    if chain_id:
        chain = CHAINS.get(chain_id)
        if chain is None:
            return None, f"unknown chain {chain_id!r} (supported: {', '.join(CHAINS)})"
        return (chain, None) if chain.valid(address) else (None, f"{address!r} is not a {chain.name} address")
    if is_pubkey(address):
        return SOLANA, None
    if EVM_ADDRESS.match(address or ""):
        return None, "an 0x address can be on several chains: add --chain ethereum, base, bsc or arbitrum"
    return None, f"{address!r} is not a Solana or EVM address"


# --- token choice -----------------------------------------------------------------------------------

def choose_token(http, console: Console, query: str | None = None, chains=None) -> TokenChoice | None:
    """A token picked by address, or by searching DEX Screener for a name or symbol on `chains`
    (default: every supported chain)."""
    while True:
        query = (query or console.ask("Token: paste its address, or type a name or symbol to search")).strip()
        if not query:
            return None
        if is_pubkey(query) or EVM_ADDRESS.match(query):
            choice = _confirm_address(http, console, query, chains)
            if choice:
                return choice
            query = None
            continue
        try:
            matches = search_tokens(http, query, chains=chains)[:10]
        except HttpError as exc:
            console.say(f"  Search failed: {exc}")
            return None
        if not matches:
            where = f" on {', '.join(CHAINS[c].name for c in chains)}" if chains else ""
            console.say(f"  No token matching {query!r} on DEX Screener{where}.")
            query = None
            continue
        _print_matches(matches, console)
        answer = console.ask("Pick a number, paste an address, or press Enter to search again")
        query = None
        if is_pubkey(answer) or EVM_ADDRESS.match(answer):
            query = answer
        elif answer.isdigit() and 1 <= int(answer) <= len(matches):
            match = matches[int(answer) - 1]
            choice = _confirm_on(http, console, match.mint, CHAINS[match.chain])
            if choice:
                return choice
        elif answer:
            console.say("  That's not a number from the list.")


def _print_matches(matches: list, console: Console) -> None:
    now = time.time()
    console.say(f"   #  {'Chain':<9} {'Token':<26} {'Address':<44}  {'24h volume':>11}  {'Liquidity':>12}  Pools  Age")
    for number, match in enumerate(matches, 1):
        label = f"{match.name or '?'} ({match.symbol or '?'})"[:26]
        age = fmt_age(now - match.created) if match.created else "?"
        console.say(f"  {number:>2}  {CHAINS[match.chain].name:<9} {label:<26} {match.mint:<44}  "
                    f"{fmt_usd(match.volume_h24):>11}  {fmt_usd(match.liquidity_usd):>12}  {match.pools:>5}  {age}")
        for flag in match.flags:
            console.say(f"      ! {flag}")
    console.say("  Many tokens share a name. The busiest is listed first; check the address against a source you trust.")


def _confirm_address(http, console: Console, address: str, chains=None) -> TokenChoice | None:
    """A Solana mint is on Solana; an 0x address can be on any EVM chain, so look it up on each."""
    if is_pubkey(address):
        if chains and "solana" not in chains:
            console.say("  That is a Solana address, and Solana isn't one of the chains chosen.")
            return None
        return _confirm_on(http, console, address, SOLANA)
    found = []
    for chain_id in (c for c in (chains or EVM_CHAINS) if CHAINS[c].evm):
        chain = CHAINS[chain_id]
        try:
            market = select_market(fetch_pairs(http, address, chain), address, chain)
        except HttpError:
            continue
        if market.found:
            found.append((chain, market))
    if not found:
        console.say("  DEX Screener lists no pool for this address on " + ", ".join(
            CHAINS[c].name for c in (chains or EVM_CHAINS) if CHAINS[c].evm) + ".")
        return None
    index = 0
    if len(found) > 1:
        console.say("  This address has pools on several chains:")
        for i, (chain, market) in enumerate(found, 1):
            console.say(f"   {i}) {chain.name}: {market.name or '?'} ({market.symbol or '?'}) · liquidity "
                        f"{fmt_usd(market.liquidity_usd)}")
        picked = parse_selection(console.ask("  Which chain?", "1"), len(found))
        index = picked[0] if picked else 0
    chain, market = found[index]
    return _show_and_confirm(console, address, chain, market)


def _confirm_on(http, console: Console, mint: str, chain) -> TokenChoice | None:
    try:
        market = select_market(fetch_pairs(http, mint, chain), mint, chain)
    except HttpError as exc:
        console.say(f"  DEX Screener request failed: {exc}")
        return None
    if not market.found:
        console.say(f"  DEX Screener lists no {chain.name} pool with this token as the base token.")
        return None
    return _show_and_confirm(console, mint, chain, market)


def _show_and_confirm(console: Console, mint: str, chain, market) -> TokenChoice | None:
    console.say(f"  {market.name or '?'} ({market.symbol or '?'}) on {chain.name} · {mint}")
    console.say(f"  Price {fmt_usd(market.price_usd)} · liquidity {fmt_usd(market.liquidity_usd)} · "
                f"{market.pool_count} pool(s), deepest {market.venue}")
    if market.name_had_hidden_chars:
        console.say("  ! Its name contains hidden text-direction characters, a trick used by look-alike tokens.")
    if console.confirm("  Is this the token you mean?", default=True):
        return TokenChoice(chain.normalize(mint), market.symbol, market.name, chain.id)
    return None


# --- discovery --------------------------------------------------------------------------------------

def run_discovery(cfg, rpcs: dict, http, choice: TokenChoice, console: Console, settings, *, quiet=False):
    chain = CHAINS[choice.chain]
    rpc = rpcs.get(chain.id)
    say = (lambda text: None) if quiet else console.say
    problem = history_problem(chain, rpcs)
    if problem:
        if quiet:
            print(json.dumps({"error": problem}), file=sys.stderr)
        else:
            console.say(f"  {problem}")
        return None
    symbol = choice.symbol or short(choice.mint)
    say("")
    if chain.evm:
        say(f"Finding the most profitable {symbol} traders on {chain.name}: the senders of the latest trades in its "
            f"busiest pools (GeckoTerminal), then the full {symbol} history of up to "
            f"{settings.candidates * EVM_OVERSAMPLE} of them.")
    else:
        say(f"Finding the most profitable {symbol} traders: up to {settings.scan_transactions:,} recent transactions in "
            f"its busiest pools (last {settings.lookback_hours:g} h), then the full history of up to "
            f"{settings.candidates} wallets.")
        if rpc.public:
            say("  On the public RPC this can take 10+ minutes; with a Helius key it usually takes under one.")
    try:
        report = discover(rpc, http, choice.mint, settings, chain=chain, gecko=Gecko(http),
                          exclude=cfg.exclude_owners if chain is SOLANA else (),
                          progress=None if quiet else console.progress)
    except (DiscoveryError, HttpError, RpcError) as exc:
        if quiet:
            print(json.dumps({"error": str(exc)}), file=sys.stderr)
        else:
            console.say(f"  Discovery failed: {exc}" if not isinstance(exc, DiscoveryError) else f"  {exc}")
        return None
    console.end_progress()
    return report


def print_report(report, console: Console, *, show: int, watched=frozenset()) -> None:
    symbol, native, chain = report.symbol or short(report.mint), report.native, CHAINS[report.chain]
    console.say("")
    price = f"price {fmt_usd(report.price_usd)}"
    if report.price_native:
        price += f" ({fmt_number(report.price_native)} {native})"
    coin = f" · {native} {fmt_usd(report.native_usd)}" if report.native_usd else ""
    console.say(f"{report.name or symbol} ({symbol}) on {chain.name} · {price}{coin}")
    window = f" ({utc(report.scan_from)} to {utc(report.scan_to)})" if report.scan_from and report.scan_to else ""
    pools = ", ".join(p.venue for p in report.pools)
    what = "the latest" if chain.evm else "Scanned"
    console.say(f"{'Read ' + what if chain.evm else what} {report.scanned:,} {'trades' if chain.evm else 'transactions'} "
                f"in {pools}{window}.")
    unit = "Alchemy compute units" if chain.evm else "RPC credits"
    console.say(f"{report.wallets_seen:,} wallets traded there; the full {symbol} history of {report.candidates} was read "
                f"(~{report.credits:,} {unit}, {report.seconds:.0f} s, {report.history_api}).")
    for note in report.notes:
        console.say(f"Note: {note}")
    console.say("")
    if not report.traders:
        console.say("No wallet passed the checks (see 'Left out' below). Try a longer lookback or a lower min_buy_usd.")
    else:
        console.say(f"  {'#':>2}  {'Wallet':<44}  {'Profit ' + native:>11}  {'Profit $':>10}  {'ROI':>6}  "
                    f"{'Spent ' + native:>10}  {'Buys/Sells':>10}  {'Avg hold':>8}  {'Holds':>5}  Last trade")
        for trader in report.traders[:show]:
            roi = f"{trader.roi_pct:+.0f}%" if trader.roi_pct is not None else "?"
            usd = f"{'+' if trader.pnl_usd >= 0 else '-'}${abs(trader.pnl_usd):,.0f}" if trader.pnl_usd is not None else "?"
            last = utc(trader.last_trade, "%Y-%m-%d") if trader.last_trade else "?"
            hold = fmt_age(trader.avg_hold_hours * 3600) if trader.avg_hold_hours is not None else "-"
            console.say(f"  {trader.rank:>2}  {trader.address:<44}  {trader.pnl_native:>+11.4f}  {usd:>10}  {roi:>6}  "
                        f"{trader.cost_native:>10.4f}  {f'{trader.buys} / {trader.sells}':>10}  {hold:>8}  "
                        f"{trader.held_pct:>4.0f}%  {last}")
            details = [f"realized {trader.realized_native:+.4f}, unrealized {trader.unrealized_native:+.4f}"] + trader.notes
            if trader.address in watched:
                details.append("already watched")
            console.say(f"      {' · '.join(details)}")
        if len(report.traders) > show:
            console.say(f"  ... and {len(report.traders) - show} more profitable wallet(s).")
    console.say("")
    console.say(f"Profit = realized + unrealized (what it still holds, at today's price), in {native}, fees included; "
                f"$ at today's {native} price.")
    if report.left_out:
        console.say("Left out: " + "; ".join(f"{count} {reason}" for reason, count in report.left_out.most_common()) + ".")
    console.say("Past results are not a prediction, and a profitable wallet can be an insider or a lucky one.")


def parse_selection(answer: str, count: int) -> list[int] | None:
    """'1,3,5', '1-5', 'all' or 'none' -> 0-based indexes; None if it can't be read."""
    answer = answer.strip().lower()
    if answer == "all":
        return list(range(count))
    if answer in ("none", "0", ""):
        return []
    chosen = []
    for part in answer.replace(" ", "").split(","):
        low, dash, high = part.partition("-")
        if not low.isdigit() or (dash and not high.isdigit()):
            return None
        first, last = int(low), int(high) if dash else int(low)
        if not 1 <= first <= last <= count:
            return None
        chosen += [i - 1 for i in range(first, last + 1) if i - 1 not in chosen]
    return chosen


def _wallet_index(wallets: list) -> dict:
    """(chain, address) -> position in a raw traders.wallets list."""
    index = {}
    for i, w in enumerate(wallets):
        entry = {"address": w} if isinstance(w, str) else w
        chain = CHAINS.get(entry.get("chain", "solana"))
        if chain and isinstance(entry.get("address"), str):
            index[(chain.id, chain.normalize(entry["address"]))] = i
    return index


def save_traders(config_path, report, chosen: list, *, tokens: str | None = None, min_trade_usd: float | None = None,
                 environ=None, today: str | None = None) -> Path:
    """Add the chosen traders to config.json's traders.wallets (wallets already there keep their label)."""
    today = today or time.strftime("%Y-%m-%d")
    symbol = report.symbol or short(report.mint)

    def change(data):
        section = data.get("traders")
        if not isinstance(section, dict):
            section = data["traders"] = {"wallets": [], "poll_seconds": 60, "alert_buys": True, "alert_sells": True,
                                         "tokens": "any", "min_trade_usd": 10}
        wallets = section.setdefault("wallets", [])
        taken = {w.get("label") for w in wallets if isinstance(w, dict)} | set((data.get("labels") or {}).values())
        where = _wallet_index(wallets)
        for trader in chosen:
            facts = {"source_mint": report.mint, "source_symbol": symbol,
                     "pnl_usd": None if trader.pnl_usd is None else round(trader.pnl_usd, 2),
                     "roi_pct": None if trader.roi_pct is None else round(trader.roi_pct, 1), "added": today}
            key = (report.chain, trader.address)
            if key in where:
                old = wallets[where[key]]
                old = {"address": old} if isinstance(old, str) else old
                old.update(facts)
                old.setdefault("label", _unique(f"{symbol} #{trader.rank}", taken))
                wallets[where[key]] = old
            else:
                label = _unique(f"{symbol} #{trader.rank}", taken)
                taken.add(label)
                entry = {"address": trader.address, "label": label, **facts}
                wallets.append({"chain": report.chain, **entry} if report.chain != "solana" else entry)
        if tokens is not None:
            section["tokens"] = tokens
        if min_trade_usd is not None:
            section["min_trade_usd"] = min_trade_usd

    return edit_config(config_path, change, environ=environ)


def switch_token(config_path, mint: str, *, environ=None) -> tuple[Path, list[str]]:
    """Point the token monitor (Solana) at another mint. Wallet lists and labels that belonged to the
    old token go (watched traders keep theirs); the backup keeps everything."""
    removed = []

    def change(data):
        old = data.get("mint")
        data["mint"] = mint
        if not old or old == mint or not is_pubkey(old):
            return
        for key in ("always_alert_owners", "exclude_owners"):
            if data.get(key):
                removed.append(f"{len(data[key])} {key}")
                data[key] = []
        labels = data.get("labels") or {}
        traders = {w if isinstance(w, str) else w.get("address") for w in (data.get("traders") or {}).get("wallets", [])}
        dropped = [address for address in labels if not address.startswith("_") and address not in traders]
        for address in dropped:
            del labels[address]
        if dropped:
            removed.append(f"{len(dropped)} label(s)")

    return edit_config(config_path, change, environ=environ), removed


def add_presets(config_path, chain_ids, lists: dict, *, environ=None) -> tuple[Path | None, int]:
    """Watch the built-in traders of these chains. Returns (backup, wallets added)."""
    added = []

    def change(data):
        section = data.setdefault("traders", {})
        wallets = section.setdefault("wallets", [])
        where = _wallet_index(wallets)
        for chain_id in chain_ids:
            for entry in presets.config_wallets(chain_id, lists.get(chain_id, [])):
                if (chain_id, CHAINS[chain_id].normalize(entry["address"])) not in where:
                    wallets.append(entry)
                    added.append(entry)

    backup = edit_config(config_path, change, environ=environ)
    return backup, len(added)


def renew_presets(config_path, lists: dict, *, environ=None) -> tuple[Path, dict]:
    """Watch these chains' new built-in lists ({chain id: entries}) instead of the old ones, in one edit:
    a wallet on both keeps your settings for it (its figures and label are updated), the others are
    added or dropped. Wallets you added yourself are never touched. Returns (backup, {chain id: (added,
    kept, dropped)})."""
    counts = {}

    def change(data):
        section = data.setdefault("traders", {})
        wallets = section.get("wallets", [])
        for chain_id, entries in lists.items():
            chain = CHAINS[chain_id]
            fresh = {chain.normalize(e["address"]): e for e in presets.config_wallets(chain_id, entries)}
            kept, dropped, out = set(), 0, []
            for w in wallets:
                if not (isinstance(w, dict) and w.get("preset") is True and w.get("chain", "solana") == chain_id):
                    out.append(w)
                    continue
                address = chain.normalize(str(w.get("address", "")))
                if address in fresh and address not in kept:
                    out.append({**w, **{k: fresh[address][k] for k in ("label", "pnl_usd", "roi_pct")}})
                    kept.add(address)
                else:
                    dropped += 1
            where = _wallet_index(out)
            new = [e for a, e in fresh.items() if a not in kept and (chain_id, a) not in where]  # yours stay yours
            wallets = out + new
            counts[chain_id] = (len(new), len(kept), dropped)
        section["wallets"] = wallets

    return edit_config(config_path, change, environ=environ), counts


RENEWED_FLAG = ".presets-renewed"  # tells the monthly systemd timer to restart the monitor


def renew_command(config_path: Path, chain_ids: list, args, console: Console) -> int:
    """Rebuild the lists of the chains whose built-in traders are watched (or of `chain_ids`), watch the
    new lists instead, and report to Telegram. What the monthly timer runs (deploy/holder-watch-presets.*)."""
    config_dir = Path(config_path).parent
    cfg = load_config(config_path, require_mint=False)
    chain_ids = chain_ids or sorted({w.chain for w in cfg.traders.wallets if w.preset}, key=list(CHAINS).index)
    if not chain_ids:
        console.say("No built-in traders are watched, so there is nothing to renew (presets add CHAIN first).")
        return 0
    http, rpcs = clients(cfg)
    criteria = presets.Criteria(days=args.days or 30)
    report, renewed = {}, {}
    for chain_id in chain_ids:
        chain = CHAINS[chain_id]
        missing = can_build_list(chain, rpcs)
        if missing:
            report[chain_id] = f"{chain.name}: not renewed, it {missing}"
            continue
        try:
            picked = build_list(config_dir, chain, http, rpcs, console, criteria, top=args.top or 5)
        except Exception as exc:  # one chain's failure must not stop the others
            log.warning("renewing the %s list failed: %s", chain.name, exc)
            report[chain_id] = f"{chain.name}: not renewed ({Redactor(cfg.secrets)(clean_text(exc, 200))}); still watching the previous traders"
            continue
        if picked:
            renewed[chain_id] = presets.load(config_dir).get(chain_id, [])
        else:
            report[chain_id] = f"{chain.name}: no trader passed this month; still watching the previous ones"
    changed = False
    if renewed:
        backup, counts = renew_presets(config_path, renewed)
        for chain_id, (added, kept, dropped) in counts.items():
            changed = changed or bool(added or dropped)
            report[chain_id] = (f"{CHAINS[chain_id].name}: {len(renewed[chain_id])} trader(s) now, {added} new, {kept} kept, "
                                f"{dropped} dropped")
        console.say(f"Saved config.json (previous version: {backup.name}).")
    lines = [report[c] for c in chain_ids if c in report]
    for line in lines:
        console.say(line)
    if changed:
        atomic_write(config_dir / RENEWED_FLAG, time.strftime("%Y-%m-%d %H:%M:%S\n"))
        console.say("Restart the monitor to watch the new lists (the monthly timer on the server does it).")
    if cfg.telegram_token:
        notifier = Notifier(TelegramSender(cfg.telegram_token, cfg.telegram_chat_id, http))
        notifier.send(build_presets_renewal(lines, changed, time.time()), level=logging.INFO)
    return 0


def remove_presets(config_path, chain_ids, *, environ=None) -> tuple[Path, int]:
    removed = []

    def change(data):
        section = data.setdefault("traders", {})
        keep = []
        for w in section.get("wallets", []):
            if isinstance(w, dict) and w.get("preset") is True and w.get("chain", "solana") in chain_ids:
                removed.append(w)
            else:
                keep.append(w)
        section["wallets"] = keep

    return edit_config(config_path, change, environ=environ), len(removed)


def _unique(label: str, taken: set) -> str:
    candidate, n = label, 2
    while candidate in taken:
        candidate, n = f"{label} ({n})", n + 1
    return candidate


def credit_summary(cfg, environ=None) -> str:
    use = monthly_credits(cfg, environ)
    plans = (("Helius credits", use["holder"] + use["solana_traders"], FREE_PLAN_CREDITS),
             ("Ankr API credits", use["ankr"], ANKR_FREE_CREDITS),
             ("Alchemy compute units", use["alchemy"], ALCHEMY_FREE_CU))
    parts = [f"about {used:,} {name} a month (free plan: {cap:,})" for name, used, cap in plans if used]
    if parts:
        text = "Estimated use: " + " and ".join(parts)
    else:
        text = "Estimated use: no API credits (public RPCs only)" if cfg.traders.wallets or cfg.mint else \
            "Estimated use: nothing is watched yet"
    if any(used > cap * 0.9 for _, used, cap in plans):
        text += ". That is close to or above a free plan: raise traders.poll_seconds or watch fewer wallets"
    return text + "."


# --- commands ---------------------------------------------------------------------------------------

def setup_command(config_path: Path, env_path: Path, console: Console) -> int:
    console.say("holder_watch setup: Telegram alerts, the chains you follow, built-in traders, and a token's most "
                "profitable traders.")
    console.say("Nothing here can trade or move funds; it only reads public data and sends you messages.")
    console.say("")
    console.say("Step 1 of 4 · Telegram")
    http = HttpClient(redact=Redactor())
    if not (os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID")) \
            or console.confirm("  Telegram is already set up. Change the bot or chat?", default=False):
        if not setup_telegram(env_path, http, console) and \
                not console.confirm("  Continue without Telegram (alerts go to the console and log only)?", default=True):
            return 1
    try:
        cfg = load_config(config_path, require_mint=False)
    except ConfigError as exc:
        console.say(str(exc))
        return 2

    console.say("")
    console.say("Step 2 of 4 · Chains")
    watched = sorted({w.chain for w in cfg.traders.wallets}, key=list(CHAINS).index)
    chains = choose_chains(console, default=watched or ("solana",))
    if "solana" in chains and cfg.uses_public_rpc and offer_helius_key(env_path, console):
        cfg = load_config(config_path, require_mint=False)
    evm = [c for c in chains if CHAINS[c].evm]
    if evm and not all(getattr(evm_source(CHAINS[c]), "history", False) for c in evm):
        offer_ankr_key(env_path, console, evm)
        skipped = [c for c in evm if evm_source(CHAINS[c]) is None]
        if skipped:
            console.say(f"  Skipping {', '.join(CHAINS[c].name for c in skipped)} for now (it needs the key); run "
                        f"setup again to add {'it' if len(skipped) == 1 else 'them'}.")
            chains = [c for c in chains if c not in skipped]
        cfg = load_config(config_path, require_mint=False)
    if not chains:
        console.say("No chain chosen; nothing else was changed.")
        return 1
    http, rpcs = clients(cfg)

    console.say("")
    console.say("Step 3 of 4 · Built-in traders")
    config_dir = Path(config_path).parent
    lists = presets.load(config_dir)
    missing = [c for c in chains if not lists.get(c)]
    buildable = [c for c in missing if can_build_list(CHAINS[c], rpcs) is None]
    if buildable and console.confirm(
            f"  No built-in list yet for {', '.join(CHAINS[c].name for c in buildable)}. Build "
            f"{'it' if len(buildable) == 1 else 'them'} now from each chain's active traders (several minutes per "
            "chain)?", default=True):
        for chain_id in buildable:
            build_list(config_dir, CHAINS[chain_id], http, rpcs, console, presets.Criteria())
        lists = presets.load(config_dir)
    available = {c: lists[c] for c in chains if lists.get(c)}
    for chain_id, entries in available.items():
        console.say(f"  {CHAINS[chain_id].name}: {len(entries)} trader(s) with a good last 30 days "
                    f"(listed {entries[0].get('as_of') or '?'}).")
    if available and console.confirm(f"  Watch the built-in traders for {', '.join(CHAINS[c].name for c in available)}?",
                                     default=True):
        backup, count = add_presets(config_path, list(available), lists)
        console.say(f"  Watching {count} more trader(s). Saved config.json (previous version: {backup.name}).")
    missing = [c for c in chains if c not in available]
    if missing:
        console.say(f"  No built-in list for {', '.join(CHAINS[c].name for c in missing)}; build one later with: "
                    f"python holder_watch.py presets refresh {' '.join(missing)}")

    console.say("")
    console.say("Step 4 of 4 · A token's most profitable traders (optional)")
    choice = None
    if console.confirm("  Find a token's most profitable traders now?", default=True):
        choice = choose_token(http, console, chains=chains)
    if choice is not None:
        report = run_discovery(cfg, rpcs, http, choice, console, cfg.discovery)
        if report is not None:
            print_report(report, console, show=cfg.discovery.show,
                         watched={w.address for w in cfg.traders.wallets if w.chain == report.chain})
            chosen = _ask_traders(report, console, cfg.discovery.watch)
            symbol = choice.symbol or short(choice.mint)
            if chosen:
                any_token = console.confirm(f"  Alert on their trades in any token (No = only {symbol})?", default=True)
                minimum = _ask_number(console, "  Smallest trade to alert, in USD", cfg.traders.min_trade_usd)
                backup = save_traders(config_path, report, chosen, tokens="any" if any_token else "source",
                                      min_trade_usd=minimum)
                console.say(f"  Watching {len(chosen)} more trader(s). Saved config.json (previous version: {backup.name}).")
            if choice.chain == "solana":
                _offer_token_monitor(config_path, cfg, choice, console)
    cfg = load_config(config_path, require_mint=False)
    console.say("")
    console.say(credit_summary(cfg))
    if cfg.telegram_token and console.confirm("Send a test alert to Telegram now?", default=True):
        notifier = Notifier(TelegramSender(cfg.telegram_token, cfg.telegram_chat_id, http))
        monitored = short(cfg.mint) if cfg.mint else "holder_watch"
        delivered = notifier.send(build_test(monitored, cfg, time.time()), level=logging.INFO)
        console.say("  Delivered." if delivered else "  Telegram delivery failed; see the error above.")
    _print_next_steps(console, cfg)
    return 0


def _offer_token_monitor(config_path, cfg, choice: TokenChoice, console: Console) -> None:
    symbol = choice.symbol or short(choice.mint)
    if cfg.mint is None:
        if console.confirm(f"  Also watch {symbol}'s top holders, price and liquidity?", default=True):
            switch_token(config_path, choice.mint)
            console.say(f"  The token monitor now follows {symbol}. Set its alert rules in config.json (README).")
    elif cfg.mint != choice.mint:
        if console.confirm(f"  The token monitor follows {short(cfg.mint)}. Switch it to {symbol}? (Its whale lists and "
                           "labels for the old token are removed; the backup keeps them.)", default=False):
            backup, removed = switch_token(config_path, choice.mint)
            console.say(f"  The token monitor now follows {symbol}" + (f"; removed {', '.join(removed)}" if removed else "")
                        + f" (previous config: {backup.name}).")


def telegram_command(env_path: Path, console: Console) -> int:
    return 0 if setup_telegram(env_path, HttpClient(redact=Redactor()), console) else 1


def discover_command(config_path: Path, args, console: Console, *, can_prompt: bool) -> int:
    """can_prompt: stdin is a terminal. With --watch or --json nothing is asked once the token is known."""
    cfg = load_config(config_path, require_mint=False)
    http, rpcs = clients(cfg)
    overrides = {key: value for key, value in (("scan_transactions", args.scan), ("lookback_hours", args.hours),
                                               ("show", args.show), ("candidates", args.candidates))
                 if value is not None}
    settings = replace(cfg.discovery, **overrides)
    unattended = args.json or args.watch is not None
    chains = [args.chain] if getattr(args, "chain", None) else None
    token = args.token or ""
    if token and (is_pubkey(token) or EVM_ADDRESS.match(token)) and (unattended or not can_prompt):
        chain, problem = _chain_of(token, getattr(args, "chain", None))
        if chain is None:
            console.say(problem)
            return 2
        market = select_market(fetch_pairs(http, token, chain), token, chain)
        if not market.found:
            console.say(f"DEX Screener lists no {chain.name} pool with this token as the base token.")
            return 1
        choice = TokenChoice(chain.normalize(token), market.symbol, market.name, chain.id)
    elif can_prompt:
        choice = choose_token(http, console, args.token, chains=chains)
    else:
        console.say("Give the token's address (searching by name needs an interactive terminal).")
        return 2
    if choice is None:
        return 1
    report = run_discovery(cfg, rpcs, http, choice, console, settings, quiet=args.json)
    if report is None:
        return 1
    watched = {w.address for w in cfg.traders.wallets if w.chain == report.chain}
    if args.json:
        print(json.dumps(_report_json(report), indent=2), file=console.out)
    else:
        print_report(report, console, show=settings.show, watched=watched)
    if args.watch is not None:
        chosen = report.traders[: args.watch]
    elif can_prompt and not unattended:
        chosen = _ask_traders(report, console, settings.watch)
    else:
        chosen = []
    if chosen:
        backup = save_traders(config_path, report, chosen)
        console.say(f"Added {len(chosen)} trader(s) to config.json (previous version: {backup.name}). "
                    "Restart the monitor to start watching them.")
    return 0


def traders_command(config_path: Path, args, console: Console) -> int:
    cfg = load_config(config_path, require_mint=False)
    if args.action == "list":
        if not cfg.traders.wallets:
            console.say("No traders watched yet. Add the built-in ones (presets add) or find some (discover).")
            return 0
        console.say(describe_traders(cfg))
        for wallet in cfg.traders.wallets:
            notes = [CHAINS[wallet.chain].name]
            if wallet.tokens or wallet.min_trade_usd is not None:
                notes.append(f"{cfg.traders.scope(wallet)} tokens, at least ${cfg.traders.minimum(wallet):,.0f}")
            if wallet.preset:
                notes.append("built-in" + (f" ({wallet.pnl_usd:+,.0f} $ in 30 days)" if wallet.pnl_usd is not None else ""))
            elif wallet.source_mint:
                found = f"discovered on {wallet.source_symbol or short(wallet.source_mint)}"
                if wallet.pnl_usd is not None:
                    found += f" ({wallet.pnl_usd:+,.0f} $"
                    found += f", {wallet.roi_pct:+.0f}%)" if wallet.roi_pct is not None else ")"
                notes.append(found + (f" on {wallet.added}" if wallet.added else ""))
            console.say(f"  {wallet.address:<44}  {wallet.label or '':<20}  {' · '.join(notes)}")
        console.say(credit_summary(cfg))
        return 0
    chain, problem = _chain_of(args.address or "", getattr(args, "chain", None))
    if chain is None:
        console.say(problem)
        return 2
    address = chain.normalize(args.address)
    settings = {key: value for key, value in (("label", clean_text(args.label, 40) if args.label else None),
                                              ("tokens", getattr(args, "tokens", None)),
                                              ("min_trade_usd", getattr(args, "min_usd", None)))
                if value is not None}

    def change(data):
        section = data.setdefault("traders", {})
        wallets = section.setdefault("wallets", [])
        where = _wallet_index(wallets).get((chain.id, address))
        if args.action == "remove":
            section["wallets"] = [w for i, w in enumerate(wallets) if i != where]
        elif where is not None:
            entry = wallets[where]
            wallets[where] = {**({"address": entry} if isinstance(entry, str) else entry), **settings}
        else:
            wallets.append({**({"chain": chain.id} if chain.evm else {}), "address": address, **settings})

    known = any(w.address == address and w.chain == chain.id for w in cfg.traders.wallets)
    if args.action == "remove" and not known or args.action == "add" and known and not settings:
        console.say(f"{short(address)} is {'already' if known else 'not'} on the watch list on {chain.name}.")
        return 0
    try:
        backup = edit_config(config_path, change)
    except ConfigError as exc:
        console.say(str(exc))
        return 2
    done = "Removed" if args.action == "remove" else ("Updated" if known else "Added")
    console.say(f"{done} {address} on {chain.name} (previous config: {backup.name}). Restart the monitor to apply it.")
    return 0


def presets_command(config_path: Path, args, console: Console) -> int:
    """List, watch, unwatch or rebuild the built-in traders."""
    config_dir = Path(config_path).parent
    lists = presets.load(config_dir)
    chosen = list(dict.fromkeys(args.chains or []))
    unknown = [c for c in chosen if c not in CHAINS]
    if unknown:
        console.say(f"Unknown chain(s): {', '.join(unknown)} (supported: {', '.join(CHAINS)})")
        return 2
    if args.action == "list":
        cfg = load_config(config_path, require_mint=False)
        watched = {w.key for w in cfg.traders.wallets}
        for chain_id in chosen or list(CHAINS):
            entries = lists.get(chain_id, [])
            console.say(f"{CHAINS[chain_id].name}: " + (f"{len(entries)} built-in trader(s), listed "
                                                         f"{entries[0].get('as_of') or '?'}" if entries else "none yet"))
            for e in entries:
                mark = "watched" if f"{chain_id}:{CHAINS[chain_id].normalize(e['address'])}" in watched else ""
                pnl = f"{e['pnl_usd']:+,.0f} $" if e.get("pnl_usd") is not None else "?"
                hold = f"{e['avg_hold_hours']:.0f} h" if e.get("avg_hold_hours") is not None else "?"
                console.say(f"  {e['address']:<44}  {pnl:>10} in {e.get('days', 30)} d · {e.get('tokens_traded', '?')} tokens · "
                            f"wins {e.get('win_rate', 0):.0%} · avg hold {hold} · last trade {e.get('last_trade') or '?'}  {mark}")
        return 0
    if args.action == "renew":
        return renew_command(config_path, chosen, args, console)
    if not chosen:
        console.say("Name the chain(s), e.g.: presets add base bsc")
        return 2
    if args.action == "add":
        empty = [c for c in chosen if not lists.get(c)]
        if empty and getattr(args, "build", False):
            http, rpcs = clients(load_config(config_path, require_mint=False))
            for chain_id in empty:
                build_list(config_dir, CHAINS[chain_id], http, rpcs, console, presets.Criteria())
            lists = presets.load(config_dir)
            empty = [c for c in chosen if not lists.get(c)]
        if empty:
            console.say(f"No built-in list for {', '.join(CHAINS[c].name for c in empty)} yet: presets refresh "
                        f"{' '.join(empty)} (or presets add --build)")
        backup, count = add_presets(config_path, [c for c in chosen if lists.get(c)], lists)
        console.say(f"Watching {count} more built-in trader(s) (previous config: {backup.name}). Restart the monitor to "
                    "apply it.")
        return 0
    if args.action == "remove":
        backup, count = remove_presets(config_path, chosen)
        console.say(f"Stopped watching {count} built-in trader(s) (previous config: {backup.name}).")
        return 0
    cfg = load_config(config_path, require_mint=False)  # refresh
    http, rpcs = clients(cfg)
    criteria = presets.Criteria(days=args.days or 30)
    for chain_id in chosen:
        picked = build_list(config_dir, CHAINS[chain_id], http, rpcs, console, criteria, top=args.top or 5)
        if picked and getattr(args, "add", False):
            backup, count = add_presets(config_path, [chain_id], presets.load(config_dir))
            console.say(f"  Watching {count} more trader(s) (previous config: {backup.name}).")
    return 0


def can_build_list(chain, rpcs: dict) -> str | None:
    """None if a built-in list can be made for the chain with these clients, else what's missing."""
    if chain.evm and (chain.id not in rpcs or not rpcs[chain.id].history):
        return NEEDS_ANKR
    if chain is SOLANA and rpcs["solana"].public:
        return "the public RPC is too slow for this; add HELIUS_API_KEY to .env (free at helius.dev)"
    return None


def build_list(config_dir: Path, chain, http, rpcs: dict, console: Console, criteria, *, top: int = 5) -> list:
    """Build and save (presets.local.json) the chain's built-in traders; returns the picked scorecards."""
    missing = can_build_list(chain, rpcs)
    if missing:
        console.say(f"{chain.name}: {missing}.")
        return []
    console.say(f"Building the {chain.name} list: active traders from its busiest pools, each scored over "
                f"{criteria.days} days. This takes several minutes.")
    picked, cards = presets.build(chain, gecko=Gecko(http), http=http, criteria=criteria, top=top,
                                  solana_rpc=rpcs.get("solana"), evm_rpc=rpcs.get(chain.id) if chain.evm else None,
                                  progress=console.progress)
    console.end_progress()
    reasons = {}
    for card in cards:
        reasons[card.reason or "picked"] = reasons.get(card.reason or "picked", 0) + 1
    summary = "; ".join(f"{n} {r}" for r, n in sorted(reasons.items(), key=lambda kv: -kv[1])) or "no candidates found"
    if not picked:  # a list that went stale is better than none: keep it
        console.say(f"{chain.name}: none picked of {len(cards)} ({summary}). Nothing saved; try again later.")
        return []
    today = time.strftime("%Y-%m-%d")
    entries = [presets.entry(card, rank, today) for rank, card in enumerate(picked, 1)]
    path = presets.save_local(config_dir, chain.id, entries, criteria)
    console.say(f"{chain.name}: {len(picked)} picked of {len(cards)} scored ({summary}). Saved {path.name}.")
    for card in picked:
        console.say(f"  {card.wallet}  {card.pnl_usd:+,.0f} $ · {card.tokens_traded} tokens · wins {card.win_rate:.0%} · "
                    f"avg hold {card.avg_hold_hours or 0:.0f} h")
    return picked


def scorecard_command(config_path: Path, args, console: Console) -> int:
    """A wallet's results over its last N days, across every token, with the built-in traders' rules."""
    chain, problem = _chain_of(args.address, args.chain)
    if chain is None:
        console.say(problem)
        return 2
    cfg = load_config(config_path, require_mint=False)
    http, rpcs = clients(cfg)
    problem = history_problem(chain, rpcs)
    if problem:
        console.say(problem + ".")
        return 2
    criteria = presets.Criteria(days=args.days or 30)
    card = presets.scorecard(chain, chain.normalize(args.address), solana_rpc=rpcs.get("solana"),
                             evm_rpc=rpcs.get(chain.id) if chain.evm else None, http=http, criteria=criteria)
    pnl = f"{card.pnl_usd:+,.0f} $" if card.pnl_usd is not None else "?"
    console.say(f"{card.wallet} on {chain.name}, last {card.days} days:")
    console.say(f"  profit {pnl} ({card.pnl_native:+.4f} {chain.native}" +
                (f", {card.roi_pct:+.0f}% of {card.cost_native:.4f} spent)" if card.roi_pct is not None else ")"))
    console.say(f"  {card.trades} trades in {card.tokens_traded} tokens · made money on {card.win_rate:.0%} of them")
    if card.avg_hold_hours is not None:
        console.say(f"  sold after {fmt_age(card.avg_hold_hours * 3600)} on average · "
                    f"{(card.scalp_share or 0):.0%} sold within {criteria.min_hold_minutes:g} min")
    if card.best_tokens:
        console.say("  best tokens: " + ", ".join(f"{name} ({usd:+,.0f} $)" if usd is not None else str(name)
                                                    for name, usd in card.best_tokens))
    console.say("  Built-in trader rules: " + ("passes" if card.reason is None else f"not picked: {card.reason}"))
    return 0


def _ask_traders(report, console: Console, default_count: int) -> list:
    if not report.traders:
        return []
    default = min(default_count, len(report.traders))
    suggestion = "none" if not default else ("1" if default == 1 else f"1-{default}")
    while True:
        answer = console.ask("Watch which of them? Numbers like 1,3,5 or 1-5, 'all' or 'none'", suggestion)
        picked = parse_selection(answer, len(report.traders))
        if picked is not None:
            return [report.traders[i] for i in picked]
        console.say(f"  Please answer with numbers from 1 to {len(report.traders)}, 'all' or 'none'.")


def _ask_number(console: Console, prompt: str, default: float) -> float:
    while True:
        answer = console.ask(prompt, f"{default:g}")
        try:
            value = float(answer)
        except ValueError:
            value = -1
        if value >= 0:
            return value
        console.say("  Please enter a number, 0 or more.")


def _report_json(report) -> dict:
    return {
        "chain": report.chain, "mint": report.mint, "symbol": report.symbol, "name": report.name,
        "price_usd": report.price_usd, "price_native": report.price_native, "native": report.native,
        "native_usd": report.native_usd, "pools": [{"address": p.address, "venue": p.venue} for p in report.pools],
        "scanned": report.scanned, "scan_from": report.scan_from, "scan_to": report.scan_to,
        "wallets_seen": report.wallets_seen, "candidates": report.candidates,
        "traders": [vars(trader) for trader in report.traders],
        "left_out": dict(report.left_out), "notes": report.notes, "credits": report.credits,
    }


def _print_next_steps(console: Console, cfg) -> None:
    console.say("")
    console.say("Done. Next:")
    console.say("  python holder_watch.py --once    one check now (the first one only notes where each wallet starts)")
    console.say("  python holder_watch.py           run continuously")
    console.say("  python holder_watch.py traders   the watch list; 'presets' and 'discover' add more")
    console.say("On the server, after changing config.json there: sudo systemctl restart holder-watch")
