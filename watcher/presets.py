"""Built-in traders: wallets with a good recent history on each chain, and how that list is made.

presets.json (shipped) holds a list per chain; `presets refresh` builds a fresh one with your keys and
saves it to presets.local.json, which wins over the shipped list and survives updates.

A list is built from the chain's busiest pools of non-major tokens (GeckoTerminal): the wallets that
traded several of them recently are scored over their last `days` (30) across every token they traded,
with the same accounting as discovery (average cost in the chain's coin, holdings valued at today's
price, fees included). A wallet is picked when it made a profit over at least 3 tokens, made money on
most of them, held what it sold for an hour or more (no scalping), isn't bot-like, and traded in the
last 7 days. Ranked by profit in USD. Past results are not a prediction.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .chains import CHAINS, NATIVE_PLACEHOLDERS
from .discover import Position, side_for, value_native
from .market import fetch_native_usd, fetch_tokens
from .state import atomic_write
from .txparse import wallet_trades

log = logging.getLogger("holder_watch.presets")

ROOT = Path(__file__).resolve().parent.parent
SHIPPED = ROOT / "presets.json"
LOCAL_NAME = "presets.local.json"
DAY = 86_400


@dataclass
class Criteria:
    days: int = 30
    min_tokens: int = 3            # profitable over at least this many different tokens traded
    min_win_rate: float = 0.5      # share of its tokens that made money
    min_pnl_usd: float = 500
    min_roi_pct: float = 10        # profit as a share of what it spent: a whale's thin margin isn't skill
    max_trades: int = 600          # in `days`: more is bot-like
    min_hold_minutes: float = 60   # tokens sold sooner count as scalping...
    max_scalp_share: float = 0.5   # ... and more than this share of its sells being scalps disqualifies it
    active_days: int = 7           # must have traded this recently
    max_transfers: int = 800       # token transfers read per wallet; more is bot-like


@dataclass
class Scorecard:
    chain: str
    wallet: str
    days: int
    pnl_usd: float | None = None
    pnl_native: float = 0.0
    cost_native: float = 0.0
    tokens_traded: int = 0
    tokens_won: int = 0
    trades: int = 0
    avg_hold_hours: float | None = None
    scalp_share: float | None = None
    last_trade: float | None = None
    reason: str | None = None      # why it isn't picked; None: it qualifies
    best_tokens: list = field(default_factory=list)  # [(symbol or address, pnl in USD)] top 3

    @property
    def win_rate(self) -> float:
        return self.tokens_won / self.tokens_traded if self.tokens_traded else 0.0

    @property
    def roi_pct(self) -> float | None:
        return self.pnl_native / self.cost_native * 100 if self.cost_native > 0 else None


def score_trades(chain, wallet: str, trades: list, criteria: Criteria, native_usd: float | None, http,
                 now: float, complete: bool = True) -> Scorecard:
    """A wallet's results over a set of trades in every token, valued at today's prices."""
    card = Scorecard(chain=chain.id, wallet=wallet, days=criteria.days)
    positions = {}
    for trade in trades:
        for mint in (trade.mint, trade.other_mint):
            if not mint or mint in chain.quotes:  # the coin and stablecoins are money, not positions
                continue
            side = side_for(trade, mint)
            if side:
                positions.setdefault(mint, Position(wallet)).apply(trade, side, value_native(trade, native_usd))
    held = [mint for mint, pos in positions.items() if pos.held]
    info = fetch_tokens(http, held, chain) if held and native_usd else {}
    price = {mint: i.price_usd / native_usd for mint, i in info.items() if i.price_usd and native_usd}
    results = []
    for mint, pos in positions.items():
        if not pos.buys:
            continue  # only sold or received: its cost is unknown
        pnl = pos.pnl(price.get(mint))
        results.append((mint, pnl, pos))
        card.pnl_native += pnl
        card.cost_native += pos.cost
        card.tokens_won += pnl > 0
        card.trades += pos.buys + pos.sells
        card.last_trade = max(card.last_trade or 0, pos.last_trade or 0) or None
    card.tokens_traded = len(results)
    card.pnl_usd = card.pnl_native * native_usd if native_usd else None
    holds = [h for _, _, pos in results for h in pos.holds]
    total = sum(amount for amount, _ in holds)
    if total:
        card.avg_hold_hours = sum(amount * seconds for amount, seconds in holds) / total / 3600
        card.scalp_share = sum(a for a, s in holds if s < criteria.min_hold_minutes * 60) / total
    best = sorted(results, key=lambda r: -r[1])[:3]
    card.best_tokens = [((info.get(m).symbol if info.get(m) else None) or chain.majors.get(m) or m,
                         round(p * native_usd, 2) if native_usd else None) for m, p, _ in best]
    signed = sum(pos.signed for _, _, pos in results)
    same_block = sum(pos.same_block for _, _, pos in results)
    card.reason = _reason(card, criteria, complete, signed, same_block, now)
    return card


def _reason(card: Scorecard, c: Criteria, complete: bool, signed: int, same_block: int, now: float) -> str | None:
    if not complete:
        return "too many transactions (bot-like)"
    if not signed:
        return "no trades of its own"
    if card.trades > c.max_trades or same_block >= 3:
        return "bot-like"
    if card.scalp_share is not None and card.scalp_share > c.max_scalp_share:
        return f"scalper (sold most within {c.min_hold_minutes:g} min)"
    if card.tokens_traded < c.min_tokens:
        return f"traded fewer than {c.min_tokens} tokens"
    if card.pnl_usd is None or card.pnl_usd < c.min_pnl_usd:
        return f"made less than ${c.min_pnl_usd:,.0f}"
    if card.roi_pct is None or card.roi_pct < c.min_roi_pct:
        return f"returned less than {c.min_roi_pct:g}% of what it spent"
    if card.win_rate < c.min_win_rate:
        return "lost money on most tokens"
    if not card.last_trade or now - card.last_trade > c.active_days * DAY:
        return f"no trade in {c.active_days} days"
    return None


def scorecard(chain, wallet: str, *, solana_rpc=None, evm_rpc=None, http, criteria: Criteria | None = None,
              native_usd: float | None = None, now: float | None = None, quick: bool = False) -> Scorecard:
    """Score one wallet over the last criteria.days on its chain. quick: when its token transfers alone
    rule it out, skip reading its transactions (the card then only has the reason)."""
    criteria = criteria or Criteria()
    now = time.time() if now is None else now
    if native_usd is None:
        native_usd = fetch_native_usd(http, chain)
    if chain.evm:
        if not evm_rpc.history:
            raise ValueError(f"{chain.name}: a wallet's history needs ANKR_API_KEY")
        tip = evm_rpc.block_number()
        start = max(0, tip - int(criteria.days * DAY / evm_rpc.block_seconds(tip)))
        blocks, complete = evm_rpc.transfers(wallet, from_block=start, to_block=tip, limit=criteria.max_transfers)
        early = _transfer_reason(chain, blocks, complete, criteria, now) if quick else None
        if early:
            return Scorecard(chain=chain.id, wallet=wallet, days=criteria.days, reason=early)
        trades = evm_rpc.trades(wallet, blocks) if complete else []
    else:
        items, complete = solana_rpc.address_transactions(wallet, limit=criteria.max_transfers,
                                                          since_ts=now - criteria.days * DAY, all_or_none=True)
        trades = [t for _sig, tx in reversed(items) for t in wallet_trades(tx, wallet)]
    return score_trades(chain, wallet, trades, criteria, native_usd, http, now, complete)


def _transfer_reason(chain, blocks: dict, complete: bool, c: Criteria, now: float) -> str | None:
    """What rules a wallet out from its token transfers alone (cheap: no transactions read). Each trade
    moves a token, so too few tokens or no recent transfer can't become a pick."""
    if not complete:
        return "too many transactions (bot-like)"
    if len({t for block in blocks.values() for t in block.tokens if t not in chain.quotes}) < c.min_tokens:
        return f"traded fewer than {c.min_tokens} tokens"
    last = max((block.time for block in blocks.values() if block.time), default=None)
    if not last or now - last > c.active_days * DAY:
        return f"no trade in {c.active_days} days"
    return None


