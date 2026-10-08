"""End-to-end cycles against the fake chain: every rule, exclusions, classification, cooldowns, restarts."""

import json
import threading
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from tests.fakes import (CPMM_AUTHORITY, METEORA_DLMM, MINT, RAYDIUM_CPMM, TELEGRAM_ENV, TELEGRAM_TOKEN,
                         TOKEN_2022_PROGRAM, UNIT, Harness, addr, make_tx, network_error, plain, populate)
from watcher.config import Heartbeat
from watcher.monitor import next_heartbeat, run_forever
from watcher.util import short


def test_first_cycle_records_a_baseline_without_alerting(tmp_path):
    h = Harness(tmp_path)
    populate(h.chain)
    report = h.cycle()
    assert report.ok and report.hits == [] and h.take() == []
    assert set(h.monitor.state["owners"]) == {addr(f"whale{i}") for i in range(1, 6)}


def test_excluded_lp_wallet_is_ignored(tmp_path):
    lp = addr("lp-wallet")
    h = Harness(tmp_path, exclude_owners=[lp], labels={lp: "LP pool"})
    populate(h.chain)
    lp_account = h.chain.hold(lp, 500_000_000)  # the largest holder
    h.cycle()
    h.chain.set_balance(lp_account, 100_000_000)  # -80%: what a pool looks like when buyers drain it
    report = h.cycle()
    assert report.ok and report.hits == [] and h.take() == []
    assert lp not in h.monitor.state["owners"]


def test_dex_pool_wallets_are_excluded_automatically(tmp_path):
    h = Harness(tmp_path)
    accounts = populate(h.chain)
    dlmm_pool = addr("meteora-pool")
    h.chain.programs[dlmm_pool] = METEORA_DLMM  # an account owned by the Meteora DLMM program
    dlmm_account = h.chain.hold(dlmm_pool, 200_000_000)
    h.cycle()
    h.chain.set_balance(accounts["pool"], 100_000_000)  # Raydium CPMM vault, -67%
    h.chain.set_balance(dlmm_account, 50_000_000)       # Meteora pool, -75%
    assert h.cycle().hits == [] and h.take() == []
    assert CPMM_AUTHORITY not in h.monitor.state["owners"] and dlmm_pool not in h.monitor.state["owners"]


def test_holder_drop_alert_classifies_a_dex_sell(tmp_path):
    h = Harness(tmp_path)
    accounts = populate(h.chain)
    h.cycle()
    whale = addr("whale2")
    h.chain.set_balance(accounts["whale2"], 28_000_000)  # 40M -> 28M: -30%
    h.chain.add_tx(make_tx("sell-sig-1", fee_payer=whale, block_time=int(h.now) + 30, programs=[RAYDIUM_CPMM],
                           token_moves=[(whale, MINT, 40_000_000 * UNIT, 28_000_000 * UNIT, 6),
                                        (CPMM_AUTHORITY, MINT, 300_000_000 * UNIT, 312_000_000 * UNIT, 6)],
                           lamports={whale: (10**9, 10**9 + 12_500_000_000 - 5000)}),
                   accounts["whale2"])
    report = h.cycle()
    assert [hit.rule for hit in report.sent] == ["holder_drop_pct"]
    [message] = h.take()
    assert "Your rule holder_drop_pct = 20% within 60 min" in message
    assert "40,000,000 → 28,000,000 TEST (−30.0%)" in message
    assert "SELL via Raydium CPMM: −12,000,000 TEST, received 12.50 SOL" in message
    assert 'href="https://solscan.io/tx/sell-sig-1"' in message
    assert f'href="https://solscan.io/account/{whale}"' in message


def test_drop_below_threshold_does_not_alert(tmp_path):
    h = Harness(tmp_path)
    accounts = populate(h.chain)
    h.cycle()
    h.chain.set_balance(accounts["whale1"], 41_000_000)  # -18% < 20%
    assert h.cycle().hits == [] and h.take() == []


def test_holder_who_sells_out_and_closes_the_account_alerts(tmp_path):
    h = Harness(tmp_path)
    accounts = populate(h.chain)
    h.cycle()
    h.chain.close(accounts["whale3"])  # account closed: it no longer exists on chain
    h.cycle()
    [message] = h.take()
    assert "30,000,000 → 0 TEST (−100.0%)" in message and "now holds 0 (sold out)" in message


