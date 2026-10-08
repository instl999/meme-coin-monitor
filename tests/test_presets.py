"""Built-in traders: scoring a wallet's last 30 days across every token, picking the good ones (no
scalpers, no bots), the shipped and refreshed lists, and the commands that watch them."""

import json
from argparse import Namespace
from types import SimpleNamespace

import pytest

from tests.fakes import (ALCHEMY_ENV, RPC_URL, TELEGRAM_ENV, USDC_ON, FakeChain, FakeResponse, addr, evm_addr,
                         evm_pair, gecko_pool, gecko_trade, make_tx, plain, write_config)
from tests.test_wizard import Script, args
from watcher import presets, wizard
from watcher.chains import ARBITRUM, BASE, CHAINS, SOLANA
from watcher.config import load_config
from watcher.evm import EvmRPC, client as evm_client, source
from watcher.gecko import BusyPool, Gecko
from watcher.presets import Criteria, score_trades
from watcher.rpc import HttpClient, SolanaRPC
from watcher.txparse import Trade

NOW = 1_790_000_000.0
HOUR, DAY = 3600, 86_400
WALLET = evm_addr("good-trader")


def trade(side, token, eth, at, *, tokens=1_000, slot=None, signer=True):
    """A Base trade of `tokens` for `eth` ETH (no fee, to keep the sums round)."""
    return Trade(owner=WALLET, side=side, mint=token, amount=tokens * 10**18, decimals=18, before=None, after=None,
                 native_change=-eth if side == "buy" else eth, block_time=at, slot=slot or int(at), signer=signer,
                 chain="base")


def round_trips(count, *, profit=0.5, size=1.0, held=6 * HOUR, start=NOW - 5 * DAY, signer=True, same_block=False):
    """Buys `size` ETH of each of `count` tokens a day apart, and sells each `held` seconds later."""
    trades = []
    for i in range(count):
        token, at = evm_addr(f"token-{i}"), start + i * DAY
        trades += [trade("buy", token, size, at, signer=signer),
                   trade("sell", token, size + profit, at + held, signer=signer, slot=int(at) if same_block else None)]
    return trades


def score(trades, *, complete=True, http=None):
    return score_trades(BASE, WALLET, trades, Criteria(), 3_000.0, http, NOW, complete)


def test_a_steady_profitable_trader_is_picked():
    card = score(round_trips(3))
    assert card.reason is None
    assert (card.tokens_traded, card.tokens_won, card.trades) == (3, 3, 6)
    assert card.pnl_usd == pytest.approx(4_500) and card.roi_pct == pytest.approx(50)
    assert card.avg_hold_hours == pytest.approx(6) and card.scalp_share == 0
    assert card.last_trade == NOW - 3 * DAY + 6 * HOUR
    assert [usd for _, usd in card.best_tokens] == [1_500, 1_500, 1_500]


@pytest.mark.parametrize("trades, complete, reason", [
    (round_trips(3, held=600), True, "scalper (sold most within 60 min)"),
    (round_trips(2), True, "traded fewer than 3 tokens"),
    (round_trips(3, profit=0.01), True, "made less than $500"),
    (round_trips(3, profit=0.5, size=100.0), True, "returned less than 10% of what it spent"),  # a whale's margin
    (round_trips(3, start=NOW - 20 * DAY), True, "no trade in 7 days"),
    (round_trips(3, same_block=True), True, "bot-like"),
    (round_trips(3, signer=False), True, "no trades of its own"),
    (round_trips(3), False, "too many transactions (bot-like)"),
])
def test_wallets_that_are_left_out(trades, complete, reason):
    assert score(trades, complete=complete).reason == reason


def test_losing_on_most_tokens_is_left_out_even_with_one_big_win():
    big = round_trips(1, profit=3.0)
    losers = [t for t in round_trips(3, profit=-0.2) if t.mint != evm_addr("token-0")]
    card = score(big + losers)
    assert card.pnl_usd == pytest.approx(7_800) and card.win_rate == pytest.approx(1 / 3)
    assert card.reason == "lost money on most tokens"


def test_holdings_are_valued_at_todays_price():
    world = FakeChain()
    world.evm_chain("base")
    held = evm_addr("still-held")
    world.token_pairs[held] = [evm_pair(BASE, held, 6.0)]  # 1,000 tokens now worth $6,000 = 2 ETH
    trades = round_trips(3) + [trade("buy", held, 1.0, NOW - DAY)]
    card = score(trades, http=HttpClient(session=world, sleep=lambda _s: None))
    assert card.tokens_traded == 4 and card.pnl_usd == pytest.approx(4_500 + 3_000)


