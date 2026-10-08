"""Trader discovery: average-cost accounting, the filters, and ranking against the fake chain."""

import pytest

from tests.fakes import (CPMM_AUTHORITY, MINT, UNIT, FakeChain, addr, ata, pair, swap_tx, transfer_tx)
from watcher.config import Discovery
from watcher.discover import Position, discover, value_native, verdict
from watcher.rpc import HttpClient, SolanaRPC
from watcher.txparse import Trade

NOW = 1_790_000_000.0
DAY = 86_400
POOL = addr("cpmm-pool")
LAUNCH = NOW - 30 * DAY
FEE = 5000 / 10**9


def trade(side, tokens, sol, *, slot=1, signer=True, at=None):
    return Trade(owner="w", side=side, mint=MINT, amount=int(tokens * UNIT), decimals=6, before=0, after=0,
                 native_change=-sol if side == "buy" else sol, slot=slot, signer=signer, block_time=at)


def apply_all(position, *trades):
    for t in trades:
        position.apply(t, t.side, value_native(t, None))
    return position


def test_average_cost_gives_realized_and_unrealized_profit():
    pos = apply_all(Position("w"), trade("buy", 1_000, 1.0), trade("buy", 1_000, 3.0, slot=2),
                    trade("sell", 500, 2.5, slot=3))
    assert pos.realized == pytest.approx(2.5 - 1.0)  # 500 of 2,000 tokens that cost 4 SOL
    assert pos.held == 1_500 * UNIT and pos.basis == pytest.approx(3.0)
    assert pos.unrealized(0.003) == pytest.approx(1_500 * 0.003 - 3.0)
    assert pos.pnl(0.003) == pytest.approx(3.0) and pos.roi(0.003) == pytest.approx(75.0)
    assert pos.winning_sells == 1 and pos.unmatched == 0


def test_transfers_move_tokens_without_profit_and_unmatched_sells_are_counted():
    pos = apply_all(Position("w"), trade("buy", 1_000, 2.0), trade("out", 500, 0))
    assert pos.held == 500 * UNIT and pos.basis == pytest.approx(1.0) and pos.realized == 0
    apply_all(pos, trade("in", 300, 0), trade("sell", 800, 4.0, slot=5))
    assert pos.unmatched == 300 * UNIT
    assert pos.realized == pytest.approx(4.0 * 500 / 800 - 1.0)


def test_a_stablecoin_leg_needs_the_sol_price():
    t = Trade(owner="w", side="buy", mint=MINT, amount=UNIT, decimals=6, before=0, after=UNIT, usd_change=-150.0)
    assert value_native(t, 150.0) == pytest.approx(-1.0) and value_native(t, None) is None


def test_verdicts():
    settings = Discovery()
    bot = apply_all(Position("bot"), *[t for slot in range(3) for t in
                                       (trade("buy", 100, 1.0, slot=slot), trade("sell", 100, 1.01, slot=slot))])
    assert "same block" in verdict(bot, settings, True)
    assert "never bought or sold it itself" in verdict(apply_all(Position("r"), trade("buy", 9, 5, signer=False)),
                                                       settings, True)
    big = apply_all(Position("big"), trade("buy", 100, 5.0))
    assert "too many transactions" in verdict(big, settings, False)
    assert verdict(big, settings, True) is None
    assert verdict(apply_all(Position("s"), trade("buy", 100, 0.5)), settings, True, native_usd=100.0) == "bought less than $100"


