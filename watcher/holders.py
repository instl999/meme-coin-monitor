"""Holder snapshots (who holds the token right now, aggregated by owner wallet) and pool detection."""

from __future__ import annotations

from dataclasses import dataclass, field

from .known import BURN_ADDRESSES, DEX_PROGRAMS, POOL_AUTHORITIES, TOKEN_PROGRAMS


@dataclass
class TokenAccount:
    address: str
    owner: str
    amount: int        # raw on-chain amount (divide by 10**decimals for display)
    closed: bool = False


@dataclass
class Snapshot:
    supply: int
    decimals: int
    accounts: dict      # token account address -> TokenAccount
    largest: list       # token account addresses from getTokenLargestAccounts, biggest first
    balances: dict = field(init=False)        # owner -> raw amount, summed over watched accounts
    owner_accounts: dict = field(init=False)  # owner -> [token account addresses]

    def __post_init__(self):
        self.balances, self.owner_accounts = {}, {}
        for account in list(self.accounts.values()):
            self._count(account)

    def add(self, account: TokenAccount) -> None:
        self.accounts[account.address] = account
        self._count(account)

    def _count(self, account: TokenAccount) -> None:
        self.balances[account.owner] = self.balances.get(account.owner, 0) + account.amount
        self.owner_accounts.setdefault(account.owner, []).append(account.address)

    def ranked_owners(self) -> list[str]:
        """Owners of the current largest token accounts, biggest aggregated balance first."""
        owners = {self.accounts[a].owner for a in self.largest if a in self.accounts}
        return sorted(owners, key=lambda owner: (-self.balances[owner], owner))

    def share(self, amount: int) -> float:
        return amount / self.supply * 100 if self.supply else 0.0


def parse_token_account(info: dict | None, mint: str) -> tuple[str, int] | None:
    """(owner, raw amount) for a jsonParsed SPL Token / Token-2022 account of `mint`, else None."""
    if not info or info.get("owner") not in TOKEN_PROGRAMS:
        return None
    data = info.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("parsed"), dict):
        raise ValueError("RPC returned token account data that is not jsonParsed")
    parsed = data["parsed"]
    detail = parsed.get("info") or {}
    if parsed.get("type") != "account" or detail.get("mint") != mint:
        return None
    return detail["owner"], int(detail["tokenAmount"]["amount"])


def fetch_snapshot(rpc, mint: str, *, known_accounts: dict | None = None, extra_owners=()) -> Snapshot:
    """Current top-20 accounts plus every previously watched account (so a holder that drops out of
    the top 20 is still seen), plus all accounts of `extra_owners`. A watched account that no longer
    exists was closed: it counts as balance 0, so selling out completely is visible.

    An extra owner whose known account is in the top 20 right now is not looked up again (saves an
    RPC call per wallet per cycle); the monitor double-checks all its accounts whenever its balance falls.
    """
    known_accounts = known_accounts or {}
    supply, decimals = rpc.token_supply(mint)
    largest = [item["address"] for item in rpc.largest_token_accounts(mint)]
    seen_in_top = {known_accounts[a] for a in largest if a in known_accounts}
    addresses = list(largest)
    for owner in extra_owners:
        if owner not in seen_in_top:
            addresses += [item["pubkey"] for item in rpc.token_accounts_by_owner(owner, mint)]
    addresses = list(dict.fromkeys(addresses + list(known_accounts)))
    accounts = {}
    for address, info in zip(addresses, rpc.multiple_accounts(addresses)):
        parsed = parse_token_account(info, mint)
        if parsed:
            accounts[address] = TokenAccount(address, parsed[0], parsed[1])
        elif address in known_accounts:
            accounts[address] = TokenAccount(address, known_accounts[address], 0, closed=True)
    return Snapshot(supply, decimals, accounts, [a for a in largest if a in accounts])


class PoolDetector:
    """Recognises owners that are DEX pools (or burn addresses), so a pool's balance swinging with
    every trade is never read as a holder selling.

    Checks, cheapest first: known pool authorities and burn addresses, pool addresses reported by
    DEX Screener, then (once per address, cached in state.json) which program owns the address.
    """

    CACHE_LIMIT = 2000

    def __init__(self, rpc, cache: dict | None = None):
        self.rpc = rpc
        self.cache = {} if cache is None else cache
        self.pairs: set[str] = set()

    def add_pairs(self, addresses) -> None:
        self.pairs.update(addresses)

    def known(self, address: str) -> str | None:
        """Pool/burn description for an address, using only what is already known (no RPC)."""
        if address in BURN_ADDRESSES:
            return BURN_ADDRESSES[address]
        if address in POOL_AUTHORITIES:
            return f"{POOL_AUTHORITIES[address]} pool"
        if address in self.pairs:
            return "DEX pool (listed on DEX Screener)"
        entry = self.cache.get(address)
        return entry.get("pool") if entry else None

    def lookup(self, addresses) -> None:
        """Resolve addresses not seen before with one getMultipleAccounts call."""
        todo = [a for a in dict.fromkeys(addresses) if a not in self.cache and not self.known(a)]
        if not todo:
            return
        infos = self.rpc.multiple_accounts(todo, encoding="base64", data_slice={"offset": 0, "length": 0})
        for address, info in zip(todo, infos):
            program = (info or {}).get("owner")
            name = DEX_PROGRAMS.get(program)
            self.cache[address] = {"pool": f"{name} pool" if name else None, "program": program}
        while len(self.cache) > self.CACHE_LIMIT:
            self.cache.pop(next(iter(self.cache)))


def excluded_reason(cfg, detector: PoolDetector, owner: str) -> str | None:
    """Why an owner is ignored by the holder rules, or None if it is watched."""
    if owner in cfg.always_alert_owners:
        return None
    if owner in cfg.exclude_owners:
        return "exclude_owners"
    if cfg.auto_exclude_pools:
        name = detector.known(owner)
        if name:
            return f"auto: {name}"
    return None