# --- the lists -------------------------------------------------------------------------------------

PRO = evm_addr("pro-1")
SHIPPED = {"version": 1, "chains": {
    "base": [{"address": PRO, "label": "Base pro #1", "pnl_usd": 8200.4, "roi_pct": 64.0, "days": 30,
              "tokens_traded": 7, "win_rate": 0.71, "avg_hold_hours": 19.0, "last_trade": "2026-10-05",
              "as_of": "2026-10-07"},
             {"address": "not-an-address", "label": "broken"}],
    "solana": [{"address": addr("sol-pro"), "label": "Solana pro #1", "pnl_usd": 3000, "as_of": "2026-10-07"}]}}


@pytest.fixture
def shipped(tmp_path, monkeypatch):
    path = tmp_path / "presets.json"
    path.write_text(json.dumps(SHIPPED), encoding="utf-8")
    monkeypatch.setattr(presets, "SHIPPED", path)
    return path


def test_a_refreshed_list_wins_over_the_shipped_one(tmp_path, shipped):
    assert [e["address"] for e in presets.load(tmp_path)["base"]] == [PRO]  # the broken entry is skipped
    card = score(round_trips(3))
    path = presets.save_local(tmp_path, "base", [presets.entry(card, 1, "2026-10-08")], Criteria())
    lists = presets.load(tmp_path)
    assert lists["base"] == [{"address": WALLET, "label": "Base pro #1", "pnl_usd": 4500.0, "roi_pct": 50.0, "days": 30,
                              "tokens_traded": 3, "win_rate": 1.0, "avg_hold_hours": 6.0, "last_trade": "2026-09-18",
                              "best_tokens": [[evm_addr("token-0"), 1500.0], [evm_addr("token-1"), 1500.0],
                                              [evm_addr("token-2"), 1500.0]], "as_of": "2026-10-08"}]
    assert lists["solana"] == SHIPPED["chains"]["solana"]  # other chains keep the shipped list
    assert json.loads(path.read_text(encoding="utf-8"))["criteria"]["max_scalp_share"] == 0.5


def test_presets_add_list_and_remove(tmp_path, shipped):
    mine = evm_addr("my-own")
    path = write_config(tmp_path, traders={"wallets": [{"chain": "base", "address": mine, "label": "Mine"}]})
    console = Script()
    assert wizard.presets_command(path, args(action="add", chains=["base", "ethereum"]), console) == 0
    assert "No built-in list for Ethereum yet: presets refresh ethereum (or presets add --build)" in console.text
    assert "Watching 1 more built-in trader(s)" in console.text
    assert wizard.presets_command(path, args(action="add", chains=["base"]), console) == 0
    assert "Watching 0 more built-in trader(s)" in console.text  # already watched
    wallets = load_config(path, environ={}).traders.wallets
    assert [(w.chain, w.address, w.preset, w.label) for w in wallets] == [
        ("base", mine, False, "Mine"), ("base", PRO, True, "Base pro #1")]
    assert wallets[1].pnl_usd == 8200.4 and wallets[1].added == "2026-10-07"
    assert wizard.presets_command(path, args(action="list", chains=["base"]), console) == 0
    assert ("+8,200 $ in 30 d · 7 tokens · wins 71% · avg hold 19 h · last trade 2026-10-05  watched"
            in console.text)
    assert wizard.presets_command(path, args(action="remove", chains=["base"]), console) == 0
    assert "Stopped watching 1 built-in trader(s)" in console.text
    assert [w.address for w in load_config(path, environ={}).traders.wallets] == [mine]  # yours stay
    assert wizard.presets_command(path, args(action="add", chains=["polygon"]), console) == 2


# --- building a list ---------------------------------------------------------------------------------

def busy(base_token, *, usd=0.05, in_quote=0.00002, change=-15.0):
    return BusyPool(address=evm_addr(f"pool:{base_token}"), name="x", base_token=base_token,
                    quote_token=USDC_ON["arbitrum"], volume_h24=1e6, base_price_usd=usd, price_in_quote=in_quote,
                    price_in_native=usd / 2_575, change_h24=change)


