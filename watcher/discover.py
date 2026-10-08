"""Find a token's most profitable traders from its on-chain trades, on Solana or an EVM chain.

1. Candidates. Solana: the wallets that traded the most in the most recent transactions of the token's
   busiest pools (up to `scan_transactions`, none older than `lookback_hours`) and signed their own
   trades (pools and programs never sign), plus the current top holders. EVM chains: the senders of
   the latest trades GeckoTerminal reports for those pools.
2. History: each candidate's complete history on this token, accounted with average cost in the
   chain's coin (SOL, ETH, BNB): realized profit on sells, plus what it still holds at today's price.
   Network fees count as costs.
3. Ranking by total profit, leaving out bot-like wallets, scalpers, small traders and wallets whose
   history can't be fully accounted for (sold tokens they weren't seen buying, or too many
   transactions). On EVM chains, senders whose tokens move through their own contracts (most
   trading bots) never trade from the wallet itself and are left out too.

Only public data is read. Past results are not a prediction.
"""

from __future__ import annotations

import logging
import time
from collections import Counter, deque
from dataclasses import dataclass, field

from .chains import SOLANA
from .holders import PoolDetector, fetch_snapshot
from .market import fetch_native_usd, fetch_pairs, select_market, token_pools
from .txparse import Trade, wallet_changes, wallet_trades

log = logging.getLogger("holder_watch.discover")

EARLY_SECONDS = 600          # a first buy this soon after the first pool opened is flagged
MAX_ACCOUNTS_PER_WALLET = 3  # Solana token accounts read per candidate (normal wallets have one)
EVM_OVERSAMPLE = 2           # most senders of EVM trades are bots that trade through contracts


class DiscoveryError(Exception):
    pass


def side_for(trade: Trade, mint: str) -> str | None:
    """The trade from `mint`'s point of view: buy, sell, in (received without paying) or out."""
    if trade.mint == mint:
        return "in" if trade.side == "swap" else trade.side
    if trade.other_mint == mint:  # swapped this token for another one
        return "out"
    return None


def value_native(trade: Trade, native_usd: float | None) -> float | None:
    """The wallet's net change of the chain's coin in the trade (negative when it paid), stablecoins
    converted at today's price, network fee included. None if a stablecoin leg can't be converted."""
    value = trade.native_change
    if trade.usd_change:
        if not native_usd:
            return None
        value += trade.usd_change / native_usd
    return value


