"""Test doubles: an in-memory Solana RPC, DEX Screener and Telegram, routed by URL like requests.Session."""

from __future__ import annotations

import copy
import hashlib
import html
import json
import re
from urllib.parse import parse_qsl

import requests

from watcher.alerts import Notifier, TelegramSender
from watcher.chains import CHAINS
from watcher.config import load_config
from watcher.evm import BLOCK_MARGIN, TRANSFER, client as evm_client
from watcher.monitor import Monitor
from watcher.rpc import HttpClient, SolanaRPC
from watcher.state import StateStore, TraderStateStore
from watcher.traders import TraderWatch
from watcher.util import Redactor, b58encode

RPC_URL = "https://rpc.test/"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
CPMM_AUTHORITY = "GpMZbSM2GgvTKHJirzeGfMFoaZ8UR2X7F4v8vHTvxFbL"
RAYDIUM_CPMM = "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C"
METEORA_DLMM = "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo"
JUPITER = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"
WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
TELEGRAM_TOKEN = "123456789:TEST_telegram_token_abcdefghijk"
TELEGRAM_ENV = {"TELEGRAM_BOT_TOKEN": TELEGRAM_TOKEN, "TELEGRAM_CHAT_ID": "4242"}
DECIMALS = 6
UNIT = 10**DECIMALS
SOL = 10**9


def addr(name: str) -> str:
    """A deterministic, valid Solana address for a test name."""
    return b58encode(hashlib.sha256(name.encode()).digest())


def plain(message_html: str) -> str:
    """A Telegram HTML message as the text the reader sees."""
    return html.unescape(re.sub(r"<[^>]+>", "", message_html))


MINT = addr("mint")


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        if isinstance(self._payload, str):
            return json.loads(self._payload)  # raises ValueError for text that isn't JSON
        return copy.deepcopy(self._payload)


def pair(mint, price, liquidity, *, dex="raydium", labels=("CPMM",), base=True, chain_id="solana",
         pair_address=None, symbol="TEST", name="Test Token", price_native="0.00001", volume=1000,
         created_ms=1, quote=None, market_cap=1):
    token = {"address": mint, "name": name, "symbol": symbol}
    other = quote or {"address": WSOL, "name": "Wrapped SOL", "symbol": "SOL"}
    address = pair_address or addr(f"pair:{dex}:{price}:{liquidity}:{base}:{mint}")
    return {
        "chainId": chain_id, "dexId": dex, "url": f"https://dexscreener.com/{chain_id}/{address}",
        "pairAddress": address, "labels": list(labels),
        "baseToken": token if base else other, "quoteToken": other if base else token,
        "priceNative": price_native, "priceUsd": None if price is None else str(price),
        "liquidity": None if liquidity is None else {"usd": liquidity, "base": 1, "quote": 1},
        "volume": {"h24": volume}, "priceChange": {"h24": 1.5}, "txns": {"h24": {"buys": 10, "sells": 5}},
        "fdv": market_cap, "marketCap": market_cap, "pairCreatedAt": created_ms,
    }


def sol_pair(price_usd=100.0):
    """SOL/USDC, where DEX Screener's price for SOL comes from."""
    return pair(WSOL, price_usd, 30_000_000, dex="orca", labels=("wp",), symbol="SOL", name="Wrapped SOL",
                price_native=str(price_usd), quote={"address": USDC, "name": "USD Coin", "symbol": "USDC"})


def _token_balance(index, owner, mint, amount, decimals):
    return {"accountIndex": index, "mint": mint, "owner": owner, "programId": TOKEN_PROGRAM,
            "uiTokenAmount": {"amount": str(amount), "decimals": decimals,
                              "uiAmount": amount / 10**decimals, "uiAmountString": str(amount / 10**decimals)}}