@pytest.mark.parametrize("pool, kept", [
    (busy(evm_addr("dory")), True),                                                        # a meme coin
    (busy(evm_addr("mover"), usd=1.05, in_quote=1.05, change=-18.0), True),                # near $1 but moving
    (busy("0x" + "0" * 40, usd=2578.0, in_quote=2577.0, change=1.2), False),               # ETH in Uniswap v4
    (busy(ARBITRUM.wrapped, usd=2578.0, in_quote=2577.0), False),                          # WETH
    (busy("0x912ce59144191c1204e64559fe8253a0e49e6548", usd=0.19, in_quote=0.19), False),  # ARB, a major
    (busy("0x5d3a1ff2b6bab83b63cd9ad0787074081a52ef34", usd=1.0, in_quote=1.0, change=0.0), False),  # USDe
    (busy("0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf", usd=83_500.0, in_quote=1.001), False),      # cbBTC/WBTC
    (busy(evm_addr("susdai"), usd=1.116, in_quote=1.1168, change=0.05), False),            # yield-bearing dollar
    (busy(evm_addr("weeth"), usd=2_838.0, in_quote=1.102, change=-5.0), False),            # staked ETH
    (busy(evm_addr("dory"), usd=98.48, in_quote=98.4, change=-3.0), True),                 # a $98 token
])
def test_candidate_pools_skip_the_coin_majors_stablecoins_and_pegged_pairs(pool, kept):
    """Real cases from Arbitrum's busiest pools on 2026-10-07: the busy wallets of pegged pools are bots."""
    assert presets.tradable(pool, ARBITRUM) is kept


def test_the_pre_screen_spots_solana_bots_by_their_pace():
    """Real candidates from Solana's busiest pools on 2026-10-07 made 1,000 transactions in 1-4 hours."""
    world = FakeChain()
    rpc = SolanaRPC(RPC_URL, HttpClient(session=world, sleep=lambda _s: None))
    bot, person = addr("bot"), addr("person")
    for i in reversed(range(100)):  # oldest first: each new one is the latest
        world.add_tx(make_tx(f"bot-{i}", fee_payer=bot, block_time=int(NOW) - i * 30), bot)
        world.add_tx(make_tx(f"person-{i}", fee_payer=person, block_time=int(NOW) - i * 3600), person)
    kept, left_out = presets.prescreen(SOLANA, [bot, person], solana_rpc=rpc, now=NOW)
    assert kept == [person] and left_out == {bot: "bot-like (100 transactions within 2 h)"}


def test_geckoterminal_requests_are_paced_and_a_rate_limit_is_waited_out():
    """Its public API allows about 6 requests a minute and answers a 7th with HTTP 429 and "Retry-After: 0"."""
    clock, sleeps = [0.0], []

    def sleep(seconds):
        sleeps.append(round(seconds, 1))
        clock[0] += seconds

    replies = [FakeResponse(429, {"status": {"error_code": 429}}, {"Retry-After": "0"}),
               FakeResponse(200, {"data": []}), FakeResponse(200, {"data": []})]
    session = SimpleNamespace(request=lambda method, url, json=None, timeout=None: replies.pop(0))
    gecko = Gecko(HttpClient(session=session, sleep=sleep), clock=lambda: clock[0])
    assert gecko.trades(BASE, "0xpool") == [] and gecko.trades(BASE, "0xpool") == []
    assert sleeps == [61.0, 10.5] and not replies  # one wait for the limit, then the usual spacing

@pytest.fixture
def base_world(tmp_path, monkeypatch):
    """Base with two active traders in a busy pool: one holds for hours, one scalps."""
    world = FakeChain()
    base = world.evm_chain("base")
    http = HttpClient(session=world, sleep=lambda _s: None)
    rpc = EvmRPC(BASE, source(BASE, ALCHEMY_ENV).url, http)
    monkeypatch.setattr(wizard, "clients", lambda cfg, environ=None: (http, {"base": rpc}))
    monkeypatch.setattr(presets, "SHIPPED", tmp_path / "none.json")
    monkeypatch.setattr("watcher.presets.time.time", lambda: NOW)
    good, scalper, few, once = evm_addr("good"), evm_addr("scalper"), evm_addr("few"), evm_addr("once")
    pool = evm_addr("degen-pool")
    world.gecko_pools["base"] = [gecko_pool(BASE, evm_addr("weth-pool"), BASE.wrapped, USDC_ON["base"])]
    world.gecko_trending["base"] = [gecko_pool(BASE, pool, evm_addr("degen"), BASE.wrapped)]
    world.gecko_trades[pool] = [gecko_trade(good), gecko_trade(good, "sell"), gecko_trade(scalper),
                                gecko_trade(scalper, "sell"), gecko_trade(few), gecko_trade(few, "sell"),
                                gecko_trade(once),  # a smart-contract wallet: it never sends a transaction
                                *[gecko_trade(evm_addr("bot")) for _ in range(8)],  # in the sample too often
                                gecko_trade(evm_addr("tiny"), volume=50.0)]  # too small to follow
    block = 1001
    for wallet, held, tokens in ((good, 6 * HOUR, 3), (scalper, 600, 3), (few, 6 * HOUR, 2)):
        for i in range(tokens):
            token, at = evm_addr(f"{wallet}:{i}"), NOW - 5 * DAY + i * DAY
            base.swap(f"0x{wallet[2:10]}b{i}", wallet, block=block, token=token, side="buy", tokens=1_000, eth=1.0,
                      time=at)
            base.swap(f"0x{wallet[2:10]}s{i}", wallet, block=block + 1, token=token, side="sell", tokens=1_000,
                      eth=1.5, time=at + held)
            block += 2
    return world, good


