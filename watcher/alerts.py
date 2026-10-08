"""Alert messages (plain text for logs, HTML for Telegram), Telegram delivery, secret-safe logging.

Alerts report which of the user's rules fired and the data behind it. They never recommend
buying or selling.
"""

from __future__ import annotations

import html
import logging
import sys
import time
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .chains import CHAINS, SOLANA
from .known import MINT_SYMBOLS
from .rpc import HttpError
from .txparse import PAID_USD
from .util import drop_pct, fmt_age, fmt_number, fmt_tokens, fmt_usd, short, solscan_account, solscan_tx, utc

log = logging.getLogger("holder_watch.alerts")


@dataclass(frozen=True)
class Link:
    text: str
    url: str


@dataclass(frozen=True)
class Bold:
    text: str


def render_plain(lines: list) -> str:
    out = []
    for parts in lines:
        text = []
        for part in parts:
            if isinstance(part, Link):
                text.append(part.text if part.text == part.url else f"{part.text} ({part.url})")
            elif isinstance(part, Bold):
                text.append(part.text)
            else:
                text.append(str(part))
        out.append("".join(text))
    return "\n".join(out)


def render_html(lines: list) -> str:
    """Telegram's HTML subset. Every line is self-contained, so messages can be split on newlines."""
    out = []
    for parts in lines:
        text = []
        for part in parts:
            if isinstance(part, Link):
                text.append(f'<a href="{html.escape(part.url)}">{html.escape(part.text)}</a>')
            elif isinstance(part, Bold):
                text.append(f"<b>{html.escape(part.text)}</b>")
            else:
                text.append(html.escape(str(part)))
        out.append("".join(text))
    return "\n".join(out)


# --- message builders ---------------------------------------------------------------------------

@dataclass
class AlertContext:
    symbol: str
    decimals: int
    supply: int | None
    labels: dict
    market: object | None   # Market, or None when the DEX Screener request failed this cycle
    now: float


def wallet(address: str, labels: dict, chain=SOLANA) -> Link:
    label = labels.get(address)
    return Link(f"{label} ({short(address)})" if label else short(address), chain.wallet_url(address))


def describe_rule(rule: str, cfg) -> str:
    r = cfg.rules
    if rule == "holder_drop_pct":
        return f"holder_drop_pct = {r.holder_drop_pct:g}% within {r.window_minutes} min"
    if rule == "always_alert_owners":
        return "always_alert_owners (any outflow)"
    if rule == "combined_drop_pct":
        return f"combined_drop_pct = {r.combined_drop_pct:g}% within {r.window_minutes} min"
    if rule == "stop_price_usd":
        return f"stop_price_usd = {fmt_usd(r.stop_price_usd)}"
    if rule == "trailing_stop_pct":
        return f"trailing_stop_pct = {r.trailing_stop_pct:g}%"
    if rule == "min_liquidity_usd":
        return f"min_liquidity_usd = {fmt_usd(r.min_liquidity_usd)}"
    return rule


def enabled_rules(cfg) -> list[str]:
    on = [describe_rule(rule, cfg) for rule in
          ("holder_drop_pct", "combined_drop_pct", "stop_price_usd", "trailing_stop_pct", "min_liquidity_usd")
          if getattr(cfg.rules, rule) is not None]
    if cfg.always_alert_owners:
        on.append(f"always_alert_owners ({len(cfg.always_alert_owners)} wallet(s), any outflow)")
    return on


SCOPE_TEXT = {"any": "in any token", "source": "in the token each was discovered on", "major": "in major tokens"}


