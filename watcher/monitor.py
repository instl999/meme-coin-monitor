"""The monitoring cycle: fetch data -> update state -> evaluate rules -> alert -> persist."""

from __future__ import annotations

import logging
import math
import signal
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

from . import alerts, history
from .classify import find_outflows
from .holders import PoolDetector, TokenAccount, excluded_reason, fetch_snapshot, parse_token_account
from .market import fetch_market
from .rpc import HttpError, RpcError
from .rules import HOLDER_RULES, evaluate
from .util import Redactor, fmt_tokens, fmt_usd, short, utc

log = logging.getLogger("holder_watch")
FORGET_ALERT_KEYS_AFTER = 7 * 86400  # bookkeeping for wallets no longer watched


@dataclass
class CycleReport:
    ok: bool = True
    errors: list = field(default_factory=list)
    hits: list = field(default_factory=list)       # every rule hit this cycle
    sent: list = field(default_factory=list)       # hits delivered in this cycle's alert
    suppressed: list = field(default_factory=list)  # hits held back by cooldown


def next_heartbeat(after_ts: float, heartbeat) -> float:
    """The first scheduled heartbeat time strictly after `after_ts` (DST-aware)."""
    zone = ZoneInfo(heartbeat.timezone)
    local = datetime.fromtimestamp(after_ts, zone)
    at = dtime(heartbeat.hour, heartbeat.minute)
    candidate = datetime.combine(local.date(), at, tzinfo=zone)
    if candidate.timestamp() <= after_ts:
        candidate = datetime.combine(local.date() + timedelta(days=1), at, tzinfo=zone)
    return candidate.timestamp()


def _describe(exc: Exception) -> str:
    return str(exc) if isinstance(exc, (HttpError, RpcError)) else f"{type(exc).__name__}: {exc}"


