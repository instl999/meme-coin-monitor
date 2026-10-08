"""Load config.json (validated, with readable errors) and secrets from the environment / .env."""

from __future__ import annotations

import difflib
import json
import math
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .chains import CHAINS, EVM_ADDRESS
from .state import atomic_write
from .util import clean_text, is_pubkey

PUBLIC_RPC_URL = "https://api.mainnet-beta.solana.com"
HELIUS_RPC_URL = "https://mainnet.helius-rpc.com/?api-key={key}"
PLACEHOLDER_MINT = "PASTE_MINT_HERE"

RULE_DEFAULTS = {
    "stop_price_usd": None,
    "trailing_stop_pct": None,
    "min_liquidity_usd": None,
    "holder_drop_pct": None,
    "combined_drop_pct": None,
    "window_minutes": 60,
}
# Cooldowns in minutes, keyed by rule. Holder rules count per wallet.
COOLDOWN_DEFAULTS = {
    "holder_drop_pct": 30,
    "always_alert_owners": 0,
    "combined_drop_pct": 30,
    "stop_price_usd": 60,
    "trailing_stop_pct": 60,
    "min_liquidity_usd": 60,
    "monitor_failure": 60,
}
HEARTBEAT_DEFAULTS = {"enabled": True, "time": "10:00", "timezone": "America/New_York"}
TRADER_DEFAULTS = {"wallets": [], "poll_seconds": 60, "alert_buys": True, "alert_sells": True, "tokens": "any",
                   "min_trade_usd": 10, "major_mints": [], "major_min_market_cap_usd": 500_000_000}
TRADER_WALLET_KEYS = frozenset({"chain", "address", "label", "source_mint", "source_symbol", "pnl_usd", "roi_pct",
                                "added", "tokens", "min_trade_usd", "preset"})
TRADER_TOKEN_SCOPES = ("any", "source", "major")
TRADER_SCOPES_HELP = ('"any" (their trades in every token), "source" (only the token each wallet was discovered '
                      'on) or "major" (only major tokens: SOL, BTC, ETH, JUP, ...)')
DISCOVERY_DEFAULTS = {"scan_transactions": 2000, "lookback_hours": 168, "pools": 3, "candidates": 40,
                      "max_wallet_transactions": 300, "min_buy_usd": 100, "max_trades": 200, "show": 15, "watch": 5,
                      "min_hold_minutes": 60, "max_scalp_share": 0.5}
# key -> (valid, expectation, whole number)
DISCOVERY_LIMITS = {
    "scan_transactions": (lambda v: 50 <= v <= 50_000, "a whole number from 50 to 50000", True),
    "lookback_hours": (lambda v: 1 <= v <= 2160, "hours from 1 to 2160 (90 days)", False),
    "pools": (lambda v: 1 <= v <= 10, "a whole number from 1 to 10", True),
    "candidates": (lambda v: 1 <= v <= 500, "a whole number from 1 to 500", True),
    "max_wallet_transactions": (lambda v: 10 <= v <= 5000, "a whole number from 10 to 5000", True),
    "min_buy_usd": (lambda v: v >= 0, "USD >= 0", False),
    "max_trades": (lambda v: v >= 2, "a whole number >= 2", True),
    "show": (lambda v: 1 <= v <= 100, "a whole number from 1 to 100", True),
    "watch": (lambda v: 0 <= v <= 50, "a whole number from 0 to 50", True),
    "min_hold_minutes": (lambda v: 0 <= v <= 10_080, "minutes from 0 (no scalper filter) to 10080", False),
    "max_scalp_share": (lambda v: 0 <= v <= 1, "a share from 0 to 1, e.g. 0.5", False),
}
TOP_LEVEL_KEYS = frozenset({
    "mint", "rpc_url", "poll_seconds", "top_n", "exclude_owners", "always_alert_owners", "labels",
    "rules", "alert_cooldown_minutes", "heartbeat", "max_consecutive_failures", "max_tracked_owners",
    "auto_exclude_pools", "state_file", "log_file", "traders", "discovery", "trader_state_file", "signal_log",
})

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
# Settings of 1.1.0 that 1.2.0 replaced (amounts moved from SOL to USD) -> their replacement, or None
# for a value that was only shown (dropped quietly).
LEGACY_KEYS = {"min_trade_sol": "min_trade_usd", "min_buy_sol": "min_buy_usd", "pnl_sol": None}
_LITERAL_KEY = re.compile(r"(?i)[?&](?:api[-_]?key|apikey|access[-_]?token|token|key)=(?!\$\{)[^&]+")
_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


