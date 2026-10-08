"""--list: print the current top holders once. Read-only; does not touch state.json."""

from __future__ import annotations

import sys
import time

from .holders import PoolDetector, excluded_reason, fetch_snapshot
from .known import TOKEN_PROGRAMS
from .market import fetch_market
from .util import fmt_tokens, fmt_usd, short, utc


def list_holders(cfg, rpc, http, *, out=None) -> int:
    out = out or sys.stdout

    def say(text=""):
        print(text, file=out)

    status, market = 0, None
    try:
        market = fetch_market(http, cfg.mint)
    except Exception as exc:
        status = 1
        say(f"Price/liquidity unavailable: {exc}")
    title = f"{market.name} ({market.symbol})" if market and market.found and market.name else short(cfg.mint)
    say(f"{title} · mint {cfg.mint}")
    if market and market.name_had_hidden_chars:
        say("  Warning: this token's name contains hidden text-direction characters, a trick used by "
            "look-alike tokens. Double-check the mint address.")
    try:
        info = rpc.account_info(cfg.mint)
        mint = info["data"]["parsed"]["info"]
        say(f"Program {TOKEN_PROGRAMS.get(info['owner'], info['owner'])} · decimals {mint['decimals']} · "
            f"mint authority: {mint.get('mintAuthority') or 'none (supply is fixed)'} · "
            f"freeze authority: {mint.get('freezeAuthority') or 'none'}")
    except Exception as exc:
        say(f"Mint details unavailable: {exc}")
    if market and market.found:
        say(f"Price {fmt_usd(market.price_usd)} · liquidity {fmt_usd(market.liquidity_usd)} · deepest pool "
            f"{market.venue} ({market.pair_address}) · {market.pool_count} pool(s) list it as base token")
    elif market:
        say("No trading pair found on DEX Screener for this mint.")
    say(f"RPC: {cfg.rpc_label}")

    detector = PoolDetector(rpc)
    if market:
        detector.add_pairs(market.pair_addresses)
    try:
        snap = fetch_snapshot(rpc, cfg.mint, extra_owners=cfg.always_alert_owners)
        detector.lookup([o for o in snap.ranked_owners() if o not in cfg.exclude_owners])
    except Exception as exc:
        say("")
        say(f"Holder data unavailable: {exc}")
        return 1

    say(f"Supply {fmt_tokens(snap.supply, snap.decimals)} · {utc(time.time())}")
    say("")
    header = (f"{'#':>3}  {'Owner wallet':<44}  {'Balance':>17}  {'% supply':>8}  {'Accts':>5}  "
              f"{'Watched':<7}  {'Excluded':<40}  Label")
    say(header)
    say("-" * len(header))
    watched_total = watched = 0
    for rank, owner in enumerate(snap.ranked_owners(), 1):
        balance = snap.balances[owner]
        reason = excluded_reason(cfg, detector, owner)
        is_watched = not reason and watched < cfg.top_n
        if is_watched:
            watched += 1
            watched_total += balance
        flag = f"yes ({reason})" if reason else "no"
        mark = "always" if owner in cfg.always_alert_owners else ("top" if is_watched else "")
        say(f"{rank:>3}  {owner:<44}  {fmt_tokens(balance, snap.decimals):>17}  {snap.share(balance):>7.2f}%  "
            f"{len(snap.owner_accounts[owner]):>5}  {mark:<7}  {flag:<40}  {cfg.labels.get(owner, '')}")
    say("")
    say(f"Watched: the top {watched} non-excluded owners hold {snap.share(watched_total):.2f}% of supply "
        f"(top_n = {cfg.top_n}).")
    ranked = set(snap.ranked_owners())
    for owner in cfg.always_alert_owners:
        if owner not in ranked:
            balance = snap.balances.get(owner, 0)
            say(f"Always-alert wallet {owner} {cfg.labels.get(owner, '')}: {fmt_tokens(balance, snap.decimals)} "
                f"({snap.share(balance):.4f}% of supply, {len(snap.owner_accounts.get(owner, []))} token account(s))")
    return status