def tradable(pool, chain) -> bool:
    """A pool worth looking for traders in: not the chain's coin, a stablecoin or a major token, and not a
    pegged pair (stablecoin/stablecoin, BTC/BTC, staked ETH/ETH), where the busiest wallets are arbitrage
    bots."""
    if pool.base_token in chain.quotes or pool.base_token in chain.majors or pool.base_token in NATIVE_PLACEHOLDERS:
        return False
    if pool.price_in_quote is not None and abs(pool.price_in_quote - 1) < 0.02:
        return False
    if pool.price_in_native is not None and 0.95 <= pool.price_in_native <= 1.4:
        return False  # a staked or wrapped version of the coin (weETH, wstETH, JitoSOL...)
    stable_like = pool.base_price_usd is not None and 0.9 <= pool.base_price_usd <= 1.2
    return not (stable_like and abs(pool.change_h24 or 0) < 2)  # incl. yield-bearing dollars like sUSDai


SAMPLE_MAX_TRADES = 5     # a wallet in a busy pool's latest 300 trades more often than this is a bot...
SAMPLE_MIN_USD = 100      # ... and one that traded less than this in them is too small to follow
BOT_BURST = (100, 2 * 3600)   # Solana: 100 transactions within 2 hours is a bot, not a person
MAX_EVM_NONCE = 5_000     # EVM: a wallet that sent more transactions than this is a bot or a bundler