def describe_traders(cfg) -> str:
    t = cfg.traders
    sides = " and ".join(name for name, on in (("buys", t.alert_buys), ("sells", t.alert_sells)) if on) or "nothing"
    size = f", at least ${t.min_trade_usd:,.0f}" if t.min_trade_usd else ""
    counts = {}
    for w in t.wallets:
        counts[w.chain] = counts.get(w.chain, 0) + 1
    where = f" ({', '.join(f'{CHAINS[c].name} {n}' for c, n in counts.items())})" if counts else ""
    text = (f"Trader watch: {len(t.wallets)} wallet(s){where} · alerts on {sides} {SCOPE_TEXT[t.tokens]}{size} · "
            f"checked every {t.poll_seconds} s")
    own = []
    for w in t.wallets:  # wallets with their own settings
        if w.tokens not in (None, t.tokens) or w.min_trade_usd is not None:
            name = f"{w.label} ({short(w.address)})" if w.label else short(w.address)
            minimum = f", at least ${t.minimum(w):,.0f}" if t.minimum(w) else ""
            own.append(f"{name}: {SCOPE_TEXT[t.scope(w)].removeprefix('in ')}{minimum}")
    return text + (" · " + "; ".join(own) if own else "")


def market_line(market) -> list:
    if market is None:
        return ["Price/liquidity: unavailable this cycle (DEX Screener request failed)"]
    if not market.found:
        return ["Price/liquidity: no trading pair found on DEX Screener"]
    venue = Link(market.venue, market.url) if market.url else market.venue
    return [f"Price {fmt_usd(market.price_usd)} · liquidity {fmt_usd(market.liquidity_usd)} · ", venue]


def build_alert(hits: list, ctx: AlertContext, cfg) -> list:
    count = sum(1 + len(hit.merged) for hit in hits)
    lines = [[Bold(f"{ctx.symbol} monitor: {count} of your rules fired"), f" · {utc(ctx.now)}"],
             market_line(ctx.market)]
    for number, hit in enumerate(hits, 1):
        lines.append([""])
        lines.append([Bold(f"{number}. Your rule {describe_rule(hit.rule, cfg)}")])
        for extra in hit.merged:
            lines.append([Bold(f"   and your rule {describe_rule(extra.rule, cfg)}")])
        lines.extend(_hit_lines(hit, ctx, cfg))
    return lines


def _hit_lines(hit, ctx: AlertContext, cfg) -> list:
    d, sym = hit.data, ctx.symbol

    def tokens(raw):
        return fmt_tokens(raw, ctx.decimals)

    if hit.rule in ("holder_drop_pct", "always_alert_owners"):
        line = ["   ", wallet(hit.owner, ctx.labels),
                f": {tokens(d['before'])} → {tokens(d['after'])} {sym} (−{d['pct']:.1f}%), "
                f"first seen falling at {utc(d['drop_ts'], '%H:%M UTC')}"]
        if d["after"] == 0:
            line.append(" — now holds 0 (sold out)")
        lines = [line]
        if ctx.supply:
            lines.append([f"   Share of supply: {d['before'] / ctx.supply * 100:.2f}% → {d['after'] / ctx.supply * 100:.2f}%"])
        for extra in hit.merged:
            lines.append([f"   Within your {cfg.rules.window_minutes}-min window: −{extra.data['pct']:.1f}%"])
        return lines + _outflow_lines(hit, ctx)
    if hit.rule == "combined_drop_pct":
        lines = [[f"   {d['wallets']} watched wallets together: {tokens(d['before'])} → {tokens(d['after'])} {sym} "
                  f"(−{d['pct']:.1f}%) since {utc(d['from_ts'], '%H:%M UTC')}"]]
        for owner, before, after in d["contributors"]:
            lines.append(["   • ", wallet(owner, ctx.labels),
                          f": {tokens(before)} → {tokens(after)} (−{drop_pct(before, after):.1f}%)"])
        return lines
    if hit.rule == "stop_price_usd":
        return [[f"   Price {fmt_usd(d['price'])} is at or below your stop price {fmt_usd(d['stop'])}"]]
    if hit.rule == "trailing_stop_pct":
        return [[f"   Price {fmt_usd(d['price'])} is at or below {fmt_usd(d['trigger'])}: "
                 f"peak {fmt_usd(d['peak'])} (seen {utc(d['peak_ts'])}) minus {cfg.rules.trailing_stop_pct:g}%"],
                [f"   Now {d['from_peak']:.1f}% below the peak"]]
    if hit.rule == "min_liquidity_usd":
        if d.get("no_pair"):
            return [["   No trading pair found on two checks in a row: DEX Screener lists no Solana pool "
                     "with this mint as the base token"]]
        if d["liquidity"] is None:
            return [[f"   Liquidity not reported by DEX Screener for {d['venue']} "
                     f"(treated as below your minimum {fmt_usd(d['min'])})"]]
        return [[f"   Liquidity {fmt_usd(d['liquidity'])} is below your minimum {fmt_usd(d['min'])} ({d['venue']})"]]
    return [[f"   {d}"]]