def test_holder_is_still_watched_after_leaving_the_top_20(tmp_path):
    h = Harness(tmp_path)
    accounts = populate(h.chain)
    h.cycle()
    for i in range(25):  # 25 bigger accounts push whale5 out of the top 20
        h.chain.hold(addr(f"newcomer{i}"), 60_000_000)
    h.chain.set_balance(accounts["whale5"], 5_000_000)  # -50%
    assert accounts["whale5"] not in h.chain.largest()
    h.cycle()
    [message] = h.take()
    assert short(addr("whale5")) in message and "10,000,000 → 5,000,000 TEST (−50.0%)" in message


def test_creator_wallet_any_outflow_alerts_and_names_the_recipient(tmp_path):
    creator, friend = addr("creator"), addr("friend")
    h = Harness(tmp_path, always_alert_owners=[creator], labels={creator: "Creator", friend: "Friend"})
    populate(h.chain)
    creator_account = h.chain.hold(creator, 500_000)  # smaller than every top-20 account
    h.cycle()
    assert creator_account not in h.chain.largest()
    assert creator in h.monitor.state["owners"]

    h.chain.set_balance(creator_account, 495_000)  # only -1%, but any outflow counts
    h.chain.hold(friend, 5_000)
    h.chain.add_tx(make_tx("creator-transfer", fee_payer=creator, block_time=int(h.now) + 20,
                           token_moves=[(creator, MINT, 500_000 * UNIT, 495_000 * UNIT, 6),
                                        (friend, MINT, None, 5_000 * UNIT, 6)]),
                   creator_account)
    h.cycle()
    [message] = h.take()
    assert "Your rule always_alert_owners (any outflow)" in message
    assert "Creator (" in message and "500,000 → 495,000 TEST (−1.0%)" in message
    assert "TRANSFER −5,000 TEST to Friend (" in plain(message)

    h.cycle()
    assert h.take() == []  # nothing new: no repeat
    h.chain.set_balance(creator_account, 600_000)  # an inflow raises the reference, no alert
    h.cycle()
    assert h.take() == []
    h.chain.set_balance(creator_account, 590_000)  # the next outflow alerts again
    h.cycle()
    [message] = h.take()
    assert "600,000 → 590,000" in message


def test_always_alert_whale_in_the_top_20_costs_no_extra_lookups(tmp_path):
    h = Harness(tmp_path, always_alert_owners=[addr("whale1")])
    populate(h.chain)
    h.cycle()  # the first cycle discovers the whale's token accounts
    assert h.chain.rpc_calls.count("getTokenAccountsByOwner") == 1
    h.chain.rpc_calls.clear()
    for _ in range(3):
        h.cycle()
    assert "getTokenAccountsByOwner" not in h.chain.rpc_calls


def test_tokens_moved_to_a_new_account_of_the_same_wallet_are_not_an_outflow(tmp_path):
    whale = addr("whale1")
    h = Harness(tmp_path, always_alert_owners=[whale])
    accounts = populate(h.chain)
    h.cycle()
    h.chain.set_balance(accounts["whale1"], 49_500_000)  # 500K leave the main account ...
    h.chain.hold(whale, 500_000)                          # ... into a new, small account of the same wallet
    h.cycle()
    assert h.take() == []
    assert h.monitor.state["owners"][whale]["history"][-1][1] == 50_000_000 * UNIT
    h.chain.set_balance(accounts["whale1"], 49_000_000)  # a real outflow afterwards still alerts
    h.cycle()
    [message] = h.take()
    assert "50,000,000 → 49,500,000 TEST (−1.0%)" in message


def test_combined_drop_fires_when_no_single_wallet_crosses_its_threshold(tmp_path):
    h = Harness(tmp_path, rules={"combined_drop_pct": 10})
    accounts = populate(h.chain)
    h.cycle()
    for name, tokens in (("whale1", 50e6), ("whale2", 40e6), ("whale3", 30e6), ("whale4", 20e6), ("whale5", 10e6)):
        h.chain.set_balance(accounts[name], tokens * 0.88)  # each -12% (< holder_drop_pct 20%)
    h.cycle()
    [message] = h.take()
    assert "Your rule combined_drop_pct = 10% within 60 min" in message
    assert "holder_drop_pct" not in message
    assert "5 watched wallets together: 150,000,000 → 132,000,000 TEST (−12.0%)" in message