class Monitor:
    def __init__(self, cfg, rpc, http, notifier, store, *, clock=time.time):
        self.cfg = cfg
        self.rpc = rpc
        self.http = http
        self.notifier = notifier
        self.store = store
        self.clock = clock
        self.redact = Redactor(cfg.secrets)
        self.state = store.load(cfg.mint)
        self.detector = PoolDetector(rpc, self.state["owner_kinds"])
        self.trader_watch = None  # set when traders are watched: the heartbeat reports on them too

    @property
    def symbol(self) -> str:
        return self.state["last"].get("symbol") or short(self.cfg.mint)

    def run_cycle(self) -> CycleReport:
        now = self.clock()
        report = CycleReport()
        market = snapshot = None
        try:
            market = fetch_market(self.http, self.cfg.mint)
            self.detector.add_pairs(market.pair_addresses)
        except Exception as exc:
            report.errors.append(f"price/liquidity (DEX Screener): {_describe(exc)}")
        try:
            snapshot = fetch_snapshot(self.rpc, self.cfg.mint, known_accounts=self.state["accounts"],
                                      extra_owners=self.cfg.always_alert_owners)
            configured = self.cfg.exclude_owners | set(self.cfg.always_alert_owners)
            self.detector.lookup([o for o in snapshot.ranked_owners() if o not in configured])
        except Exception as exc:
            report.errors.append(f"holders (Solana RPC): {_describe(exc)}")
            snapshot = None
        try:
            self._remember_market(market, now)
            if snapshot is not None:
                self._update_holders(snapshot, now)
            report.hits = evaluate(self.cfg, self.state, now, holders_ok=snapshot is not None, market=market)
            self._dispatch(report, snapshot, market, now)
        except Exception as exc:  # a bug in one cycle must not stop the monitor
            log.exception("internal error during the cycle")
            report.errors.append(f"internal error: {_describe(exc)}")
        report.errors = [self.redact(error) for error in report.errors]
        report.ok = not report.errors
        self._track_health(report, now)
        self._maybe_heartbeat(market, now)
        self._log_cycle(report)
        self._save()
        return report

    def announce_start(self) -> None:
        self.notifier.send(alerts.build_startup(self.symbol, self.cfg, self.clock()), level=logging.INFO)

    # --- state updates ---------------------------------------------------------------------------

    def _who(self, owner: str) -> str:
        label = self.cfg.labels.get(owner)
        return f"{label} ({short(owner)})" if label else short(owner)

    def _retention(self) -> float:
        return self.cfg.rules.window_minutes * 60 + 2 * self.cfg.poll_seconds

    def _remember_market(self, market, now: float) -> None:
        if market is None:
            return
        last = self.state["last"]
        last.update(market_ts=now, found=market.found, price=market.price_usd, liquidity=market.liquidity_usd,
                    venue=market.venue, price_change_h24=market.price_change_h24,
                    no_pair_checks=0 if market.found else last.get("no_pair_checks", 0) + 1)
        if market.symbol:
            last["symbol"] = market.symbol
        if market.found and market.price_usd is not None:
            peak = self.state["peak"]
            if peak is None or market.price_usd > peak["usd"]:
                self.state["peak"] = {"usd": market.price_usd, "ts": now}

    def _update_holders(self, snap, now: float) -> None:
        cfg, owners = self.cfg, self.state["owners"]

        def excluded(owner):
            return excluded_reason(cfg, self.detector, owner)

        top = [o for o in snap.ranked_owners() if not excluded(o)][: cfg.top_n]
        watch = set(top) | set(cfg.always_alert_owners) | {o for o in owners if not excluded(o)}
        for owner in [o for o in owners if o not in watch]:
            log.info("no longer watching %s (%s)", self._who(owner), excluded(owner))
            del owners[owner]
        for owner in watch:
            rec = owners.get(owner)
            if rec and snap.balances.get(owner, 0) < rec["history"][-1][1]:
                self._recheck_accounts(snap, owner)
        for owner in sorted(watch, key=lambda o: -snap.balances.get(o, 0)):
            balance = snap.balances.get(owner, 0)
            rec = owners.get(owner)
            if rec is None:
                owners[owner] = rec = {"first_seen": now, "history": [[now, balance]]}
                log.info("now watching %s: %s %s (%.2f%% of supply)", self._who(owner),
                         fmt_tokens(balance, snap.decimals), self.symbol, snap.share(balance))
            elif history.record(rec["history"], now, balance):
                before = rec["history"][-2][1]
                change = f"{(balance - before) / before * 100:+.1f}%" if before else "new"
                log.info("balance change %s: %s -> %s %s (%s)", self._who(owner), fmt_tokens(before, snap.decimals),
                         fmt_tokens(balance, snap.decimals), self.symbol, change)
            if owner in cfg.always_alert_owners and (rec.get("always_ref") is None or balance > rec["always_ref"]):
                rec["always_ref"], rec["always_ref_ts"] = balance, now
        self._prune(top, now)
        self.state["accounts"] = {a.address: a.owner for a in snap.accounts.values()
                                  if a.owner in owners and not a.closed}
        self.state["top_owners"] = top
        self.state["last"].update(holders_ts=now, supply=snap.supply, decimals=snap.decimals, watching=len(owners),
                                  top_share=snap.share(sum(snap.balances.get(o, 0) for o in top)))

    def _recheck_accounts(self, snap, owner: str) -> None:
        """A watched wallet's balance fell: list all its token accounts for this mint before believing it,
        because tokens moved to a new account of the same wallet are not a sale."""
        try:
            items = self.rpc.token_accounts_by_owner(owner, self.cfg.mint)
        except Exception as exc:  # fail open: better a possible false alarm than a missed sale
            log.warning("could not double-check the token accounts of %s: %s", self._who(owner),
                        self.redact(_describe(exc)))
            return
        for item in items:
            parsed = parse_token_account(item.get("account"), self.cfg.mint)
            if parsed and parsed[0] == owner and item["pubkey"] not in snap.accounts:
                log.info("found another token account of %s: %s", self._who(owner), short(item["pubkey"]))
                snap.add(TokenAccount(item["pubkey"], owner, parsed[1]))

    def _prune(self, top: list, now: float) -> None:
        cfg, state = self.cfg, self.state
        owners, pending = state["owners"], state["pending"]
        keep = set(top) | set(cfg.always_alert_owners)
        for owner in list(owners):
            last_ts, last_balance = owners[owner]["history"][-1]
            held_back = any(f"{rule}:{owner}" in pending for rule in HOLDER_RULES)
            if owner not in keep and not held_back and last_balance == 0 and now - last_ts > self._retention():
                log.info("stopped watching %s: balance 0 since %s", self._who(owner), utc(last_ts))
                del owners[owner]
        spare = sorted((o for o in owners if o not in keep), key=lambda o: owners[o]["history"][-1][1])
        for owner in spare[: max(0, len(owners) - cfg.max_tracked_owners)]:
            log.info("stopped watching %s (max_tracked_owners=%d reached)", self._who(owner), cfg.max_tracked_owners)
            del owners[owner]
        for owner, rec in owners.items():
            cutoff = now - self._retention()
            for key in (f"holder_drop_pct:{owner}", "combined_drop_pct"):
                if key in pending:
                    cutoff = min(cutoff, pending[key])
            history.prune(rec["history"], cutoff)
        for book in (state["alerts"], pending):
            for key in list(book):
                owner = key.partition(":")[2]
                if owner and owner not in owners and now - book[key] > FORGET_ALERT_KEYS_AFTER:
                    del book[key]

    # --- alerting --------------------------------------------------------------------------------

    def _dispatch(self, report: CycleReport, snapshot, market, now: float) -> None:
        state, cfg = self.state, self.cfg
        due = []
        for hit in report.hits:
            last = state["alerts"].get(hit.key)
            wait = cfg.cooldowns.get(hit.rule, 0) * 60 - (now - last) if last is not None else 0
            if wait > 0:
                report.suppressed.append(hit)
                if hit.since is not None:
                    state["pending"].setdefault(hit.key, hit.since)
                log.info("rule %s fired%s, but it is in cooldown for %d more min", hit.rule,
                         f" for {self._who(hit.owner)}" if hit.owner else "", math.ceil(wait / 60))
            else:
                due.append(hit)
        fired = {hit.key for hit in report.hits}
        for key in [k for k in state["pending"] if k not in fired]:
            del state["pending"][key]
        if not due:
            return
        due = _merge_same_wallet(due)
        if snapshot is not None:
            self._classify(due, snapshot, now)
        context = alerts.AlertContext(symbol=self.symbol, decimals=state["last"].get("decimals", 0),
                                      supply=state["last"].get("supply"), labels=cfg.labels, market=market, now=now)
        if not self.notifier.send(alerts.build_alert(due, context, cfg), level=logging.WARNING):
            report.errors.append("alert delivery failed (Telegram error logged above); retrying next cycle")
            return
        for hit in due:
            for delivered in (hit, *hit.merged):
                state["alerts"][delivered.key] = now
                state["pending"].pop(delivered.key, None)
                rec = state["owners"].get(delivered.owner)
                if delivered.rule == "always_alert_owners" and rec:
                    rec["always_ref"], rec["always_ref_ts"] = delivered.data["after"], now
        state["stats"]["alerts"] += 1
        report.sent = due

    def _classify(self, hits: list, snapshot, now: float) -> None:
        results = {}
        for hit in hits:
            if hit.rule not in HOLDER_RULES or not hit.owner:
                continue
            if hit.owner not in results:
                since = hit.data.get("drop_ts", now) - self.cfg.poll_seconds - 300
                try:
                    flows = find_outflows(self.rpc, hit.owner, snapshot.owner_accounts.get(hit.owner, []),
                                          self.cfg.mint, since_ts=since, detector=self.detector)
                    results[hit.owner] = (flows, None)
                except Exception as exc:
                    reason = self.redact(_describe(exc))
                    log.warning("could not classify the outflow of %s: %s", self._who(hit.owner), reason)
                    results[hit.owner] = ([], f"outflow classification unavailable ({reason})")
            hit.outflows, hit.classify_error = results[hit.owner]

    def _track_health(self, report: CycleReport, now: float) -> None:
        state, cfg = self.state, self.cfg
        failures, stats = state["failures"], state["stats"]
        stats["cycles"] += 1
        if report.ok:
            if failures["alerted"]:
                self.notifier.send(alerts.build_recovery(self.symbol, failures["count"], failures["since"], now),
                                   level=logging.INFO)
            failures.update(count=0, since=None, alerted=False, last_errors=[])
            return
        stats["failed"] += 1
        failures["count"] += 1
        failures["since"] = failures["since"] or now
        failures["last_errors"] = report.errors[:5]
        if failures["count"] < cfg.max_consecutive_failures:
            return
        last = state["alerts"].get("monitor_failure")
        if last is not None and now - last < cfg.cooldowns["monitor_failure"] * 60:
            return
        if self.notifier.send(alerts.build_failure(self.symbol, failures, cfg.poll_seconds), level=logging.ERROR):
            state["alerts"]["monitor_failure"] = now
            failures["alerted"] = True

    def _maybe_heartbeat(self, market, now: float) -> None:
        if self.cfg.heartbeat is None:
            return
        beat = self.state["heartbeat"]
        if beat.get("last_ts") is None:  # first run: the first heartbeat is the next scheduled one
            beat["last_ts"] = now
            return
        if now < next_heartbeat(beat["last_ts"], self.cfg.heartbeat):
            return
        extra = self.trader_watch.heartbeat_lines(now) if self.trader_watch else None
        message = alerts.build_heartbeat(self.symbol, self.state, self.cfg, market, now, extra=extra)
        if self.notifier.send(message, level=logging.INFO):
            beat["last_ts"] = now
            self.state["stats"] = {"cycles": 0, "failed": 0, "alerts": 0}

    def _log_cycle(self, report: CycleReport) -> None:
        last = self.state["last"]
        parts = [f"price {fmt_usd(last.get('price'))}", f"liquidity {fmt_usd(last.get('liquidity'))}"]
        if last.get("top_share") is not None:
            parts.append(f"top {self.cfg.top_n} hold {last['top_share']:.1f}%")
        parts.append(f"watching {len(self.state['owners'])} wallets")
        parts.append(f"{len(report.hits)} rule hit(s), {len(report.sent)} sent, {len(report.suppressed)} in cooldown")
        if report.ok:
            log.info("cycle ok | %s", " | ".join(parts))
        else:
            log.warning("cycle had errors | %s | %s", " | ".join(parts), " ; ".join(report.errors))

    def _save(self) -> None:
        try:
            self.store.save(self.state)
        except Exception:
            log.exception("could not save %s", self.store.path)


