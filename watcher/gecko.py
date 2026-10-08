"""Recent trades and busy pools from the GeckoTerminal public API (no key).

Used to find candidate traders on any chain: each pool's latest 300 trades (24 h) name the wallet
that sent each trade. Checked 2026-10-07 against live responses: /networks/{network}/pools/{pool}/trades
returns {"data": [{"attributes": {"tx_from_address", "kind", "volume_in_usd", ...}}]} and
/networks/{network}/pools?sort=h24_volume_usd_desc returns the busiest pools, 20 per page.

Rate limit, measured the same day: 6 requests a minute (the docs say 30), cached replies included.
A 7th gets HTTP 429 with "Retry-After: 0", so requests are spaced 10.5 s apart and a 429 waits out
the minute.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .rpc import HttpError, Pacer
from .util import iso_time

API = "https://api.geckoterminal.com/api/v2"
MIN_INTERVAL = 10.5  # seconds between requests: stays under 6 a minute


@dataclass(frozen=True)
class PoolTrade:
    trader: str          # the wallet that sent the trade
    kind: str            # "buy" or "sell" of the pool's base token
    volume_usd: float
    tx: str
    block: int
    time: float | None


@dataclass(frozen=True)
class BusyPool:
    address: str
    name: str
    base_token: str      # the token traded, as an address on the chain
    quote_token: str
    volume_h24: float
    base_price_usd: float | None = None
    price_in_quote: float | None = None  # the base token's price in the quote token
    price_in_native: float | None = None  # ... and in the chain's coin (ETH, BNB, SOL)
    change_h24: float | None = None      # % price change over 24 h


class Gecko:
    def __init__(self, http, *, clock=time.monotonic):
        self.http = http
        self.pacer = Pacer(http, MIN_INTERVAL, clock=clock)

    def _get(self, path: str, what: str) -> dict:
        resp = self.pacer.request("GET", f"{API}{path}", what=what)
        try:
            data = resp.json()
        except ValueError:
            raise HttpError(what, "response was not JSON") from None
        if not isinstance(data, dict) or not isinstance(data.get("data"), list):
            raise HttpError(what, "unexpected response")
        return data

    def trades(self, chain, pool: str, *, min_usd: float | None = None) -> list[PoolTrade]:
        """The pool's latest trades (up to 300, last 24 h), only those of at least min_usd if given: in a
        busy pool the latest 300 cover minutes, most of them dust from bots; $100 and up cover hours."""
        query = f"?trade_volume_in_usd_greater_than={min_usd:g}" if min_usd else ""
        data = self._get(f"/networks/{chain.geckoterminal}/pools/{pool}/trades{query}", "GeckoTerminal trades")
        trades = []
        for item in data["data"]:
            a = (item.get("attributes") or {}) if isinstance(item, dict) else {}
            trader, kind = a.get("tx_from_address"), a.get("kind")
            if not isinstance(trader, str) or kind not in ("buy", "sell"):
                continue
            trades.append(PoolTrade(trader=chain.normalize(trader), kind=kind, volume_usd=_float(a.get("volume_in_usd")),
                                    tx=str(a.get("tx_hash") or ""), block=int(a.get("block_number") or 0),
                                    time=iso_time(a.get("block_timestamp"))))
        return trades

    def busy_pools(self, chain, pages: int = 2) -> list[BusyPool]:
        """The chain's pools with the most 24 h volume (on Solana mostly wash-traded: see trending_pools)."""
        pools = []
        for page in range(1, pages + 1):
            pools += self._pools(chain, f"/networks/{chain.geckoterminal}/pools?sort=h24_volume_usd_desc&page={page}")
        return pools

    def trending_pools(self, chain) -> list[BusyPool]:
        """GeckoTerminal's trending pools of the last 24 h: where people trade (checked 2026-10-07: their
        $100+ trades came from 125-150 different wallets per pool, the busiest pools' from a few bots)."""
        return self._pools(chain, f"/networks/{chain.geckoterminal}/trending_pools?duration=24h")

    def _pools(self, chain, path: str) -> list[BusyPool]:
        pools = []
        for item in self._get(path, "GeckoTerminal pools")["data"]:
            a = (item.get("attributes") or {}) if isinstance(item, dict) else {}
            rel = (item.get("relationships") or {}) if isinstance(item, dict) else {}
            base = ((rel.get("base_token") or {}).get("data") or {}).get("id", "")
            quote_token = ((rel.get("quote_token") or {}).get("data") or {}).get("id", "")
            if not a.get("address") or not base:
                continue
            pools.append(BusyPool(address=a["address"], name=str(a.get("name") or "?"),
                                  base_token=chain.normalize(base.rpartition("_")[2]),
                                  quote_token=chain.normalize(quote_token.rpartition("_")[2]),
                                  volume_h24=_float((a.get("volume_usd") or {}).get("h24")),
                                  base_price_usd=_float(a.get("base_token_price_usd"), None),
                                  price_in_quote=_float(a.get("base_token_price_quote_token"), None),
                                  price_in_native=_float(a.get("base_token_price_native_currency"), None),
                                  change_h24=_float((a.get("price_change_percentage") or {}).get("h24"), None)))
        return pools


def _float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
