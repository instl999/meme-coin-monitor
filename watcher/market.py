"""Prices, liquidity, pools and token search from the DEX Screener public API (no key needed), on
every supported chain."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from urllib.parse import quote

from .chains import CHAINS, SOLANA
from .rpc import HttpError
from .util import clean_text, has_hidden_chars

# Checked 2026-10-05 against DEX Screener's API reference (docs.dexscreener.com, openapi-spec.yml):
#   GET /token-pairs/v1/{chainId}/{tokenAddress}
#   "Get the pools of a given token address (rate-limit 300 requests per minute)"
# It returns a JSON array of Pair objects. Pools where the token is the *quote* side are included
# (their priceUsd is the other token's price), so we keep only pairs whose baseToken is our mint.
# Checked 2026-10-06/07 against live responses: /tokens/v1/{chainId}/{addresses} (comma-separated, up
# to 30) returns an array with each token's main pair; /latest/dex/search?q= returns {"pairs": [...]}.
# EVM addresses come back checksummed (mixed case): they are compared in lower case.
TOKEN_PAIRS_URL = "https://api.dexscreener.com/token-pairs/v1/{chain}/{mint}"
TOKENS_URL = "https://api.dexscreener.com/tokens/v1/{chain}/{mints}"
SEARCH_URL = "https://api.dexscreener.com/latest/dex/search?q={query}"
TOKENS_PER_REQUEST = 30
BY_DEXSCREENER_ID = {chain.dexscreener: chain for chain in CHAINS.values()}


@dataclass(frozen=True)
class Market:
    found: bool
    price_usd: float | None = None
    liquidity_usd: float | None = None
    venue: str | None = None          # e.g. "Raydium CPMM CYBERLEEK/SOL"
    pair_address: str | None = None
    url: str | None = None
    symbol: str | None = None
    name: str | None = None
    price_change_h24: float | None = None
    pool_count: int = 0               # pools on the chain with our token as base
    pair_addresses: frozenset = frozenset()  # every pool address seen (used to recognise pool wallets)
    name_had_hidden_chars: bool = False
    price_native: float | None = None  # price in the quote token of the deepest pool
    quote_mint: str | None = None


@dataclass(frozen=True)
class Pool:
    address: str
    venue: str
    liquidity_usd: float | None
    volume_h24: float
    txns_h24: int
    created: float | None  # unix seconds


@dataclass(frozen=True)
class TokenInfo:
    """A token's main pool, for alerts about tokens the watched traders buy and sell."""
    mint: str
    symbol: str | None
    name: str | None
    price_usd: float | None
    liquidity_usd: float | None
    market_cap: float | None
    created: float | None  # when the pool opened (unix seconds)
    url: str | None
    venue: str
    hidden_chars: bool = False


@dataclass
class TokenMatch:
    """One token in search results, summed over its pools."""
    mint: str
    symbol: str | None
    name: str | None
    chain: str = "solana"
    pools: int = 0
    liquidity_usd: float = 0.0
    volume_h24: float = 0.0
    txns_h24: int = 0
    created: float | None = None  # first pool opened
    url: str | None = None
    hidden_chars: bool = False
    flags: list = field(default_factory=list)


def fetch_pairs(http, mint: str, chain=SOLANA) -> list:
    resp = http.request("GET", TOKEN_PAIRS_URL.format(chain=chain.dexscreener, mint=mint), what="DEX Screener token-pairs")
    return _pairs(resp, "DEX Screener token-pairs")


def fetch_market(http, mint: str, chain=SOLANA) -> Market:
    return select_market(fetch_pairs(http, mint, chain), mint, chain)