def _outflow_lines(hit, ctx: AlertContext) -> list:
    lines = [["   • "] + outflow_parts(flow, ctx) for flow in hit.outflows[:4]]
    if len(hit.outflows) > 4:
        lines.append([f"   • …and {len(hit.outflows) - 4} more outflow transaction(s)"])
    if hit.classify_error:
        lines.append([f"   • {hit.classify_error}"])
    elif not hit.outflows:
        lines.append(["   • No matching outflow transaction found in recent history"])
    lines.append(["   ", Link("Wallet on Solscan", solscan_account(hit.owner))])
    return lines


def outflow_parts(flow, ctx: AlertContext) -> list:
    amount = f"{fmt_tokens(flow.amount, ctx.decimals)} {ctx.symbol}"
    if flow.kind == "sell":
        parts = [f"SELL via {' → '.join(flow.venues) or 'unknown route'}: −{amount}"]
        if flow.proceeds:
            parts.append(", received " + ", ".join(f"{fmt_number(v)} {k}" for k, v in flow.proceeds.items()))
        if flow.counterparty:
            parts += [" with " if flow.proceeds else "; proceeds went to ", wallet(flow.counterparty, ctx.labels)]
        elif flow.note:
            parts.append(f" ({flow.note})")
    elif flow.kind == "transfer":
        parts = [f"TRANSFER −{amount} to ", wallet(flow.counterparty, ctx.labels)]
        if flow.other_recipients:
            parts.append(f" (+{flow.other_recipients} more)")
        if flow.note:
            parts.append(f" ({flow.note})")
    elif flow.kind == "burn":
        parts = [f"BURN −{amount}"]
    else:
        parts = [f"OUTFLOW −{amount} (type unclear)"]
    if flow.block_time:
        parts.append(f" at {utc(flow.block_time, '%H:%M UTC')}")
    return parts + [" · ", Link("tx", solscan_tx(flow.signature))]


# --- trader watch -------------------------------------------------------------------------------

@dataclass
class TraderContext:
    tokens: dict             # (chain id, mint) -> market.TokenInfo, for tokens DEX Screener lists
    prices: dict             # chain id -> USD price of its coin (SOL, ETH, BNB)
    labels: dict
    now: float
    scopes: dict = field(default_factory=dict)  # wallet key -> "any" / "source" / "major"


SIDE_TITLES = {"buy": "BUY", "sell": "SELL", "swap": "SWAP"}


def build_trader_alert(items: list, ctx: TraderContext) -> list:
    """items: (TraderWallet, Trade) pairs, oldest trade first."""
    count = len(items)
    lines = [[Bold(f"Trader watch: {count} new trade{'' if count == 1 else 's'}"), f" · {utc(ctx.now)}"]]
    for number, (trader, trade) in enumerate(items, 1):
        chain = CHAINS[trade.chain]
        where = "" if chain is SOLANA else f" on {chain.name}"
        lines.append([""])
        head = [Bold(f"{number}. {SIDE_TITLES.get(trade.side, trade.side.upper())}{where}"), " by ",
                wallet(trader.address, ctx.labels, chain)]
        if ctx.scopes.get(trader.key) == "major":
            head.append(" · major-token monitor")
        found = discovered_note(trader)
        if found:
            head.append(f" · {found}")
        lines.append(head)
        lines.append(["   "] + trade_parts(trade, ctx, chain))
        lines.extend(token_lines(trade.mint, ctx, chain))
    return lines