def candidates(gecko, chain, *, pools: int = 8, limit: int = 40) -> list[str]:
    """Wallets that traded the chain's trending (then busiest) pools of ordinary tokens in the last day
    like people do: trades of at least SAMPLE_MIN_USD, a few times each in a pool's latest 300 such
    trades (bots fire dozens of times). Those in more pools, then with more volume, first."""
    chosen = {}
    for pool in gecko.trending_pools(chain) + gecko.busy_pools(chain, pages=1):
        if tradable(pool, chain) and len(chosen) < pools:
            chosen.setdefault(pool.address, pool)
    seen = {}  # wallet -> [pools traded, trades, USD volume]
    for pool in chosen.values():
        for trade in gecko.trades(chain, pool.address, min_usd=SAMPLE_MIN_USD):
            row = seen.setdefault(trade.trader, [set(), 0, 0.0])
            row[0].add(pool.address)
            row[1] += 1
            row[2] += trade.volume_usd
    people = [w for w, (_pools, trades, usd) in seen.items() if trades <= SAMPLE_MAX_TRADES and usd >= SAMPLE_MIN_USD]
    return sorted(people, key=lambda w: (-len(seen[w][0]), -seen[w][2]))[:limit]


def prescreen(chain, wallets: list, *, solana_rpc=None, evm_rpc=None, now: float) -> tuple[list, dict]:
    """(wallets that may be people, {wallet: why not}) from one cheap look each, before any history is
    read: Solana, how fast its latest transactions came; EVM, how many transactions it has sent in all
    (one free batched call). A wallet that sent none is a smart-contract wallet whose trades another
    account sends: the watch couldn't follow it."""
    out = {}
    if chain.evm:
        counts = evm_rpc.nonces(wallets) if wallets else {}
        for wallet in wallets:
            if counts.get(wallet, 0) > MAX_EVM_NONCE:
                out[wallet] = f"bot-like (sent over {MAX_EVM_NONCE:,} transactions)"
            elif not counts.get(wallet):
                out[wallet] = "smart-contract wallet (its trades are sent by others)"
    else:
        count, seconds = BOT_BURST
        for wallet in wallets:
            try:
                recent = solana_rpc.signatures(wallet, limit=count)
            except Exception as exc:  # scored anyway; its history read will tell
                log.info("could not pre-screen %s: %s", wallet, exc)
                continue
            if len(recent) >= count and now - (recent[-1].get("blockTime") or now) < seconds:
                out[wallet] = f"bot-like ({count} transactions within {seconds // 3600} h)"
    return [w for w in wallets if w not in out], out


