"""EVM chains (Ethereum, Base, BNB Chain, Arbitrum): read-only JSON-RPC.

What a wallet gained and lost in a block comes from that block's receipts (ERC-20 Transfer events,
and the wrapped coin's Deposit/Withdrawal events, which wrapping ETH/BNB emits instead of a Transfer)
and from the wallet's coin balance just before and after the block. The balance difference is exact
on every chain: it includes the gas paid and the ETH/BNB a router paid out internally after a sale,
which no event shows. Which blocks to read comes from a transfer index (see `source`):
- Ankr's ankr_getTokenTransfers (ANKR_API_KEY; free plan: 50 requests a minute) or Alchemy's
  alchemy_getAssetTransfers (ALCHEMY_API_KEY): a wallet's transfers over any period;
- without a key, the chain's public RPC: eth_getLogs filtered by the wallet, which public RPCs allow
  over a few minutes of blocks only. Enough for the trader watch on Ethereum, Base and Arbitrum, but
  not for discovery or built-in lists (they need a wallet's history), and no public BNB Chain RPC
  allows it (checked 2026-10-07).
The trader watch notices new activity when a wallet's transaction count goes up. It asks the chain's
public RPCs for the counts, so a key's quota is only spent on wallets that traded. Nothing here can
sign or send a transaction.
"""

from __future__ import annotations

import itertools
import logging
import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .rpc import HttpError, Pacer, RpcError
from .txparse import WalletChange, derive_trades
from .util import clean_text, iso_time

log = logging.getLogger("holder_watch.evm")

TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"    # Transfer(address,address,uint256)
DEPOSIT = "0xe1fffcc4923d04b559f4d29a8bfc6cda04eb5b0d3c460751c2402c5c5cc9109c"     # Deposit(address,uint256)
WITHDRAWAL = "0x7fcf532c15f0a6db0bd6d0e038bea71d30d808c7d98cb3bf7268a95bf5081b65"  # Withdrawal(address,uint256)
DECIMALS_CALL = "0x313ce567"  # decimals()
ALCHEMY_URL = "https://{network}.g.alchemy.com/v2/{key}"
ANKR_URL = "https://rpc.ankr.com/{network}/{key}"
ANKR_MULTICHAIN = "https://rpc.ankr.com/multichain/{key}"  # Advanced API
# What calls cost, to estimate usage (docs, 2026-10-07). Alchemy: compute units per method. Ankr: API
# credits, 200 per RPC request and 700 per Advanced API request.
CU = {"eth_blockNumber": 10, "eth_getBalance": 20, "eth_getTransactionCount": 20, "eth_getTransactionReceipt": 20,
      "eth_call": 26, "eth_getBlockByNumber": 20, "eth_getLogs": 60, "alchemy_getAssetTransfers": 120}
ANKR_RPC_CREDITS, ANKR_INDEX_CREDITS = 200, 700
ANKR_INDEX_INTERVAL = 1.3   # seconds between Advanced API requests: its free plan allows 50 a minute
ANKR_PAGE = 10_000          # transfers per page, its maximum
ANKR_MAX_PAGES = 5          # more transfers than this is a bot, not a trader
RETRY_CODES = frozenset({429, -32005, -32007, -32603})  # rate limited, or a node hiccup
BATCH = 50
MAX_LOG_CALLS = 60          # eth_getLogs calls for one listing: a longer period needs a transfer index
BLOCK_MARGIN = 2            # blocks a keyed node may trail the public one that gave the latest block


@dataclass(frozen=True)
class Source:
    """Where an EVM chain's data comes from. Never shows a key."""
    kind: str                 # "ankr", "alchemy", "custom" (<CHAIN>_RPC_URL) or "public" (no key)
    url: str = field(repr=False)   # JSON-RPC for receipts, balances and token decimals
    index: str = "alchemy"    # transfer index: "ankr", "alchemy" (any period) or "logs" (recent blocks)
    index_url: str | None = field(default=None, repr=False)
    label: str = ""

    @property
    def history(self) -> bool:
        """It can list a wallet's transfers over any period (discovery and built-in lists need that)."""
        return self.index != "logs"