def select_market(pairs: list, mint: str, chain=SOLANA) -> Market:
    """Pick the deepest pool on the chain where baseToken is the mint."""
    here = [p for p in pairs if isinstance(p, dict) and p.get("chainId") == chain.dexscreener]
    addresses = frozenset(chain.normalize(p["pairAddress"]) for p in here if isinstance(p.get("pairAddress"), str))
    ours = _ours(here, mint, chain)
    best = _best(ours)
    if best is None:
        return Market(found=False, pair_addresses=addresses)
    base, quote_token = best.get("baseToken") or {}, best.get("quoteToken") or {}
    symbol = clean_text(base.get("symbol"), 20) or None
    quote_mint = quote_token.get("address")
    return Market(
        found=True,
        price_usd=_number(best.get("priceUsd")),
        liquidity_usd=_liquidity(best),
        venue=_venue(best),
        pair_address=best.get("pairAddress"),
        url=best.get("url"),
        symbol=symbol,
        name=clean_text(base.get("name"), 40) or None,
        price_change_h24=_number((best.get("priceChange") or {}).get("h24")),
        pool_count=len(ours),
        pair_addresses=addresses,
        name_had_hidden_chars=has_hidden_chars(base.get("name")) or has_hidden_chars(base.get("symbol")),
        price_native=_number(best.get("priceNative")),
        quote_mint=chain.normalize(quote_mint) if isinstance(quote_mint, str) else None,
    )


def token_pools(pairs: list, mint: str, chain=SOLANA) -> list[Pool]:
    """The token's pools on the chain (as base token), busiest first."""
    pools = [Pool(address=p["pairAddress"], venue=_venue(p), liquidity_usd=_liquidity(p), volume_h24=_volume(p),
                  txns_h24=_txns(p), created=_created(p))
             for p in pairs if isinstance(p, dict) and p.get("chainId") == chain.dexscreener
             and _same(chain, (p.get("baseToken") or {}).get("address"), mint) and isinstance(p.get("pairAddress"), str)]
    return sorted(pools, key=lambda pool: (-pool.volume_h24, -(pool.liquidity_usd or 0)))


def fetch_tokens(http, mints, chain=SOLANA) -> dict[str, TokenInfo]:
    """Main pool of each token (up to 30 per request), keyed as given. Tokens DEX Screener doesn't list
    are left out."""
    mints = list(dict.fromkeys(mints))
    found = {}
    for start in range(0, len(mints), TOKENS_PER_REQUEST):
        chunk = mints[start:start + TOKENS_PER_REQUEST]
        url = TOKENS_URL.format(chain=chain.dexscreener, mints=",".join(chunk))
        pairs = [p for p in _pairs(http.request("GET", url, what="DEX Screener tokens"), "DEX Screener tokens")
                 if isinstance(p, dict) and p.get("chainId") == chain.dexscreener]
        for mint in chunk:
            best = _best(_ours(pairs, mint, chain))
            if best is not None:
                base = best.get("baseToken") or {}
                found[mint] = TokenInfo(
                    mint=mint, symbol=clean_text(base.get("symbol"), 20) or None,
                    name=clean_text(base.get("name"), 40) or None, price_usd=_number(best.get("priceUsd")),
                    liquidity_usd=_liquidity(best),
                    market_cap=_number(best.get("marketCap")) or _number(best.get("fdv")),
                    created=_created(best), url=best.get("url"), venue=_venue(best),
                    hidden_chars=has_hidden_chars(base.get("name")) or has_hidden_chars(base.get("symbol")))
    return found


def fetch_native_usd(http, chain=SOLANA) -> float | None:
    """The USD price of the chain's coin (SOL, ETH, BNB), from its wrapped token."""
    info = fetch_tokens(http, [chain.wrapped], chain).get(chain.wrapped)
    return info.price_usd if info else None