def _merge_same_wallet(hits: list) -> list:
    """If a wallet fires both always_alert_owners and holder_drop_pct, report it once, naming both rules."""
    always = {hit.owner: hit for hit in hits if hit.rule == "always_alert_owners"}
    merged = []
    for hit in hits:
        if hit.rule == "holder_drop_pct" and hit.owner in always:
            always[hit.owner].merged.append(hit)
        else:
            merged.append(hit)
    return merged


def run_forever(monitor: Monitor | None, traders=None, *, install_signals: bool = True) -> int:
    """Run holder cycles every poll_seconds and trader checks every traders.poll_seconds until
    SIGINT/SIGTERM (either can be None: traders only, or no traders). A failing cycle never stops the loop."""
    stop = threading.Event()

    def handle(signum, _frame):
        if stop.is_set():
            raise KeyboardInterrupt
        log.info("signal %s received; stopping after the current cycle (repeat to force)", signum)
        stop.set()

    if install_signals:
        signal.signal(signal.SIGINT, handle)
        signal.signal(signal.SIGTERM, handle)
    jobs = []  # [job, interval, next run (monotonic)]
    if monitor is not None:
        monitor.trader_watch = traders
        monitor.announce_start()
        jobs.append([monitor, monitor.cfg.poll_seconds, 0.0])
    elif traders is not None:
        traders.announce_start()
    if traders is not None:
        jobs.append([traders, traders.cfg.traders.poll_seconds, 0.0])
    while not stop.is_set():
        for job in jobs:
            if stop.is_set() or time.monotonic() < job[2]:
                continue
            job[2] = time.monotonic() + job[1]
            try:
                job[0].run_cycle()
            except Exception:
                log.exception("unexpected error; the loop continues")
        stop.wait(max(1.0, min(job[2] for job in jobs) - time.monotonic()))
    log.info("monitor stopped")
    return 0
