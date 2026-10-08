"""EVM chains: trades from receipts and coin balances (checked against real swaps in tests/fixtures/evm),
Alchemy's transfer index, and the trader watch on Ethereum, Base, BNB Chain and Arbitrum wallets."""

import json
import logging
from pathlib import Path

import pytest

import holder_watch
from tests.fakes import (ALCHEMY_ENV, ANKR_ENV, TELEGRAM_ENV, USDC_ON, FakeChain, Harness, event, evm_addr, evm_pair,
                         gecko_trade, plain, topic, write_config)
from watcher import traders
from watcher.chains import BASE, BSC, CHAINS, ETHEREUM
from watcher.config import Discovery
from watcher.discover import DiscoveryError, discover
from watcher.evm import DEPOSIT, TRANSFER, WITHDRAWAL, EvmRPC, block_change, client, source
from watcher.gecko import Gecko
from watcher.rpc import HttpClient, RpcError
from watcher.txparse import derive_trades
from watcher.util import short

FIXTURES = Path(__file__).parent / "fixtures" / "evm"
GAS = 21_000 * 10**9  # what each of the fake chain's transactions costs
WALLET, POOL, TOKEN = evm_addr("wallet"), evm_addr("pool"), evm_addr("token")


def fixture(name):
    return json.loads((FIXTURES / f"evm_{name}.json").read_text(encoding="utf-8"))


def fixture_trades(name, *, balances=True):
    data = fixture(name)
    chain, receipt = CHAINS[data["chain"]], data["receipt"]
    before, after = (int(data[key], 16) if balances and data[key] else None
                     for key in ("balance_before", "balance_after"))
    change = block_change(chain, data["wallet"].lower(), [receipt], before, after, {})
    return derive_trades(change, chain, signature=receipt["transactionHash"], slot=int(receipt["blockNumber"], 16),
                         signer=True)


# --- real swaps ------------------------------------------------------------------------------------

def test_real_base_sell_paid_out_in_eth_matches_the_pool():
    """Sold 4,077 BRETT on Base; the router paid the ETH out internally, which only the balance shows."""
    pool_trade = fixture("base_sell")["gecko_trade"]
    [trade] = fixture_trades("base_sell")
    assert (trade.chain, trade.side, trade.mint) == ("base", "sell", pool_trade["from_token_address"])
    assert trade.tokens == pytest.approx(float(pool_trade["from_token_amount"]))
    assert trade.native == pytest.approx(float(pool_trade["to_token_amount"]))  # gas is not part of the price
    assert trade.before is None and trade.after is None  # token balances aren't read on EVM chains


def test_real_bnb_chain_buy_paid_in_stablecoins():
    """The sender of this pool sale was a router; its own trade was a buy of another token for 1,000 USDT."""
    [trade] = fixture_trades("bsc_sell")
    assert (trade.side, trade.mint) == ("buy", "0x4c067de26475e1cefee8b8d1f6e2266b33a2372e")
    assert trade.usd == pytest.approx(1_000.0) and trade.native == 0


def test_real_ethereum_token_for_token_swap():
    [trade] = fixture_trades("ethereum_sell")
    assert (trade.side, trade.mint, trade.other_mint) == (
        "swap", "0x6982508145454ce325ddbe47a25d4ec3d2311933", "0x66b4503a54cfe769d14c418a426f7d1bd1d50796")


@pytest.mark.parametrize("name", ["arbitrum_buy", "arbitrum_sell", "base_buy", "bsc_buy", "ethereum_buy"])
def test_real_trades_of_a_contracts_funds_are_not_the_senders(name):
    """These senders had a contract trade its own tokens (bots, vaults): their wallets only paid gas, so
    there is nothing to follow."""
    assert fixture_trades(name) == []


def test_without_balances_a_sale_for_eth_is_not_guessed():
    """When the node no longer has the block's state only the token side is known: tokens leaving
    without a visible payment are not called a sale (and not alerted)."""
    [trade] = fixture_trades("base_sell", balances=False)
    assert trade.side == "out"


