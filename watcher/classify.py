"""Best-effort classification of a holder's outflow: sold on a DEX, or transferred to another wallet.

Decided from the transaction's token-balance changes rather than instruction names alone: a
transaction can run through a DEX program without being a sale (for example Jupiter's ClaimToken
moving tokens between wallets), so the deciding question is where the tokens went.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .known import MINT_SYMBOLS, WSOL_MINT
from .txparse import account_keys, balance_owners, token_deltas, venues
from .util import short

SOL_DUST_LAMPORTS = 10_000_000  # 0.01 SOL: below this a SOL gain is rent refunds, not sale proceeds
_SWAP_LOG = re.compile(r"Instruction: (?:Swap|Sell|Route|SharedAccountsRoute|ExactOutRoute)")


@dataclass
class Outflow:
    signature: str
    kind: str                    # "sell", "transfer", "burn" or "unknown"
    amount: int                  # raw amount of the token that left the holder in this transaction
    block_time: int | None = None
    venues: list = field(default_factory=list)     # DEX / aggregator programs involved, in call order
    proceeds: dict = field(default_factory=dict)   # symbol -> amount the holder received in return
    counterparty: str | None = None   # transfer recipient, or the wallet that received sale proceeds
    other_recipients: int = 0
    note: str | None = None


def classify_transaction(tx: dict, owner: str, mint: str, pool_name) -> Outflow | None:
    """Classify `owner`'s outflow of `mint` in a jsonParsed transaction (None if it isn't one).

    pool_name(address) returns a description if the address is a known DEX pool, else None.
    """
    meta = tx.get("meta") or {}
    if meta.get("err") is not None:
        return None
    body = tx.get("transaction") or {}
    message = body.get("message") or {}
    keys = account_keys(message, meta)
    deltas, decimals = token_deltas(meta)
    sent = -deltas.get((owner, mint), 0)
    if sent <= 0:
        return None

    received = {m: d for (who, m), d in deltas.items() if who == owner and m != mint and d > 0}
    sol_gain = _sol_delta(owner, keys, meta)
    if sol_gain >= SOL_DUST_LAMPORTS:
        received[WSOL_MINT] = received.get(WSOL_MINT, 0) + sol_gain
        decimals[WSOL_MINT] = 9
    recipients = {who: d for (who, m), d in deltas.items() if m == mint and who != owner and d > 0}
    pools = [who for who in recipients if pool_name(who)]
    wallets = {who: d for who, d in recipients.items() if who not in pools}

    flow = Outflow(signature=(body.get("signatures") or ["?"])[0], kind="unknown", amount=sent,
                   block_time=tx.get("blockTime"), venues=venues(message, meta, keys),
                   proceeds={MINT_SYMBOLS.get(m, short(m)): d / 10 ** decimals.get(m, 0) for m, d in received.items()})
    if pools:
        flow.kind = "sell"
        if not received:
            flow.counterparty = _proceeds_recipient(deltas, owner, mint, pool_name)
            flow.note = "proceeds were not received by this wallet"
    elif wallets and received:
        flow.kind = "sell"  # tokens to a wallet and something back in the same transaction
        flow.counterparty = max(wallets, key=wallets.get)
        flow.venues = flow.venues or ["direct trade"]
    elif wallets:
        flow.kind = "transfer"
        flow.counterparty = max(wallets, key=wallets.get)
        flow.other_recipients = len(wallets) - 1
        if flow.venues and any(_SWAP_LOG.search(line) for line in meta.get("logMessages") or []):
            flow.note = "the transaction also contains swap instructions"
    elif received:
        flow.kind = "sell"
        flow.venues = flow.venues or ["unrecognised route"]
    elif sum(d for (_, m), d in deltas.items() if m == mint) < 0:
        flow.kind = "burn"
    return flow


def find_outflows(rpc, owner: str, token_accounts: list, mint: str, *, since_ts: float, detector,
                  max_transactions: int = 5, per_account: int = 10) -> list[Outflow]:
    """Look up the holder's most recent transactions since `since_ts` and classify each outflow.

    Searches the holder's token accounts for this mint (precise even for busy wallets), falling back
    to the wallet itself when no token account is known.
    """
    found = {}
    for address in (token_accounts or [owner])[:3]:
        for item in rpc.signatures(address, limit=per_account):
            if item.get("err") is None:
                found.setdefault(item["signature"], item)
    recent = [s for s in found.values() if (s.get("blockTime") or since_ts) >= since_ts]
    recent.sort(key=lambda s: s.get("slot", 0), reverse=True)
    flows = []
    for item in recent[:max_transactions]:
        tx = rpc.transaction(item["signature"])
        if not tx:
            continue
        detector.lookup(balance_owners(tx))
        flow = classify_transaction(tx, owner, mint, detector.known)
        if flow:
            flow.signature = item["signature"]
            flows.append(flow)
    return flows


def _sol_delta(owner: str, keys: list, meta: dict) -> int:
    """Lamports gained by the owner's wallet, ignoring the fee it paid."""
    if owner not in keys:
        return 0
    i = keys.index(owner)
    pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
    if i >= len(pre) or i >= len(post):
        return 0
    fee = (meta.get("fee") or 0) if i == 0 else 0  # account 0 is the fee payer
    return post[i] - pre[i] + fee


def _proceeds_recipient(deltas: dict, owner: str, mint: str, pool_name) -> str | None:
    """For a sell whose proceeds left the seller's wallet: the non-pool wallet that gained other tokens."""
    gains = {who: d for (who, m), d in deltas.items() if m != mint and who != owner and d > 0 and not pool_name(who)}
    return max(gains, key=gains.get) if gains else None