def test_presets_refresh_builds_saves_and_watches_a_chains_list(tmp_path, base_world):
    world, good = base_world
    path = write_config(tmp_path)
    console = Script()
    assert wizard.presets_command(path, args(action="refresh", chains=["base"], add=True), console) == 0
    assert ("Base: 1 picked of 4 scored (1 picked; 1 scalper (sold most within 60 min); 1 traded fewer than 3 "
            "tokens; 1 smart-contract wallet (its trades are sent by others))") in console.text
    # the wallet with 2 tokens was ruled out from its transfers alone: only the others' trades were read
    assert world.evm["base"].calls.count("eth_getTransactionReceipt") == 12
    [entry] = presets.load(tmp_path)["base"]
    assert (entry["address"], entry["label"], entry["tokens_traded"]) == (good, "Base pro #1", 3)
    assert entry["pnl_usd"] == pytest.approx(4_500, abs=1)  # gas included
    [wallet] = load_config(path, environ={}).traders.wallets
    assert (wallet.chain, wallet.address, wallet.preset, wallet.label) == ("base", good, True, "Base pro #1")


def test_presets_add_can_build_a_missing_list_first(tmp_path, base_world):
    _world, good = base_world
    path = write_config(tmp_path)
    console = Script()
    assert wizard.presets_command(path, args(action="add", chains=["base"], build=True), console) == 0
    assert "Building the Base list" in console.text and "Watching 1 more built-in trader(s)" in console.text
    assert [w.address for w in load_config(path, environ={}).traders.wallets] == [good]


def test_setup_builds_a_missing_list_and_watches_it(tmp_path, base_world, monkeypatch):
    _world, good = base_world
    for name, value in {**TELEGRAM_ENV, **ALCHEMY_ENV}.items():
        monkeypatch.setenv(name, value)
    path = write_config(tmp_path)
    # keep Telegram · Base · build its list now · watch it · no token search · no test alert
    console = Script("n", "3", "y", "y", "n", "n")
    assert wizard.setup_command(path, tmp_path / ".env", console) == 0
    [wallet] = load_config(path).traders.wallets
    assert (wallet.chain, wallet.address, wallet.preset) == ("base", good, True)
    assert "Base: 1 trader(s) with a good last 30 days" in console.text
    assert "Alchemy compute units a month" in console.text and not console.answers


def test_without_an_ankr_key_lists_are_not_built_but_setup_keeps_the_keyless_chains(tmp_path, monkeypatch):
    world = FakeChain()
    for chain_id in ("base", "bsc"):
        world.evm_chain(chain_id)
    http = HttpClient(session=world, sleep=lambda _s: None)

    def keyless_clients(cfg, environ=None):
        rpcs = {chain_id: evm_client(CHAINS[chain_id], http, {}) for chain_id in ("base", "bsc")}
        return http, {chain_id: rpc for chain_id, rpc in rpcs.items() if rpc is not None}

    monkeypatch.setattr(wizard, "clients", keyless_clients)
    monkeypatch.setattr(presets, "SHIPPED", tmp_path / "none.json")
    for name in ("ANKR_API_KEY", "ALCHEMY_API_KEY", "BASE_RPC_URL", "BSC_RPC_URL"):
        monkeypatch.delenv(name, raising=False)
    for name, value in TELEGRAM_ENV.items():
        monkeypatch.setenv(name, value)
    path = write_config(tmp_path)
    console = Script()
    assert wizard.presets_command(path, args(action="refresh", chains=["base"]), console) == 0
    assert "Base: needs ANKR_API_KEY in .env (free at https://www.ankr.com/rpc/, or run setup)." in console.text
    # keep Telegram · Base and BNB Chain · no Ankr key · no token search · no test alert
    console = Script("n", "3,4", "n", "n", secrets=[""])
    assert wizard.setup_command(path, tmp_path / ".env", console) == 0
    assert "Skipping BNB Chain for now (it needs the key)" in console.text
    assert "No built-in list for Base; build one later with: python holder_watch.py presets refresh base" in console.text
    assert not console.answers