# --- receipts -------------------------------------------------------------------------------------

def receipt(*logs, status="0x1", l1_fee=0):
    return {"transactionHash": "0xabc", "status": status, "from": WALLET, "gasUsed": hex(100_000),
            "effectiveGasPrice": hex(10**9), "l1Fee": hex(l1_fee), "logs": list(logs)}


def block_trades(chain, receipts, coin_change):
    """Trades in a block where the wallet's coin balance moved by coin_change wei (gas included)."""
    change = block_change(chain, WALLET, receipts, 10**19, 10**19 + coin_change, {})
    return derive_trades(change, chain, signer=True)


def test_selling_for_wrapped_eth_is_selling_for_eth():
    r = receipt(event(TOKEN, TRANSFER, WALLET, POOL, value=10**21),
                event(BASE.wrapped, TRANSFER, POOL, WALLET, value=5 * 10**17), l1_fee=3 * 10**12)
    fee = 100_000 * 10**9 + 3 * 10**12  # gas plus Base's L1 data fee
    [trade] = block_trades(BASE, [r], -fee)
    assert (trade.side, trade.mint, trade.tokens) == ("sell", TOKEN, 1_000)
    assert trade.native == pytest.approx(0.5) and trade.fee == pytest.approx(fee / 10**18)


def test_wrapping_or_unwrapping_eth_is_not_a_trade():
    fee = 100_000 * 10**9
    assert block_trades(BASE, [receipt(event(BASE.wrapped, DEPOSIT, WALLET, value=10**18))], -10**18 - fee) == []
    assert block_trades(BASE, [receipt(event(BASE.wrapped, WITHDRAWAL, WALLET, value=10**18))], 10**18 - fee) == []


def test_reverted_transactions_and_nfts_move_no_tokens():
    reverted = receipt(event(TOKEN, TRANSFER, POOL, WALLET, value=10**21), status="0x0")
    nft = receipt({"address": TOKEN, "topics": [TRANSFER, topic(POOL), topic(WALLET), "0x" + "0" * 63 + "7"],
                   "data": "0x"})
    assert block_trades(BASE, [reverted], -100_000 * 10**9) == []
    assert block_trades(BASE, [nft], -10**17 - 100_000 * 10**9) == []


# --- where data comes from ------------------------------------------------------------------------

def evm_rpc(chain_id="base"):
    """An Alchemy client on a fresh fake chain (no public nodes: every call goes to Alchemy)."""
    world = FakeChain()
    fake = world.evm_chain(chain_id)
    chain = CHAINS[chain_id]
    return EvmRPC(chain, source(chain, ALCHEMY_ENV).url, HttpClient(session=world, sleep=lambda _s: None)), fake


def test_where_each_chain_is_read_from():
    ankr = source(BASE, {"ANKR_API_KEY": "SECRET42", "ALCHEMY_API_KEY": "a"})
    assert (ankr.kind, ankr.url, ankr.index, ankr.index_url) == (
        "ankr", "https://rpc.ankr.com/base/SECRET42", "ankr", "https://rpc.ankr.com/multichain/SECRET42")
    alchemy = source(BSC, {"ALCHEMY_API_KEY": "a"})
    assert (alchemy.kind, alchemy.url, alchemy.index) == ("alchemy", "https://bnb-mainnet.g.alchemy.com/v2/a", "alchemy")
    own = source(BSC, {"ANKR_API_KEY": "SECRET42", "BSC_RPC_URL": "https://bsc.example/x"})
    assert (own.kind, own.url, own.index) == ("custom", "https://bsc.example/x", "ankr")  # Ankr still lists transfers
    keyless = source(ETHEREUM, {})
    assert (keyless.kind, keyless.url, keyless.index, keyless.history) == (
        "public", "https://rpc.mevblocker.io", "logs", False)
    assert source(BSC, {}) is None  # no public BNB Chain RPC lists a wallet's transfers
    assert "SECRET42" not in repr(ankr)  # keys never show