class ConfigError(Exception):
    def __init__(self, problems, source="config.json"):
        self.problems = list(problems)
        listing = "\n".join(f"  - {problem}" for problem in self.problems)
        super().__init__(f"{source} has {len(self.problems)} problem(s):\n{listing}")


@dataclass(frozen=True)
class Rules:
    stop_price_usd: float | None
    trailing_stop_pct: float | None
    min_liquidity_usd: float | None
    holder_drop_pct: float | None
    combined_drop_pct: float | None
    window_minutes: int


@dataclass(frozen=True)
class Heartbeat:
    hour: int
    minute: int
    timezone: str


@dataclass(frozen=True)
class TraderWallet:
    address: str
    label: str | None = None
    source_mint: str | None = None    # the token it was discovered on, and its result there at the time
    source_symbol: str | None = None
    pnl_usd: float | None = None
    roi_pct: float | None = None
    added: str | None = None
    tokens: str | None = None          # this wallet's own scope; None = traders.tokens
    min_trade_usd: float | None = None  # this wallet's own minimum; None = traders.min_trade_usd
    chain: str = "solana"
    preset: bool = False               # one of the built-in traders (presets.json)

    @property
    def key(self) -> str:
        """Unique across chains: an EVM address can be watched on several of them."""
        return f"{self.chain}:{self.address}"


@dataclass(frozen=True)
class Traders:
    wallets: tuple = ()
    poll_seconds: int = 60
    alert_buys: bool = True
    alert_sells: bool = True
    tokens: str = "any"         # "any", "source" (the token each wallet was discovered on) or "major"
    min_trade_usd: float = 10.0
    major_mints: frozenset = frozenset()  # traders.major_mints: majors on top of each chain's built-in list
    major_min_market_cap_usd: float | None = 500_000_000  # any token this big (and liquid) is major too

    def scope(self, wallet) -> str:
        return wallet.tokens or self.tokens

    def minimum(self, wallet) -> float:
        return self.min_trade_usd if wallet.min_trade_usd is None else wallet.min_trade_usd

    def listed_major(self, chain, mint: str) -> bool:
        return mint in chain.majors or chain.normalize(mint) in self.major_mints


@dataclass(frozen=True)
class Discovery:
    scan_transactions: int = 2000      # recent pool transactions to scan for candidates
    lookback_hours: float = 168        # ... but none older than this
    pools: int = 3                     # busiest pools to scan
    candidates: int = 40               # wallets whose full history on the token is read
    max_wallet_transactions: int = 300  # more than this on one token: history not fully readable (bot-like)
    min_buy_usd: float = 100           # ignore wallets that bought less (at today's coin price)
    max_trades: int = 200              # more trades than this: bot-like
    show: int = 15
    watch: int = 5                     # default number of top traders to watch
    min_hold_minutes: float = 60       # tokens sold sooner than this after buying count as scalping
    max_scalp_share: float = 0.5       # left out as a scalper when more than this share of its sells were


