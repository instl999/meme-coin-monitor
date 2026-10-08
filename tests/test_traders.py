"""Trader watch: new trades of watched wallets become one alert per check, against the fake chain."""

from datetime import datetime
from zoneinfo import ZoneInfo

from tests.fakes import (CPMM_AUTHORITY, MINT, RAYDIUM_CPMM, UNIT, WSOL, Harness, addr, ata, make_tx, pair, plain,
                         populate, sol_for_usd_tx, swap_tx, transfer_tx)
from watcher import alerts
from watcher.util import short

TRADER = addr("trader")
WIF = addr("wif-mint")
WALLET = {"address": TRADER, "label": "TEST #1", "source_mint": MINT, "source_symbol": "TEST", "pnl_usd": 1200.0,
          "roi_pct": 240.0, "added": "2026-10-06"}


def harness(tmp_path, **traders):
    h = Harness(tmp_path, traders={"wallets": [WALLET], **traders})
    h.chain.set_market(price=0.002, liquidity=600_000)
    h.chain.token_pairs[WIF] = [pair(WIF, 0.0003, 85_000, dex="pumpswap", labels=(), symbol="WIF", name="Wif Two",
                                     created_ms=int((h.now - 3 * 3600) * 1000), market_cap=300_000)]
    return h


def trade(h, signature, mint, side, tokens, sol, before=0, **kwargs):
    tx = swap_tx(signature, TRADER, mint, side=side, tokens=tokens, sol=sol, before=before,
                 block_time=int(h.now) + 30, **kwargs)
    h.chain.add_tx(tx, TRADER, ata(TRADER, mint))


def test_first_check_only_notes_where_to_start(tmp_path):
    h = harness(tmp_path)
    trade(h, "old-buy", WIF, "buy", 1_000, 1)
    report = h.check()
    assert report.ok and report.trades == [] and h.take() == []
    assert h.traders.state["wallets"][f"solana:{TRADER}"]["last_signature"] == "old-buy"


def test_a_buy_alerts_with_the_token_and_the_position(tmp_path):
    h = harness(tmp_path)
    h.check()
    trade(h, "buy-1", WIF, "buy", 1_250_000, 3.2)
    report = h.check()
    assert report.ok and [t.side for _, t in report.sent] == ["buy"]
    [message] = h.take()
    text = plain(message)
    assert "Trader watch: 1 new trade" in text
    assert "1. BUY by TEST #1 (" in text and "discovered on TEST: +$1,200, +240%" in text
    assert "Bought 1,250,000 WIF for 3.20 SOL ($320.00)" in text and "new position" in text and "via Raydium CPMM" in text
    assert "Wif Two (WIF) · price $0.0003000 · liquidity $85,000 · market cap $300,000 · pool opened 3 h ago" in text
    assert 'href="https://solscan.io/tx/buy-1"' in message and 'href="https://dexscreener.com/solana/' in message
    h.check()
    assert h.take() == []  # reported once


def test_sells_report_how_much_of_the_position_went(tmp_path):
    h = harness(tmp_path)
    h.check()
    trade(h, "sell-part", MINT, "sell", 400_000, 6.5, before=1_000_000)
    trade(h, "sell-rest", MINT, "sell", 600_000, 8.1, before=600_000)
    h.check()
    [message] = h.take()
    text = plain(message)
    assert "Trader watch: 2 new trades" in text
    assert text.index("Sold 400,000 TEST for 6.50 SOL ($650.00)") < text.index("Sold 600,000 TEST for 8.10 SOL ($810.00)")
    assert "40% of the position, 600,000 left" in text and "sold the whole position" in text


def test_small_trades_transfers_and_failed_transactions_are_not_alerted(tmp_path):
    h = harness(tmp_path, min_trade_usd=50)
    h.check()
    trade(h, "tiny-buy", WIF, "buy", 1_000, 0.2)
    h.chain.add_tx(transfer_tx("gift", TRADER, addr("friend"), MINT, 10_000, sender_before=50_000), TRADER)
    failed = swap_tx("failed-buy", TRADER, WIF, side="buy", tokens=5_000, sol=5)
    failed["meta"]["err"] = {"InstructionError": [2, {"Custom": 6001}]}
    h.chain.add_tx(failed, TRADER)
    report = h.check()
    assert report.ok and h.take() == [] and len(report.trades) == 2  # the small buy and the transfer
    assert h.traders.state["wallets"][f"solana:{TRADER}"]["last_signature"] == "failed-buy"