def test_ankr_lists_transfers_and_polling_goes_to_public_nodes():
    world = FakeChain()
    base = world.evm_chain("base")
    rpc = client(BASE, HttpClient(session=world, sleep=lambda _s: None), ANKR_ENV)
    base.swap("0xbuy", WALLET, block=1010, token=TOKEN, side="buy", tokens=50_000, eth=0.25, time=1_790_000_100)
    assert rpc.nonces([WALLET]) == {WALLET: 1} and rpc.block_number() == 1010  # 2 blocks back: the keyed node has it
    assert rpc.credits == 0  # public nodes
    blocks, complete = rpc.transfers(WALLET, from_block=1001, to_block=1010)
    assert complete and list(blocks) == [1010] and blocks[1010].time == 1_790_000_100
    [buy] = rpc.trades(WALLET, blocks)
    assert (buy.side, buy.tokens, buy.signature) == ("buy", 50_000, "0xbuy") and buy.native == pytest.approx(0.25)
    assert rpc.credits == 700 + 3 * 200  # one Advanced API listing; a receipt and two balances from Ankr's node


def test_an_ankr_listing_stops_just_past_the_limit():
    """A busy wallet's transfers fit in one 10,000-transfer page: it must still count as busy, without
    its transactions being read."""
    world = FakeChain()
    base = world.evm_chain("base")
    rpc = client(BASE, HttpClient(session=world, sleep=lambda _s: None), ANKR_ENV)
    for n in range(5):
        base.swap(f"0xbuy{n}", WALLET, block=1001 + n, token=TOKEN, side="buy", tokens=100, eth=0.1)
    blocks, complete = rpc.transfers(WALLET, from_block=1001, limit=3)
    assert not complete and list(blocks) == [1001, 1002, 1003]
    assert rpc.transfers(WALLET, from_block=1001, limit=5) == (rpc.transfers(WALLET, from_block=1001)[0], True)


def test_polling_falls_back_to_the_keyed_node():
    world = FakeChain()
    world.evm_chain("base")
    world.down.update(BASE.public_rpcs)
    rpc = client(BASE, HttpClient(session=world, sleep=lambda _s: None), ANKR_ENV)
    assert rpc.block_number() == 1000 and rpc.credits == 200  # no margin: the keyed node answered itself


def test_without_a_key_recent_transfers_come_from_the_public_rpcs_logs():
    world = FakeChain()
    base = world.evm_chain("base")
    rpc = client(BASE, HttpClient(session=world, sleep=lambda _s: None), {})
    base.swap("0xbuy", WALLET, block=1010, token=TOKEN, side="buy", tokens=50_000, eth=0.25, time=1_790_000_100)
    base.swap("0xsell", WALLET, block=1011, token=TOKEN, side="sell", tokens=20_000, usd=240, time=1_790_000_102)
    base.log_timestamps = False  # an older node: the block headers give the times
    blocks, complete = rpc.transfers(WALLET, from_block=1001, to_block=1011)
    assert complete and list(blocks) == [1010, 1011] and blocks[1010].time == 1_790_000_000 + 10 * 2
    buy, sell = rpc.trades(WALLET, blocks)
    assert (buy.side, sell.side, sell.usd) == ("buy", "sell", pytest.approx(240)) and buy.native == pytest.approx(0.25)
    assert rpc.credits == 0 and not rpc.history
    with pytest.raises(RpcError, match="needs ANKR_API_KEY"):  # a long period is more than public RPCs list
        rpc.transfers(WALLET, from_block=0, to_block=1_000_000)


def test_transfers_list_blocks_and_stop_where_the_listing_is_complete():
    rpc, fake = evm_rpc()
    for n in range(5):
        fake.swap(f"0xbuy{n}", WALLET, block=1001 + n, token=TOKEN, side="buy", tokens=100, weth=0.1,
                  time=1_790_000_000 + n)
    blocks, complete = rpc.transfers(WALLET, from_block=1001)
    assert complete and list(blocks) == [1001, 1002, 1003, 1004, 1005]
    assert blocks[1002].hashes == ["0xbuy1"] and blocks[1002].time == 1_790_000_001
    blocks, complete = rpc.transfers(WALLET, from_block=1001, limit=3)
    assert not complete and list(blocks) == [1001, 1002]  # block 1003 may go on in the next page