def test_combined_drop_ignores_wallets_joining_or_leaving_the_watch_list(tmp_path):
    h = Harness(tmp_path, rules={"holder_drop_pct": None, "combined_drop_pct": 10}, max_tracked_owners=6)
    populate(h.chain)
    h.cycle()
    h.chain.hold(addr("new-whale-1"), 70_000_000)  # joins the top 5; whale5 stays watched (6 wallets)
    h.cycle()
    h.chain.hold(addr("new-whale-2"), 65_000_000)  # 7 wallets > max 6: the smallest (whale5) is dropped
    h.cycle()
    assert addr("whale5") not in h.monitor.state["owners"]
    assert h.take() == []


def test_stop_price(tmp_path):
    h = Harness(tmp_path, rules={"stop_price_usd": 0.001})
    populate(h.chain)
    h.cycle()
    assert h.take() == []
    h.chain.set_market(price=0.0009, liquidity=500_000)
    h.cycle()
    [message] = h.take()
    assert "Your rule stop_price_usd = $0.001000" in message
    assert "Price $0.0009000 is at or below your stop price $0.001000" in message


def test_trailing_stop_uses_the_peak_persisted_across_a_restart(tmp_path):
    h = Harness(tmp_path, rules={"trailing_stop_pct": 30})
    populate(h.chain)
    h.cycle()
    h.chain.set_market(price=0.004, liquidity=600_000)
    h.cycle()
    assert h.monitor.state["peak"]["usd"] == 0.004
    h.restart()
    h.chain.set_market(price=0.0029, liquidity=600_000)  # 27.5% below the peak
    h.cycle()
    assert h.take() == []
    h.chain.set_market(price=0.0027, liquidity=600_000)  # 32.5% below the peak
    h.cycle()
    [message] = h.take()
    assert "Your rule trailing_stop_pct = 30%" in message
    assert "peak $0.004000" in message and "32.5% below the peak" in message


def test_low_liquidity_and_missing_pair(tmp_path):
    h = Harness(tmp_path, rules={"min_liquidity_usd": 100_000})
    populate(h.chain)
    h.cycle()
    assert h.take() == []
    h.chain.set_market(price=0.002, liquidity=40_000)
    h.cycle()
    [message] = h.take()
    assert "Liquidity $40,000 is below your minimum $100,000" in message
    h.chain.pairs = []
    h.cycle()
    assert h.take() == []  # one empty reply could be an API hiccup
    h.cycle()
    [message] = h.take()
    assert "No trading pair found on two checks in a row" in message
    h.chain.set_market(price=0.002, liquidity=600_000)
    h.cycle()
    h.chain.pairs = []
    h.cycle()
    assert h.take() == []  # the streak restarts once a pair is back


def test_cooldown_holds_back_repeats_of_an_ongoing_condition_then_reminds(tmp_path):
    h = Harness(tmp_path, rules={"stop_price_usd": 0.001})
    populate(h.chain)
    h.chain.set_market(price=0.0005, liquidity=500_000)
    h.cycle()
    assert len(h.take()) == 1
    for _ in range(58):  # inside the 60-minute cooldown
        h.cycle()
    assert h.take() == []
    h.cycle(minutes=2)  # cooldown over and the price is still below the stop: a reminder
    assert len(h.take()) == 1


def test_one_holder_event_alerts_once_even_after_the_cooldown(tmp_path):
    h = Harness(tmp_path)
    accounts = populate(h.chain)
    h.cycle()
    h.chain.set_balance(accounts["whale1"], 30_000_000)
    h.cycle()
    assert len(h.take()) == 1
    for _ in range(120):  # two hours with the balance unchanged; cooldown is 30 minutes
        h.cycle()
    assert h.take() == []


def test_a_drop_during_cooldown_is_reported_when_the_cooldown_ends(tmp_path):
    h = Harness(tmp_path, cooldowns={"holder_drop_pct": 90})
    accounts = populate(h.chain)
    h.cycle()
    h.chain.set_balance(accounts["whale1"], 35_000_000)  # -30%: alert
    h.cycle()
    assert len(h.take()) == 1
    h.cycle(minutes=29)
    h.chain.set_balance(accounts["whale1"], 0)  # sells the rest, inside the 90-minute cooldown
    h.cycle()
    assert h.take() == []
    for _ in range(60):  # the 60-minute window passes before the cooldown ends ...
        h.cycle()
    [message] = h.take()  # ... and the sell-out is still reported
    assert "35,000,000 → 0 TEST (−100.0%)" in message


