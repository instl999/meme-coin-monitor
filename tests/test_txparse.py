"""Per-wallet balance changes and trades, checked against real CYBERLEEK mainnet transactions (tests/fixtures)."""

import json
from pathlib import Path

import pytest

from tests.fakes import USDC, WSOL, addr, make_tx
from watcher.known import USDT_MINT
from watcher.txparse import signers, wallet_changes, wallet_trades

FIXTURES = Path(__file__).parent / "fixtures"
CYBERLEEK = "ApZuxdpzMrbEYTGEzeY9afh5pj9d6qPRJCTgQYiipbKg"
CPMM_AUTHORITY = "GpMZbSM2GgvTKHJirzeGfMFoaZ8UR2X7F4v8vHTvxFbL"


def load(name):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))["transaction"]


def test_real_buy_adds_to_a_position_and_counts_fees_as_cost():
    tx = load("buy_jupiter")
    buyer = "6KdJ1LMFYLP77WECoa7HjnHw1kJFJhSWzSU5vfkAPyxg"
    [trade] = wallet_trades(tx, buyer)
    assert trade.side == "buy" and trade.mint == CYBERLEEK and trade.signer
    assert trade.amount == 8_692_310_812_784 and trade.decimals == 9
    assert (trade.before, trade.after) == (7_878_086_787_446, 16_570_397_600_230)
    assert trade.native_change == pytest.approx(-0.12440334)    # everything that left the wallet
    assert trade.native == pytest.approx(0.12440334 - 0.0001059)  # the swap, network fee excluded
    assert trade.venues == ["Jupiter", "Raydium CPMM"]


def test_real_buy_that_opens_the_token_account():
    tx = load("buy_v1_transaction")
    [trade] = wallet_trades(tx, "EkghwDQNpFCfoRNn6CR6STUKMFqSU71tpNZfxciKvrtA")
    assert trade.side == "buy" and trade.before == 0 and trade.amount == 71_212_003_608_303
    assert trade.native_change == pytest.approx(-1.011005)


def test_real_sell_of_a_whole_position_for_sol():
    tx = load("sell_jupiter_cpmm_sol")
    seller = "EtcypjU3iERqtNZGmTa5jQ1qSyQvTN6KX6Fet7m2KyAj"
    [trade] = wallet_trades(tx, seller)
    assert trade.side == "sell" and trade.after == 0 and trade.amount == 1_459_668_419_511_158
    assert trade.native_change == pytest.approx(20.367476082)  # proceeds net of the fee it paid
    assert trade.native == pytest.approx(20.367486561)


def test_pool_side_is_not_a_signer():
    tx = load("sell_jupiter_cpmm_sol")
    assert CPMM_AUTHORITY not in signers(tx)
    pool = wallet_changes(tx)[CPMM_AUTHORITY]
    assert pool.tokens[WSOL][1] - pool.tokens[WSOL][0] == -20_387_874_435  # SOL left the pool's vault
    assert pool.native == -20_387_874_435  # wrapped SOL is counted through its account's lamports


@pytest.mark.parametrize("fixture, owner", [
    ("sell_jupiter_wsol", "7JQeyNK55fkUPUmEotupBFpiBGpgEQYLe8Ht1VdSfxcP"),     # proceeds went to another wallet
    ("transfer_plain", "FSVbw7VvwTFJRGVTRWNoEG54R1MnN54abaqN7izpJxnR"),        # sent, and paid the recipient's rent
    ("claim_multi_transfer", "HFqp6ErWHY6Uzhj8rFyjYuDya2mXUpYEk8VW75K9PSiY"),  # moved by someone else's transaction
])
def test_real_outflows_without_payment_are_not_sells(fixture, owner):
    [trade] = wallet_trades(load(fixture), owner)
    assert trade.side == "out"


def test_real_transfer_recipient_received_without_paying():
    [trade] = wallet_trades(load("transfer_plain"), "C3F87Ag1kGLF4ZMrsAHMsfo5xknEePkTwcVK5hxhmJ2z")
    assert trade.side == "in" and trade.amount == 35_000 * 10**9


def test_rent_for_a_new_token_account_is_not_spending():
    """Rent moved from the wallet into its own new token account stays the wallet's SOL."""
    wallet, mint = addr("wallet"), addr("token")
    tx = make_tx("airdrop", fee_payer=wallet, lamports={wallet: (10**9, 10**9 - 5000 - 2_039_280)},
                 token_moves=[(wallet, mint, None, 1_000_000, 6)])
    tx["meta"]["preBalances"][-1], tx["meta"]["postBalances"][-1] = 0, 2_039_280  # the account it created
    [trade] = wallet_trades(tx, wallet)
    assert trade.side == "in" and wallet_changes(tx)[wallet].native == -5000


def test_sol_bought_or_sold_for_stablecoins_is_a_trade_of_sol():
    wallet, pool = addr("wallet"), addr("pool")
    sold = make_tx("sol-sell", fee_payer=wallet, lamports={wallet: (100 * 10**9, 50 * 10**9 - 5000)},
                   token_moves=[(wallet, USDC, 1_000_000, 5_901_000_000, 6), (pool, USDC, 10**12, 10**12 - 5_900_000_000, 6)])
    [trade] = wallet_trades(sold, wallet)
    assert (trade.side, trade.mint, trade.amount, trade.decimals) == ("sell", WSOL, 50 * 10**9, 9)
    assert trade.usd_change == pytest.approx(5_900.0)
    assert (trade.before, trade.after) == (100 * 10**9, 50 * 10**9 - 5000)  # the wallet's own SOL
    bought = make_tx("sol-buy", fee_payer=wallet, lamports={wallet: (10**9, 11 * 10**9 - 5000)},
                     token_moves=[(wallet, USDC, 1_200_000_000, 20_000_000, 6)])
    [trade] = wallet_trades(bought, wallet)
    assert (trade.side, trade.amount) == ("buy", 10 * 10**9) and trade.usd_change == pytest.approx(-1_180.0)


def test_moving_sol_alone_or_swapping_stablecoins_is_not_a_trade():
    wallet = addr("wallet")
    sent = make_tx("sol-send", fee_payer=wallet, lamports={wallet: (5 * 10**9, 4 * 10**9 - 5000)},
                   token_moves=[(wallet, USDC, 5_000_000, 5_000_000, 6)])
    stables = make_tx("usdc-usdt", fee_payer=wallet, token_moves=[(wallet, USDC, 10**9, None, 6),
                                                                  (wallet, USDT_MINT, None, 10**9, 6)])
    assert wallet_trades(sent, wallet) == [] and wallet_trades(stables, wallet) == []


def test_token_for_token_swap_and_failed_transactions():
    wallet, a, b = addr("wallet"), addr("token-a"), addr("token-b")
    moves = [(wallet, a, 5_000_000, 1_000_000, 6), (wallet, b, None, 2_000_000, 9)]
    [trade] = wallet_trades(make_tx("swap", fee_payer=wallet, token_moves=moves), wallet)
    assert (trade.side, trade.mint, trade.other_mint, trade.other_amount) == ("swap", b, a, 4_000_000)
    assert wallet_trades(make_tx("failed", fee_payer=wallet, token_moves=moves, err={"InstructionError": [0, {}]}),
                         wallet) == []