def test_trades_come_from_receipts_and_the_balance_around_each_block():
    rpc, fake = evm_rpc()
    fake.swap("0xbuy", WALLET, block=1010, token=TOKEN, side="buy", tokens=50_000, eth=0.25)
    fake.swap("0xsell", WALLET, block=1020, token=TOKEN, side="sell", tokens=20_000, usd=240)
    blocks, _ = rpc.transfers(WALLET, from_block=1001)
    buy, sell = rpc.trades(WALLET, blocks)
    assert (buy.side, buy.tokens, buy.signature, buy.slot, buy.signer) == ("buy", 50_000, "0xbuy", 1010, True)
    assert buy.native == pytest.approx(0.25) and buy.fee == pytest.approx(GAS / 10**18)
    assert (sell.side, sell.tokens, sell.native) == ("sell", 20_000, 0) and sell.usd == pytest.approx(240)
    assert rpc.credits == 2 * 120 + 2 * 20 + 4 * 20  # transfers both ways, receipts, balances (decimals: listed)


def test_blocks_whose_state_was_pruned_show_their_token_side_only(caplog):
    rpc, fake = evm_rpc("bsc")
    fake.swap("0xold", WALLET, block=1010, token=TOKEN, side="buy", tokens=10, eth=1.0)
    fake.swap("0xnew", WALLET, block=1500, token=TOKEN, side="buy", tokens=10, eth=1.0)
    fake.pruned_before = 1400
    blocks, _ = rpc.transfers(WALLET, from_block=1001)
    with caplog.at_level(logging.WARNING):
        old, new = rpc.trades(WALLET, blocks)
    assert (old.side, new.side) == ("in", "buy") and new.native == pytest.approx(1.0)
    assert "BNB Chain: the node no longer has the balances of 2 block(s)" in caplog.text
    fake.errors["eth_getBalance"] = [{"code": -32000, "message": "header not found"}]
    with pytest.raises(RpcError):  # any other error fails the check, which is retried
        rpc.trades(WALLET, blocks)


# --- discovery ------------------------------------------------------------------------------------

def test_discovery_ranks_a_base_tokens_traders():
    """Candidates are the senders GeckoTerminal reports for the token's pool; each one's history on the
    token comes from Alchemy, and losers, scalpers and routers are left out."""
    rpc, base = evm_rpc()
    world_http = rpc.http
    world = world_http.session
    token = evm_addr("brett")
    world.pairs = [evm_pair(BASE, token, 0.012, symbol="BRETT", name="Brett")]  # 0.000004 ETH
    winner, loser, scalper, router = (evm_addr(name) for name in ("winner", "loser", "scalper", "router"))
    world.gecko_trades[evm_addr(f"pool:{token}")] = [gecko_trade(w, volume=v) for w, v in (
        (winner, 900.0), (loser, 3_000.0), (scalper, 600.0), (router, 5_000.0))]
    t0 = 1_790_000_000 - 30 * 86_400
    base.swap("0xw1", winner, block=1001, token=token, side="buy", tokens=100_000, eth=0.2, time=t0)
    base.swap("0xw2", winner, block=1100, token=token, side="sell", tokens=50_000, eth=0.3, time=t0 + 3 * 3600)
    base.swap("0xl1", loser, block=1002, token=token, side="buy", tokens=100_000, eth=1.0, time=t0)
    base.swap("0xl2", loser, block=1101, token=token, side="sell", tokens=100_000, eth=0.5, time=t0 + 5 * 3600)
    base.swap("0xs1", scalper, block=1003, token=token, side="buy", tokens=100_000, weth=0.2, time=t0)
    base.swap("0xs2", scalper, block=1004, token=token, side="sell", tokens=100_000, weth=0.4, time=t0 + 600)
    base.tx("0xr1", router, block=1005, moves=[(token, 5_000, True), (token, 5_000, False)])  # passes tokens on
    report = discover(rpc, world_http, token, Discovery(), chain=BASE, gecko=Gecko(world_http))
    assert (report.chain, report.native, report.symbol, report.candidates) == ("base", "ETH", "BRETT", 4)
    [top] = report.traders
    assert top.address == winner and top.buys == 1 and top.sells == 1 and top.held_pct == pytest.approx(50)
    gas = 2 * GAS / 10**18
    assert top.pnl_native == pytest.approx(0.3 - gas) and top.pnl_usd == pytest.approx((0.3 - gas) * 3_000)
    assert top.avg_hold_hours == pytest.approx(3)
    assert dict(report.left_out) == {"lost money or broke even": 1,
                                     "scalper (sold most of it within 60 min of buying)": 1,
                                     "never bought or sold it itself (pool, program, contract-run bot or transfers only)": 1}