def source(chain, environ=None) -> Source | None:
    """The best source with the keys at hand: <CHAIN>_RPC_URL, else Ankr, else Alchemy, else the
    chain's public RPC where it allows the trader watch. None: the chain can't be read."""
    environ = os.environ if environ is None else environ
    ankr = (environ.get("ANKR_API_KEY") or "").strip()
    alchemy = (environ.get("ALCHEMY_API_KEY") or "").strip()
    own = (environ.get(f"{chain.id.upper()}_RPC_URL") or "").strip()
    ankr_index = ANKR_MULTICHAIN.format(key=ankr) if ankr and chain.ankr else None
    if own:
        index = "ankr" if ankr_index else ("alchemy" if "alchemy.com" in own else "logs")
        return Source("custom", own, index, ankr_index, f"{chain.id.upper()}_RPC_URL")
    if ankr_index:
        return Source("ankr", ANKR_URL.format(network=chain.ankr, key=ankr), "ankr", ankr_index, "Ankr (ANKR_API_KEY)")
    if alchemy and chain.alchemy:
        return Source("alchemy", ALCHEMY_URL.format(network=chain.alchemy, key=alchemy), "alchemy", None,
                      "Alchemy (ALCHEMY_API_KEY)")
    if chain.log_span and chain.public_rpcs:
        return Source("public", chain.public_rpcs[0], "logs", None,
                      f"public RPC {urlsplit(chain.public_rpcs[0]).hostname}, no key")
    return None


def client(chain, http, environ=None):
    """An EvmRPC for the chain with the keys at hand, or None when it can't be read (see `source`)."""
    src = source(chain, environ)
    if src is None:
        return None
    return EvmRPC(chain, src.url, http, index=src.index, index_url=src.index_url, kind=src.kind,
                  poll_urls=chain.public_rpcs)


def _int(value) -> int | None:
    """An integer from a JSON number, a decimal string or a 0x hex string."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    try:
        text = str(value).strip()
        return int(text, 16) if text.lower().startswith("0x") else int(float(text))
    except ValueError:
        return None


@dataclass
class Block:
    """A block where a wallet's tokens moved: its transactions, the tokens and when it was made."""
    number: int
    hashes: list = field(default_factory=list)
    time: float | None = None
    tokens: set = field(default_factory=set)