@dataclass
class Position:
    """One wallet's trading in one token, with average-cost accounting in the chain's coin."""
    owner: str
    decimals: int = 0
    buys: int = 0
    sells: int = 0
    bought: int = 0          # raw tokens bought, and the coin paid (fees included)
    cost: float = 0.0
    sold: int = 0            # raw tokens sold, and the coin received (fees deducted)
    proceeds: float = 0.0
    held: int = 0            # raw tokens still held from its buys, and their remaining cost
    basis: float = 0.0
    realized: float = 0.0
    winning_sells: int = 0
    received: int = 0        # raw tokens received without paying (transfers, airdrops, swaps from other tokens)
    sent: int = 0            # raw tokens sent away without being paid
    unmatched: int = 0       # raw tokens sold beyond what it was seen buying
    signed: int = 0          # buys and sells it signed itself
    same_block: int = 0      # sells in the same block as one of its buys
    first_buy: float | None = None
    last_trade: float | None = None
    buy_slots: set = field(default_factory=set, repr=False)
    lots: deque = field(default_factory=deque, repr=False)  # [raw tokens left, bought at], oldest first
    holds: list = field(default_factory=list, repr=False)   # (raw tokens sold, seconds held), first in first out

    def apply(self, trade: Trade, side: str, value: float | None) -> None:
        self.decimals = trade.decimals
        if trade.block_time:
            self.last_trade = max(self.last_trade or 0, trade.block_time)
        if side == "buy" and value is not None:
            cost = -value
            self.buys += 1
            self.signed += trade.signer
            self.bought += trade.amount
            self.cost += cost
            self.held += trade.amount
            self.basis += cost
            self.buy_slots.add(trade.slot)
            self.lots.append([trade.amount, trade.block_time])
            if trade.block_time:
                self.first_buy = min(self.first_buy or trade.block_time, trade.block_time)
        elif side == "sell" and value is not None:
            self.sells += 1
            self.signed += trade.signer
            self.sold += trade.amount
            self.proceeds += value
            self.same_block += trade.slot in self.buy_slots
            matched = min(trade.amount, self.held)
            if matched:
                matched_cost = self.basis * matched / self.held
                gain = value * matched / trade.amount - matched_cost
                self.realized += gain
                self.winning_sells += gain > 0
                self.basis -= matched_cost
                self.held -= matched
                self._take_lots(matched, trade.block_time)
            self.unmatched += trade.amount - matched
        elif side in ("in", "buy"):  # a buy that couldn't be valued counts as received
            self.received += trade.amount
        else:  # sent away, or a sell that couldn't be valued
            self.sent += trade.amount
            moved = min(trade.amount, self.held)
            if moved:
                self.basis -= self.basis * moved / self.held
                self.held -= moved
                self._take_lots(moved, None)

    def _take_lots(self, amount: int, sold_at: float | None) -> None:
        """Use up the oldest lots; for a sell, note how long each part was held."""
        while amount > 0 and self.lots:
            lot = self.lots[0]
            take = min(amount, lot[0])
            if sold_at is not None and lot[1] is not None:
                self.holds.append((take, sold_at - lot[1]))
            lot[0] -= take
            amount -= take
            if not lot[0]:
                self.lots.popleft()

    def hold_stats(self, quick_seconds: float) -> tuple[float | None, float | None]:
        """(average time its sold tokens were held, in seconds; share of them held under quick_seconds),
        or (None, None) if it hasn't sold anything it bought."""
        total = sum(amount for amount, _ in self.holds)
        if not total:
            return None, None
        average = sum(amount * seconds for amount, seconds in self.holds) / total
        return average, sum(amount for amount, seconds in self.holds if seconds < quick_seconds) / total

    def unrealized(self, price_native: float | None) -> float:
        if price_native is None or not self.held:
            return 0.0
        return self.held / 10 ** self.decimals * price_native - self.basis

    def pnl(self, price_native: float | None) -> float:
        return self.realized + self.unrealized(price_native)

    def roi(self, price_native: float | None) -> float | None:
        return self.pnl(price_native) / self.cost * 100 if self.cost > 0 else None


def verdict(pos: Position, settings, complete: bool, native_usd: float | None = None) -> str | None:
    """Why a wallet is left out of the ranking, or None if it is ranked."""
    if not complete:
        return "too many transactions to account for (bot-like)"
    if not pos.signed:
        return "never bought or sold it itself (pool, program, contract-run bot or transfers only)"
    if pos.buys + pos.sells > settings.max_trades:
        return "bot-like (too many trades)"
    if pos.same_block >= 2:
        return "bot-like (sold in the same block it bought)"
    _, quick = pos.hold_stats(settings.min_hold_minutes * 60)
    if quick is not None and quick > settings.max_scalp_share:
        return f"scalper (sold most of it within {settings.min_hold_minutes:g} min of buying)"
    if pos.unmatched > 0.02 * pos.sold:
        return "sold tokens it wasn't seen buying"
    if native_usd and pos.cost * native_usd < settings.min_buy_usd:
        return f"bought less than ${settings.min_buy_usd:,.0f}"
    return None


@dataclass
class RankedTrader:
    rank: int
    address: str
    pnl_native: float          # in the chain's coin (SOL, ETH, BNB)
    realized_native: float
    unrealized_native: float
    roi_pct: float | None
    cost_native: float
    proceeds_native: float
    buys: int
    sells: int
    winning_sells: int
    held_tokens: float     # still held from its buys
    held_pct: float        # ... as a share of what it bought
    first_buy: float | None
    last_trade: float | None
    notes: list = field(default_factory=list)
    avg_hold_hours: float | None = None  # how long what it sold was held (None: nothing sold yet)
    scalp_share: float | None = None     # share of it sold within discovery.min_hold_minutes
    pnl_usd: float | None = None         # at today's price of the coin