def test_discovery_without_a_transfer_index_says_what_it_needs():
    world = FakeChain()
    world.evm_chain("base")
    http = HttpClient(session=world, sleep=lambda _s: None)
    with pytest.raises(DiscoveryError, match="needs a transfer index: add ANKR_API_KEY"):
        discover(client(BASE, http, {}), http, evm_addr("brett"), Discovery(), chain=BASE, gecko=Gecko(http))


# --- the trader watch -----------------------------------------------------------------------------

TRADER = evm_addr("base-trader")
DEGEN = evm_addr("degen")
PRESET = {"chain": "base", "address": TRADER, "label": "Base pro #1", "preset": True, "pnl_usd": 5400.0,
          "roi_pct": 85.0}


def watch(tmp_path, *wallets, env=None, **settings):
    world = FakeChain()
    base = world.evm_chain("base")  # ETH at $3,000
    world.token_pairs[DEGEN] = [evm_pair(BASE, DEGEN, 0.012, symbol="DEGEN", name="Degen")]
    h = Harness(tmp_path, world, env={**TELEGRAM_ENV, **ALCHEMY_ENV} if env is None else env,
                traders={"wallets": list(wallets) or [PRESET], **settings})
    return h, base


def test_a_base_buy_alerts_once_and_goes_to_the_signal_log(tmp_path):
    h, base = watch(tmp_path)
    base.swap("0xbefore", TRADER, block=995, token=DEGEN, side="buy", tokens=1_000, eth=0.01)
    assert h.check().ok and h.take() == []  # notes where to start
    base.swap("0xbuy", TRADER, block=1010, token=DEGEN, side="buy", tokens=50_000, eth=0.25, time=int(h.now) + 30)
    report = h.check()
    assert report.ok and [t.signature for _, t in report.sent] == ["0xbuy"]  # not the trade from before
    [message] = h.take()
    text = plain(message)
    assert f"1. BUY on Base by Base pro #1 ({short(TRADER)}) · built-in trader: +$5,400, +85% over 30 days" in text
    assert "Bought 50,000 DEGEN for 0.25 ETH ($750.00) at " in text
    assert "Degen (DEGEN) · price $0.01200 · liquidity $500,000 · market cap $5,000,000" in text
    assert 'href="https://basescan.org/tx/0xbuy"' in message and f'href="https://basescan.org/address/{TRADER}"' in message
    [signal] = [json.loads(line) for line in (tmp_path / "signals.jsonl").read_text(encoding="utf-8").splitlines()]
    assert {key: signal[key] for key in ("v", "chain", "wallet", "label", "side", "token", "symbol", "amount", "native",
                                         "usd_paid", "value_usd", "price_usd", "tx")} == {
        "v": 1, "chain": "base", "wallet": TRADER, "label": "Base pro #1", "side": "buy", "token": DEGEN,
        "symbol": "DEGEN", "amount": 50_000.0, "native": 0.25, "usd_paid": 0.0, "value_usd": 750.0,
        "price_usd": 0.012, "tx": "0xbuy"}
    h.check()
    assert h.take() == []  # read again while the wallet is active, but alerted once