@dataclass(frozen=True)
class Config:
    mint: str | None
    rpc_url: str = field(repr=False)
    rpc_label: str
    uses_public_rpc: bool
    poll_seconds: int
    top_n: int
    exclude_owners: frozenset
    always_alert_owners: tuple
    labels: dict
    rules: Rules
    cooldowns: dict
    heartbeat: Heartbeat | None
    max_consecutive_failures: int
    max_tracked_owners: int
    auto_exclude_pools: bool
    state_file: Path
    log_file: Path
    traders: Traders = Traders()
    discovery: Discovery = Discovery()
    trader_state_file: Path | None = None
    signal_log: Path | None = None   # one JSON line per alerted trade (None: off)
    telegram_token: str | None = field(default=None, repr=False)
    telegram_chat_id: str | None = None
    secrets: tuple = field(default=(), repr=False)
    notes: tuple = ()   # settings from an older version that are accepted but no longer used


def load_dotenv(path, environ=None) -> list[str]:
    """Load KEY=VALUE lines from a .env file without overriding variables that are already set."""
    environ = os.environ if environ is None else environ
    path = Path(path)
    if not path.is_file():
        return []
    loaded = []
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:  # unquoted: " #..." (or a leading "#") starts a comment
            value = re.split(r"(?:^|\s)#", value, maxsplit=1)[0].strip()
        if value and not environ.get(key):
            environ[key] = value
            loaded.append(key)
    return loaded


def load_config(path, environ=None, *, require_mint=True) -> Config:
    """require_mint=False lets the setup and discovery commands run before a token is chosen (mint None)."""
    environ = os.environ if environ is None else environ
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raise ConfigError([f"file not found: {path}"], path.name) from None
    except json.JSONDecodeError as exc:
        raise ConfigError([f"not valid JSON: {exc.msg} at line {exc.lineno}, column {exc.colno}"], path.name) from None
    if not isinstance(data, dict):
        raise ConfigError(["the file must contain a JSON object: { ... }"], path.name)

    problems: list[str] = []
    _check_keys(problems, "", data, TOP_LEVEL_KEYS)

    mint = data.get("mint")
    if mint in (None, "", PLACEHOLDER_MINT):
        if require_mint:
            problems.append(f"mint: replace {PLACEHOLDER_MINT!r} with your token's mint address "
                            "(or run: python holder_watch.py setup)")
        mint = None
    elif not is_pubkey(mint):
        problems.append(f"mint: {mint!r} is not a valid Solana address (base58, 32 bytes)")

    rpc_url, rpc_label, secrets, uses_public = _resolve_rpc(problems, data.get("rpc_url"), environ)

    poll_seconds = _number(problems, "poll_seconds", data.get("poll_seconds", 120), lambda v: v >= 15,
                           "a whole number of seconds, at least 15", integer=True) or 120
    top_n = _number(problems, "top_n", data.get("top_n", 10), lambda v: 1 <= v <= 20,
                    "a whole number from 1 to 20 (getTokenLargestAccounts returns at most 20 accounts)",
                    integer=True) or 10
    exclude = _addresses(problems, "exclude_owners", data.get("exclude_owners"))
    always = _addresses(problems, "always_alert_owners", data.get("always_alert_owners"))
    for address in sorted(set(exclude) & set(always)):
        problems.append(f"{address} is in both exclude_owners and always_alert_owners; keep it in one")
    labels = _labels(problems, data.get("labels"))
    notes: list[str] = []
    traders = _traders(problems, _legacy(notes, "traders.", data.get("traders")), notes)
    for wallet in traders.wallets:  # explicit labels win
        if wallet.label and wallet.address not in labels:
            labels[wallet.address] = wallet.label
    discovery = _discovery(problems, _legacy(notes, "discovery.", data.get("discovery")))
    rules = _rules(problems, data.get("rules"))
    cooldowns = _cooldowns(problems, data.get("alert_cooldown_minutes"))
    heartbeat = _heartbeat(problems, data.get("heartbeat", {}))
    max_failures = _number(problems, "max_consecutive_failures", data.get("max_consecutive_failures", 5),
                           lambda v: v >= 1, "a whole number >= 1", integer=True) or 5
    max_tracked = _number(problems, "max_tracked_owners", data.get("max_tracked_owners", 50),
                          lambda v: v >= top_n, f"a whole number >= top_n ({top_n})", integer=True) or max(50, top_n)
    auto_pools = data.get("auto_exclude_pools", True)
    if not isinstance(auto_pools, bool):
        problems.append("auto_exclude_pools: expected true or false")
        auto_pools = True
    state_file = _path(problems, "state_file", data.get("state_file", "state.json"), path.parent)
    log_file = _path(problems, "log_file", data.get("log_file", "logs/holder_watch.log"), path.parent)
    trader_state_file = _path(problems, "trader_state_file", data.get("trader_state_file", "trader_state.json"),
                              path.parent)
    signal_raw = data.get("signal_log", "signals.jsonl")
    signal_log = None if signal_raw is None else _path(problems, "signal_log", signal_raw, path.parent)

    for name in ("ANKR_API_KEY", "ALCHEMY_API_KEY",
                 *(f"{chain_id.upper()}_RPC_URL" for chain_id in CHAINS if chain_id != "solana")):
        value = (environ.get(name) or "").strip()
        if value:  # EVM data keys: masked in logs like the others, also when a URL carries one
            parts = urlsplit(value)
            secrets.append(value)
            secrets += [part for part in parts.path.split("/") if len(part) >= 16]
            secrets += [v for _, v in parse_qsl(parts.query) if len(v) >= 16]
    token = (environ.get("TELEGRAM_BOT_TOKEN") or "").strip() or None
    chat_id = (environ.get("TELEGRAM_CHAT_ID") or "").strip() or None
    if bool(token) != bool(chat_id):
        problems.append("Telegram: set both TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env, or neither "
                        "(alerts then go to the console and log file only)")
    if token:
        secrets.append(token)

    if problems:
        raise ConfigError(problems, path.name)
    return Config(
        mint=mint, rpc_url=rpc_url, rpc_label=rpc_label, uses_public_rpc=uses_public,
        poll_seconds=poll_seconds, top_n=top_n, exclude_owners=frozenset(exclude),
        always_alert_owners=tuple(always), labels=labels, rules=rules, cooldowns=cooldowns,
        heartbeat=heartbeat, max_consecutive_failures=max_failures, max_tracked_owners=max_tracked,
        auto_exclude_pools=auto_pools, state_file=state_file, log_file=log_file, traders=traders,
        discovery=discovery, trader_state_file=trader_state_file, signal_log=signal_log,
        telegram_token=token, telegram_chat_id=chat_id, secrets=tuple(secrets), notes=tuple(notes),
    )