def test_scalpers_are_left_out_and_holding_time_is_measured_first_in_first_out():
    settings = Discovery()
    quick = Position("quick")
    for i in range(3):  # in and out within ten minutes, three times (never in the same block)
        start = 1_000_000 + i * 7_200
        apply_all(quick, trade("buy", 100, 1.0, slot=i * 10, at=start), trade("sell", 100, 1.2, slot=i * 10 + 1, at=start + 600))
    assert quick.hold_stats(3600) == (600, 1.0)
    assert verdict(quick, settings, True) == "scalper (sold most of it within 60 min of buying)"
    patient = apply_all(Position("patient"), trade("buy", 100, 1.0, at=0), trade("buy", 100, 1.0, slot=2, at=86_400),
                        trade("sell", 150, 3.0, slot=3, at=3 * 86_400))
    # 100 bought on day 0 and 50 of the 100 bought on day 1 were sold on day 3: held 3 and 2 days
    assert patient.hold_stats(3600) == ((100 * 3 * 86_400 + 50 * 2 * 86_400) / 150, 0.0)
    assert verdict(patient, settings, True) is None
    assert verdict(quick, Discovery(min_hold_minutes=5), True) is None  # a shorter threshold lets it through


def scenario(chain: FakeChain) -> dict:
    """Wallets trading through the pool in the last two days, and a whale that bought at launch."""
    chain.pairs = [pair(MINT, 0.002, 600_000, pair_address=POOL, price_native="0.00002",
                        created_ms=int(LAUNCH * 1000))]
    chain.hold(CPMM_AUTHORITY, 300_000_000, account=ata(CPMM_AUTHORITY))  # the pool's vault: largest holder
    names = ("alpha", "beta", "loser", "bot", "dust", "gifted", "whale", "donor")
    w = {name: addr(name) for name in names}
    t = NOW - 2 * DAY

    def add(signature, name, side, tokens, sol, *, before=0, at=0.0, slot=None):
        tx = swap_tx(signature, w[name], MINT, side=side, tokens=tokens, sol=sol, before=before, block_time=int(t + at))
        chain.add_tx(tx, POOL, ata(w[name]), w[name], slot=slot)

    whale_buy = swap_tx("whale-buy", w["whale"], MINT, side="buy", tokens=30_000_000, sol=10,
                        block_time=int(LAUNCH + 120))
    chain.add_tx(whale_buy, POOL, ata(w["whale"]), w["whale"])  # older than the scan window: found as a top holder
    chain.hold(w["whale"], 30_000_000, account=ata(w["whale"]))
    add("alpha-buy", "alpha", "buy", 1_000_000, 5, at=0)
    add("alpha-sell", "alpha", "sell", 600_000, 9, before=1_000_000, at=3600)
    add("beta-buy", "beta", "buy", 2_000_000, 10, at=100)
    add("beta-sell", "beta", "sell", 2_000_000, 12, before=2_000_000, at=7200)
    add("loser-buy", "loser", "buy", 1_000_000, 5, at=200)
    add("loser-sell", "loser", "sell", 1_000_000, 2, before=1_000_000, at=9000)
    for i in range(3):  # buys and sells in the same block, three times
        slot = 5_000 + i
        add(f"bot-buy-{i}", "bot", "buy", 100_000, 2, at=300 + i, slot=slot)
        add(f"bot-sell-{i}", "bot", "sell", 100_000, 2.02, before=100_000, at=300 + i, slot=slot)
    add("dust-buy", "dust", "buy", 10_000, 0.1, at=400)
    add("dust-sell", "dust", "sell", 10_000, 0.5, before=10_000, at=5000)
    gift = transfer_tx("gift", w["donor"], w["gifted"], MINT, 500_000, sender_before=900_000, block_time=int(t + 500))
    chain.add_tx(gift, ata(w["gifted"]), ata(w["donor"]), w["gifted"])
    add("gifted-sell", "gifted", "sell", 500_000, 4, before=500_000, at=6000)
    return w


def run(chain, **rpc_flags):
    http = HttpClient(session=chain, sleep=lambda _s: None)
    rpc = SolanaRPC("https://rpc.test/", http)
    for key, value in rpc_flags.items():
        setattr(rpc, key, value)
    return discover(rpc, http, MINT, Discovery(), clock=lambda: NOW), rpc