def test_sales_for_dollars_and_eth_sold_for_dollars(tmp_path):
    h, base = watch(tmp_path)
    h.check()
    base.swap("0xsell", TRADER, block=1010, token=DEGEN, side="sell", tokens=20_000, usd=240)
    base.tx("0xeth", TRADER, block=1011, moves=[(USDC_ON["base"], 3_000, True)], eth=-1.0)
    h.check()
    text = plain(h.take()[0])
    assert "Sold 20,000 DEGEN for $240.00 in stablecoins" in text
    assert "Sold 1.00 ETH for $3,000 in stablecoins" in text and "ETH balance now 9.00" in text


def test_each_wallet_keeps_its_own_transaction_count(tmp_path):
    """A quiet wallet listed before a busy one must not take the busy one's count, and then miss its own
    next trade."""
    quiet, busy = evm_addr("quiet"), evm_addr("busy")
    h, base = watch(tmp_path, {"chain": "base", "address": quiet, "label": "Quiet"},
                    {"chain": "base", "address": busy, "label": "Busy"})
    base.swap("0xold", busy, block=990, token=DEGEN, side="buy", tokens=1, eth=0.01)
    h.check()
    base.swap("0xbusy", busy, block=1010, token=DEGEN, side="buy", tokens=1_000, eth=0.1)
    h.check()
    assert "BUY on Base by Busy" in plain(h.take()[0])
    base.swap("0xquiet", quiet, block=1020, token=DEGEN, side="sell", tokens=1_000, eth=0.2)
    h.check()
    assert "SELL on Base by Quiet" in plain(h.take()[0])


def test_trades_the_index_lists_late_are_still_found(tmp_path):
    h, base = watch(tmp_path)
    h.check()
    base.swap("0xlate", TRADER, block=1010, token=DEGEN, side="buy", tokens=50_000, eth=0.25)
    base.lagging.add("0xlate")
    report = h.check()
    assert report.ok and report.trades == [] and h.take() == []
    base.lagging.clear()
    h.check()
    assert "Bought 50,000 DEGEN" in plain(h.take()[0])
    h.check()
    assert h.take() == []


def test_a_quiet_wallet_costs_one_batched_call_per_check(tmp_path):
    h, base = watch(tmp_path)
    h.check()
    base.swap("0xbuy", TRADER, block=1010, token=DEGEN, side="buy", tokens=50_000, eth=0.25)
    h.check()
    h.take()
    base.calls.clear()
    h.check(minutes=traders.ACTIVE_SECONDS / 60 + 1)  # past the few minutes it stays active
    assert base.calls == ["eth_blockNumber", "eth_getTransactionCount"]


def test_a_very_busy_wallet_is_read_on_over_several_checks(tmp_path, monkeypatch):
    monkeypatch.setattr(traders, "PAGE", 1)  # at most 4 transfers each way per check
    h, base = watch(tmp_path)
    h.check()
    for n in range(6):
        base.swap(f"0xb{n}", TRADER, block=1010 + n, token=DEGEN, side="buy", tokens=1_000, eth=0.1)
    first, second = h.check(), h.check()
    assert [t.signature for _, t in first.sent] == ["0xb0", "0xb1", "0xb2"]
    assert [t.signature for _, t in second.sent] == ["0xb3", "0xb4", "0xb5"]


def test_the_major_token_monitor_on_base(tmp_path):
    cbbtc = "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf"
    h, base = watch(tmp_path, {**PRESET, "tokens": "major"})
    base.decimals[cbbtc] = 8
    h.check()
    base.swap("0xdegen", TRADER, block=1010, token=DEGEN, side="buy", tokens=50_000, eth=0.25)
    base.swap("0xbtc", TRADER, block=1011, token=cbbtc, side="buy", tokens=0.01, usd=1_200)
    h.check()
    [message] = h.take()
    text = plain(message)
    assert "Trader watch: 1 new trade" in text and "· major-token monitor" in text
    assert "Bought 0.01 cbBTC for $1,200 in stablecoins" in text