@dataclass
class DiscoveryReport:
    mint: str
    symbol: str | None
    name: str | None
    price_usd: float | None
    price_native: float | None
    native_usd: float | None
    pools: list                # Pool objects scanned
    scanned: int               # Solana: pool transactions read; EVM: recent trades read
    scan_from: float | None    # time of the oldest and newest
    scan_to: float | None
    wallets_seen: int          # wallets that bought or sold in the scan
    candidates: int            # wallets whose history was read
    traders: list              # RankedTrader, most profitable first
    left_out: Counter          # reason -> number of candidates
    notes: list
    credits: int               # estimated provider credits used
    history_api: str
    seconds: float
    chain: str = "solana"
    native: str = "SOL"


def discover(rpc, http, mint: str, settings, *, chain=SOLANA, gecko=None, exclude=(), progress=None,
             clock=None) -> DiscoveryReport:
    """Rank the token's traders. progress(stage, done, total) is called as the work advances. On an
    EVM chain `rpc` is an evm.EvmRPC and `gecko` a gecko.Gecko."""
    progress = progress or (lambda stage, done, total: None)
    clock = clock or time.time
    if chain.evm:
        if not rpc.history:
            raise DiscoveryError(f"Reading traders' histories on {chain.name} needs a transfer index: add ANKR_API_KEY "
                                 "to .env (free at https://www.ankr.com/rpc/)")
        return _discover_evm(rpc, http, gecko, chain, chain.normalize(mint), settings,
                             {chain.normalize(a) for a in exclude}, progress, clock)
    paced = rpc.max_rps is not None
    if not paced:  # stay under the provider's rate limit (Helius free plan: 10 requests/s); slows down on 429
        rpc.limit_rate(3 if rpc.public else 8)
    try:
        return _discover_solana(rpc, http, mint, settings, set(exclude), progress, clock)
    finally:
        if not paced:
            rpc.limit_rate(None)


def _market(http, mint, chain, settings, notes):
    pairs = fetch_pairs(http, mint, chain)
    market = select_market(pairs, mint, chain)
    all_pools = token_pools(pairs, mint, chain)
    if not market.found or not all_pools:
        raise DiscoveryError(f"DEX Screener lists no {chain.name} pool with this token as the base token")
    try:
        native_usd = fetch_native_usd(http, chain)
    except Exception as exc:  # only stablecoin trades and USD figures need it
        log.warning("%s price unavailable: %s", chain.native, exc)
        native_usd = None
    price_native = _price_in_native(market, native_usd, chain)
    if price_native is None:
        notes.append(f"Current price in {chain.native} unavailable: holdings are not valued, only realized "
                     "profit counts.")
    launch = min((pool.created for pool in all_pools if pool.created), default=None)
    return market, _busiest(all_pools, settings.pools), native_usd, price_native, launch


