"""Read balance changes, per wallet, and turn them into trades.

Shared by the outflow classifier (holder alerts), trader discovery and the trader watch, on every
chain: each chain only has to say what a wallet gained and lost (Solana from a transaction's
pre/post balances, EVM chains from receipts and balances, see evm.py); derive_trades() decides what
was bought and sold. Never from instruction names alone: a transaction can run through a DEX
program without being a trade.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .chains import SOLANA
from .known import DEX_PROGRAMS

LAMPORTS_PER_SOL = 1_000_000_000
PAID_USD = 0.10  # smaller stablecoin changes are rounding, not a payment


def account_keys(message: dict, meta: dict) -> list[str]:
    keys = [k["pubkey"] if isinstance(k, dict) else k for k in message.get("accountKeys") or []]
    if len(keys) < len(meta.get("preBalances") or []):  # non-parsed encoding lists lookup-table keys separately
        loaded = meta.get("loadedAddresses") or {}
        keys += list(loaded.get("writable") or []) + list(loaded.get("readonly") or [])
    return keys


def signature(tx: dict) -> str | None:
    return ((tx.get("transaction") or {}).get("signatures") or [None])[0]


def signers(tx: dict) -> set[str]:
    message = (tx.get("transaction") or {}).get("message") or {}
    keys = message.get("accountKeys") or []
    if keys and isinstance(keys[0], dict):
        return {k["pubkey"] for k in keys if k.get("signer")}
    count = (message.get("header") or {}).get("numRequiredSignatures") or 0
    return set(keys[:count])


def token_deltas(meta: dict) -> tuple[dict, dict]:
    """{(owner, mint): raw change} from pre/post token balances, and {mint: decimals}."""
    deltas, decimals = {}, {}
    for sign, field_name in ((-1, "preTokenBalances"), (1, "postTokenBalances")):
        for entry in meta.get(field_name) or []:
            ui = entry.get("uiTokenAmount") or {}
            owner, mint, amount = entry.get("owner"), entry.get("mint"), ui.get("amount")
            if owner is None or mint is None or amount is None:
                continue
            deltas[(owner, mint)] = deltas.get((owner, mint), 0) + sign * int(amount)
            decimals[mint] = int(ui.get("decimals") or 0)
    return deltas, decimals


def venues(message: dict, meta: dict, keys: list) -> list[str]:
    """DEX / aggregator programs the transaction ran, in call order."""
    programs = []

    def add(instruction):
        program = instruction.get("programId")
        if program is None and isinstance(instruction.get("programIdIndex"), int):
            index = instruction["programIdIndex"]
            program = keys[index] if 0 <= index < len(keys) else None
        if program in DEX_PROGRAMS and DEX_PROGRAMS[program] not in programs:
            programs.append(DEX_PROGRAMS[program])

    for instruction in message.get("instructions") or []:
        add(instruction)
    for group in meta.get("innerInstructions") or []:
        for instruction in group.get("instructions") or []:
            add(instruction)
    return programs


def balance_owners(tx: dict) -> set[str]:
    meta = tx.get("meta") or {}
    return {entry["owner"] for name in ("preTokenBalances", "postTokenBalances")
            for entry in meta.get(name) or [] if entry.get("owner")}


@dataclass
class WalletChange:
    """What one wallet gained and lost in one transaction (on EVM chains: in one block)."""
    owner: str
    tokens: dict = field(default_factory=dict)    # token -> [raw before, raw after, decimals]
    accounts: dict = field(default_factory=dict)  # Solana: token -> the wallet's token accounts for it
    native: int = 0    # net change of the chain's coin in its smallest unit (lamports, wei), fee included;
                       # on Solana with the rent and wrapped SOL held in the wallet's own token accounts
    fee: int = 0       # the network fee, if this wallet paid it
    native_before: int | None = None  # the wallet's own coin balance, when known
    native_after: int | None = None
    balances_known: bool = True       # False on EVM chains: tokens then hold [0, net change]


def wallet_changes(tx: dict) -> dict[str, WalletChange]:
    """Every wallet that has a token account in the transaction, with its balance changes.

    SOL is counted across the wallet's own account and its token accounts, so rent moved into a new
    token account and SOL wrapped as WSOL are not mistaken for spending.
    """
    meta = tx.get("meta") or {}
    message = (tx.get("transaction") or {}).get("message") or {}
    keys = account_keys(message, meta)
    pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []

    def lamport_change(index):
        return post[index] - pre[index] if 0 <= index < min(len(pre), len(post)) else 0

    changes, counted = {}, set()
    for side, name in ((0, "preTokenBalances"), (1, "postTokenBalances")):
        for entry in meta.get(name) or []:
            ui = entry.get("uiTokenAmount") or {}
            owner, mint, amount, index = entry.get("owner"), entry.get("mint"), ui.get("amount"), entry.get("accountIndex")
            if owner is None or mint is None or amount is None:
                continue
            change = changes.setdefault(owner, WalletChange(owner))
            row = change.tokens.setdefault(mint, [0, 0, int(ui.get("decimals") or 0)])
            row[side] += int(amount)
            if isinstance(index, int) and index not in counted:
                counted.add(index)
                change.native += lamport_change(index)
                if index < len(keys):
                    change.accounts.setdefault(mint, []).append(keys[index])
    for owner, change in changes.items():
        if owner in keys:
            index = keys.index(owner)
            change.native += lamport_change(index)
            if index < min(len(pre), len(post)):
                change.native_before, change.native_after = pre[index], post[index]
            if index == 0:  # account 0 is the fee payer
                change.fee = meta.get("fee") or 0
    return changes


@dataclass
class Trade:
    """A wallet's trade of one token in one transaction."""
    owner: str
    side: str          # "buy" / "sell" (for the chain's coin or stablecoins), "swap" (for another token),
                       # "in" / "out" (received or sent without payment: transfers, airdrops, LP moves)
    mint: str          # the token bought, sold, received or sent; for "swap" the token received
    amount: int        # raw amount of `mint`
    decimals: int
    before: int | None  # the wallet's raw balance of `mint` before and after (None where the chain doesn't say)
    after: int | None
    native_change: float = 0.0  # the wallet's net change of the chain's coin (SOL, ETH, BNB, wrapped or not),
                                # network fee included (negative = spent)
    fee: float = 0.0            # network fee paid by the wallet, in the chain's coin
    usd_change: float = 0.0     # net stablecoin change
    signature: str | None = None
    slot: int = 0               # slot or block number
    index: int = 0              # position in the block, when known
    block_time: int | None = None
    signer: bool = False        # the wallet signed (sent) the transaction
    venues: list = field(default_factory=list)
    other_mint: str | None = None  # "swap": the token given up
    other_amount: int = 0
    other_decimals: int = 0
    counterparty: str | None = None  # a sell whose proceeds went to another wallet: that wallet
    chain: str = "solana"

    @property
    def tokens(self) -> float:
        return self.amount / 10 ** self.decimals

    @property
    def native(self) -> float:
        """The chain's coin paid or received for the trade, network fee excluded."""
        return abs(self.native_change + self.fee)

    @property
    def usd(self) -> float:
        return abs(self.usd_change)