def test_without_any_key_base_trades_are_watched_through_its_public_rpc(tmp_path):
    h, base = watch(tmp_path, env=dict(TELEGRAM_ENV))
    h.check()
    base.swap("0xbuy", TRADER, block=1010, token=DEGEN, side="buy", tokens=50_000, eth=0.25)
    report = h.check()
    assert report.ok and "Bought 50,000 DEGEN for 0.25 ETH ($750.00)" in plain(h.take()[0])
    assert "eth_getLogs" in base.calls and h.traders.evm["base"].credits == 0


def test_bnb_chain_wallets_need_a_key(tmp_path):
    world = FakeChain()
    world.evm_chain("bsc")
    h = Harness(tmp_path, world, env=dict(TELEGRAM_ENV), traders={"wallets": [{"chain": "bsc", "address": TRADER}]})
    report = h.check()
    assert not report.ok and report.errors == [
        "BNB Chain: no RPC for 1 wallet(s); add ANKR_API_KEY to .env (free at ankr.com)"]


def test_the_ankr_key_never_reaches_logs_alerts_or_state(tmp_path, caplog):
    h, base = watch(tmp_path, env={**TELEGRAM_ENV, **ANKR_ENV})
    key = ANKR_ENV["ANKR_API_KEY"]
    base.errors["eth_blockNumber"] = [{"code": -32000, "message": f"rejected https://rpc.ankr.com/base/{key}"}] * 100
    with caplog.at_level(logging.DEBUG):
        reports = [h.check() for _ in range(5)]  # the 5th failure in a row is alerted
    assert not reports[-1].ok and "rejected" in reports[-1].errors[0]
    alerts = "\n".join(h.take())
    assert "checks in a row failed" in alerts
    assert key not in caplog.text + alerts + (tmp_path / "trader_state.json").read_text(encoding="utf-8")


def test_the_alchemy_key_never_reaches_logs_alerts_or_state(tmp_path, caplog):
    h, base = watch(tmp_path)
    key = ALCHEMY_ENV["ALCHEMY_API_KEY"]
    base.errors["eth_blockNumber"] = [{"code": -32000, "message": f"rejected https://base-mainnet.g.alchemy.com/v2/{key}"}] * 100
    with caplog.at_level(logging.DEBUG):
        reports = [h.check() for _ in range(5)]  # the 5th failure in a row is alerted
    assert not reports[-1].ok and "rejected" in reports[-1].errors[0]
    alerts = "\n".join(h.take())
    assert "checks in a row failed" in alerts
    assert key not in caplog.text + alerts + (tmp_path / "trader_state.json").read_text(encoding="utf-8")


def test_traders_only_from_the_command_line(tmp_path, monkeypatch):
    """No token to monitor, only watched wallets: --once checks them (the first check notes where each starts)."""
    world = FakeChain()
    world.evm_chain("base")
    path = write_config(tmp_path, mint="PASTE_MINT_HERE", traders={"wallets": [PRESET]})
    monkeypatch.setattr(holder_watch, "HttpClient",
                        lambda redact=None: HttpClient(session=world, sleep=lambda _s: None, redact=redact))
    monkeypatch.setattr(holder_watch.alerts, "setup_logging", lambda *a, **k: None)
    for name, value in {**TELEGRAM_ENV, **ALCHEMY_ENV}.items():
        monkeypatch.setenv(name, value)
    assert holder_watch.main(["--once", "--config", str(path)]) == 0
    state = json.loads((tmp_path / "trader_state.json").read_text(encoding="utf-8"))
    assert list(state["wallets"]) == [f"base:{TRADER}"] and not (tmp_path / "state.json").exists()
    assert holder_watch.main(["--list", "--config", str(path)]) == 2  # listing holders needs a token
