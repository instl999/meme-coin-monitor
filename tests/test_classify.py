"""Sell vs. transfer classification, checked against real CYBERLEEK mainnet transactions (tests/fixtures)."""

import json
from pathlib import Path

import pytest

from watcher.classify import classify_transaction, find_outflows
from watcher.holders import PoolDetector

FIXTURES = Path(__file__).parent / "fixtures"
CYBERLEEK = "ApZuxdpzMrbEYTGEzeY9afh5pj9d6qPRJCTgQYiipbKg"
CPMM_AUTHORITY = "GpMZbSM2GgvTKHJirzeGfMFoaZ8UR2X7F4v8vHTvxFbL"
ORCA_POOL = "C1MgLojNLWBKADvu9BHdtgzz1oZX4dZ5zGdGcgvvW8Wz"  # a Whirlpool account in the multi-hop sell
WHIRLPOOL_PROGRAM = "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc"


def load(name):
    data = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return data["signature"], data["transaction"]


def pools(*extra):
    known = {CPMM_AUTHORITY: "Raydium CPMM pool", **{address: "Orca Whirlpool pool" for address in extra}}
    return known.get


def test_real_sell_for_sol_through_jupiter_and_raydium():
    signature, tx = load("sell_jupiter_cpmm_sol")
    seller = "EtcypjU3iERqtNZGmTa5jQ1qSyQvTN6KX6Fet7m2KyAj"
    flow = classify_transaction(tx, seller, CYBERLEEK, pools())
    assert flow.kind == "sell"
    assert flow.signature == signature
    assert flow.amount == 1_459_668_419_511_158  # 1,459,668.42 CYBERLEEK
    assert flow.venues == ["Jupiter", "Raydium CPMM"]
    assert flow.proceeds["SOL"] == pytest.approx(20.3675, abs=1e-4)  # lamports gained plus the fee it paid
    assert flow.counterparty is None


def test_real_sell_whose_proceeds_went_to_another_wallet():
    _, tx = load("sell_jupiter_wsol")
    seller = "7JQeyNK55fkUPUmEotupBFpiBGpgEQYLe8Ht1VdSfxcP"
    flow = classify_transaction(tx, seller, CYBERLEEK, pools(ORCA_POOL))
    assert flow.kind == "sell"
    assert flow.amount == 218_880_947_897_137
    assert flow.venues == ["Jupiter", "Raydium CPMM", "Orca Whirlpool"]
    assert flow.proceeds == {}  # the seller only paid fees
    assert flow.counterparty == "6tZT9AUcQn4iHMH79YZEXSy55kDLQ4VbA3PMtfLVNsFX"  # received the JUP


def test_real_plain_transfer_to_another_wallet():
    _, tx = load("transfer_plain")
    flow = classify_transaction(tx, "FSVbw7VvwTFJRGVTRWNoEG54R1MnN54abaqN7izpJxnR", CYBERLEEK, pools())
    assert flow.kind == "transfer"
    assert flow.amount == 35_000 * 10**9
    assert flow.counterparty == "C3F87Ag1kGLF4ZMrsAHMsfo5xknEePkTwcVK5hxhmJ2z"
    assert flow.venues == [] and flow.proceeds == {}


def test_real_transfer_through_a_dex_program_is_still_a_transfer():
    """Jupiter's ClaimToken moved tokens from eight wallets to one: a DEX program ran, but nothing was sold."""
    _, tx = load("claim_multi_transfer")
    flow = classify_transaction(tx, "HFqp6ErWHY6Uzhj8rFyjYuDya2mXUpYEk8VW75K9PSiY", CYBERLEEK, pools())
    assert flow.kind == "transfer"
    assert flow.venues == ["Jupiter"]
    assert flow.counterparty == "7JQeyNK55fkUPUmEotupBFpiBGpgEQYLe8Ht1VdSfxcP"
    assert flow.amount == 53_506_052_182_009


@pytest.mark.parametrize("fixture, buyer", [
    ("buy_jupiter", "6KdJ1LMFYLP77WECoa7HjnHw1kJFJhSWzSU5vfkAPyxg"),
    ("buy_v1_transaction", "EkghwDQNpFCfoRNn6CR6STUKMFqSU71tpNZfxciKvrtA"),
])
def test_real_buys_are_not_outflows(fixture, buyer):
    _, tx = load(fixture)
    assert classify_transaction(tx, buyer, CYBERLEEK, pools()) is None


def test_version_1_transactions_parse():
    _, tx = load("buy_v1_transaction")
    assert tx["version"] == 1
    flow = classify_transaction(tx, CPMM_AUTHORITY, CYBERLEEK, lambda _address: None)
    assert flow is not None and flow.amount == 71_212_003_608_303  # the pool side of the buy


class StubRPC:
    """Serves fixture transactions by signature and owning programs for pool detection."""

    def __init__(self, fixtures, programs):
        self.txs = dict(load(name) for name in fixtures)
        self.programs = programs
        self.lookups = []

    def signatures(self, address, limit=10):
        return [{"signature": sig, "slot": i, "err": None, "blockTime": tx["blockTime"]}
                for i, (sig, tx) in enumerate(self.txs.items())][:limit]

    def transaction(self, signature):
        return self.txs[signature]

    def multiple_accounts(self, addresses, encoding="jsonParsed", data_slice=None):
        self.lookups.extend(addresses)
        return [{"owner": self.programs[a]} if a in self.programs else None for a in addresses]


def test_find_outflows_looks_up_unknown_pools_and_skips_inflows():
    rpc = StubRPC(["sell_jupiter_wsol", "buy_jupiter"], {ORCA_POOL: WHIRLPOOL_PROGRAM})
    detector = PoolDetector(rpc)
    flows = find_outflows(rpc, "7JQeyNK55fkUPUmEotupBFpiBGpgEQYLe8Ht1VdSfxcP", ["token-account"], CYBERLEEK,
                          since_ts=0, detector=detector)
    assert [flow.kind for flow in flows] == ["sell"]
    assert flows[0].counterparty == "6tZT9AUcQn4iHMH79YZEXSy55kDLQ4VbA3PMtfLVNsFX"
    assert detector.known(ORCA_POOL) == "Orca Whirlpool pool"  # learned via its owning program
    assert CPMM_AUTHORITY not in rpc.lookups                    # known authority: no lookup needed


def test_find_outflows_ignores_transactions_before_the_drop():
    rpc = StubRPC(["sell_jupiter_cpmm_sol"], {})
    _, tx = load("sell_jupiter_cpmm_sol")
    flows = find_outflows(rpc, "EtcypjU3iERqtNZGmTa5jQ1qSyQvTN6KX6Fet7m2KyAj", ["token-account"], CYBERLEEK,
                          since_ts=tx["blockTime"] + 1, detector=PoolDetector(rpc))
    assert flows == []