def derive_trades(change: WalletChange, chain, *, signature=None, slot=0, index=0, block_time=None, signer=False,
                  venues=()) -> list[Trade]:
    """The wallet's trades: one per token it gained or lost. The chain's coin and stablecoins are what it
    paid or received, except when it swapped one for the other: that is a trade of the coin itself
    (mint = chain.wrapped), bought or sold for dollars."""
    unit = 10 ** chain.decimals
    dust = unit // 1000  # 0.001 of the coin: smaller changes are tips and rounding, not a payment
    moved = {mint: row for mint, row in change.tokens.items() if mint not in chain.quotes and row[0] != row[1]}
    usd_change = sum((row[1] - row[0]) / 10 ** row[2] for mint, row in change.tokens.items() if mint in chain.stables)
    wrapped = change.tokens.get(chain.wrapped)  # on EVM chains wrapped ETH/BNB is a token like any other
    coin = change.native + (wrapped[1] - wrapped[0] if wrapped and chain.evm else 0)
    gross = coin + change.fee
    paid = gross < -dust or usd_change < -PAID_USD
    received = gross > dust or usd_change > PAID_USD
    coin_trade = (gross < -dust and usd_change > PAID_USD) or (gross > dust and usd_change < -PAID_USD)
    if not moved and not coin_trade:
        return []
    known = change.balances_known
    common = dict(native_change=coin / unit, fee=change.fee / unit, usd_change=usd_change, signature=signature,
                  slot=slot, index=index, block_time=block_time, signer=signer, chain=chain.id)
    if not moved:  # the coin for stablecoins, or the reverse
        return [Trade(owner=change.owner, side="sell" if gross < 0 else "buy", mint=chain.wrapped, amount=abs(gross),
                      decimals=chain.decimals, before=change.native_before, after=change.native_after,
                      venues=list(venues), **common)]

    def trade(side, mint, **extra):
        before, after, decimals = moved[mint]
        return Trade(owner=change.owner, side=side, mint=mint, amount=abs(after - before), decimals=decimals,
                     before=before if known else None, after=after if known else None, venues=list(venues),
                     **common, **extra)

    up = [mint for mint, row in moved.items() if row[1] > row[0]]
    down = [mint for mint, row in moved.items() if row[1] < row[0]]
    if len(up) == 1 and len(down) == 1:
        before, after, decimals = moved[down[0]]
        return [trade("swap", up[0], other_mint=down[0], other_amount=before - after, other_decimals=decimals)]
    single = len(moved) == 1
    trades = [trade("buy" if single and paid and not received else "in", mint) for mint in up]
    trades += [trade("sell" if single and received and not paid else "out", mint) for mint in down]
    return trades


def wallet_trades(tx: dict, owner: str, *, changes: dict | None = None) -> list[Trade]:
    """The wallet's trades in a Solana transaction."""
    meta = tx.get("meta") or {}
    if meta.get("err") is not None:
        return []
    change = (wallet_changes(tx) if changes is None else changes).get(owner)
    if change is None:
        return []
    message = (tx.get("transaction") or {}).get("message") or {}
    return derive_trades(change, SOLANA, signature=signature(tx), slot=tx.get("slot") or 0,
                         index=tx.get("transactionIndex") or 0, block_time=tx.get("blockTime"),
                         signer=owner in signers(tx), venues=venues(message, meta, account_keys(message, meta)))