def edit_config(path, change, *, environ=None) -> Path:
    """Apply change(data) to config.json, keeping its keys, their order and "_" comments.

    Saves a timestamped backup first and replaces the file atomically; if the result doesn't validate,
    the original is put back and ConfigError is raised. Returns the backup's path.
    """
    path = Path(path)
    original = path.read_text(encoding="utf-8-sig")
    data = json.loads(original)
    change(data)
    backup = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    atomic_write(backup, original)
    atomic_write(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    try:
        load_config(path, environ, require_mint=False)
    except ConfigError:
        atomic_write(path, original)
        raise
    return backup


def update_dotenv(path, values: dict) -> None:
    """Set KEY=value lines in a .env file and keep every other line. Written with mode 600 on Linux."""
    path = Path(path)
    lines = path.read_text(encoding="utf-8-sig").splitlines() if path.is_file() else []
    out, written = [], set()
    for line in lines:
        body = line.strip()
        if body.startswith("export "):
            body = body[len("export "):].lstrip()
        key = body.partition("=")[0].strip()
        if not body.startswith("#") and "=" in body and key in values:
            if key not in written:  # later duplicates of the key are dropped
                out.append(f"{key}={_dotenv_value(values[key])}")
                written.add(key)
            continue
        out.append(line)
    missing = [key for key in values if key not in written]
    if missing:
        if out and out[-1].strip():
            out.append("")
        out += [f"{key}={_dotenv_value(values[key])}" for key in missing]
    atomic_write(path, "\n".join(out) + "\n", mode=0o600)


def _dotenv_value(value) -> str:
    value = str(value)
    return f'"{value}"' if not value or re.search(r"[\s#'\"]", value) else value


# Reading one EVM wallet's trades for a month (~10 a day; each keeps it "active" for 5 checks), in the
# provider's units. The per-check transaction counts go to public RPCs, which cost nothing.
EVM_WALLET_MONTH = {"ankr": 1_400_000, "alchemy": 180_000}


def monthly_credits(cfg, environ=None) -> dict:
    """Rough usage per 30 days: Helius credits for Solana, and Ankr credits or Alchemy compute units
    for the EVM chains read through them (see evm.source; public RPCs cost nothing).

    Token monitor: about 4 credits per cycle (README). Solana trader watch: 1 per wallet per check,
    plus 1 per new transaction. EVM wallets: EVM_WALLET_MONTH each.
    """
    from .evm import source  # here: evm is only needed for this estimate

    month = 30 * 86_400
    checks = month / cfg.traders.poll_seconds
    use = {"holder": round(month / cfg.poll_seconds * 4) if cfg.mint else 0,
           "solana_traders": round(checks * sum(w.chain == "solana" for w in cfg.traders.wallets)),
           "ankr": 0, "alchemy": 0}
    for wallet in cfg.traders.wallets:
        src = source(CHAINS[wallet.chain], environ) if wallet.chain != "solana" else None
        provider = src and ("alchemy" if "alchemy.com" in src.url else "ankr" if src.index == "ankr" else None)
        if provider:
            use[provider] += EVM_WALLET_MONTH[provider]
    return use


def _resolve_rpc(problems, raw, environ):
    """Returns (url, safe label, secret values to redact, is the public endpoint)."""
    secrets = []
    if raw is None:
        raw = PUBLIC_RPC_URL
    if not isinstance(raw, str) or not raw.strip():
        problems.append(f"rpc_url: expected a URL such as {PUBLIC_RPC_URL}")
        raw = PUBLIC_RPC_URL
    raw = raw.strip()
    if _LITERAL_KEY.search(raw):
        problems.append("rpc_url: appears to contain a literal API key. Keep keys out of config.json: put "
                        "HELIUS_API_KEY=... in .env (Helius is then used automatically), or reference your own "
                        "variable as ${YOUR_VAR} in the URL")
    names = _ENV_REF.findall(raw)
    missing = [name for name in names if not environ.get(name)]
    if missing:
        problems.append("rpc_url: needs " + ", ".join(missing) + " - add it to .env")
    secrets += [environ[name] for name in names if environ.get(name)]
    url = _ENV_REF.sub(lambda m: environ.get(m.group(1), ""), raw)

    source = ", ".join(names) or None
    if environ.get("SOLANA_RPC_URL"):
        url = environ["SOLANA_RPC_URL"].strip()
        source = "SOLANA_RPC_URL"
        parts = urlsplit(url)
        secrets += [url] + [s for s in (parts.path.strip("/"), parts.query) if len(s) >= 8]
    elif url.rstrip("/") == PUBLIC_RPC_URL and environ.get("HELIUS_API_KEY"):
        key = environ["HELIUS_API_KEY"].strip()
        url = HELIUS_RPC_URL.format(key=key)
        source = "HELIUS_API_KEY"
        secrets.append(key)

    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        problems.append(f"rpc_url: {source or 'the value'} must be an http(s) URL")
    label = (parts.hostname or "?") + (f" (key from {source})" if source else "")
    return url, label, secrets, url.rstrip("/") == PUBLIC_RPC_URL


def _number(problems, where, value, valid, expectation, *, integer=False):
    """Validate a number. None passes through (null means 'off' / 'use the default')."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        problems.append(f"{where}: expected {expectation}, got {json.dumps(value)}")
        return None
    if integer and value != int(value):
        problems.append(f"{where}: expected {expectation}, got {value}")
        return None
    value = int(value) if integer else float(value)
    if not valid(value):
        problems.append(f"{where}: expected {expectation}, got {value:g}")
        return None
    return value


def _rules(problems, raw) -> Rules:
    raw = {} if raw is None else raw
    if not isinstance(raw, dict):
        problems.append("rules: expected an object { ... }")
        raw = {}
    _check_keys(problems, "rules.", raw, RULE_DEFAULTS)
    merged = {**RULE_DEFAULTS, **raw}
    pct = "a percentage above 0 and at most 100, or null to turn the rule off"
    rules = Rules(
        stop_price_usd=_number(problems, "rules.stop_price_usd", merged["stop_price_usd"], lambda v: v > 0,
                               "a USD price above 0, or null to turn the rule off"),
        trailing_stop_pct=_number(problems, "rules.trailing_stop_pct", merged["trailing_stop_pct"],
                                  lambda v: 0 < v < 100, "a percentage between 0 and 100, or null to turn the rule off"),
        min_liquidity_usd=_number(problems, "rules.min_liquidity_usd", merged["min_liquidity_usd"], lambda v: v >= 0,
                                  "a USD amount >= 0, or null to turn the rule off"),
        holder_drop_pct=_number(problems, "rules.holder_drop_pct", merged["holder_drop_pct"],
                                lambda v: 0 < v <= 100, pct),
        combined_drop_pct=_number(problems, "rules.combined_drop_pct", merged["combined_drop_pct"],
                                  lambda v: 0 < v <= 100, pct),
        window_minutes=_number(problems, "rules.window_minutes", merged["window_minutes"], lambda v: v >= 1,
                               "a whole number of minutes >= 1", integer=True) or 60,
    )
    if merged["window_minutes"] is None and (rules.holder_drop_pct or rules.combined_drop_pct):
        problems.append("rules.window_minutes: required when holder_drop_pct or combined_drop_pct is set")
    return rules


def _cooldowns(problems, raw) -> dict:
    raw = {} if raw is None else raw
    if not isinstance(raw, dict):
        problems.append('alert_cooldown_minutes: expected an object like {"holder_drop_pct": 30}')
        raw = {}
    _check_keys(problems, "alert_cooldown_minutes.", raw, COOLDOWN_DEFAULTS)
    cooldowns = dict(COOLDOWN_DEFAULTS)
    for key, value in raw.items():
        if key not in COOLDOWN_DEFAULTS:
            continue
        if value is None:
            problems.append(f"alert_cooldown_minutes.{key}: expected minutes >= 0, got null")
            continue
        minutes = _number(problems, f"alert_cooldown_minutes.{key}", value, lambda v: v >= 0, "minutes >= 0")
        if minutes is not None:
            cooldowns[key] = minutes
    return cooldowns


def _heartbeat(problems, raw) -> Heartbeat | None:
    if raw is None or raw is False:
        return None
    if not isinstance(raw, dict):
        problems.append('heartbeat: expected an object like {"time": "10:00", "timezone": "America/New_York"}')
        return None
    _check_keys(problems, "heartbeat.", raw, HEARTBEAT_DEFAULTS)
    merged = {**HEARTBEAT_DEFAULTS, **raw}
    if merged["enabled"] is False:
        return None
    if merged["enabled"] is not True:
        problems.append("heartbeat.enabled: expected true or false")
    match = _HHMM.match(str(merged["time"]))
    if not match:
        problems.append(f"heartbeat.time: expected HH:MM in 24-hour time, got {merged['time']!r}")
        return None
    zone = str(merged["timezone"])
    try:
        ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError):
        problems.append(f"heartbeat.timezone: unknown time zone {zone!r} "
                        "(on Windows, install time zone data with: pip install tzdata)")
        return None
    return Heartbeat(int(match.group(1)), int(match.group(2)), zone)


def _legacy(notes, prefix, data):
    """1.1.0's SOL amounts were replaced by USD ones: accept them (without effect) and say so, so that
    updating never stops a monitor whose config.json has them. Returns `data` without them."""
    if not isinstance(data, dict) or not LEGACY_KEYS.keys() & data.keys():
        return data
    for key in [key for key in data if key in LEGACY_KEYS]:  # in the file's order
        if LEGACY_KEYS[key]:
            notes.append(f"{prefix}{key} is no longer used (amounts are in USD since 1.2.0); "
                         f"{prefix}{LEGACY_KEYS[key]} applies instead")
    return {key: value for key, value in data.items() if key not in LEGACY_KEYS}


def _traders(problems, raw, notes) -> Traders:
    raw = {} if raw is None else raw
    if not isinstance(raw, dict):
        problems.append('traders: expected an object like {"wallets": [...], "poll_seconds": 60}')
        raw = {}
    _check_keys(problems, "traders.", raw, TRADER_DEFAULTS)
    merged = {**TRADER_DEFAULTS, **raw}
    for key in ("alert_buys", "alert_sells"):
        if not isinstance(merged[key], bool):
            problems.append(f"traders.{key}: expected true or false")
            merged[key] = True
    if merged["tokens"] not in TRADER_TOKEN_SCOPES:
        problems.append(f"traders.tokens: expected {TRADER_SCOPES_HELP}")
        merged["tokens"] = "any"
    majors = []
    for i, item in enumerate(merged["major_mints"] or []):
        if is_pubkey(item) or (isinstance(item, str) and EVM_ADDRESS.match(item)):
            majors.append(item.lower() if item.startswith("0x") else item)
        else:
            problems.append(f"traders.major_mints[{i}]: {item!r} is not a token address on a supported chain")
    return Traders(
        wallets=tuple(_trader_wallets(problems, merged["wallets"], notes)),
        poll_seconds=_number(problems, "traders.poll_seconds", merged["poll_seconds"], lambda v: v >= 15,
                             "a whole number of seconds, at least 15", integer=True) or 60,
        alert_buys=merged["alert_buys"], alert_sells=merged["alert_sells"], tokens=merged["tokens"],
        min_trade_usd=_number(problems, "traders.min_trade_usd", merged["min_trade_usd"], lambda v: v >= 0,
                              "USD >= 0, or null for no minimum") or 0.0,
        major_mints=frozenset(majors),
        major_min_market_cap_usd=_number(problems, "traders.major_min_market_cap_usd",
                                         merged["major_min_market_cap_usd"], lambda v: v > 0,
                                         "a USD amount above 0, or null to use the major-token list only"),
    )


def _trader_wallets(problems, raw, notes) -> list[TraderWallet]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        problems.append("traders.wallets: expected a list of wallets")
        return []
    wallets, seen = [], set()
    for i, item in enumerate(raw):
        where = f"traders.wallets[{i}]"
        if isinstance(item, str):  # a Solana address; an 0x one needs its chain (see the hint below)
            item = {"address": item}
        if not isinstance(item, dict):
            problems.append(f'{where}: expected a wallet address or an object like {{"address": "...", "label": "..."}}')
            continue
        item = _legacy(notes, f"{where}.", item)
        _check_keys(problems, f"{where}.", item, TRADER_WALLET_KEYS)
        chain = CHAINS.get(item.get("chain", "solana"))
        if chain is None:
            problems.append(f"{where}.chain: unknown chain {item.get('chain')!r} (supported: {', '.join(CHAINS)})")
            continue
        address = item.get("address")
        if not chain.valid(address):
            hint = ""
            if not chain.evm and isinstance(address, str) and EVM_ADDRESS.match(address):
                hint = f' (an 0x address needs "chain": one of {", ".join(c for c in CHAINS if CHAINS[c].evm)})'
            problems.append(f"{where}.address: {address!r} is not a valid {chain.name} address{hint}")
            continue
        address = chain.normalize(address)
        if (chain.id, address) in seen:
            problems.append(f"{where}: {address} is listed more than once on {chain.name}")
            continue
        seen.add((chain.id, address))
        label = item.get("label")
        if label is not None and (not isinstance(label, str) or not clean_text(label)):
            problems.append(f"{where}.label: expected a non-empty name")
            label = None
        source = item.get("source_mint")
        if source is not None and not chain.valid(source):
            problems.append(f"{where}.source_mint: {source!r} is not a valid {chain.name} token address")
            source = None
        numbers = {key: _number(problems, f"{where}.{key}", item.get(key), lambda v: True, "a number or null")
                   for key in ("pnl_usd", "roi_pct")}
        tokens = item.get("tokens")
        if tokens is not None and tokens not in TRADER_TOKEN_SCOPES:
            problems.append(f"{where}.tokens: expected {TRADER_SCOPES_HELP}, or leave it out to use traders.tokens")
            tokens = None
        minimum = _number(problems, f"{where}.min_trade_usd", item.get("min_trade_usd"), lambda v: v >= 0,
                          "USD >= 0, or leave it out to use traders.min_trade_usd")
        wallets.append(TraderWallet(address=address, label=clean_text(label, 40) if label else None,
                                    source_mint=chain.normalize(source) if source else None,
                                    source_symbol=clean_text(item.get("source_symbol"), 20) or None,
                                    added=clean_text(item.get("added"), 20) or None, tokens=tokens,
                                    min_trade_usd=minimum, chain=chain.id, preset=item.get("preset") is True,
                                    **numbers))
    return wallets


def _discovery(problems, raw) -> Discovery:
    raw = {} if raw is None else raw
    if not isinstance(raw, dict):
        problems.append('discovery: expected an object like {"scan_transactions": 2000}')
        raw = {}
    _check_keys(problems, "discovery.", raw, DISCOVERY_DEFAULTS)
    values = {}
    for key, default in DISCOVERY_DEFAULTS.items():
        valid, expectation, integer = DISCOVERY_LIMITS[key]
        value = _number(problems, f"discovery.{key}", raw.get(key, default), valid, expectation, integer=integer)
        values[key] = default if value is None else value
    return Discovery(**values)


def _check_keys(problems, prefix, data, allowed):
    """Reject unknown keys (typos would otherwise be silently ignored). Keys starting with _ are comments."""
    for key in data:
        if key.startswith("_") or key in allowed:
            continue
        close = difflib.get_close_matches(key, list(allowed), n=1)
        hint = f"did you mean {close[0]!r}?" if close else "allowed: " + ", ".join(sorted(allowed))
        problems.append(f"{prefix}{key}: unknown setting ({hint})")


def _addresses(problems, where, raw) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        problems.append(f"{where}: expected a list of wallet addresses")
        return []
    out = []
    for i, item in enumerate(raw):
        if is_pubkey(item):
            out.append(item)
        else:
            problems.append(f"{where}[{i}]: {item!r} is not a valid Solana address")
    return list(dict.fromkeys(out))


def _labels(problems, raw) -> dict:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        problems.append('labels: expected an object like {"<wallet address>": "Team wallet"}')
        return {}
    labels = {}
    for address, name in raw.items():
        if address.startswith("_"):
            continue
        if not is_pubkey(address) and not EVM_ADDRESS.match(address):
            problems.append(f"labels: {address!r} is not a valid Solana or EVM address")
        elif not isinstance(name, str) or not clean_text(name):
            problems.append(f"labels.{address}: expected a non-empty name")
        else:
            labels[address.lower() if address.startswith("0x") else address] = clean_text(name, 40)
    return labels


def _path(problems, where, raw, base: Path) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        problems.append(f"{where}: expected a file path")
        return base / "invalid"
    path = Path(raw).expanduser()
    return path if path.is_absolute() else base / path