def test_restart_keeps_history_and_does_not_resend_old_alerts(tmp_path):
    h = Harness(tmp_path)
    accounts = populate(h.chain)
    h.cycle()
    h.restart()
    h.chain.set_balance(accounts["whale1"], 25_000_000)
    h.cycle()
    assert len(h.take()) == 1  # measured against the balance seen before the restart
    for _ in range(3):
        h.restart()
        h.cycle()
    assert h.take() == []
    saved = json.loads(Path(h.monitor.cfg.state_file).read_text(encoding="utf-8"))
    assert f"holder_drop_pct:{addr('whale1')}" in saved["alerts"]


def test_every_hit_in_a_cycle_goes_into_one_message(tmp_path):
    h = Harness(tmp_path, rules={"stop_price_usd": 0.001, "min_liquidity_usd": 100_000})
    accounts = populate(h.chain)
    h.cycle()
    h.chain.set_balance(accounts["whale1"], 10_000_000)
    h.chain.set_balance(accounts["whale2"], 10_000_000)
    h.chain.set_market(price=0.0008, liquidity=50_000)
    h.cycle()
    [message] = h.take()
    assert "4 of your rules fired" in message
    assert message.count("Your rule holder_drop_pct") == 2
    assert "Your rule stop_price_usd" in message and "Your rule min_liquidity_usd" in message
    assert "Price $0.0008000 · liquidity $50,000" in message


def test_failure_alert_after_consecutive_failed_cycles_then_recovery(tmp_path):
    h = Harness(tmp_path)
    populate(h.chain)
    h.cycle()
    h.chain.rpc_down = True
    for _ in range(4):
        assert not h.cycle().ok
    assert h.take() == []
    h.cycle()
    [message] = h.take()
    assert "5 cycles in a row failed" in message
    assert "holders (Solana RPC): RPC getTokenSupply: HTTP 503" in message
    h.cycle()
    assert h.take() == []  # not repeated every cycle
    h.chain.rpc_down = False
    assert h.cycle().ok
    [message] = h.take()
    assert "recovered after 6 failed cycle(s)" in plain(message)


def test_a_price_outage_does_not_block_holder_rules(tmp_path):
    h = Harness(tmp_path)
    accounts = populate(h.chain)
    h.cycle()
    h.chain.dex_down = True
    h.chain.set_balance(accounts["whale1"], 25_000_000)
    report = h.cycle()
    assert not report.ok and report.errors[0].startswith("price/liquidity (DEX Screener)")
    [message] = h.take()
    assert "Your rule holder_drop_pct" in message and "unavailable this cycle" in message