def make_tx(signature, *, fee_payer, token_moves=(), lamports=None, programs=(), logs=(), block_time=0, fee=5000,
            err=None):
    """A jsonParsed transaction. token_moves: (owner, mint, raw before or None, raw after or None, decimals[,
    token account address]). lamports: {address: (before, after)}; the fee payer's default pays just the fee."""
    lamports = dict(lamports or {})
    lamports.setdefault(fee_payer, (10 * 10**9, 10 * 10**9 - fee))
    keys = [fee_payer] + [k for k in lamports if k != fee_payer]
    pre, post = [lamports[k][0] for k in keys], [lamports[k][1] for k in keys]
    pre_tokens, post_tokens = [], []
    for i, move in enumerate(token_moves):
        owner, mint, before, after, decimals = move[:5]
        keys.append(move[5] if len(move) > 5 else addr(f"{signature}:token-account:{i}"))
        pre.append(2_039_280)
        post.append(2_039_280)
        if before is not None:
            pre_tokens.append(_token_balance(len(keys) - 1, owner, mint, before, decimals))
        if after is not None:
            post_tokens.append(_token_balance(len(keys) - 1, owner, mint, after, decimals))
    for program in programs:
        keys.append(program)
        pre.append(1)
        post.append(1)
    return {
        "blockTime": block_time, "slot": 1000, "version": 0,
        "meta": {"err": err, "fee": fee, "preBalances": pre, "postBalances": post,
                 "preTokenBalances": pre_tokens, "postTokenBalances": post_tokens,
                 "innerInstructions": [], "logMessages": list(logs)},
        "transaction": {
            "signatures": [signature],
            "message": {"accountKeys": [{"pubkey": k, "signer": k == fee_payer, "writable": True,
                                         "source": "transaction"} for k in keys],
                        "instructions": [{"programId": p, "accounts": [], "data": ""} for p in programs]},
        },
    }


def ata(owner, mint=MINT) -> str:
    """A stand-in for the owner's token account for `mint`."""
    return addr(f"ata:{owner}:{mint}")


def swap_tx(signature, trader, mint, *, side, tokens, sol, before=0, block_time=0, decimals=DECIMALS,
            pool_owner=CPMM_AUTHORITY, signer=True, fee=5000, programs=(RAYDIUM_CPMM,)):
    """`trader` buys or sells `tokens` of `mint` for `sol` SOL through a pool. `before`: its balance before."""
    unit = 10**decimals
    raw, lamports, before_raw = int(tokens * unit), int(sol * SOL), int(before * unit)
    sign = 1 if side == "buy" else -1
    after_raw = before_raw + sign * raw
    start = 100 * SOL
    moves = [(trader, mint, before_raw or None, after_raw or None, decimals, ata(trader, mint)),
             (pool_owner, mint, 10**15, 10**15 - sign * raw, decimals, ata(pool_owner, mint))]
    return make_tx(signature, fee_payer=trader if signer else addr("relayer"), token_moves=moves,
                   lamports={trader: (start, start - sign * lamports - (fee if signer else 0))},
                   programs=programs, block_time=block_time, fee=fee)


def sol_for_usd_tx(signature, trader, *, side, sol, usd, block_time=0, fee=5000):
    """`trader` sells `sol` SOL for `usd` USDC through Jupiter, or buys it with them."""
    lamports, raw_usd = int(sol * SOL), int(usd * 10**6)
    sign = -1 if side == "sell" else 1  # the trader's SOL change
    start, held = 100 * SOL, 1_000 * 10**6
    moves = [(trader, USDC, held, held - sign * raw_usd, 6, ata(trader, USDC)),
             (CPMM_AUTHORITY, USDC, 10**15, 10**15 + sign * raw_usd, 6, ata(CPMM_AUTHORITY, USDC))]
    return make_tx(signature, fee_payer=trader, token_moves=moves, programs=[JUPITER], block_time=block_time,
                   fee=fee, lamports={trader: (start, start + sign * lamports - fee)})


def transfer_tx(signature, sender, recipient, mint, tokens, *, sender_before, recipient_before=0, block_time=0,
                decimals=DECIMALS):
    unit = 10**decimals
    raw, sent_from, held = int(tokens * unit), int(sender_before * unit), int(recipient_before * unit)
    moves = [(sender, mint, sent_from, (sent_from - raw) or None, decimals, ata(sender, mint)),
             (recipient, mint, held or None, held + raw, decimals, ata(recipient, mint))]
    return make_tx(signature, fee_payer=sender, token_moves=moves, block_time=block_time)


def evm_addr(name: str) -> str:
    """A deterministic EVM address for a test name."""
    return "0x" + hashlib.sha256(name.encode()).hexdigest()[:40]


USDC_ON = {"ethereum": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48", "base": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
           "bsc": "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d", "arbitrum": "0xaf88d065e77c8cc2239327c5edb3a432268e5831"}


def topic(address: str) -> str:
    """An address as an indexed event topic."""
    return "0x" + "0" * 24 + address[2:]


def event(contract, signature_topic, *addresses, value):
    return {"address": contract, "topics": [signature_topic, *(topic(a) for a in addresses)],
            "data": "0x" + format(value, "064x")}


