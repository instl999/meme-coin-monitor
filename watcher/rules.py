"""Evaluate the user's rules against the latest data.

Pure functions: they read state but never change it. The monitor decides what to send and then
updates cooldowns and baselines.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import history
from .util import drop_pct

HOLDER_RULES = frozenset({"holder_drop_pct", "always_alert_owners"})
NO_PAIR_CHECKS = 2  # "no trading pair" must be seen on this many checks in a row (rules out one empty API reply)


@dataclass
class Hit:
    rule: str                    # the config rule that fired, e.g. "holder_drop_pct"
    key: str                     # cooldown key: the rule, or rule:wallet for per-wallet rules
    owner: str | None = None
    since: float | None = None   # start of the period a drop was measured over
    data: dict = field(default_factory=dict)
    merged: list = field(default_factory=list)   # other hits for the same wallet, reported together
    outflows: list = field(default_factory=list)  # classification of the holder's recent outflows
    classify_error: str | None = None


def baseline(state: dict, key: str, window_start: float) -> float:
    """Where a drop is measured from: the window start, but never before the last alert for the same
    key (so one event alerts once), and reaching back for a drop that was held back by cooldown."""
    since = window_start
    pending = state["pending"].get(key)
    if pending is not None:
        since = min(since, pending)
    last = state["alerts"].get(key)
    if last is not None:
        since = max(since, last)
    return since


def evaluate(cfg, state: dict, now: float, *, holders_ok: bool, market) -> list[Hit]:
    hits = []
    if holders_ok:  # never judge holders on stale data
        hits += _holder_drops(cfg, state, now)
        hits += _always_alert(cfg, state, now)
        hits += _combined_drop(cfg, state, now)
    if market is not None:
        hits += _price_rules(cfg, state, market)
    return hits


def _holder_drops(cfg, state, now):
    """Rule 1: a watched wallet's balance is down >= holder_drop_pct within the window."""
    threshold = cfg.rules.holder_drop_pct
    if threshold is None:
        return []
    window_start = now - cfg.rules.window_minutes * 60
    hits = []
    for owner, rec in state["owners"].items():
        hist = rec["history"]
        current = hist[-1][1]
        key = f"holder_drop_pct:{owner}"
        since = baseline(state, key, window_start)
        peak = history.peak_since(hist, since)
        if not peak or peak[0] <= 0 or current >= peak[0]:
            continue
        pct = drop_pct(peak[0], current)
        if pct >= threshold:
            drop_ts = history.first_drop_after(hist, peak[1], peak[0]) or now
            hits.append(Hit("holder_drop_pct", key, owner=owner, since=since,
                            data={"before": peak[0], "after": current, "pct": pct, "drop_ts": drop_ts}))
    return sorted(hits, key=lambda h: -h.data["pct"])


def _always_alert(cfg, state, now):
    """Rule 2: any outflow from an always_alert_owners wallet (e.g. the creator)."""
    hits = []
    for owner in cfg.always_alert_owners:
        rec = state["owners"].get(owner)
        if not rec or rec.get("always_ref") is None:
            continue
        reference, current = rec["always_ref"], rec["history"][-1][1]
        if current < reference:
            drop_ts = history.first_drop_after(rec["history"], rec.get("always_ref_ts", 0), reference) or now
            hits.append(Hit("always_alert_owners", f"always_alert_owners:{owner}", owner=owner,
                            data={"before": reference, "after": current, "pct": drop_pct(reference, current),
                                  "drop_ts": drop_ts}))
    return hits


def _combined_drop(cfg, state, now):
    """Rule 3: the watched wallets' combined balance is down >= combined_drop_pct within the window.

    Compared over a fixed set of wallets: at each candidate start time, only wallets already
    watched then (and still watched now) count, so wallets joining or leaving the watch list
    never look like a drop.
    """
    threshold = cfg.rules.combined_drop_pct
    owners = state["owners"]
    if threshold is None or not owners:
        return []
    key = "combined_drop_pct"
    since = baseline(state, key, now - cfg.rules.window_minutes * 60)
    current = {owner: rec["history"][-1][1] for owner, rec in owners.items()}
    times = {since} | {ts for rec in owners.values() for ts, _ in rec["history"] if since < ts <= now}
    best = None
    for t in sorted(times):
        members = [owner for owner, rec in owners.items() if rec["first_seen"] <= t]
        before = sum(history.value_at(owners[o]["history"], t) or 0 for o in members)
        after = sum(current[o] for o in members)
        if before > 0 and after < before and (best is None or drop_pct(before, after) > best[0]):
            best = (drop_pct(before, after), t, before, after, members)
    if best is None or best[0] < threshold:
        return []
    pct, t, before, after, members = best
    moves = [(o, history.value_at(owners[o]["history"], t) or 0, current[o]) for o in members]
    contributors = sorted((m for m in moves if m[2] < m[1]), key=lambda m: m[2] - m[1])[:3]
    return [Hit("combined_drop_pct", key, since=since,
                data={"before": before, "after": after, "pct": pct, "from_ts": t,
                      "wallets": len(members), "contributors": contributors})]


def _price_rules(cfg, state, market):
    """Rules 4-6: stop price, trailing stop from the persisted peak, liquidity floor / no pair."""
    rules, hits = cfg.rules, []
    price = market.price_usd if market.found else None
    if rules.stop_price_usd is not None and price is not None and price <= rules.stop_price_usd:
        hits.append(Hit("stop_price_usd", "stop_price_usd", data={"price": price, "stop": rules.stop_price_usd}))
    peak = state.get("peak")
    if rules.trailing_stop_pct is not None and price is not None and peak:
        trigger = peak["usd"] * (1 - rules.trailing_stop_pct / 100)
        if price <= trigger:
            hits.append(Hit("trailing_stop_pct", "trailing_stop_pct",
                            data={"price": price, "trigger": trigger, "peak": peak["usd"], "peak_ts": peak["ts"],
                                  "from_peak": drop_pct(peak["usd"], price)}))
    if rules.min_liquidity_usd is not None:
        if not market.found:
            if state["last"].get("no_pair_checks", 0) >= NO_PAIR_CHECKS:
                hits.append(Hit("min_liquidity_usd", "min_liquidity_usd:no_pair", data={"no_pair": True}))
        elif market.liquidity_usd is None or market.liquidity_usd < rules.min_liquidity_usd:
            hits.append(Hit("min_liquidity_usd", "min_liquidity_usd:low",
                            data={"liquidity": market.liquidity_usd, "min": rules.min_liquidity_usd,
                                  "venue": market.venue}))
    return hits