def test_a_bug_in_one_cycle_does_not_crash_the_monitor(tmp_path, monkeypatch):
    h = Harness(tmp_path)
    populate(h.chain)
    h.chain.dex_payload = "<html>not json</html>"

    def broken(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("watcher.monitor.evaluate", broken)
    report = h.cycle()
    assert not report.ok
    assert any("not JSON" in error for error in report.errors)
    assert any("internal error: RuntimeError: boom" in error for error in report.errors)
    assert Path(h.monitor.cfg.state_file).exists()  # state still saved


def test_daily_heartbeat_at_10am_new_york(tmp_path):
    new_york = ZoneInfo("America/New_York")
    start = datetime(2026, 1, 15, 9, 0, tzinfo=new_york).timestamp()
    h = Harness(tmp_path, start=start, heartbeat={"enabled": True, "time": "10:00", "timezone": "America/New_York"})
    populate(h.chain)
    h.cycle(minutes=50)  # 09:50: the first run only notes the time
    h.cycle(minutes=9)   # 09:59
    assert h.take() == []
    h.cycle(minutes=2)   # 10:01
    [message] = h.take()
    assert "daily heartbeat" in message and "Price $0.002000" in message
    assert "Top 5 holders (pools and excluded wallets not counted) hold 15.0% of supply" in message
    h.cycle(minutes=60)
    assert h.take() == []
    h.now = datetime(2026, 1, 16, 9, 59, tzinfo=new_york).timestamp()
    h.cycle(minutes=0)
    assert h.take() == []
    h.cycle(minutes=1.5)
    assert len(h.take()) == 1


@pytest.mark.parametrize("after, expected_utc", [
    ((2026, 1, 15, 9, 0), "2026-01-15 15:00"),    # EST (UTC-5)
    ((2026, 7, 15, 10, 30), "2026-07-16 14:00"),  # EDT (UTC-4), already past 10:00
    ((2026, 3, 7, 10, 0), "2026-03-08 14:00"),    # across the DST switch on 2026-03-08
])
def test_next_heartbeat_is_dst_aware(after, expected_utc):
    after_ts = datetime(*after, tzinfo=ZoneInfo("America/New_York")).timestamp()
    due = next_heartbeat(after_ts, Heartbeat(10, 0, "America/New_York"))
    assert datetime.fromtimestamp(due, ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M") == expected_utc


def test_alerts_report_rules_and_never_give_advice(tmp_path):
    creator = addr("creator")
    h = Harness(tmp_path, always_alert_owners=[creator],
                rules={"stop_price_usd": 0.001, "trailing_stop_pct": 20, "min_liquidity_usd": 100_000,
                       "combined_drop_pct": 10})
    accounts = populate(h.chain)
    creator_account = h.chain.hold(creator, 2_000_000)
    h.cycle()
    for name in ("whale1", "whale2", "whale3"):
        h.chain.set_balance(accounts[name], 1_000_000)
    h.chain.set_balance(creator_account, 0)
    h.chain.set_market(price=0.0005, liquidity=10_000)
    h.cycle()
    [message] = h.take()
    assert message.count("Your rule") >= 6
    lowered = message.lower()
    for advice in ("should", "sell now", "buy now", "recommend", "advice"):
        assert advice not in lowered


def test_secrets_never_reach_logs_or_state(tmp_path, caplog):
    helius_key = "helius-secret-key-1234567890"
    env = {**TELEGRAM_ENV, "HELIUS_API_KEY": helius_key}
    h = Harness(tmp_path, env=env, rpc_url="https://api.mainnet-beta.solana.com")
    assert "helius-rpc.com" in h.monitor.rpc.url
    accounts = populate(h.chain)
    h.cycle()
    h.chain.telegram_failure = network_error(
        f"HTTPSConnectionPool: Max retries exceeded with url: /bot{TELEGRAM_TOKEN}/sendMessage")
    h.chain.set_balance(accounts["whale1"], 1_000_000)
    with caplog.at_level("DEBUG"):
        h.cycle()
        h.chain.telegram_failure = 400
        h.cycle()
        h.chain.rpc_errors["getTokenSupply"] = [{"code": -32602, "message": f"bad url /?api-key={helius_key}"}]
        h.cycle()
    text = caplog.text + Path(h.monitor.cfg.state_file).read_text(encoding="utf-8")
    assert "Telegram delivery failed" in caplog.text
    assert "alert delivery failed" in caplog.text  # a delivery failure marks the cycle as failed
    assert TELEGRAM_TOKEN not in text and helius_key not in text


def test_the_loop_runs_holder_cycles_and_trader_checks_on_their_own_schedules(monkeypatch):
    clock, calls = [0.0], []

    class Job:
        def __init__(self, name, every):
            self.name = name
            self.cfg = SimpleNamespace(poll_seconds=every, traders=SimpleNamespace(poll_seconds=every))

        def run_cycle(self):
            calls.append((self.name, clock[0]))
            if len(calls) == 6:
                raise KeyboardInterrupt  # stop the test here

    def wait(self, timeout=None):
        clock[0] += timeout
        return self.is_set()

    monkeypatch.setattr("watcher.monitor.time.monotonic", lambda: clock[0])
    monkeypatch.setattr(threading.Event, "wait", wait)
    holders, traders = Job("holders", 30), Job("traders", 60)
    holders.announce_start = lambda: None
    with pytest.raises(KeyboardInterrupt):
        run_forever(holders, traders, install_signals=False)
    assert calls == [("holders", 0), ("traders", 0), ("holders", 30), ("holders", 60), ("traders", 60), ("holders", 90)]
    assert holders.trader_watch is traders  # the heartbeat reports on the trader watch


def test_token_2022_holders_are_supported(tmp_path):
    h = Harness(tmp_path)
    accounts = populate(h.chain, program=TOKEN_2022_PROGRAM)
    h.cycle()
    h.chain.set_balance(accounts["whale1"], 20_000_000)
    h.cycle()
    [message] = h.take()
    assert "50,000,000 → 20,000,000 TEST (−60.0%)" in message