class FakeEvm:
    """One EVM chain in memory, answering the JSON-RPC calls the monitor makes: a node's, Alchemy's
    transfer index and Ankr's (one fake behind every URL of the chain)."""

    def __init__(self, chain):
        self.chain = chain
        self.tip = 1_000
        self.nonces = {}       # wallet -> transactions sent
        self.balances = {}     # wallet -> [(block, wei)], the balance from that block on
        self.receipts = {}     # tx hash -> receipt
        self.transfers = []    # Alchemy erc20 transfer records (+ "_ts": unix time, for Ankr's)
        self.decimals = {}     # token -> decimals (eth_call decimals())
        self.lagging = set()   # tx hashes the transfer indexes don't list yet
        self.pruned_before = None  # balances of older blocks fail like a full node's ("missing trie node")
        self.errors = {}       # method -> queued JSON-RPC errors
        self.disabled = False  # the network is off in the Alchemy app (HTTP 403)
        self.log_timestamps = True  # logs carry blockTimestamp, as newer nodes add
        self.calls = []

    def balance_at(self, wallet: str, block: int) -> int:
        value = 0
        for at, wei in self.balances.get(wallet, [(0, 10 * 10**18)]):
            if at <= block:
                value = wei
        return value

    def tx(self, tx_hash, wallet, *, block, moves=(), eth=0.0, fee_wei=21_000 * 10**9, time=1_790_000_000,
           sent=True):
        """A transaction in `block`. moves: (token, whole tokens, True if the wallet receives them) against
        a pool; eth: the wallet's ETH/BNB change besides gas (negative: paid). sent=False: another wallet
        sent it (a relayer), so the wallet's transaction count doesn't move."""
        logs, pool = [], evm_addr("pool")
        for token, amount, wallet_gets in moves:
            decimals = self.decimals.setdefault(token, 6 if token in self.chain.stables else 18)
            raw = int(round(amount * 10**decimals))
            src, dst = (pool, wallet) if wallet_gets else (wallet, pool)
            logs.append({**event(token, TRANSFER, src, dst, value=raw), "blockNumber": hex(block),
                         "transactionHash": tx_hash, "logIndex": hex(len(logs)), "_ts": time})
            self.transfers.append({"blockNum": hex(block), "hash": tx_hash, "from": src, "to": dst, "category": "erc20",
                                   "rawContract": {"address": token, "value": hex(raw), "decimal": hex(decimals)},
                                   "metadata": {"blockTimestamp": _iso(time)}, "_ts": time})
        change = int(round(eth * 10**18)) - (fee_wei if sent else 0)
        history = self.balances.setdefault(wallet, [(0, 10 * 10**18)])
        history.append((block, self.balance_at(wallet, block) + change))  # after any earlier ones in the block
        self.receipts[tx_hash] = {"transactionHash": tx_hash, "blockNumber": hex(block), "status": "0x1",
                                  "from": wallet if sent else evm_addr("relayer"), "to": evm_addr("router"),
                                  "gasUsed": hex(21_000), "effectiveGasPrice": hex(fee_wei // 21_000), "logs": logs}
        if sent:
            self.nonces[wallet] = self.nonces.get(wallet, 0) + 1
        self.tip = max(self.tip, block + BLOCK_MARGIN)  # blocks keep coming after it

    def swap(self, tx_hash, wallet, *, block, token, side, tokens, eth=0.0, weth=0.0, usd=0.0, **kwargs):
        """`wallet` buys (or sells) `tokens` of `token`, paying (or getting) ETH/BNB, the wrapped coin or USDC."""
        buy = side == "buy"
        moves = [(token, tokens, buy)]
        if weth:
            moves.append((self.chain.wrapped, weth, not buy))
        if usd:
            moves.append((USDC_ON[self.chain.id], usd, not buy))
        self.tx(tx_hash, wallet, block=block, moves=moves, eth=-eth if buy else eth, **kwargs)

    def handle(self, payload):
        if isinstance(payload, list):
            return [self._one(item) for item in payload]
        return self._one(payload)

    def _one(self, item):
        method, params = item["method"], item.get("params", [])
        self.calls.append(method)
        if self.errors.get(method):
            return {"jsonrpc": "2.0", "id": item["id"], "error": self.errors[method].pop(0)}
        if method == "eth_getBalance" and self.pruned_before is not None and int(params[1], 16) < self.pruned_before:
            return {"jsonrpc": "2.0", "id": item["id"],
                    "error": {"code": -32000, "message": "missing trie node 5f3c0e7a (path ) <nil>"}}
        result = getattr(self, "_" + method)(*(params if isinstance(params, list) else [params]))
        return {"jsonrpc": "2.0", "id": item["id"], "result": result}

    def _eth_getLogs(self, query):
        low, high = int(query["fromBlock"], 16), int(query["toBlock"], 16)
        addresses = query.get("address")
        addresses = None if addresses is None else {a.lower() for a in ([addresses] if isinstance(addresses, str) else addresses)}
        found = []
        for receipt in self.receipts.values():
            for entry in receipt["logs"]:
                block = int(entry["blockNumber"], 16)
                if not low <= block <= high or (addresses and entry["address"] not in addresses):
                    continue
                wanted = query.get("topics") or []
                if all(want is None or (entry["topics"][i] in want if isinstance(want, list) else entry["topics"][i] == want)
                       for i, want in enumerate(wanted) if i < len(entry["topics"])) and len(wanted) <= len(entry["topics"]):
                    out = {k: v for k, v in entry.items() if k != "_ts"}
                    if self.log_timestamps:
                        out["blockTimestamp"] = hex(entry["_ts"])
                    found.append(out)
        return copy.deepcopy(sorted(found, key=lambda e: int(e["blockNumber"], 16)))

    def _ankr_getTokenTransfers(self, params):
        """Ankr's Token API: transfers to or from the address, both directions in one listing."""
        wallet = params["address"][0]
        low = int(params.get("fromBlock") or 0)
        high = self.tip if params.get("toBlock") in (None, "latest") else int(params["toBlock"])
        items = [t for t in self.transfers if t["hash"] not in self.lagging and low <= int(t["blockNum"], 16) <= high
                 and wallet in (t["from"], t["to"])]
        start, size = int(params.get("pageToken") or 0), int(params.get("pageSize") or 10_000)
        page = [{"blockHeight": int(t["blockNum"], 16), "blockchain": self.chain.ankr,
                 "contractAddress": t["rawContract"]["address"], "fromAddress": t["from"], "toAddress": t["to"],
                 "transactionHash": t["hash"], "timestamp": t["_ts"], "tokenDecimals": int(t["rawContract"]["decimal"], 16),
                 "valueRawInteger": str(int(t["rawContract"]["value"], 16))} for t in items[start:start + size]]
        result = {"transfers": page}
        if start + size < len(items):
            result["nextPageToken"] = str(start + size)
        return result

    def _eth_blockNumber(self):
        return hex(self.tip)

    def _eth_getTransactionCount(self, wallet, _block):
        return hex(self.nonces.get(wallet, 0))

    def _eth_getTransactionReceipt(self, tx_hash):
        return copy.deepcopy(self.receipts.get(tx_hash))

    def _eth_getBalance(self, wallet, block):
        return hex(self.balance_at(wallet, int(block, 16)))

    def _eth_call(self, call, _block):
        return hex(self.decimals.get(call["to"], 18))

    def _eth_getBlockByNumber(self, block, _full):
        """Blocks come every chain.block_seconds; block 1000 was made at 1_790_000_000."""
        number = int(block, 16)
        return {"number": block, "timestamp": hex(int(1_790_000_000 + (number - 1000) * self.chain.block_seconds))}

    def _alchemy_getAssetTransfers(self, options):
        low, high = int(options.get("fromBlock", "0x0"), 16), options.get("toBlock", "latest")
        high = self.tip if high == "latest" else int(high, 16)
        contracts = set(options.get("contractAddresses") or [])
        items = [t for t in self.transfers if t["hash"] not in self.lagging and low <= int(t["blockNum"], 16) <= high
                 and (not contracts or t["rawContract"]["address"] in contracts)
                 and (t["from"] == options.get("fromAddress") or t["to"] == options.get("toAddress"))]
        start, count = int(options.get("pageKey") or 0), int(options.get("maxCount", "0x3e8"), 16)
        page = items[start:start + count]
        result = {"transfers": copy.deepcopy(page)}
        if start + count < len(items):
            result["pageKey"] = str(start + count)
        return result


def _iso(ts: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


ALCHEMY_ENV = {"ALCHEMY_API_KEY": "alchemy-test-key-0123456789"}
ANKR_ENV = {"ANKR_API_KEY": "ankr-test-key-0123456789abcdef"}


def native_pair(chain, price_usd):
    """The chain's wrapped coin against a stablecoin, where its USD price comes from."""
    stable = sorted(chain.stables)[0]
    return pair(chain.wrapped, price_usd, 50_000_000, dex="uniswap", labels=("v3",), chain_id=chain.dexscreener,
                symbol=chain.native, name=f"Wrapped {chain.native}", price_native=str(price_usd),
                pair_address=evm_addr(f"native-pool:{chain.id}"), quote={"address": stable, "symbol": "USDC"})


def gecko_pool(chain, address, base_token, quote_token, *, volume=1_000_000, name="TEST / WETH", price_usd=0.012,
               price_in_quote=0.000004, change_h24=-12.5):
    """A GeckoTerminal /pools item (by default a meme token against the wrapped coin)."""
    net = chain.geckoterminal
    return {"id": f"{net}_{address}", "type": "pool",
            "attributes": {"address": address, "name": name, "volume_usd": {"h24": str(volume)},
                           "base_token_price_usd": str(price_usd), "base_token_price_quote_token": str(price_in_quote),
                           "price_change_percentage": {"h24": str(change_h24)}},
            "relationships": {"base_token": {"data": {"id": f"{net}_{base_token}", "type": "token"}},
                              "quote_token": {"data": {"id": f"{net}_{quote_token}", "type": "token"}}}}


def gecko_trade(trader, kind="buy", *, volume=500.0, tx="0x1", block=1000, at="2026-09-21T14:00:00Z"):
    """A GeckoTerminal /trades item."""
    return {"type": "trade", "attributes": {"tx_from_address": trader, "kind": kind, "volume_in_usd": str(volume),
                                            "tx_hash": tx, "block_number": block, "block_timestamp": at}}


def evm_pair(chain, token, price_usd, *, liquidity=500_000, market_cap=5_000_000, symbol="TEST", name="Test Token"):
    """The token's pool against the wrapped coin (worth $3,000, as in FakeChain.evm_chain)."""
    return pair(token, price_usd, liquidity, dex="uniswap", labels=("v2",), chain_id=chain.dexscreener, symbol=symbol,
                name=name, pair_address=evm_addr(f"pool:{token}"), market_cap=market_cap,
                price_native=f"{price_usd / 3_000:.12f}",
                quote={"address": chain.wrapped, "name": f"Wrapped {chain.native}", "symbol": chain.native})


class FakeChain:
    """Solana RPC + DEX Screener + Telegram in memory. Amounts passed to hold()/set_balance() are whole tokens."""

    def __init__(self, mint=MINT, supply_tokens=1_000_000_000):
        self.mint = mint
        self.supply = supply_tokens * UNIT
        self.token_accounts = {}  # address -> {"owner", "amount" (raw), "program"}
        self.programs = {}        # non-token address -> owning program (pool accounts etc.)
        self.signatures = {}      # address -> [signature info], newest first
        self.transactions = {}    # signature -> transaction
        self.pairs = []
        self.token_pairs = {WSOL: [sol_pair()]}  # other tokens' pools, served by /tokens/v1
        self.search_results = []  # pairs returned by /latest/dex/search
        self.telegram = []        # texts sent to Telegram
        self.telegram_chats = []  # the chat each text went to
        self.bot_tokens = {TELEGRAM_TOKEN}  # tokens Telegram accepts
        self.updates = []         # what getUpdates returns (filtered by offset)
        self.incoming = []        # updates that "arrive" during the next long poll
        self.webhook = False      # a webhook makes getUpdates fail with 409
        self.rpc_down = False
        self.rpc_errors = {}      # method -> queued failures: HTTP status (int) or JSON-RPC error (dict)
        self.bulk_history = False  # serve Helius' getTransactionsForAddress
        self.dex_down = False
        self.dex_payload = None   # override the DEX Screener token-pairs response body
        self.telegram_failure = None  # an HTTP status, or an exception to raise
        self.rpc_calls = []
        self.evm = {}             # chain id -> FakeEvm, reached through Alchemy URLs
        self.alchemy_keys = {ALCHEMY_ENV["ALCHEMY_API_KEY"]}  # keys Alchemy accepts
        self.ankr_keys = {ANKR_ENV["ANKR_API_KEY"]}           # ... and Ankr
        self.down = set()         # URLs that don't answer (network error)
        self.gecko_pools = {}     # GeckoTerminal network -> its busiest pools (gecko_pool items)
        self.gecko_trending = {}  # GeckoTerminal network -> its trending pools
        self.gecko_trades = {}    # pool address -> its latest trades (gecko_trade items)
        self._slot = 1000

    # --- scenario helpers
    def evm_chain(self, chain_id) -> FakeEvm:
        if chain_id not in self.evm:
            self.evm[chain_id] = FakeEvm(CHAINS[chain_id])
            self.token_pairs.setdefault(CHAINS[chain_id].wrapped, [native_pair(CHAINS[chain_id], 3_000.0)])
        return self.evm[chain_id]

    def hold(self, owner, tokens, *, account=None, program=TOKEN_PROGRAM) -> str:
        account = account or addr(f"token-account:{owner}:{len(self.token_accounts)}")
        self.token_accounts[account] = {"owner": owner, "amount": int(tokens * UNIT), "program": program}
        return account

    def set_balance(self, account, tokens):
        self.token_accounts[account]["amount"] = int(tokens * UNIT)

    def close(self, account):
        del self.token_accounts[account]

    def set_market(self, price, liquidity, **kwargs):
        self.pairs = [pair(self.mint, price, liquidity, **kwargs)]

    def add_tx(self, tx, *addresses, slot=None):
        """Record a transaction, newest so far, under each address. Pass the same slot to put two in one block."""
        self._slot = slot if slot is not None else self._slot + 1
        tx["slot"] = self._slot
        signature = tx["transaction"]["signatures"][0]
        self.transactions[signature] = tx
        for address in addresses:
            self.signatures.setdefault(address, []).insert(0, {
                "signature": signature, "slot": self._slot, "err": tx["meta"]["err"], "memo": None,
                "blockTime": tx.get("blockTime"), "confirmationStatus": "confirmed"})

    def largest(self) -> list[str]:
        ranked = sorted(self.token_accounts.items(), key=lambda kv: -kv[1]["amount"])
        return [address for address, _ in ranked[:20]]

    # --- requests.Session interface
    def request(self, method, url, json=None, timeout=None):
        if "dexscreener.com" in url:
            if self.dex_down:
                return FakeResponse(503, {"error": "down"})
            if "/latest/dex/search" in url:
                return FakeResponse(200, {"schemaVersion": "1.0.0", "pairs": self.search_results})
            if "/tokens/v1/" in url:
                mints = url.rsplit("/", 1)[1].split(",")
                every = self.pairs + [p for pairs in self.token_pairs.values() for p in pairs]
                return FakeResponse(200, [p for p in every if p["baseToken"]["address"] in mints])
            return FakeResponse(200, self.dex_payload if self.dex_payload is not None else self.pairs)
        if "api.telegram.org" in url:
            return self._telegram(url, json or {})
        if url == RPC_URL or "helius-rpc.com" in url:
            return self._rpc(json)
        if url in self.down:
            raise network_error(f"HTTPSConnectionPool: Max retries exceeded with url: {url}")
        if "rpc.ankr.com/" in url:
            network, _, key = url.split("rpc.ankr.com/", 1)[1].partition("/")
            if key not in self.ankr_keys:
                return FakeResponse(401, {"jsonrpc": "2.0", "id": 1, "error": {
                    "code": -32052, "message": "Unauthorized: You must authenticate your request with an API key"}})
            name = json["params"]["blockchain"] if network == "multichain" else network
            fake = next((f for f in self.evm.values() if f.chain.ankr == name), None)
            if fake is None:
                return FakeResponse(403, {"error": "message: API key is not allowed to access blockchain"})
            return FakeResponse(200, fake.handle(json))
        public = next((f for f in self.evm.values() if url in f.chain.public_rpcs), None)
        if public is not None:
            return FakeResponse(200, public.handle(json))
        if ".g.alchemy.com/v2/" in url:
            network, key = url.split("//", 1)[1].split(".", 1)[0], url.rsplit("/", 1)[1]
            if key not in self.alchemy_keys:
                return FakeResponse(401, {"jsonrpc": "2.0", "id": 1, "error": {"code": 401,
                                                                              "message": "Must be authenticated!"}})
            fake = next((f for f in self.evm.values() if f.chain.alchemy == network), None)
            if fake is None or fake.disabled:  # like an Alchemy app without that network turned on
                return FakeResponse(403, {"jsonrpc": "2.0", "id": 1, "error": {
                    "code": -32600, "message": f"{network.upper().replace('-', '_')} is not enabled for this app"}})
            return FakeResponse(200, fake.handle(json))
        if "api.geckoterminal.com/api/v2/networks/" in url:
            network, _, rest = url.split("/networks/", 1)[1].partition("/")
            path, _, query = rest.partition("?")
            params = dict(parse_qsl(query))
            if path.endswith("/trades"):
                minimum = float(params.get("trade_volume_in_usd_greater_than", 0))
                return FakeResponse(200, {"data": [t for t in self.gecko_trades.get(path.split("/")[1], [])
                                                   if float(t["attributes"]["volume_in_usd"]) >= minimum]})
            if path == "trending_pools":
                return FakeResponse(200, {"data": self.gecko_trending.get(network, [])})
            page = int(params.get("page", 1))
            return FakeResponse(200, {"data": self.gecko_pools.get(network, [])[(page - 1) * 20: page * 20]})
        raise AssertionError(f"unexpected request to {url}")

    def _telegram(self, url, params):
        if isinstance(self.telegram_failure, Exception):
            raise self.telegram_failure
        if self.telegram_failure:
            return FakeResponse(self.telegram_failure, {"ok": False, "error_code": self.telegram_failure,
                                                        "description": "Bad Request: chat not found"})
        token, _, method = url.split("/bot", 1)[1].partition("/")
        if token not in self.bot_tokens:
            return FakeResponse(401, {"ok": False, "error_code": 401, "description": "Unauthorized"})
        if method == "getMe":
            return FakeResponse(200, {"ok": True, "result": {"id": 1, "is_bot": True, "username": "test_watch_bot"}})
        if method == "getUpdates":
            if self.webhook:
                return FakeResponse(409, {"ok": False, "error_code": 409, "description":
                                          "Conflict: can't use getUpdates method while webhook is active"})
            if params.get("timeout"):
                self.updates += self.incoming
                self.incoming = []
            offset = params.get("offset")
            return FakeResponse(200, {"ok": True, "result": [u for u in self.updates
                                                             if offset is None or u["update_id"] >= offset]})
        assert method == "sendMessage", method
        self.telegram.append(params["text"])
        self.telegram_chats.append(params["chat_id"])
        return FakeResponse(200, {"ok": True, "result": {"message_id": len(self.telegram)}})

    def _rpc(self, payload):
        method, params = payload["method"], payload.get("params", [])
        self.rpc_calls.append(method)
        if self.rpc_down:
            return FakeResponse(503, {"jsonrpc": "2.0", "error": {"code": 503, "message": "unavailable"}})
        if method == "getTransactionsForAddress" and not self.bulk_history:
            return FakeResponse(200, {"jsonrpc": "2.0", "id": payload["id"],
                                      "error": {"code": -32601, "message": "Method not found"}})
        queued = self.rpc_errors.get(method)
        if queued:
            failure = queued.pop(0)
            if isinstance(failure, int):
                return FakeResponse(failure, {"jsonrpc": "2.0", "error": {"code": failure, "message": "Too many requests"},
                                              "id": payload["id"]})
            return FakeResponse(200, {"jsonrpc": "2.0", "error": failure, "id": payload["id"]})
        result = getattr(self, f"_rpc_{method}")(*params)
        return FakeResponse(200, {"jsonrpc": "2.0", "result": result, "id": payload["id"]})

    def _amount(self, raw):
        return {"amount": str(raw), "decimals": DECIMALS, "uiAmount": raw / UNIT, "uiAmountString": str(raw / UNIT)}

    def _account(self, address):
        account = self.token_accounts.get(address)
        if account:
            program_name = "spl-token" if account["program"] == TOKEN_PROGRAM else "spl-token-2022"
            info = {"isNative": False, "mint": self.mint, "owner": account["owner"], "state": "initialized",
                    "tokenAmount": self._amount(account["amount"])}
            return {"data": {"parsed": {"info": info, "type": "account"}, "program": program_name, "space": 165},
                    "executable": False, "lamports": 2_039_280, "owner": account["program"], "rentEpoch": 0, "space": 165}
        if address in self.programs:
            return {"data": ["", "base64"], "executable": False, "lamports": 1, "owner": self.programs[address],
                    "rentEpoch": 0, "space": 0}
        return None

    def _rpc_getTokenSupply(self, mint, options=None):
        return {"context": {"slot": self._slot}, "value": self._amount(self.supply)}

    def _rpc_getTokenLargestAccounts(self, mint, options=None):
        return {"context": {"slot": self._slot},
                "value": [{"address": a, **self._amount(self.token_accounts[a]["amount"])} for a in self.largest()]}

    def _rpc_getMultipleAccounts(self, addresses, options=None):
        assert len(addresses) <= 100, "getMultipleAccounts accepts at most 100 addresses"
        return {"context": {"slot": self._slot}, "value": [self._account(a) for a in addresses]}

    def _rpc_getTokenAccountsByOwner(self, owner, filters, options=None):
        return {"context": {"slot": self._slot},
                "value": [{"pubkey": a, "account": self._account(a)}
                          for a, account in self.token_accounts.items() if account["owner"] == owner]}

    def _rpc_getAccountInfo(self, address, options=None):
        if address == self.mint:
            info = {"decimals": DECIMALS, "freezeAuthority": None, "mintAuthority": None, "isInitialized": True,
                    "supply": str(self.supply)}
            value = {"data": {"parsed": {"info": info, "type": "mint"}, "program": "spl-token", "space": 82},
                     "executable": False, "lamports": 1, "owner": TOKEN_PROGRAM, "rentEpoch": 0, "space": 82}
            return {"context": {"slot": self._slot}, "value": value}
        return {"context": {"slot": self._slot}, "value": self._account(address)}

    def _rpc_getSignaturesForAddress(self, address, options=None):
        options = options or {}
        items = self.signatures.get(address, [])
        sigs = [item["signature"] for item in items]
        if options.get("before"):
            items = items[sigs.index(options["before"]) + 1:] if options["before"] in sigs else []
            sigs = [item["signature"] for item in items]
        if options.get("until") in sigs:
            items = items[: sigs.index(options["until"])]
        return copy.deepcopy(items[: options.get("limit", 1000)])

    def _rpc_getTransaction(self, signature, options=None):
        return copy.deepcopy(self.transactions.get(signature))

    def _rpc_getTransactionsForAddress(self, address, options=None):
        """Helius' bulk history, as documented: full transactions, newest first, paginated."""
        options = options or {}
        items = [item for item in self.signatures.get(address, []) if item["err"] is None]
        since = ((options.get("filters") or {}).get("blockTime") or {}).get("gte")
        if since is not None:
            items = [item for item in items if (item["blockTime"] or 0) >= since]
        if options.get("sortOrder") == "asc":
            items = items[::-1]
        start, limit = int(options.get("paginationToken") or 0), options.get("limit", 1000)
        page = items[start:start + limit]
        return {"data": [copy.deepcopy(self.transactions[item["signature"]]) for item in page],
                "paginationToken": str(start + limit) if start + limit < len(items) else None}


def network_error(message):
    return requests.ConnectionError(message)


BASE_CONFIG = {
    "mint": MINT,
    "rpc_url": RPC_URL,
    "poll_seconds": 60,
    "top_n": 5,
    "exclude_owners": [],
    "always_alert_owners": [],
    "labels": {},
    "rules": {"stop_price_usd": None, "trailing_stop_pct": None, "min_liquidity_usd": None,
              "holder_drop_pct": 20, "combined_drop_pct": None, "window_minutes": 60},
    "heartbeat": {"enabled": False},
}


def write_config(tmp_path, *, rules=None, cooldowns=None, **settings):
    data = copy.deepcopy(BASE_CONFIG)
    data["rules"].update(rules or {})
    if cooldowns:
        data["alert_cooldown_minutes"] = cooldowns
    data.update(settings)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


WHALES = {"whale1": 50_000_000, "whale2": 40_000_000, "whale3": 30_000_000, "whale4": 20_000_000,
          "whale5": 10_000_000, "whale6": 8_000_000}


def populate(chain: FakeChain, *, program=TOKEN_PROGRAM) -> dict:
    """A Raydium CPMM pool vault (auto-excluded), six whales and 13 small holders: exactly 20 accounts.
    With top_n = 5 the watched wallets are whale1..whale5 (150M tokens together)."""
    chain.set_market(price=0.002, liquidity=600_000)
    accounts = {"pool": chain.hold(CPMM_AUTHORITY, 300_000_000)}
    for name, tokens in WHALES.items():
        accounts[name] = chain.hold(addr(name), tokens, program=program)
    for i in range(13):
        chain.hold(addr(f"small{i}"), 1_000_000)
    return accounts


class Harness:
    """A Monitor and a TraderWatch wired to a FakeChain, with a controllable clock. restart() simulates a
    process restart."""

    def __init__(self, tmp_path, chain=None, *, env=None, start=1_790_000_000.0, **settings):
        self.chain = chain or FakeChain()
        self.now = start
        self.env = dict(TELEGRAM_ENV) if env is None else env
        self.config_path = write_config(tmp_path, **settings)
        self.monitor = self.restart()

    def clock(self):
        return self.now

    def restart(self) -> Monitor:
        cfg = load_config(self.config_path, environ=self.env)
        http = HttpClient(session=self.chain, sleep=lambda _seconds: None, redact=Redactor(cfg.secrets))
        rpc = SolanaRPC(cfg.rpc_url, http)
        sender = TelegramSender(cfg.telegram_token, cfg.telegram_chat_id, http) if cfg.telegram_token else None
        notifier = Notifier(sender)
        self.monitor = Monitor(cfg, rpc, http, notifier, StateStore(cfg.state_file), clock=self.clock)
        evm = {}
        for chain_id in self.chain.evm:
            rpc = evm_client(CHAINS[chain_id], http, self.env)
            if rpc is not None:
                evm[chain_id] = rpc
        self.traders = TraderWatch(cfg, rpc, http, notifier, TraderStateStore(cfg.trader_state_file), evm=evm,
                                   signal_log=cfg.signal_log, clock=self.clock)
        if cfg.traders.wallets:
            self.monitor.trader_watch = self.traders
        return self.monitor

    def cycle(self, minutes=1.0):
        self.now += minutes * 60
        return self.monitor.run_cycle()

    def check(self, minutes=1.0):
        """One trader-watch check."""
        self.now += minutes * 60
        return self.traders.run_cycle()

    def take(self) -> list[str]:
        """Telegram messages sent since the last take()."""
        messages, self.chain.telegram[:] = list(self.chain.telegram), []
        return messages