def discovered_note(trader) -> str:
    result = ""
    if trader.pnl_usd is not None:
        roi = f", {trader.roi_pct:+.0f}%" if trader.roi_pct is not None else ""
        result = f"{'+' if trader.pnl_usd >= 0 else '-'}${abs(trader.pnl_usd):,.0f}{roi}"
    if trader.preset:
        return "built-in trader" + (f": {result} over 30 days" if result else "")
    if not trader.source_mint:
        return ""
    where = trader.source_symbol or short(trader.source_mint)
    return f"discovered on {where}" + (f": {result}" if result else "")


def token_link(mint: str, ctx: TraderContext, chain=SOLANA) -> Link:
    info = ctx.tokens.get((chain.id, mint))
    symbol = ((info.symbol if info and info.symbol else None) or chain.majors.get(mint)
              or (None if chain.evm else MINT_SYMBOLS.get(mint)))
    return Link(symbol or short(mint), chain.token_url(mint))


def payment(trade, ctx: TraderContext, chain=SOLANA) -> str | None:
    paid = []
    price = ctx.prices.get(chain.id)
    if trade.mint != chain.wrapped and trade.native >= 0.001:  # trades of the coin itself are paid in dollars
        usd = f" ({fmt_usd(trade.native * price)})" if price else ""
        paid.append(f"{fmt_number(trade.native)} {chain.native}{usd}")
    if trade.usd >= PAID_USD:
        paid.append(f"{fmt_usd(trade.usd)} in stablecoins")
    return " + ".join(paid) or None


def trade_parts(trade, ctx: TraderContext, chain=SOLANA) -> list:
    amount = f"{fmt_tokens(trade.amount, trade.decimals)} "
    when = f" at {utc(trade.block_time, '%H:%M UTC')}" if trade.block_time else ""
    if trade.side == "swap":
        parts = [f"Swapped {fmt_tokens(trade.other_amount, trade.other_decimals)} ",
                 token_link(trade.other_mint, ctx, chain), f" for {amount}", token_link(trade.mint, ctx, chain), when]
    else:
        parts = [f"{'Bought' if trade.side == 'buy' else 'Sold'} {amount}", token_link(trade.mint, ctx, chain)]
        paid = payment(trade, ctx, chain)
        if paid:
            parts.append(f" for {paid}")
        parts.append(when)
        if trade.mint == chain.wrapped:  # the coin itself: its wallet balance, not a "position"
            parts.append(f" · {chain.native} balance now {fmt_tokens(trade.after, chain.decimals)}" if trade.after else "")
        elif trade.before is None or trade.after is None:  # EVM chains: balances aren't read
            pass
        elif trade.side == "buy":
            parts.append(" · new position" if trade.before == 0
                         else f" · adds to a position of {fmt_tokens(trade.before, trade.decimals)}")
        elif trade.after == 0:
            parts.append(" · sold the whole position")
        else:
            parts.append(f" · {trade.amount / trade.before * 100:.0f}% of the position, "
                         f"{fmt_tokens(trade.after, trade.decimals)} left")
        if trade.counterparty:
            parts += ["; proceeds went to ", wallet(trade.counterparty, ctx.labels, chain)]
    if trade.venues:
        parts.append(f" · via {' → '.join(trade.venues)}")
    return parts + [" · ", Link("tx", chain.tx_url(trade.signature))]