def _discover_solana(rpc, http, mint, settings, excluded, progress, clock) -> DiscoveryReport:
    started, credits_before, notes = clock(), rpc.credits, []
    market, pools, native_usd, price_native, launch = _market(http, mint, SOLANA, settings, notes)

    # 1. Scan the busiest pools.
    cache, scanned = {}, []
    since = clock() - settings.lookback_hours * 3600
    for pool, budget in zip(pools, _budgets(pools, settings.scan_transactions)):
        stage = f"Scanning {pool.venue}"
        progress(stage, 0, budget)
        items, _complete = rpc.address_transactions(pool.address, limit=budget, since_ts=since, cache=cache,
                                                    progress=lambda done, total, stage=stage: progress(stage, done, total))
        scanned += items
        progress(stage, len(items), len(items))

    # 2. Candidates: who traded the most in the scan (and signed it), plus the top holders.
    activity, accounts = {}, {}
    for _sig, tx in scanned:
        changes = wallet_changes(tx)
        for owner, change in changes.items():
            row = change.tokens.get(mint)
            if not row or row[0] == row[1] or owner in excluded:
                continue
            accounts.setdefault(owner, set()).update(change.accounts.get(mint, []))
            for trade in wallet_trades(tx, owner, changes=changes):
                side, value = side_for(trade, mint), value_native(trade, native_usd)
                if side in ("buy", "sell") and value is not None and trade.signer:
                    activity[owner] = activity.get(owner, 0.0) + abs(value)
    candidates = sorted(activity, key=lambda owner: -activity[owner])[: settings.candidates]
    try:
        snapshot = fetch_snapshot(rpc, mint)
        detector = PoolDetector(rpc)
        detector.add_pairs(market.pair_addresses)
        holders = [owner for owner in snapshot.ranked_owners() if owner not in excluded]
        detector.lookup(holders)
        for owner in holders:
            if not detector.known(owner) and owner not in candidates:
                candidates.append(owner)
                accounts.setdefault(owner, set()).update(snapshot.owner_accounts.get(owner, []))
    except Exception as exc:  # reported in the summary; details go to the log file
        log.info("top holders unavailable: %s", exc)
        notes.append("Top holders were not checked (the RPC refused getTokenLargestAccounts); "
                     "only wallets that traded in the scanned window were.")

    # 3. Each candidate's full history on this token.
    def history(owner):
        txs, complete = {}, True
        for account in sorted(accounts.get(owner, ()))[:MAX_ACCOUNTS_PER_WALLET]:
            items, done = rpc.address_transactions(account, limit=settings.max_wallet_transactions, cache=cache,
                                                   all_or_none=True)
            txs.update(reversed(items))  # oldest first, so same-block trades keep their order below
            complete = complete and done
            if not complete:  # too busy to account for: skip its other accounts too
                break
        ordered = sorted(txs.items(), key=lambda kv: (kv[1].get("slot") or 0, kv[1].get("transactionIndex") or 0))
        return [trade for _sig, tx in ordered for trade in wallet_trades(tx, owner)], complete

    times = [tx.get("blockTime") for _sig, tx in scanned if tx.get("blockTime")]
    return _rank(candidates, history, mint, settings, native_usd, price_native, launch, progress, notes,
                 chain=SOLANA, market=market, pools=pools, scanned=len(scanned), times=times, wallets_seen=len(activity),
                 credits=lambda: rpc.credits - credits_before, started=started, clock=clock,
                 history_api="getTransactionsForAddress" if rpc.history_api else "getSignaturesForAddress + getTransaction")


def _discover_evm(rpc, http, gecko, chain, token, settings, excluded, progress, clock) -> DiscoveryReport:
    started, credits_before, notes = clock(), rpc.credits, []
    market, pools, native_usd, price_native, launch = _market(http, token, chain, settings, notes)

    # 1. Candidates: the senders of the latest trades in the busiest pools.
    activity, times, read = {}, [], 0
    for pool in pools:
        progress(f"Reading recent trades of {pool.venue}", 0, 0)
        for trade in gecko.trades(chain, pool.address, min_usd=settings.min_buy_usd or None):  # hours, not minutes
            read += 1
            if trade.time:
                times.append(trade.time)
            if trade.trader not in excluded:
                activity[trade.trader] = activity.get(trade.trader, 0.0) + trade.volume_usd
    candidates = sorted(activity, key=lambda w: -activity[w])[: settings.candidates * EVM_OVERSAMPLE]
    notes.append(f"Candidates come from the latest {read} trades GeckoTerminal reports (about the last day).")

    # 2. Each candidate's full history on this token.
    def history(wallet):
        blocks, complete = rpc.transfers(wallet, contracts=[token], limit=settings.max_wallet_transactions + 1)
        if not complete:
            return [], False
        return rpc.trades(wallet, blocks), True

    return _rank(candidates, history, token, settings, native_usd, price_native, launch, progress, notes,
                 chain=chain, market=market, pools=pools, scanned=read, times=times, wallets_seen=len(activity),
                 credits=lambda: rpc.credits - credits_before, started=started, clock=clock,
                 history_api=f"{'Ankr' if rpc.index == 'ankr' else 'Alchemy'} transfers, receipts and balances")