class EvmRPC:
    """Read-only JSON-RPC for one EVM chain. Nothing here can sign or send a transaction.

    url: the node for receipts, balances and decimals; index: how a wallet's transfers are listed
    ("alchemy" on url, "ankr" on index_url, or "logs" on url); poll_urls: free public nodes asked first
    for the latest block and transaction counts."""

    def __init__(self, chain, url: str, http, *, index: str = "alchemy", index_url: str | None = None,
                 kind: str = "alchemy", poll_urls=()):
        self.chain = chain
        self.url = url
        self.http = http
        self.index = index
        self.index_url = index_url
        self.kind = kind
        self.poll_urls = [u for u in poll_urls if u != url] if kind != "public" else list(poll_urls)
        self.credits = 0  # estimated usage of the keyed provider: Alchemy compute units or Ankr credits
        self.pacer = Pacer(http, ANKR_INDEX_INTERVAL) if index == "ankr" else None
        self._ids = itertools.count(1)
        self._decimals: dict = {}
        self._block_seconds: float | None = None

    @property
    def history(self) -> bool:
        """It can list a wallet's transfers over any period (discovery and built-in lists need that)."""
        return self.index != "logs"

    def _cost(self, method: str, url: str) -> int:
        if url != self.url:
            return 0  # a public node
        if "alchemy.com" in url:
            return CU.get(method, 20)
        return ANKR_RPC_CREDITS if "rpc.ankr.com" in url else 0

    def call(self, method: str, params: list):
        return self.batch([(method, params)])[0]

    def batch(self, calls: list, *, url: str | None = None, attempts: int | None = None) -> list:
        """Results in the order asked; raises RpcError on the first error a node returns."""
        results = []
        for start in range(0, len(calls), BATCH):
            results += self._batch(calls[start:start + BATCH], url or self.url, attempts or self.http.attempts)
        return results

    def _poll(self, calls: list) -> tuple[list, bool]:
        """Calls any node answers (latest block, transaction counts): the public nodes first, which cost
        nothing, then the keyed one. Also whether a public node answered."""
        for url in self.poll_urls:
            try:
                return self.batch(calls, url=url, attempts=2), url != self.url
            except (HttpError, RpcError) as exc:
                log.debug("%s public RPC %s: %s", self.chain.name, urlsplit(url).hostname, self.http.redact(str(exc)))
        return self.batch(calls), False

    def _batch(self, calls: list, url: str, attempts: int) -> list:
        what = f"{self.chain.name} RPC {calls[0][0]}" + (f" (+{len(calls) - 1})" if len(calls) > 1 else "")
        for attempt in range(1, attempts + 1):
            ids = [next(self._ids) for _ in calls]
            payload = [{"jsonrpc": "2.0", "id": i, "method": m, "params": p} for i, (m, p) in zip(ids, calls)]
            self.credits += sum(self._cost(m, url) for m, _ in calls)
            resp = self.http.request("POST", url, what=what, json=payload if len(calls) > 1 else payload[0],
                                     attempts=attempts if url != self.url else None)
            try:
                body = resp.json()
            except ValueError:
                raise RpcError(calls[0][0], None, "response was not JSON") from None
            items = body if isinstance(body, list) else [body]
            by_id = {item.get("id"): item for item in items if isinstance(item, dict)}
            errors = [by_id.get(i, {}).get("error") for i in ids if "error" in by_id.get(i, {})]
            codes = {e.get("code") if isinstance(e, dict) else None for e in errors}
            if errors and codes & RETRY_CODES and attempt < attempts:
                self.http.sleep(self.http.backoff(attempt))
                continue
            if errors or len(by_id) < len(ids):
                error = errors[0] if errors else {"message": "missing results in the batch reply"}
                message = clean_text(error.get("message") if isinstance(error, dict) else error, 300)
                raise RpcError(calls[0][0], error.get("code") if isinstance(error, dict) else None, message)
            return [by_id[i].get("result") for i in ids]
        raise RpcError(calls[0][0], None, "retries exhausted")  # not reached: the loop returns or raises

    def block_number(self) -> int:
        """The latest block, from a public node when one answers (a few blocks back then, so the keyed
        node has it too)."""
        [tip], public = self._poll([("eth_blockNumber", [])])
        return int(tip, 16) - (BLOCK_MARGIN if public and self.kind != "public" else 0)

    def block_seconds(self, tip: int, span: int = 10_000) -> float:
        """The chain's average time between blocks over the last `span` blocks, measured (once per run):
        chains get faster (BNB Chain went from 3 s to under 0.5 s), so time windows don't assume it."""
        if self._block_seconds is None:
            first, last = self._poll([("eth_getBlockByNumber", [hex(max(0, tip - span)), False]),
                                      ("eth_getBlockByNumber", [hex(tip), False])])[0]
            try:
                seconds = (int(last["timestamp"], 16) - int(first["timestamp"], 16)) / min(span, tip)
            except (TypeError, KeyError, ValueError, ZeroDivisionError):
                seconds = 0
            self._block_seconds = seconds if seconds > 0 else self.chain.block_seconds
        return self._block_seconds

    def nonces(self, wallets: list) -> dict[str, int]:
        """How many transactions each wallet has sent: a rise means it did something."""
        results, _public = self._poll([("eth_getTransactionCount", [w, "latest"]) for w in wallets])
        return {w: int(r, 16) for w, r in zip(wallets, results)}

    def receipts(self, hashes) -> dict[str, dict]:
        hashes = list(dict.fromkeys(hashes))
        return {h: r for h, r in zip(hashes, self.batch([("eth_getTransactionReceipt", [h]) for h in hashes])) if r}

    def balances(self, queries, *, partial: bool = False) -> dict[tuple, int]:
        """{(wallet, block): wei} for each (wallet, block) asked. partial: leave out blocks whose state
        the node no longer keeps (BNB Chain full nodes keep only the last few minutes) instead of failing."""
        queries = list(dict.fromkeys(queries))
        try:
            results = self.batch([("eth_getBalance", [w, hex(b)]) for w, b in queries])
        except RpcError as exc:
            if not (partial and state_gone(exc)):
                raise
            found = {}
            for wallet, block in queries:  # one by one, to keep the ones the node still has
                try:
                    found[(wallet, block)] = int(self.call("eth_getBalance", [wallet, hex(block)]), 16)
                except RpcError as one:
                    if not state_gone(one):
                        raise
            log.warning("%s: the node no longer has the balances of %d block(s); trades paid in %s there show "
                        "their token side only", self.chain.name, len(queries) - len(found), self.chain.native)
            return found
        return {q: int(r, 16) for q, r in zip(queries, results)}

    def decimals(self, tokens) -> dict[str, int]:
        missing = [t for t in dict.fromkeys(tokens) if t not in self._decimals]
        if missing:
            results = []
            for token in missing:  # one by one: a token without decimals() must not fail the rest
                try:
                    results.append(self.call("eth_call", [{"to": token, "data": DECIMALS_CALL}, "latest"]))
                except RpcError:
                    results.append(None)
            for token, value in zip(missing, results):
                try:
                    self._decimals[token] = int(value, 16) if value and value != "0x" else 18
                except ValueError:
                    self._decimals[token] = 18
        return {t: self._decimals[t] for t in tokens}

    def transfers(self, wallet: str, *, from_block: int = 0, to_block: int | None = None, contracts=None,
                  limit: int = 1000) -> tuple[dict[int, Block], bool]:
        """Blocks where `wallet` sent or received ERC-20 tokens (only `contracts`, if given), oldest
        first, at most about `limit` transfers; also whether that was everything. When it wasn't, only
        the blocks listed completely are returned (at least one), so reading on from the block after
        the last one misses nothing."""
        if self.index == "ankr":
            return self._ankr_transfers(wallet, from_block, to_block, contracts, limit)
        if self.index == "logs":
            return self._log_transfers(wallet, from_block, to_block, contracts, limit)
        return self._alchemy_transfers(wallet, from_block, to_block, contracts, limit)

    def _ankr_transfers(self, wallet, from_block, to_block, contracts, limit):
        """Ankr's ankr_getTokenTransfers: both directions in one listing, up to 10,000 a page."""
        wanted = {c.lower() for c in contracts} if contracts else None
        size = ANKR_PAGE if wanted else min(ANKR_PAGE, limit + 1)  # one more than asked tells there are more
        blocks, seen, page, pages = {}, 0, None, 0
        while True:
            params = {"blockchain": self.chain.ankr, "address": [wallet], "fromBlock": from_block,
                      "toBlock": "latest" if to_block is None else to_block, "descOrder": False, "pageSize": size}
            if page:
                params["pageToken"] = page
            result = self._index_call("ankr_getTokenTransfers", params) or {}
            for item in result.get("transfers") or []:
                token, number = str(item.get("contractAddress") or "").lower(), _int(item.get("blockHeight"))
                if not token or number is None or (wanted and token not in wanted):
                    continue
                if seen >= limit:  # more than asked for: a busy wallet
                    return _trimmed(blocks, [number]), False
                block = blocks.setdefault(number, Block(number))
                tx = item.get("transactionHash")
                if tx and tx not in block.hashes:
                    block.hashes.append(tx)
                block.time = block.time or _int(item.get("timestamp"))
                block.tokens.add(token)
                decimals = _int(item.get("tokenDecimals"))
                if decimals is not None:
                    self._decimals.setdefault(token, decimals)
                seen += 1
            page, pages = result.get("nextPageToken"), pages + 1
            if not page:
                return dict(sorted(blocks.items())), True
            if pages >= ANKR_MAX_PAGES:
                return _trimmed(blocks, [max(blocks)] if blocks else []), False

    def _index_call(self, method: str, params: dict):
        """One Ankr Advanced API request, paced to its rate limit."""
        payload = {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params}
        self.credits += ANKR_INDEX_CREDITS
        resp = self.pacer.request("POST", self.index_url, what=f"{self.chain.name} Ankr {method}", json=payload)
        try:
            body = resp.json()
        except ValueError:
            raise RpcError(method, None, "response was not JSON") from None
        error = body.get("error") if isinstance(body, dict) else None
        if error:
            raise RpcError(method, error.get("code") if isinstance(error, dict) else None,
                           clean_text(error.get("message") if isinstance(error, dict) else error, 300))
        return body.get("result") if isinstance(body, dict) else None

    def _log_transfers(self, wallet, from_block, to_block, contracts, limit):
        """eth_getLogs filtered by the wallet, chain.log_span blocks per call: what public RPCs allow,
        so only for the last minutes (the trader watch)."""
        span = self.chain.log_span or 1000
        if to_block is None:
            to_block = self.block_number()
        chunks = [(start, min(to_block, start + span - 1)) for start in range(max(0, from_block), to_block + 1, span)]
        topic = "0x" + "0" * 24 + wallet[2:].lower()
        filters = [{"topics": [TRANSFER, topic]}, {"topics": [TRANSFER, None, topic]}]
        if contracts:
            filters = [{**f, "address": list(contracts)} for f in filters]
        else:  # wrapping or unwrapping ETH/BNB itself emits Deposit/Withdrawal, not Transfer
            filters.append({"address": self.chain.wrapped, "topics": [[DEPOSIT, WITHDRAWAL], topic]})
        if len(chunks) * len(filters) > MAX_LOG_CALLS:
            raise RpcError("eth_getLogs", None, f"{to_block - from_block:,} blocks is more than {self.chain.name}'s "
                           "public RPC lists; a wallet's history needs ANKR_API_KEY")
        results = self.batch([("eth_getLogs", [{**f, "fromBlock": hex(a), "toBlock": hex(b)}])
                              for a, b in chunks for f in filters])
        blocks, counts = {}, {}
        for logs in results:
            for entry in logs or []:
                topics = [t.lower() for t in entry.get("topics") or []]
                if not topics or (topics[0] == TRANSFER and len(topics) != 3):  # ERC-721 has a 4th topic
                    continue
                number = int(entry["blockNumber"], 16)
                block = blocks.setdefault(number, Block(number))
                tx = entry.get("transactionHash")
                if tx and tx not in block.hashes:
                    block.hashes.append(tx)
                block.tokens.add(entry["address"].lower())
                if entry.get("blockTimestamp") and block.time is None:
                    block.time = _int(entry["blockTimestamp"])
                counts[number] = counts.get(number, 0) + 1
        undated = [n for n, block in blocks.items() if block.time is None]
        if undated:
            for number, header in zip(undated, self.batch([("eth_getBlockByNumber", [hex(n), False]) for n in undated])):
                blocks[number].time = _int((header or {}).get("timestamp"))
        if sum(counts.values()) <= limit:
            return dict(sorted(blocks.items())), True
        kept, total = {}, 0
        for number in sorted(blocks):  # too busy: the oldest blocks up to the limit (at least one)
            total += counts[number]
            if kept and total > limit:
                break
            kept[number] = blocks[number]
        return kept, False

    def _alchemy_transfers(self, wallet, from_block, to_block, contracts, limit):
        """Alchemy's alchemy_getAssetTransfers, each direction separately, at most `limit` each."""
        blocks, complete, stops = {}, True, []
        for direction in ("fromAddress", "toAddress"):
            seen, page, number = 0, None, None
            while True:
                params = {"fromBlock": hex(from_block), "toBlock": hex(to_block) if to_block is not None else "latest",
                          direction: wallet, "category": ["erc20"], "withMetadata": True, "excludeZeroValue": True,
                          "order": "asc", "maxCount": hex(min(1000, limit - seen))}
                if contracts:
                    params["contractAddresses"] = list(contracts)
                if page:
                    params["pageKey"] = page
                result = self.call("alchemy_getAssetTransfers", [params]) or {}
                for item in result.get("transfers") or []:
                    number = int(item["blockNum"], 16)
                    block = blocks.setdefault(number, Block(number))
                    if item.get("hash") and item["hash"] not in block.hashes:
                        block.hashes.append(item["hash"])
                    block.time = block.time or iso_time((item.get("metadata") or {}).get("blockTimestamp"))
                    raw = item.get("rawContract") or {}
                    if raw.get("address"):
                        block.tokens.add(raw["address"].lower())
                        if raw.get("decimal"):
                            self._decimals.setdefault(raw["address"].lower(), int(raw["decimal"], 16))
                    seen += 1
                page = result.get("pageKey")
                if not page:
                    break
                if seen >= limit:
                    complete = False
                    if number is not None:
                        stops.append(number)  # this direction's last block may go on in the next page
                    break
        return _trimmed(blocks, stops), complete

    def trades(self, wallet: str, blocks: dict[int, Block]) -> list:
        """The wallet's trades in these blocks (see the module docstring for how they are read)."""
        if not blocks:
            return []
        receipts = self.receipts(h for block in blocks.values() for h in block.hashes)
        balances = self.balances([(wallet, b - 1) for b in blocks] + [(wallet, b) for b in blocks], partial=True)
        moved = {log_entry["address"].lower() for r in receipts.values() for log_entry in r.get("logs") or []
                 if _touches(log_entry, wallet)}
        decimals = self.decimals(sorted(moved))
        trades = []
        for number, block in sorted(blocks.items()):
            mine = [receipts[h] for h in block.hashes if h in receipts]
            change = block_change(self.chain, wallet, mine, balances.get((wallet, number - 1)),
                                  balances.get((wallet, number)), decimals)
            signer = any((r.get("from") or "").lower() == wallet for r in mine)
            first = block.hashes[0] if block.hashes else None
            for trade in derive_trades(change, self.chain, signature=first, slot=number, block_time=block.time,
                                       signer=signer):
                trade.signature = _hash_moving(mine, wallet, trade.mint, self.chain.wrapped) or first
                trades.append(trade)
        return trades