def token_lines(mint: str, ctx: TraderContext, chain=SOLANA) -> list:
    info = ctx.tokens.get((chain.id, mint))
    if info is None:
        return [["   ", Link(f"Token {short(mint)}", chain.token_url(mint)), " is not listed on DEX Screener (yet)"]]
    name = f"{info.name} ({info.symbol})" if info.name and info.symbol else (info.symbol or info.name or short(mint))
    facts = [f"price {fmt_usd(info.price_usd)}", f"liquidity {fmt_usd(info.liquidity_usd)}"]
    if info.market_cap:
        facts.append(f"market cap {fmt_usd(info.market_cap)}")
    if info.created:
        facts.append(f"pool opened {fmt_age(ctx.now - info.created)} ago")
    line = ["   ", Link(name, chain.token_url(mint)), " · " + " · ".join(facts)]
    if info.url:
        line += [" · ", Link("chart", info.url)]
    lines = [line]
    if info.hidden_chars:
        lines.append(["   Warning: this token's name contains hidden text-direction characters, a trick used by "
                      "look-alike tokens. Check the mint."])
    return lines


def build_trader_failure(failures: dict, poll_seconds: int) -> list:
    lines = [[Bold(f"Trader watch: {failures['count']} checks in a row failed"),
              f" · failing since {utc(failures['since'])}"],
             ["Your watched wallets' trades can't be checked until this recovers. Latest errors:"]]
    lines += [[f"  - {error}"] for error in failures["last_errors"]]
    lines.append([f"Still retrying every {poll_seconds} s; trades made meanwhile are reported once it recovers."])
    return lines


def build_trader_recovery(count: int, since: float | None, now: float) -> list:
    when = f" (failing since {utc(since)})" if since else ""
    return [[Bold("Trader watch: recovered"), f" after {count} failed check(s){when} · {utc(now)}"]]


def build_presets_renewal(lines: list, changed: bool, now: float) -> list:
    """The monthly renewal of the built-in trader lists: what changed per chain."""
    out = [[Bold("Built-in traders renewed"), f" · {utc(now)}"]]
    out += [[f"• {line}"] for line in lines]
    out.append(["Picked on their last 30 days across every token; past results are not a prediction."
                + (" The new lists are watched once the monitor restarts (on the server, right away)." if changed else "")])
    return out


def build_heartbeat(symbol: str, state: dict, cfg, market, now: float, extra=None) -> list:
    last, stats, failures = state["last"], state["stats"], state["failures"]
    lines = [[Bold(f"{symbol} monitor: daily heartbeat"), f" · {utc(now)} · running"]]
    if market is not None and market.found:
        change = f" (24h {market.price_change_h24:+.1f}%)" if market.price_change_h24 is not None else ""
        lines.append([f"Price {fmt_usd(market.price_usd)}{change} · liquidity {fmt_usd(market.liquidity_usd)} · {market.venue}"])
    elif last.get("price") is not None:
        lines.append([f"Price {fmt_usd(last['price'])} · liquidity {fmt_usd(last.get('liquidity'))} "
                      f"(last known, {utc(last['market_ts'])})"])
    else:
        lines.append(["Price/liquidity: not available"])
    if last.get("top_share") is not None:
        lines.append([f"Top {cfg.top_n} holders (pools and excluded wallets not counted) hold "
                      f"{last['top_share']:.1f}% of supply · watching {last.get('watching', 0)} wallets"])
    lines.append([f"Since the last heartbeat: {stats['cycles']} cycles, {stats['failed']} failed, "
                  f"{stats['alerts']} alert message(s)"])
    if failures["count"]:
        lines.append([f"Currently failing: {failures['count']} cycle(s) in a row. Last error: "
                      + "; ".join(failures["last_errors"])[:300]])
    return lines + list(extra or [])


def build_failure(symbol: str, failures: dict, poll_seconds: int) -> list:
    lines = [[Bold(f"{symbol} monitor: {failures['count']} cycles in a row failed"),
              f" · failing since {utc(failures['since'])}"],
             ["Your rules can't be fully checked until data comes back. Latest errors:"]]
    lines += [[f"  - {error}"] for error in failures["last_errors"]]
    lines.append([f"Still retrying every {poll_seconds} s; you'll get a message when it recovers."])
    return lines


def build_recovery(symbol: str, count: int, since: float | None, now: float) -> list:
    when = f" (failing since {utc(since)})" if since else ""
    return [[Bold(f"{symbol} monitor: recovered"), f" after {count} failed cycle(s){when} · {utc(now)}"]]


