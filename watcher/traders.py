"""Trader watch: alerts when a watched wallet buys or sells a token, on Solana and EVM chains.

Every `traders.poll_seconds`:
- Solana wallets: the transactions since the last one seen (getSignaturesForAddress with `until`)
  are read and turned into trades from their balance changes.
- EVM wallets (Ethereum, Base, BNB Chain, Arbitrum): one batched call per chain asks how many
  transactions each wallet has sent. For a wallet whose count went up, the blocks where its tokens
  moved come from Alchemy's transfer index and its trades from those blocks' receipts and its coin
  balance before and after (evm.py). The wallet stays active for a few minutes after its count rises,
  so trades the index lists late are still found; blocks already read are never alerted twice.
The first check of a wallet only notes where to start, so past trades are never alerted. A wallet's
place only moves forward once its trades are delivered. All trades of one check go into one message,
and each delivered trade is also written to the signal log (one JSON line), ready for other tools.
Read-only, like the rest of the monitor.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field

from . import alerts
from .chains import CHAINS
from .classify import classify_transaction
from .holders import PoolDetector
from .known import MAJOR_MIN_LIQUIDITY_USD
from .market import fetch_native_usd, fetch_tokens
from .monitor import next_heartbeat
from .rpc import HttpError, RpcError
from .txparse import Trade, balance_owners, wallet_trades
from .util import Redactor, short, utc

log = logging.getLogger("holder_watch.traders")

PAGE = 25              # Solana transactions read per wallet per check; a wallet busier than that is bot-like
DAY = 86_400
ALERTED_SIDES = ("buy", "sell", "swap")
ACTIVE_SECONDS = 300   # keep reading an EVM wallet's transfers this long after it sent a transaction
DONE_BLOCKS = 200      # EVM blocks remembered per wallet, so reading a block again never alerts twice


@dataclass
class TraderReport:
    ok: bool = True
    errors: list = field(default_factory=list)
    trades: list = field(default_factory=list)  # every trade found this check
    sent: list = field(default_factory=list)    # (wallet, trade) pairs delivered


def _describe(exc: Exception) -> str:
    return str(exc) if isinstance(exc, (HttpError, RpcError)) else f"{type(exc).__name__}: {exc}"


class TraderWatch:
    def __init__(self, cfg, rpc, http, notifier, store, *, evm=None, signal_log=None, clock=time.time):
        """rpc: the Solana RPC; evm: {chain id: evm.EvmRPC} for EVM chains with watched wallets;
        signal_log: a file that gets one JSON line per delivered trade (None: off)."""
        self.cfg = cfg
        self.rpc = rpc
        self.http = http
        self.notifier = notifier
        self.store = store
        self.evm = evm or {}
        self.signal_log = signal_log
        self.clock = clock
        self.redact = Redactor(cfg.secrets)
        self.state = store.load()
        self.detector = PoolDetector(rpc, self.state["owner_kinds"]) if rpc is not None else None
        self.wallets = {wallet.key: wallet for wallet in cfg.traders.wallets}
        self.owns_heartbeat = False  # set when there is no token monitor (traders only)
        for key in [k for k in self.state["wallets"] if k not in self.wallets]:
            del self.state["wallets"][key]  # removed from config.json

    def _who(self, wallet) -> str:
        label = self.cfg.labels.get(wallet.address)
        name = f"{label} ({short(wallet.address)})" if label else short(wallet.address)
        return name if wallet.chain == "solana" else f"{name} on {CHAINS[wallet.chain].name}"

    def chains(self) -> dict:
        """chain id -> watched wallets on it, in config order."""
        grouped = {}
        for wallet in self.wallets.values():
            grouped.setdefault(wallet.chain, []).append(wallet)
        return grouped

    def run_cycle(self) -> TraderReport:
        now = self.clock()
        report = TraderReport()
        checked = []  # (wallet, commit: moves its place forward, or None, trades)
        for chain_id, wallets in self.chains().items():
            chain = CHAINS[chain_id]
            if chain.evm:
                rpc = self.evm.get(chain_id)
                if rpc is None:
                    report.errors.append(f"{chain.name}: no RPC for {len(wallets)} wallet(s); add ANKR_API_KEY "
                                         "to .env (free at ankr.com)")
                    continue
                try:
                    checked += self._check_evm(chain, rpc, wallets, now)
                except Exception as exc:
                    report.errors.append(f"{chain.name} wallets: {_describe(exc)}")
                continue
            for wallet in wallets:
                try:
                    trades, commit = self._check_solana(wallet, now)
                except Exception as exc:
                    report.errors.append(f"trader {self._who(wallet)}: {_describe(exc)}")
                    continue
                checked.append((wallet, commit, trades))
        report.trades = [trade for _, _, trades in checked for trade in trades]
        hold = set()  # wallets whose place must not move: their trades weren't delivered
        try:
            due, context = self._select([(wallet, trade) for wallet, _, trades in checked for trade in trades])
            if due:
                if self.notifier.send(alerts.build_trader_alert(due, context), level=logging.WARNING):
                    report.sent = due
                    self.state["alerted"] += [now] * len(due)
                    self._record_signals(due, context)
                else:
                    report.errors.append("trade alert delivery failed (Telegram error logged above); retrying next check")
                    hold = {wallet.key for wallet, _ in due}
        except Exception as exc:  # a bug must not lose trades: they are read again next check
            log.exception("internal error during the trader check")
            report.errors.append(f"internal error: {_describe(exc)}")
            hold = {wallet.key for wallet, _, trades in checked if trades}
        for wallet, commit, _ in checked:
            if commit and wallet.key not in hold:
                commit()
        self.state["alerted"] = [ts for ts in self.state["alerted"] if now - ts < DAY]
        self.state["last_check"] = now
        report.errors = [self.redact(error) for error in report.errors]
        report.ok = not report.errors
        self._track_health(report, now)
        if self.owns_heartbeat:
            self.maybe_heartbeat(now)
        self._log(report)
        self._save()
        return report

    # --- Solana ---------------------------------------------------------------------------------

    def _check_solana(self, wallet, now: float):
        """Trades in the wallet's transactions since the last check (oldest first), and how to move its
        place past them."""
        rec = self.state["wallets"].get(wallet.key)
        if rec is None:  # first check: start from its newest transaction, don't alert the past
            newest = self.rpc.signatures(wallet.address, limit=1)
            self.state["wallets"][wallet.key] = {"last_signature": newest[0]["signature"] if newest else None,
                                                 "since": now}
            log.info("now watching trader %s", self._who(wallet))
            return [], None
        page = self.rpc.signatures(wallet.address, limit=PAGE, until=rec.get("last_signature"))
        if not rec.get("last_signature"):  # the wallet had no transactions when watching started
            page = [item for item in page if (item.get("blockTime") or now) >= rec.get("since", now)]
        if len(page) >= PAGE:
            log.warning("trader %s made %d+ transactions since the last check; only the newest %d are read",
                        self._who(wallet), PAGE, PAGE)
        trades, cursor = [], None
        for item in reversed(page):  # oldest first
            if item.get("err") is None:
                tx = self.rpc.transaction(item["signature"])
                if not tx:  # not served yet: read it next check
                    break
                trades += self._solana_trades(tx, wallet.address, item["signature"])
            cursor = item["signature"]
        if cursor is None:
            return trades, None
        return trades, lambda: rec.update(last_signature=cursor)

    def _solana_trades(self, tx: dict, owner: str, signature: str) -> list[Trade]:
        trades = wallet_trades(tx, owner)
        for trade in trades:
            trade.signature = trade.signature or signature
            if trade.side == "out" and trade.venues:  # through a DEX: a sell whose proceeds went elsewhere?
                self.detector.lookup(balance_owners(tx))
                flow = classify_transaction(tx, owner, trade.mint, self.detector.known)
                if flow and flow.kind == "sell":
                    trade.side, trade.counterparty = "sell", flow.counterparty
        return trades

    # --- EVM chains -----------------------------------------------------------------------------

    def _check_evm(self, chain, rpc, wallets: list, now: float) -> list:
        tip = rpc.block_number()
        counts = rpc.nonces([wallet.address for wallet in wallets])
        lag = max(2, int(30 / chain.block_seconds))  # re-read ~30 s: the transfer index can trail the chain
        results = []
        for wallet in wallets:
            rec = self.state["wallets"].get(wallet.key)
            count = counts[wallet.address]
            if rec is None:  # first check: start from now, don't alert the past
                self.state["wallets"][wallet.key] = {"nonce": count, "block": tip, "start": tip, "since": now,
                                                     "done": []}
                log.info("now watching trader %s", self._who(wallet))
                continue
            sent = count > rec.get("nonce", count)
            active_until = now + ACTIVE_SECONDS if sent else rec.get("active_until", 0)
            if not sent and active_until <= now:  # it sent nothing: only move its place
                results.append((wallet, lambda rec=rec, count=count: rec.update(block=tip, nonce=count, active_from=None,
                                                                                resume=None), []))
                continue
            active_from = rec.get("active_from") or rec["block"]  # its new transactions come after this block
            # Never before watching started; past what a busy wallet's last check got through.
            first = max(active_from - lag + 1, rec.get("start", -1) + 1, rec.get("resume") or 0)
            blocks, complete = rpc.transfers(wallet.address, from_block=first, to_block=tip,
                                             limit=PAGE * 4) if first <= tip else ({}, True)
            resume = None
            if not complete and blocks:
                resume = max(blocks) + 1
                active_until = max(active_until, now + ACTIVE_SECONDS)
                log.warning("trader %s moved tokens %d+ times since the last check; the rest is read next check",
                            self._who(wallet), PAGE * 4)
            done = set(rec.get("done") or [])
            fresh = {number: block for number, block in blocks.items() if number not in done}
            trades = rpc.trades(wallet.address, fresh)

            def commit(rec=rec, count=count, fresh=fresh, active_until=active_until, active_from=active_from,
                       resume=resume):
                rec.update(nonce=count, block=tip, active_until=active_until, active_from=active_from, resume=resume,
                           done=(list(rec.get("done") or []) + sorted(fresh))[-DONE_BLOCKS:])
            results.append((wallet, commit, trades))
        return results

    # --- alerting -------------------------------------------------------------------------------

    def _select(self, found: list) -> tuple[list, alerts.TraderContext | None]:
        """The trades your settings ask to be alerted, with prices and token details for the message."""
        settings = self.cfg.traders
        wanted = [(wallet, trade) for wallet, trade in found
                  if trade.side in ALERTED_SIDES
                  and (trade.side != "buy" or settings.alert_buys) and (trade.side != "sell" or settings.alert_sells)
                  and (settings.scope(wallet) != "source" or not wallet.source_mint  # added by hand: every token
                       or wallet.source_mint in (trade.mint, trade.other_mint))]
        if not wanted:
            return [], None
        wanted.sort(key=lambda item: (item[1].block_time or 0, item[1].slot, item[1].index))
        prices, tokens = {}, {}
        for chain_id in dict.fromkeys(trade.chain for _, trade in wanted):
            chain = CHAINS[chain_id]
            try:  # for sizes, token details and market caps; without them alerts still go out (majors: list only)
                prices[chain_id] = fetch_native_usd(self.http, chain)
                mints = [m for _, t in wanted if t.chain == chain_id for m in (t.mint, t.other_mint) if m]
                tokens.update({(chain_id, mint): info for mint, info in fetch_tokens(self.http, mints, chain).items()})
            except Exception as exc:
                log.warning("token details unavailable on %s: %s", chain.name, self.redact(_describe(exc)))
        context = alerts.TraderContext(tokens=tokens, prices=prices, labels=self.cfg.labels, now=self.clock(),
                                       scopes={w.key: settings.scope(w) for w in self.wallets.values()})
        due = []
        for wallet, trade in wanted:
            chain = CHAINS[trade.chain]
            if settings.scope(wallet) == "major" and not any(self._is_major(chain, mint, tokens)
                                                             for mint in (trade.mint, trade.other_mint) if mint):
                log.info("trade by %s in a token that isn't major: not alerted", self._who(wallet))
                continue
            value = value_usd(trade, chain, tokens, prices.get(trade.chain))
            if value is not None and value < settings.minimum(wallet):
                log.info("trade by %s below its minimum ($%.2f): not alerted", self._who(wallet), value)
                continue
            due.append((wallet, trade))
        return due, context

    def _is_major(self, chain, mint: str, tokens: dict) -> bool:
        """On the chain's major-token list, or (by DEX Screener) past the market-cap threshold with deep
        liquidity."""
        settings = self.cfg.traders
        if settings.listed_major(chain, mint):
            return True
        info = tokens.get((chain.id, mint))
        return bool(settings.major_min_market_cap_usd and info
                    and (info.market_cap or 0) >= settings.major_min_market_cap_usd
                    and (info.liquidity_usd or 0) >= MAJOR_MIN_LIQUIDITY_USD)

    def _record_signals(self, due: list, context) -> None:
        """One JSON line per delivered trade: a stable record other tools (backtests, a future
        copy-trading executor) can read. Failing to write it never blocks alerts."""
        if not self.signal_log:
            return
        try:
            with open(self.signal_log, "a", encoding="utf-8") as fh:
                for wallet, trade in due:
                    chain = CHAINS[trade.chain]
                    info = context.tokens.get((trade.chain, trade.mint))
                    value = value_usd(trade, chain, context.tokens, context.prices.get(trade.chain))
                    fh.write(json.dumps({
                        "v": 1, "time": trade.block_time, "seen": context.now, "chain": trade.chain,
                        "wallet": wallet.address, "label": wallet.label, "side": trade.side, "token": trade.mint,
                        "symbol": info.symbol if info else chain.majors.get(trade.mint),
                        "amount": trade.tokens, "other_token": trade.other_mint,
                        "native": round(trade.native, 9), "usd_paid": round(trade.usd, 2),
                        "value_usd": None if value is None else round(value, 2),
                        "price_usd": info.price_usd if info else None, "tx": trade.signature}) + "\n")
        except OSError as exc:
            log.warning("could not write the signal log %s: %s", self.signal_log, exc)

    # --- health, heartbeat ----------------------------------------------------------------------

    def heartbeat_lines(self, now: float) -> list:
        failures = self.state["failures"]
        recent = [ts for ts in self.state["alerted"] if now - ts < DAY]
        status = f"failing, {failures['count']} checks in a row" if failures["count"] else "ok"
        last = self.state.get("last_check")
        per_chain = ", ".join(f"{CHAINS[c].name} {len(ws)}" for c, ws in self.chains().items())
        return [[f"Trader watch: {len(self.wallets)} wallet(s) ({per_chain}) · {len(recent)} trade(s) alerted in "
                 f"the last 24 h · last check {utc(last, '%H:%M UTC') if last else 'not yet'} ({status})"]]

    def announce_start(self) -> None:
        """For running without a token monitor (traders only)."""
        self.notifier.send([[alerts.Bold("Trader watch started"), f" · {utc(self.clock())}"],
                            [alerts.describe_traders(self.cfg)]], level=logging.INFO)

    def maybe_heartbeat(self, now: float) -> None:
        """Daily heartbeat when there is no token monitor to send one."""
        if self.cfg.heartbeat is None:
            return
        last = self.state.get("heartbeat_ts")
        if last is None:
            self.state["heartbeat_ts"] = now
            return
        if now >= next_heartbeat(last, self.cfg.heartbeat):
            lines = [[alerts.Bold("Trader watch: daily heartbeat"), f" · {utc(now)} · running"]] + self.heartbeat_lines(now)
            if self.notifier.send(lines, level=logging.INFO):
                self.state["heartbeat_ts"] = now

    def _track_health(self, report: TraderReport, now: float) -> None:
        state, cfg = self.state, self.cfg
        failures = state["failures"]
        if report.ok:
            if failures["alerted"]:
                self.notifier.send(alerts.build_trader_recovery(failures["count"], failures["since"], now),
                                   level=logging.INFO)
            failures.update(count=0, since=None, alerted=False, last_errors=[])
            return
        failures["count"] += 1
        failures["since"] = failures["since"] or now
        failures["last_errors"] = report.errors[:5]
        if failures["count"] < cfg.max_consecutive_failures:
            return
        last = state["alerts"].get("trader_failure")
        if last is not None and now - last < cfg.cooldowns["monitor_failure"] * 60:
            return
        if self.notifier.send(alerts.build_trader_failure(failures, cfg.traders.poll_seconds), level=logging.ERROR):
            state["alerts"]["trader_failure"] = now
            failures["alerted"] = True

    def _log(self, report: TraderReport) -> None:
        parts = f"{len(self.wallets)} wallet(s) | {len(report.trades)} new trade(s), {len(report.sent)} alerted"
        if report.ok:
            log.info("trader check ok | %s", parts)
        else:
            log.warning("trader check had errors | %s | %s", parts, " ; ".join(report.errors))

    def _save(self) -> None:
        try:
            self.store.save(self.state)
        except Exception:
            log.exception("could not save %s", self.store.path)


def value_usd(trade: Trade, chain, tokens: dict, native_usd: float | None) -> float | None:
    """The trade's size in USD, or None if it can't be told (then it is alerted regardless)."""
    if trade.mint == chain.wrapped:  # the chain's coin itself, bought or sold for stablecoins
        return trade.tokens * native_usd if native_usd else (trade.usd or None)
    if trade.native >= 0.001:
        return trade.native * native_usd + trade.usd if native_usd else (trade.usd or None)
    if trade.usd:
        return trade.usd
    info = tokens.get((chain.id, trade.mint))
    if info and info.price_usd:  # a swap, or a sell paid to another wallet: value the tokens
        return trade.tokens * info.price_usd
    return None