def block_change(chain, wallet: str, receipts: list, before: int | None, after: int | None,
                 decimals: dict) -> WalletChange:
    """What `wallet` gained and lost across these receipts (all from one block)."""
    deltas = token_deltas(receipts, wallet, chain.wrapped)
    fee = sum(_fee(r) for r in receipts if (r.get("from") or "").lower() == wallet)
    # Without both balances only the gas is known; trades then show their token side only.
    native = after - before if before is not None and after is not None else -fee
    return WalletChange(owner=wallet, tokens={t: [0, d, decimals.get(t, 18)] for t, d in deltas.items()},
                        native=native, fee=fee, native_before=before, native_after=after, balances_known=False)


def token_deltas(receipts: list, wallet: str, wrapped: str) -> dict[str, int]:
    """Raw token changes of `wallet` from ERC-20 Transfer events and wrapped-coin deposits/withdrawals."""
    deltas = {}
    for receipt in receipts:
        if int(receipt.get("status") or "0x1", 16) != 1:  # a failed transaction moved nothing
            continue
        for entry in receipt.get("logs") or []:
            topics = [t.lower() for t in entry.get("topics") or []]
            token = (entry.get("address") or "").lower()
            if not topics or not entry.get("data"):
                continue
            if topics[0] == TRANSFER and len(topics) == 3:  # ERC-20; ERC-721 has a 4th topic
                value = int(entry["data"][:66], 16)
                if _address(topics[1]) == wallet:
                    deltas[token] = deltas.get(token, 0) - value
                if _address(topics[2]) == wallet:
                    deltas[token] = deltas.get(token, 0) + value
            elif token == wrapped and len(topics) == 2 and topics[0] in (DEPOSIT, WITHDRAWAL) \
                    and _address(topics[1]) == wallet:
                value = int(entry["data"][:66], 16)
                deltas[token] = deltas.get(token, 0) + (value if topics[0] == DEPOSIT else -value)
    return {token: delta for token, delta in deltas.items() if delta}