def build_startup(symbol: str, cfg, now: float) -> list:
    rules = enabled_rules(cfg)
    lines = [[Bold(f"{symbol} monitor started"),
              f" · {utc(now)} · mint {short(cfg.mint)} · every {cfg.poll_seconds} s · RPC {cfg.rpc_label}"],
             ["Rules on: " + ("; ".join(rules) if rules else "none (set them in config.json)")]]
    if cfg.traders.wallets:
        lines.append([describe_traders(cfg)])
    if cfg.heartbeat:
        lines.append([f"Daily heartbeat at {cfg.heartbeat.hour:02d}:{cfg.heartbeat.minute:02d} {cfg.heartbeat.timezone}"])
    return lines


def build_test(symbol: str, cfg, now: float) -> list:
    rules = enabled_rules(cfg)
    lines = [[Bold(f"{symbol} monitor: test alert"), f" · {utc(now)}"],
             ["This is a test message; no rule fired. If you can read it, alert delivery works."],
             ["Rules on: " + ("; ".join(rules) if rules else "none (set them in config.json)")]]
    if cfg.traders.wallets:
        lines.append([describe_traders(cfg)])
    return lines


# --- delivery -----------------------------------------------------------------------------------

class TelegramAPI:
    """Bot API calls. The token is part of the URL, which never appears in errors or logs."""

    URL = "https://api.telegram.org/bot{token}/{method}"

    def __init__(self, token: str, http):
        self.token = token
        self.http = http

    def call(self, method: str, **params):
        what = f"Telegram {method}"
        params = {key: value for key, value in params.items() if value is not None}
        resp = self.http.request("POST", self.URL.format(token=self.token, method=method), what=what, json=params)
        try:
            body = resp.json()
        except ValueError:
            raise HttpError(what, "response was not JSON") from None
        if not isinstance(body, dict) or not body.get("ok"):
            raise HttpError(what, str((body or {}).get("description", "not ok"))[:200])
        return body.get("result")


class TelegramSender:
    LIMIT = 4000  # Telegram's hard limit is 4096 characters per message

    def __init__(self, token: str, chat_id: str, http):
        self.api = TelegramAPI(token, http)
        self.chat_id = chat_id

    def send(self, html_text: str) -> None:
        for chunk in split_message(html_text, self.LIMIT):
            self.api.call("sendMessage", chat_id=self.chat_id, text=chunk, parse_mode="HTML",
                          disable_web_page_preview=True)


def split_message(text: str, limit: int) -> list[str]:
    chunks, current = [], ""
    for line in text.split("\n"):
        if len(line) > limit:
            line = line[: limit - 1] + "…"
        if current and len(current) + 1 + len(line) > limit:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks


class Notifier:
    """Every message goes to the console and the rotating log file, and to Telegram if configured."""

    def __init__(self, telegram: TelegramSender | None = None):
        self.telegram = telegram

    def send(self, lines: list, *, level=logging.WARNING) -> bool:
        log.log(level, "\n%s", render_plain(lines))
        if self.telegram is None:
            return True
        try:
            self.telegram.send(render_html(lines))
            return True
        except Exception as exc:
            log.error("Telegram delivery failed: %s", exc)
            return False


class RedactingFormatter(logging.Formatter):
    """Formats log records in UTC and masks secrets in the final text (including tracebacks)."""

    converter = time.gmtime

    def __init__(self, redactor):
        super().__init__("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%SZ")
        self.redactor = redactor

    def format(self, record):
        return self.redactor(super().format(record))


def setup_logging(log_file: Path, redactor, *, verbose=False, console_level=None) -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    formatter = RedactingFormatter(redactor)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    if console_level is not None:
        console.setLevel(console_level)
    root.addHandler(console)
    try:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(log_file, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError as exc:
        root.warning("cannot write log file %s (%s); logging to the console only", log_file, exc)
    for noisy in ("urllib3", "requests"):  # their debug logs include full URLs
        logging.getLogger(noisy).setLevel(logging.WARNING)