def build(chain, *, gecko, http, solana_rpc=None, evm_rpc=None, criteria: Criteria | None = None, top: int = 5,
          pools: int = 8, limit: int = 80, progress=None, now: float | None = None) -> tuple[list, list]:
    """(picked scorecards, best first, at most `top`; every scorecard made, including the wallets the
    pre-screen left out, with why)."""
    criteria = criteria or Criteria()
    progress = progress or (lambda stage, done, total: None)
    now = time.time() if now is None else now
    progress(f"Finding active {chain.name} traders", 0, 0)
    found = candidates(gecko, chain, pools=pools, limit=limit * 3)
    progress(f"Pre-screening {len(found)} {chain.name} wallets", 0, 0)
    wallets, left_out = prescreen(chain, found, solana_rpc=solana_rpc, evm_rpc=evm_rpc, now=now)
    wallets = wallets[:limit]
    native_usd = fetch_native_usd(http, chain)
    cards = []
    for i, wallet in enumerate(wallets):
        progress(f"Scoring {chain.name} wallets", i, len(wallets))
        try:
            cards.append(scorecard(chain, wallet, solana_rpc=solana_rpc, evm_rpc=evm_rpc, http=http,
                                   criteria=criteria, native_usd=native_usd, now=now, quick=True))
        except Exception as exc:  # one unreadable wallet must not stop the list
            log.info("could not score %s on %s: %s", wallet, chain.name, exc)
    progress(f"Scoring {chain.name} wallets", len(wallets), len(wallets))
    cards += [Scorecard(chain=chain.id, wallet=w, days=criteria.days, reason=why) for w, why in left_out.items()]
    picked = sorted((c for c in cards if c.reason is None), key=lambda c: -(c.pnl_usd or 0))[:top]
    return picked, cards


# --- the lists ---------------------------------------------------------------------------------------

def load(config_dir: Path | None = None) -> dict:
    """{chain id: [entry, ...]}: the shipped lists, with any refreshed in presets.local.json on top."""
    lists = {}
    for path in (SHIPPED, (config_dir or ROOT) / LOCAL_NAME):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for chain_id, entries in (data.get("chains") or {}).items():
            if chain_id in CHAINS and isinstance(entries, list):
                lists[chain_id] = [e for e in entries if isinstance(e, dict) and CHAINS[chain_id].valid(e.get("address"))]
    return lists


def entry(card: Scorecard, rank: int, today: str) -> dict:
    return {"address": card.wallet, "label": f"{CHAINS[card.chain].name} pro #{rank}",
            "pnl_usd": None if card.pnl_usd is None else round(card.pnl_usd, 2),
            "roi_pct": None if card.roi_pct is None else round(card.roi_pct, 1), "days": card.days,
            "tokens_traded": card.tokens_traded, "win_rate": round(card.win_rate, 2),
            "avg_hold_hours": None if card.avg_hold_hours is None else round(card.avg_hold_hours, 1),
            "last_trade": time.strftime("%Y-%m-%d", time.gmtime(card.last_trade)) if card.last_trade else None,
            "best_tokens": card.best_tokens, "as_of": today}


def save_local(config_dir: Path, chain_id: str, entries: list, criteria: Criteria) -> Path:
    path = config_dir / LOCAL_NAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {"version": 1, "chains": {}}
    data.setdefault("chains", {})[chain_id] = entries
    data["criteria"] = asdict(criteria)
    atomic_write(path, json.dumps(data, indent=1) + "\n")
    return path


def config_wallets(chain_id: str, entries: list) -> list[dict]:
    """traders.wallets entries for a chain's built-in traders."""
    return [{"chain": chain_id, "address": e["address"], "label": e.get("label") or f"{CHAINS[chain_id].name} pro",
             "preset": True, "pnl_usd": e.get("pnl_usd"), "roi_pct": e.get("roi_pct"), "added": e.get("as_of")}
            for e in entries]