def _trimmed(blocks: dict, stops: list) -> dict:
    """The blocks before the earliest block a listing stopped in (it may go on in the next page), or
    that block alone when nothing comes before it; oldest first."""
    if stops and any(n < min(stops) for n in blocks):
        blocks = {n: block for n, block in blocks.items() if n < min(stops)}
    elif stops:
        blocks = {n: block for n, block in blocks.items() if n <= min(stops)}
    return dict(sorted(blocks.items()))


def state_gone(exc: RpcError) -> bool:
    """The node pruned that block's state (a full node, not an archive node): asking again won't help."""
    message = (exc.message or "").lower()
    return any(text in message for text in ("missing trie node", "state is not available", "state not available",
                                            "historical state", "pruned"))


def _touches(entry: dict, wallet: str) -> bool:
    topics = [t.lower() for t in entry.get("topics") or []]
    return len(topics) >= 2 and topics[0] in (TRANSFER, DEPOSIT, WITHDRAWAL) and wallet in {_address(t) for t in topics[1:3]}


def _hash_moving(receipts: list, wallet: str, token: str, wrapped: str) -> str | None:
    """The transaction that moved `token` for the wallet (a block can hold several of its transactions)."""
    for receipt in receipts:
        if token in token_deltas([receipt], wallet, wrapped):
            return receipt.get("transactionHash")
    return None


def _fee(receipt: dict) -> int:
    gas = int(receipt.get("gasUsed") or "0x0", 16) * int(receipt.get("effectiveGasPrice") or "0x0", 16)
    return gas + int(receipt.get("l1Fee") or "0x0", 16)  # rollups (Base) also charge for the L1 data


def _address(topic: str) -> str:
    return "0x" + topic[-40:].lower()


def check_access(rpc: EvmRPC) -> str | None:
    """None if the keyed node (and Ankr's transfer index) answer, else why not (for setup)."""
    try:
        tip = int(rpc.call("eth_blockNumber", []), 16)
        if rpc.index == "ankr":
            rpc._index_call("ankr_getTokenTransfers", {"blockchain": rpc.chain.ankr, "address": [rpc.chain.wrapped],
                                                       "fromBlock": max(0, tip - 10), "toBlock": tip, "pageSize": 1})
    except (HttpError, RpcError) as exc:
        return str(exc)
    return None