def search_tokens(http, query: str, *, now: float | None = None, chains=None) -> list[TokenMatch]:
    """Tokens on the supported chains (or only `chains`) matching a name or symbol, busiest first,
    with warnings about look-alikes."""
    now = time.time() if now is None else now
    wanted = {CHAINS[c].dexscreener for c in chains} if chains else set(BY_DEXSCREENER_ID)
    url = SEARCH_URL.format(query=quote(query.strip()))
    pairs = _pairs(http.request("GET", url, what="DEX Screener search"), "DEX Screener search")
    tokens: dict[tuple, TokenMatch] = {}
    for p in pairs:
        if not isinstance(p, dict) or p.get("chainId") not in wanted:
            continue
        chain = BY_DEXSCREENER_ID[p["chainId"]]
        base = p.get("baseToken") or {}
        mint = base.get("address")
        if not isinstance(mint, str):
            continue
        mint = chain.normalize(mint)
        match = tokens.get((chain.id, mint))
        if match is None:
            match = tokens[(chain.id, mint)] = TokenMatch(
                mint=mint, symbol=clean_text(base.get("symbol"), 20) or None,
                name=clean_text(base.get("name"), 40) or None, chain=chain.id, url=p.get("url"))
        match.pools += 1
        match.liquidity_usd += _liquidity(p) or 0.0
        match.volume_h24 += _volume(p)
        match.txns_h24 += _txns(p)
        created = _created(p)
        if created and (match.created is None or created < match.created):
            match.created = created
        match.hidden_chars = match.hidden_chars or has_hidden_chars(base.get("name")) or has_hidden_chars(base.get("symbol"))
    for match in tokens.values():
        if match.hidden_chars:
            match.flags.append("hidden text-direction characters in its name")
        if match.liquidity_usd > 50_000 and match.volume_h24 < match.liquidity_usd / 1000:
            match.flags.append("reported liquidity far above its trading volume")
        if match.volume_h24 == 0:
            match.flags.append("no trades in 24 h")
        if match.created and now - match.created < 86_400:
            match.flags.append("first pool opened less than 24 h ago")
    return sorted(tokens.values(), key=lambda m: (-m.volume_h24, -m.liquidity_usd))


def _pairs(resp, what: str) -> list:
    try:
        data = resp.json()
    except ValueError:
        raise HttpError(what, "response was not JSON") from None
    if isinstance(data, dict):  # search and the older /latest/dex endpoints wrap pairs as {"pairs": [...]}
        data = data.get("pairs") or []
    if not isinstance(data, list):
        raise HttpError(what, f"unexpected response type {type(data).__name__}")
    return data


def _same(chain, address, mint) -> bool:
    return isinstance(address, str) and chain.normalize(address) == chain.normalize(mint)


def _ours(pairs: list, mint: str, chain=SOLANA) -> list:
    return [p for p in pairs if isinstance(p, dict) and p.get("chainId") == chain.dexscreener
            and _same(chain, (p.get("baseToken") or {}).get("address"), mint) and _number(p.get("priceUsd")) is not None]


def _best(pairs: list) -> dict | None:
    return max(pairs, key=lambda p: (_liquidity(p) or 0.0, _volume(p)), default=None)


def _venue(pair: dict) -> str:
    dex = " ".join([str(pair.get("dexId") or "?").title(), *(str(label) for label in pair.get("labels") or [])])
    base = clean_text((pair.get("baseToken") or {}).get("symbol"), 20) or "?"
    quote_symbol = clean_text((pair.get("quoteToken") or {}).get("symbol"), 20) or "?"
    return f"{dex} {base}/{quote_symbol}"


def _number(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _liquidity(pair: dict) -> float | None:
    return _number((pair.get("liquidity") or {}).get("usd"))


def _volume(pair: dict) -> float:
    return _number((pair.get("volume") or {}).get("h24")) or 0.0


def _txns(pair: dict) -> int:
    day = (pair.get("txns") or {}).get("h24") or {}
    return int((_number(day.get("buys")) or 0) + (_number(day.get("sells")) or 0))


def _created(pair: dict) -> float | None:
    ms = _number(pair.get("pairCreatedAt"))
    return ms / 1000 if ms else None