def test_renewing_swaps_only_the_built_in_wallets(tmp_path):
    old, kept, mine, new = (evm_addr(name) for name in ("old", "kept", "mine", "new"))
    sol = addr("sol-pro")
    path = write_config(tmp_path, traders={"wallets": [
        {"chain": "base", "address": old, "label": "Base pro #1", "preset": True},
        {"chain": "base", "address": kept, "label": "Base pro #2", "preset": True, "tokens": "major", "pnl_usd": 1},
        {"chain": "base", "address": mine, "label": "Mine"},
        {"address": sol, "label": "Solana pro #1", "preset": True}]})
    entries = [{"address": kept, "label": "Base pro #1", "pnl_usd": 900.0, "roi_pct": 30.0, "as_of": "2026-11-01"},
               {"address": new, "label": "Base pro #2", "pnl_usd": 700.0, "roi_pct": 20.0, "as_of": "2026-11-01"},
               {"address": mine, "label": "Base pro #3", "pnl_usd": 600.0, "roi_pct": 15.0, "as_of": "2026-11-01"}]
    backup, counts = wizard.renew_presets(path, {"base": entries})
    assert counts == {"base": (1, 1, 1)}  # added, kept, dropped
    wallets = json.loads(path.read_text(encoding="utf-8"))["traders"]["wallets"]
    assert [(w.get("chain", "solana"), w["address"], w["label"]) for w in wallets] == [
        ("base", kept, "Base pro #1"), ("base", mine, "Mine"), ("solana", sol, "Solana pro #1"), ("base", new, "Base pro #2")]
    assert wallets[0]["tokens"] == "major" and wallets[0]["pnl_usd"] == 900.0  # your setting stays, figures update
    assert backup.exists()


def test_the_monthly_renewal_reports_and_flags_a_restart(tmp_path, base_world, monkeypatch):
    world, good = base_world
    for name, value in TELEGRAM_ENV.items():
        monkeypatch.setenv(name, value)
    stale = evm_addr("last-months-pick")
    path = write_config(tmp_path, traders={"wallets": [
        {"chain": "base", "address": stale, "label": "Base pro #1", "preset": True}]})
    console = Script()
    assert wizard.presets_command(path, args(action="renew"), console) == 0
    assert [w["address"] for w in json.loads(path.read_text(encoding="utf-8"))["traders"]["wallets"]] == [good]
    assert (tmp_path / wizard.RENEWED_FLAG).exists()  # the systemd timer restarts the monitor
    message = plain(world.telegram[-1])
    assert "Built-in traders renewed" in message and "• Base: 1 trader(s) now, 1 new, 0 kept, 1 dropped" in message
    assert "past results are not a prediction" in message


def test_a_month_without_a_new_pick_keeps_the_watched_traders(tmp_path, base_world, monkeypatch):
    world, _good = base_world
    for name, value in TELEGRAM_ENV.items():
        monkeypatch.setenv(name, value)
    stale = evm_addr("last-months-pick")
    path = write_config(tmp_path, traders={"wallets": [
        {"chain": "base", "address": stale, "label": "Base pro #1", "preset": True}]})
    before = path.read_text(encoding="utf-8")
    world.gecko_trades.clear()  # no candidates this month
    assert wizard.presets_command(path, args(action="renew"), Script()) == 0
    assert path.read_text(encoding="utf-8") == before and not (tmp_path / wizard.RENEWED_FLAG).exists()
    assert "Base: no trader passed this month; still watching the previous ones" in plain(world.telegram[-1])


def test_scorecard_of_one_wallet(tmp_path, base_world):
    _world, good = base_world
    console = Script()
    command = Namespace(address=good.upper().replace("0X", "0x"), chain="base", days=None)
    assert wizard.scorecard_command(write_config(tmp_path), command, console) == 0
    assert f"{good} on Base, last 30 days:" in console.text
    assert "6 trades in 3 tokens · made money on 100% of them" in console.text
    assert "sold after 6 h on average · 0% sold within 60 min" in console.text
    assert "Built-in trader rules: passes" in console.text
    assert wizard.scorecard_command(write_config(tmp_path), Namespace(address=good, chain=None, days=None),
                                    console) == 2
    assert "add --chain ethereum, base, bsc or arbitrum" in console.text