def _rank(candidates, history, mint, settings, native_usd, price_native, launch, progress, notes, *, chain, market,
          pools, scanned, times, wallets_seen, credits, started, clock, history_api) -> DiscoveryReport:
    left_out, positions = Counter(), {}
    for i, owner in enumerate(candidates):
        progress("Reading trader histories", i, len(candidates))
        try:
            trades, complete = history(owner)
        except Exception as exc:  # counted under "Left out"; details go to the log file
            log.info("history of %s unavailable: %s", owner, exc)
            left_out["history unavailable (RPC error)"] += 1
            continue
        position = Position(owner)
        for trade in trades:
            side = side_for(trade, mint)
            if side:
                position.apply(trade, side, value_native(trade, native_usd))
        reason = verdict(position, settings, complete, native_usd)
        if reason is None and position.pnl(price_native) <= 0:
            reason = "lost money or broke even"
        if reason:
            left_out[reason] += 1
        else:
            positions[owner] = position
    progress("Reading trader histories", len(candidates), len(candidates))
    ranked = sorted(positions.values(), key=lambda p: -p.pnl(price_native))
    return DiscoveryReport(
        mint=mint, symbol=market.symbol, name=market.name, price_usd=market.price_usd, price_native=price_native,
        native_usd=native_usd, pools=pools, scanned=scanned, scan_from=min(times, default=None),
        scan_to=max(times, default=None), wallets_seen=wallets_seen, candidates=len(candidates),
        traders=[_ranked(rank, pos, price_native, native_usd, launch, settings) for rank, pos in enumerate(ranked, 1)],
        left_out=left_out, notes=notes, credits=credits(), history_api=history_api, seconds=clock() - started,
        chain=chain.id, native=chain.native)


def _ranked(rank: int, pos: Position, price_native, native_usd, launch, settings) -> RankedTrader:
    average, quick = pos.hold_stats(settings.min_hold_minutes * 60)
    notes = []
    if launch and pos.first_buy and pos.first_buy - launch < EARLY_SECONDS:
        notes.append(f"early: first buy {max(0, pos.first_buy - launch) / 60:.0f} min after the first pool opened")
    if pos.sent > 0.1 * pos.bought:
        notes.append(f"sent {pos.sent / pos.bought * 100:.0f}% of what it bought to other wallets")
    if pos.received:
        notes.append("also received tokens by transfer (not counted)")
    pnl = pos.pnl(price_native)
    return RankedTrader(
        rank=rank, address=pos.owner, pnl_native=pnl, realized_native=pos.realized,
        unrealized_native=pos.unrealized(price_native), roi_pct=pos.roi(price_native), cost_native=pos.cost,
        proceeds_native=pos.proceeds, buys=pos.buys, sells=pos.sells, winning_sells=pos.winning_sells,
        held_tokens=pos.held / 10 ** pos.decimals, held_pct=pos.held / pos.bought * 100 if pos.bought else 0.0,
        first_buy=pos.first_buy, last_trade=pos.last_trade, notes=notes,
        avg_hold_hours=None if average is None else average / 3600, scalp_share=quick,
        pnl_usd=pnl * native_usd if native_usd else None)


def _busiest(pools: list, count: int) -> list:
    """The busiest pools, skipping ones with under 2% of the token's 24 h volume (always at least one)."""
    total = sum(pool.volume_h24 for pool in pools)
    busy = [pool for pool in pools if total and pool.volume_h24 >= 0.02 * total]
    return (busy or pools[:1])[:count]


def _budgets(pools: list, total: int) -> list[int]:
    """Split the scan between pools by 24 h volume, at least 50 transactions each."""
    volume = sum(pool.volume_h24 for pool in pools)
    if volume <= 0:
        return [max(50, total // len(pools))] * len(pools)
    return [max(50, round(total * pool.volume_h24 / volume)) for pool in pools]


def _price_in_native(market, native_usd: float | None, chain) -> float | None:
    if market.quote_mint == chain.wrapped and market.price_native:
        return market.price_native
    if market.price_usd and native_usd:
        return market.price_usd / native_usd
    return None