@pytest.mark.parametrize("bulk", [False, True])
def test_ranks_profitable_traders_and_explains_who_was_left_out(bulk):
    chain = FakeChain()
    chain.bulk_history = bulk
    w = scenario(chain)
    report, rpc = run(chain, helius=bulk)

    assert [t.address for t in report.traders] == [w["whale"], w["alpha"], w["beta"]]
    whale, alpha, beta = report.traders
    assert whale.pnl_native == pytest.approx(30_000_000 * 0.00002 - (10 + FEE))  # all unrealized
    assert whale.notes[0].startswith("early: first buy 2 min after the first pool opened")
    assert alpha.realized_native == pytest.approx((9 - FEE) - 0.6 * (5 + FEE))
    assert alpha.unrealized_native == pytest.approx(400_000 * 0.00002 - 0.4 * (5 + FEE))
    assert alpha.roi_pct == pytest.approx(alpha.pnl_native / (5 + FEE) * 100)
    assert (alpha.buys, alpha.sells, round(alpha.held_pct)) == (1, 1, 40)
    assert beta.pnl_native == pytest.approx((12 - FEE) - (10 + FEE))
    assert report.left_out == {
        "lost money or broke even": 1,
        "bot-like (sold in the same block it bought)": 1,
        "bought less than $100": 1,
        "sold tokens it wasn't seen buying": 1,
    }
    assert report.scanned == 15                 # the whale's launch buy is outside the scanned window
    assert report.wallets_seen == 6 and report.candidates == 7
    assert report.price_native == pytest.approx(0.00002) and report.native_usd == pytest.approx(100.0)
    assert whale.pnl_usd == pytest.approx(whale.pnl_native * 100.0) and report.native == "SOL"
    if bulk:
        assert rpc.history_api is True and "getTransaction" not in chain.rpc_calls
    else:
        assert "getTransactionsForAddress" not in chain.rpc_calls


def test_falls_back_when_the_bulk_history_method_is_missing():
    chain = FakeChain()
    w = scenario(chain)
    report, rpc = run(chain, helius=True)  # a Helius URL, but the method isn't offered
    assert rpc.history_api is False and chain.rpc_calls.count("getTransactionsForAddress") == 1
    assert [t.address for t in report.traders] == [w["whale"], w["alpha"], w["beta"]]


def test_scan_is_limited_to_the_lookback_window_and_budget():
    chain = FakeChain()
    scenario(chain)
    http = HttpClient(session=chain, sleep=lambda _s: None)
    rpc = SolanaRPC("https://rpc.test/", http)
    report = discover(rpc, http, MINT, Discovery(scan_transactions=50, lookback_hours=1), clock=lambda: NOW)
    assert report.scanned == 0 and report.wallets_seen == 0
    assert [t.address for t in report.traders] == [addr("whale")]  # still found through the holder list


def test_a_wallet_with_too_many_transactions_is_left_out_without_reading_them():
    chain = FakeChain()
    scenario(chain)
    busy = addr("busy")
    for i in range(12):  # an older history the scan doesn't reach
        chain.add_tx(swap_tx(f"busy-old-{i}", busy, MINT, side="buy", tokens=1_000, sol=1, before=1_000 * i,
                             block_time=int(LAUNCH + i)), ata(busy), busy)
    chain.add_tx(swap_tx("busy-now", busy, MINT, side="buy", tokens=1_000, sol=50, before=12_000,
                         block_time=int(NOW - 3600)), POOL, ata(busy), busy)
    http = HttpClient(session=chain, sleep=lambda _s: None)
    rpc = SolanaRPC("https://rpc.test/", http)
    fetched, read = [], rpc.transaction
    rpc.transaction = lambda signature: fetched.append(signature) or read(signature)
    report = discover(rpc, http, MINT, Discovery(max_wallet_transactions=10), clock=lambda: NOW)
    assert report.left_out["too many transactions to account for (bot-like)"] == 1
    assert "busy-now" in fetched and not [s for s in fetched if s.startswith("busy-old")]


def test_holders_unavailable_is_a_note_not_a_failure():
    chain = FakeChain()
    w = scenario(chain)
    chain.rpc_errors["getTokenLargestAccounts"] = [429] * 5
    report, _ = run(chain)
    assert w["whale"] not in [t.address for t in report.traders]
    assert any("Top holders were not checked" in note for note in report.notes)