def test_only_the_discovery_token_when_tokens_is_source(tmp_path):
    by_hand = addr("added-by-hand")
    h = harness(tmp_path, tokens="source", alert_buys=False, wallets=[WALLET, {"address": by_hand, "label": "Friend"}])
    h.check()
    trade(h, "wif-buy", WIF, "buy", 1_000_000, 2)
    trade(h, "test-buy", MINT, "buy", 1_000_000, 2)
    trade(h, "test-sell", MINT, "sell", 500_000, 3, before=1_000_000)
    h.chain.add_tx(swap_tx("friend-sell", by_hand, WIF, side="sell", tokens=1_000, sol=1, before=1_000,
                           block_time=int(h.now) + 40), by_hand)
    h.check()
    [message] = h.take()
    text = plain(message)
    assert "2 new trades" in text and "Sold 500,000 TEST" in text
    assert "SELL by Friend (" in text  # a wallet added by hand has no discovery token: every token counts


def test_a_failed_delivery_keeps_the_trades_for_the_next_check(tmp_path):
    h = harness(tmp_path)
    h.check()
    trade(h, "buy-1", WIF, "buy", 1_000_000, 2)
    h.chain.telegram_failure = 400
    report = h.check()
    assert not report.ok and "trade alert delivery failed" in report.errors[0]
    h.chain.telegram_failure = None
    trade(h, "buy-2", WIF, "buy", 500_000, 1, before=1_000_000)
    h.check()
    [message] = h.take()
    assert "2 new trades" in message and "adds to a position of 1,000,000" in plain(message)


def test_a_sell_paid_to_another_wallet_is_still_a_sell(tmp_path):
    h = harness(tmp_path)
    h.check()
    cold = addr("cold-wallet")
    moves = [(TRADER, MINT, 500_000 * UNIT, 300_000 * UNIT, 6, ata(TRADER)),
             (CPMM_AUTHORITY, MINT, 10**15, 10**15 + 200_000 * UNIT, 6, ata(CPMM_AUTHORITY)),
             (CPMM_AUTHORITY, WSOL, 10**15, 10**15 - 4 * 10**9, 9, ata(CPMM_AUTHORITY, WSOL)),
             (cold, WSOL, None, 4 * 10**9, 9, ata(cold, WSOL))]
    h.chain.add_tx(make_tx("sell-elsewhere", fee_payer=TRADER, token_moves=moves, programs=[RAYDIUM_CPMM],
                           block_time=int(h.now)), TRADER)
    h.check()
    [message] = h.take()
    text = plain(message)
    assert "SELL" in text and "Sold 200,000 TEST at" in text and f"proceeds went to {cold[:4]}…" in text


def test_a_restart_keeps_the_place_and_never_repeats_alerts(tmp_path):
    h = harness(tmp_path)
    h.check()
    trade(h, "buy-1", WIF, "buy", 1_000_000, 2)
    h.check()
    assert len(h.take()) == 1
    h.restart()
    h.check()
    assert h.take() == []
    trade(h, "buy-2", WIF, "buy", 1_000_000, 2, before=1_000_000)
    h.check()
    [message] = h.take()
    assert "Bought 1,000,000 WIF" in plain(message)


def test_wallets_removed_from_config_are_forgotten(tmp_path):
    h = harness(tmp_path)
    h.check()
    h.traders.state["wallets"][f"solana:{addr('removed')}"] = {"last_signature": "x", "since": 0}
    h.traders._save()
    h.restart()
    assert set(h.traders.state["wallets"]) == {f"solana:{TRADER}"}


def test_failure_alert_after_consecutive_failed_checks_then_recovery(tmp_path):
    h = harness(tmp_path)
    h.check()
    h.chain.rpc_down = True
    for _ in range(4):
        assert not h.check().ok
    assert h.take() == []
    h.check()
    [message] = h.take()
    assert "Trader watch: 5 checks in a row failed" in message and "getSignaturesForAddress: HTTP 503" in message
    h.chain.rpc_down = False
    assert h.check().ok
    [message] = h.take()
    assert "Trader watch: recovered" in plain(message)


def test_startup_and_heartbeat_describe_the_trader_watch(tmp_path):
    new_york = ZoneInfo("America/New_York")
    start = datetime(2026, 1, 15, 9, 0, tzinfo=new_york).timestamp()
    h = Harness(tmp_path, start=start, heartbeat={"enabled": True, "time": "10:00", "timezone": "America/New_York"},
                traders={"wallets": [WALLET], "min_trade_usd": 25})
    startup = alerts.render_plain(alerts.build_startup("TEST", h.monitor.cfg, h.now))
    assert ("Trader watch: 1 wallet(s) (Solana 1) · alerts on buys and sells in any token, at least $25 · "
            "checked every 60 s") in startup
    populate(h.chain)
    h.cycle(minutes=50)
    h.check(minutes=0)
    h.cycle(minutes=11)
    [message] = h.take()
    assert "daily heartbeat" in message and "Trader watch: 1 wallet(s) (Solana 1) · 0 trade(s) alerted in the last 24 h" in message


JUP = "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN"  # on the built-in major-token list
BIG = addr("big-unlisted-token")
FAKE_CAP = addr("fake-cap-token")


def major_harness(tmp_path, **traders):
    h = harness(tmp_path, wallets=[{"address": TRADER, "label": "Trader 1", "tokens": "major"}], **traders)
    h.chain.token_pairs[JUP] = [pair(JUP, 0.45, 50_000_000, symbol="JUP", name="Jupiter", market_cap=1_100_000_000)]
    h.chain.token_pairs[BIG] = [pair(BIG, 2.0, 3_000_000, symbol="BIG", name="Big One", market_cap=900_000_000)]
    h.chain.token_pairs[FAKE_CAP] = [pair(FAKE_CAP, 1.0, 20_000, symbol="FAKE", name="Fake Cap",
                                          market_cap=5_000_000_000)]
    return h


def test_major_token_monitor_reports_only_major_token_trades(tmp_path):
    h = major_harness(tmp_path)
    h.check()
    trade(h, "jup-buy", JUP, "buy", 10_000, 30)        # on the list
    trade(h, "wif-buy", WIF, "buy", 1_000_000, 2)      # a small token: not major
    trade(h, "big-buy", BIG, "buy", 1_000, 15)         # not listed, but past $500M market cap with $1M+ liquidity
    trade(h, "fake-buy", FAKE_CAP, "buy", 1_000, 15)   # a huge market cap on shallow liquidity: not major
    h.chain.add_tx(sol_for_usd_tx("sol-sell", TRADER, side="sell", sol=50, usd=5_900, block_time=int(h.now) + 40),
                   TRADER)
    report = h.check()
    assert report.ok and len(report.trades) == 5
    [message] = h.take()
    text = plain(message)
    assert "Trader watch: 3 new trades" in text
    assert "1. BUY by Trader 1 (" in text and text.count("major-token monitor") == 3
    assert "Bought 10,000 JUP for 30.00 SOL" in text and "Bought 1,000 BIG for 15.00 SOL" in text
    assert "3. SELL by Trader 1" in text and "Sold 50.00 SOL for $5,900 in stablecoins" in text
    assert "SOL balance now 50.00 · via Jupiter" in text
    assert "WIF" not in text and "FAKE" not in text


def test_without_a_market_cap_threshold_only_the_list_counts(tmp_path):
    h = major_harness(tmp_path, major_min_market_cap_usd=None, major_mints=[WIF])
    h.check()
    trade(h, "big-buy", BIG, "buy", 1_000, 15)
    trade(h, "wif-buy", WIF, "buy", 1_000_000, 2)  # added to the list in config.json
    h.check()
    [message] = h.take()
    assert "1 new trade" in message and "Bought 1,000,000 WIF" in plain(message)


def test_a_wallet_can_have_its_own_minimum(tmp_path):
    h = harness(tmp_path, wallets=[{**WALLET, "min_trade_usd": 500}], min_trade_usd=10)
    h.check()
    trade(h, "small", WIF, "buy", 1_000, 2)  # $200: below its own $500 minimum
    trade(h, "large", WIF, "buy", 1_000, 6, before=1_000)
    h.check()
    [message] = h.take()
    assert "1 new trade" in message and "for 6.00 SOL" in plain(message)


def test_startup_names_wallets_with_their_own_settings(tmp_path):
    h = major_harness(tmp_path)
    text = alerts.render_plain(alerts.build_startup("TEST", h.monitor.cfg, h.now))
    assert (f"alerts on buys and sells in any token, at least $10 · checked every 60 s · "
            f"Trader 1 ({short(TRADER)}): major tokens, at least $10") in text


def test_alert_text_never_gives_advice(tmp_path):
    h = harness(tmp_path)
    h.check()
    trade(h, "buy-1", WIF, "buy", 1_250_000, 3.2)
    trade(h, "sell-1", MINT, "sell", 100_000, 1.5, before=100_000)
    h.check()
    [message] = h.take()
    for advice in ("should", "sell now", "buy now", "recommend", "advice", "copy"):
        assert advice not in message.lower()
