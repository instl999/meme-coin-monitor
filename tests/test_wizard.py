"""Setup commands: Telegram connection, token search, choosing traders, and safe edits of config.json / .env."""

import io
import json
import os
from argparse import Namespace
from pathlib import Path

import pytest

import holder_watch
from tests.fakes import (ANKR_ENV, MINT, TELEGRAM_TOKEN, FakeChain, addr, evm_addr, evm_pair, pair, write_config)
from tests.test_discover import NOW, POOL, scenario
from watcher import wizard
from watcher.chains import BASE, SOLANA
from watcher.config import ConfigError, edit_config, load_config, update_dotenv
from watcher.discover import DiscoveryReport, RankedTrader
from watcher.market import search_tokens
from watcher.rpc import HttpClient, SolanaRPC
from watcher.wizard import (Console, choose_token, parse_selection, save_traders, setup_telegram, switch_token)


class Script(Console):
    """A console that answers prompts from a list and records everything shown."""

    def __init__(self, *answers, secrets=()):
        self.answers, self.secrets, self.prompts = list(answers), list(secrets), []
        super().__init__(out=io.StringIO(), input_fn=self._answer, secret_fn=self._secret)

    def _answer(self, prompt):
        self.prompts.append(prompt)
        return self.answers.pop(0)

    def _secret(self, prompt):
        self.prompts.append(prompt)
        return self.secrets.pop(0)

    @property
    def text(self):
        return self.out.getvalue()


def http_for(chain):
    return HttpClient(session=chain, sleep=lambda _s: None)


def private_chat(update_id=7, chat_id=4242):
    return {"update_id": update_id, "message": {"message_id": 1, "text": "/start",
                                                "chat": {"id": chat_id, "type": "private", "first_name": "Lee",
                                                         "username": "lee"}}}


# --- Telegram -----------------------------------------------------------------------------------------

def test_telegram_setup_finds_the_chat_tests_it_and_saves_both_values(tmp_path):
    chain = FakeChain()
    chain.updates = [private_chat()]
    env = tmp_path / ".env"
    env.write_text("# my settings\nHELIUS_API_KEY=abc123\nTELEGRAM_BOT_TOKEN=\nTELEGRAM_CHAT_ID=\n", encoding="utf-8")
    console, environ = Script("y", secrets=[TELEGRAM_TOKEN]), {}
    assert setup_telegram(env, http_for(chain), console, environ=environ)
    assert "Bot @test_watch_bot found" in console.text and "Lee (@lee), private chat 4242" in console.prompts[-1]
    assert chain.telegram_chats == ["4242"] and "connected" in chain.telegram[0]
    assert env.read_text(encoding="utf-8") == ("# my settings\nHELIUS_API_KEY=abc123\n"
                                               f"TELEGRAM_BOT_TOKEN={TELEGRAM_TOKEN}\nTELEGRAM_CHAT_ID=4242\n")
    assert environ == {"TELEGRAM_BOT_TOKEN": TELEGRAM_TOKEN, "TELEGRAM_CHAT_ID": "4242"}
    assert TELEGRAM_TOKEN not in console.text  # typed hidden, never shown


def test_telegram_setup_rejects_bad_tokens_then_waits_for_a_message(tmp_path):
    chain = FakeChain()
    chain.incoming = [private_chat(update_id=12, chat_id=-100555)]
    unknown = "987654321:" + "x" * 35
    console = Script("y", secrets=["not-a-token", unknown, TELEGRAM_TOKEN])
    assert setup_telegram(tmp_path / ".env", http_for(chain), console, environ={})
    assert "doesn't look like a bot token" in console.text
    assert "Telegram rejected this token: HTTP 401 (Unauthorized)" in console.text
    assert "Now send any message to @test_watch_bot" in console.text
    assert "TELEGRAM_CHAT_ID=-100555" in (tmp_path / ".env").read_text(encoding="utf-8")


def test_telegram_setup_with_a_webhook_asks_for_the_chat_id(tmp_path):
    chain = FakeChain()
    chain.webhook = True
    console = Script("-1001234567890", secrets=[TELEGRAM_TOKEN])
    assert setup_telegram(tmp_path / ".env", http_for(chain), console, environ={})
    assert "has a webhook" in console.text and chain.telegram_chats == ["-1001234567890"]


def test_nothing_is_saved_when_the_test_message_fails(tmp_path):
    chain = FakeChain()
    chain.updates = [private_chat()]
    console = Script("y", secrets=[TELEGRAM_TOKEN])
    chain.telegram_failure = None
    original_send = chain._telegram

    def refuse_messages(url, params):
        if url.endswith("/sendMessage"):
            chain.telegram_failure = 403
        return original_send(url, params)

    chain._telegram = refuse_messages
    assert not setup_telegram(tmp_path / ".env", http_for(chain), console, environ={})
    assert "Telegram refused the test message" in console.text and not (tmp_path / ".env").exists()


def test_the_ankr_key_is_checked_on_each_chosen_chain(tmp_path):
    world = FakeChain()
    world.evm_chain("base")  # BNB Chain is not served to this key
    good = ANKR_ENV["ANKR_API_KEY"]
    console, environ = Script(secrets=["wrong-key-0123456789", good]), {}
    assert wizard.offer_ankr_key(tmp_path / ".env", console, ["base", "bsc"], environ=environ,
                                 http=http_for(world)) == ["base"]
    assert "Base trades are still watched through public RPCs" in console.text
    assert ("Ankr rejected this key: Base RPC eth_blockNumber: HTTP 401 (Unauthorized: You must authenticate your "
            "request with an API key)") in console.text
    assert "Key works on Base. Saved ANKR_API_KEY to .env." in console.text
    assert "BNB Chain refused it (BNB Chain RPC eth_blockNumber: HTTP 403 (message: API key is not allowed to access " \
           "blockchain))." in console.text
    assert environ == {"ANKR_API_KEY": good} and f"ANKR_API_KEY={good}" in (tmp_path / ".env").read_text()
    assert good not in console.text


# --- token choice and selection --------------------------------------------------------------------------

def test_search_lists_lookalikes_with_warnings_and_picks_by_number():
    chain = FakeChain()
    real = pair(MINT, 0.0013, 600_000, symbol="CYBERLEEK", name="CyberLeek", volume=157_000)
    fake = pair(addr("fake"), 0.02, 68_000_000, symbol="CYBERLEEK", name="‮kaeLrebyC", volume=0.1, dex="orca")
    chain.search_results, chain.pairs = [fake, real], [real]
    console = Script("cyberleek", "1", "y")
    choice = choose_token(http_for(chain), console)
    assert (choice.mint, choice.symbol, choice.name) == (MINT, "CYBERLEEK", "CyberLeek")
    text = console.text
    assert text.index(MINT) < text.index(addr("fake"))  # the busiest token first
    assert "hidden text-direction characters" in text and "liquidity far above its trading volume" in text


def test_search_covers_every_chain_and_an_0x_address_is_looked_up_on_each():
    world = FakeChain()
    token = evm_addr("brett")
    on_base = evm_pair(BASE, token, 0.05, symbol="BRETT", name="Brett")
    on_bsc = pair(token, 0.04, 20_000, chain_id="bsc", symbol="BRETT", name="Brett", volume=10,
                  pair_address=evm_addr("bsc-pool"))
    world.search_results = [on_bsc, on_base, pair(MINT, 0.0013, 600_000, symbol="BRETT", name="Brett", volume=50)]
    http = http_for(world)
    assert [(m.chain, m.mint) for m in search_tokens(http, "brett")] == [("base", token), ("solana", MINT),
                                                                          ("bsc", token)]
    assert [m.chain for m in search_tokens(http, "brett", chains=["bsc", "solana"])] == ["solana", "bsc"]
    world.pairs = [on_base, on_bsc]  # the address's pools, which DEX Screener lists per chain
    console = Script(token.upper().replace("0X", "0x"), "2", "y")
    choice = choose_token(http, console, chains=["base", "bsc", "solana"])
    assert (choice.chain, choice.mint, choice.symbol) == ("bsc", token, "BRETT")
    assert "This address has pools on several chains" in console.text


def test_which_chain_an_address_is_on():
    wallet = evm_addr("w")
    assert wizard._chain_of(MINT, None) == (SOLANA, None) and wizard._chain_of(wallet, "base") == (BASE, None)
    assert "add --chain ethereum, base, bsc or arbitrum" in wizard._chain_of(wallet, None)[1]
    assert wizard._chain_of(MINT, "base") == (None, f"{MINT!r} is not a Base address")
    assert wizard._chain_of(wallet, "polygon")[1].startswith("unknown chain 'polygon'")
    assert wizard._chain_of("hello", None) == (None, "'hello' is not a Solana or EVM address")


def test_a_pasted_mint_without_pools_is_refused():
    chain = FakeChain()
    console = Script(MINT, "")
    assert choose_token(http_for(chain), console) is None
    assert "no Solana pool" in console.text


@pytest.mark.parametrize("answer, expected", [
    ("1,3", [0, 2]), ("2-4", [1, 2, 3]), ("all", [0, 1, 2, 3, 4]), ("none", []), ("", []),
    ("1, 5-5", [0, 4]), ("1,1", [0]), ("6", None), ("x", None), ("3-1", None), ("0-2", None),
])
def test_parse_selection(answer, expected):
    assert parse_selection(answer, 5) == expected


# --- config.json and .env edits -------------------------------------------------------------------------------

def ranked(rank, name, pnl=10.0, roi=100.0):
    return RankedTrader(rank=rank, address=addr(name), pnl_native=pnl, realized_native=pnl, unrealized_native=0.0,
                        roi_pct=roi, cost_native=10.0, proceeds_native=20.0, buys=1, sells=1, winning_sells=1,
                        held_tokens=0.0, held_pct=0.0, first_buy=None, last_trade=None, pnl_usd=pnl * 100)


def report_of(*traders, symbol="TEST"):
    return DiscoveryReport(mint=MINT, symbol=symbol, name="Test", price_usd=0.002, price_native=0.00002, native_usd=100.0,
                           pools=[], scanned=0, scan_from=None, scan_to=None, wallets_seen=0, candidates=0,
                           traders=list(traders), left_out={}, notes=[], credits=0, history_api="x", seconds=0.0)


def test_save_traders_adds_wallets_keeps_comments_labels_and_a_backup(tmp_path):
    kept = addr("kept")
    path = write_config(tmp_path, _comment="mine", labels={addr("alpha"): "Alpha"},
                        traders={"wallets": [{"address": kept, "label": "My pick"}], "poll_seconds": 90})
    before = path.read_text(encoding="utf-8")
    backup = save_traders(path, report_of(ranked(1, "kept", 50, 500), ranked(2, "alpha"), ranked(3, "gamma", 4.567, None)),
                          [ranked(1, "kept", 50, 500), ranked(2, "alpha"), ranked(3, "gamma", 4.567, None)],
                          tokens="source", min_trade_usd=50, environ={}, today="2026-10-06")
    assert backup.read_text(encoding="utf-8") == before
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["_comment"] == "mine" and data["traders"]["poll_seconds"] == 90
    assert data["traders"]["tokens"] == "source" and data["traders"]["min_trade_usd"] == 50
    wallets = {w["address"]: w for w in data["traders"]["wallets"]}
    assert wallets[kept]["label"] == "My pick" and wallets[kept]["pnl_usd"] == 5000
    assert wallets[addr("alpha")]["label"] == "TEST #2"
    assert wallets[addr("gamma")] == {"address": addr("gamma"), "label": "TEST #3", "source_mint": MINT,
                                      "source_symbol": "TEST", "pnl_usd": 456.7, "roi_pct": None, "added": "2026-10-06"}
    cfg = load_config(path, environ={})
    assert cfg.labels[addr("alpha")] == "Alpha"  # an explicit label wins over the trader's
    assert [w.address for w in cfg.traders.wallets] == [kept, addr("alpha"), addr("gamma")]


def test_labels_stay_unique():
    taken = {"TEST #1", "TEST #1 (2)"}
    assert wizard._unique("TEST #1", taken) == "TEST #1 (3)" and wizard._unique("TEST #4", taken) == "TEST #4"


def test_an_edit_that_would_break_config_json_is_rolled_back(tmp_path):
    path = write_config(tmp_path)
    before = path.read_text(encoding="utf-8")
    with pytest.raises(ConfigError, match="traders.tokens"):
        edit_config(path, lambda data: data.update(traders={"tokens": "everything"}), environ={})
    assert path.read_text(encoding="utf-8") == before


def test_update_dotenv_replaces_appends_and_keeps_everything_else(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# comment\nexport TELEGRAM_CHAT_ID=1\nOTHER=x\nTELEGRAM_CHAT_ID=2\n# TELEGRAM_BOT_TOKEN=old\n",
                   encoding="utf-8")
    update_dotenv(env, {"TELEGRAM_CHAT_ID": "-100", "TELEGRAM_BOT_TOKEN": "1:abc", "LABEL": "two words"})
    assert env.read_text(encoding="utf-8") == ("# comment\nTELEGRAM_CHAT_ID=-100\nOTHER=x\n# TELEGRAM_BOT_TOKEN=old\n\n"
                                               'TELEGRAM_BOT_TOKEN=1:abc\nLABEL="two words"\n')
    if os.name == "posix":
        assert env.stat().st_mode & 0o777 == 0o600


def test_switch_token_drops_the_old_tokens_wallet_lists_but_keeps_traders(tmp_path):
    whale, trader, other = addr("whale"), addr("trader"), addr("other-mint")
    path = write_config(tmp_path, mint=other, always_alert_owners=[whale], exclude_owners=[addr("lp")],
                        labels={whale: "Whale 1", trader: "Trader 1", "_note": "kept"},
                        traders={"wallets": [trader]})
    backup, removed = switch_token(path, MINT, environ={})
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["mint"] == MINT and data["always_alert_owners"] == [] and data["exclude_owners"] == []
    assert data["labels"] == {trader: "Trader 1", "_note": "kept"}
    assert removed == ["1 always_alert_owners", "1 exclude_owners", "1 label(s)"]
    assert json.loads(backup.read_text(encoding="utf-8"))["mint"] == other


def test_setup_can_run_before_a_token_is_chosen(tmp_path):
    path = write_config(tmp_path, mint="PASTE_MINT_HERE")
    assert load_config(path, environ={}, require_mint=False).mint is None
    with pytest.raises(ConfigError):
        load_config(path, environ={})


# --- commands ---------------------------------------------------------------------------------------------

def args(**values):
    defaults = {"token": None, "watch": None, "json": False, "scan": None, "hours": None, "candidates": None,
                "show": None, "action": "list", "address": None, "label": None, "tokens": None, "min_usd": None,
                "chain": None, "chains": [], "top": None, "days": None, "add": False}
    return Namespace(**{**defaults, **values})


@pytest.fixture
def fake_clients(monkeypatch):
    chain = FakeChain()
    http = http_for(chain)
    monkeypatch.setattr(wizard, "clients", lambda cfg, environ=None: (http, {"solana": SolanaRPC(cfg.rpc_url, http)}))
    monkeypatch.setattr(wizard.presets, "SHIPPED", Path("no-shipped-presets.json"))
    monkeypatch.setattr("watcher.discover.time.time", lambda: NOW)
    return chain


def test_setup_from_start_to_finish(tmp_path, fake_clients, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TELEGRAM_TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "4242")
    for name in ("HELIUS_API_KEY", "SOLANA_RPC_URL"):
        monkeypatch.delenv(name, raising=False)
    w = scenario(fake_clients)
    path = write_config(tmp_path)
    # keep Telegram · Solana · don't build its built-in list now · find a token's traders · token · it's the
    # one · watch 1-2 · only this token · min $50 · send a test alert
    console = Script("n", "1", "n", "y", MINT, "y", "1-2", "n", "50", "y")
    assert wizard.setup_command(path, tmp_path / ".env", console) == 0
    cfg = load_config(path, environ={"TELEGRAM_BOT_TOKEN": TELEGRAM_TOKEN, "TELEGRAM_CHAT_ID": "4242"})
    assert [x.address for x in cfg.traders.wallets] == [w["whale"], w["alpha"]]
    assert cfg.traders.tokens == "source" and cfg.traders.min_trade_usd == 50
    assert "Estimated use" in console.text and "Delivered." in console.text
    assert "No built-in list yet for Solana. Build it now" in console.prompts[2]
    assert "build one later with: python holder_watch.py presets refresh solana" in console.text
    assert "test alert" in fake_clients.telegram[-1]
    assert not console.answers  # every prompt was expected


def test_discover_command_watches_the_top_n_without_asking(tmp_path, fake_clients):
    w = scenario(fake_clients)
    path = write_config(tmp_path)
    console = Script()
    assert wizard.discover_command(path, args(token=MINT, watch=2), console, can_prompt=True) == 0
    wallets = load_config(path, environ={}).traders.wallets
    assert [x.address for x in wallets] == [w["whale"], w["alpha"]]
    assert [x.label for x in wallets] == ["TEST #1", "TEST #2"] and wallets[0].source_mint == MINT
    assert "Profit = realized + unrealized" in console.text and "Left out:" in console.text
    assert console.prompts == []


def test_discover_command_json(tmp_path, fake_clients):
    scenario(fake_clients)
    console = Script()
    assert wizard.discover_command(write_config(tmp_path), args(token=MINT, json=True), console, can_prompt=False) == 0
    report = json.loads(console.text)
    assert [t["rank"] for t in report["traders"]] == [1, 2, 3] and report["pools"][0]["address"] == POOL


def test_discover_by_name_needs_a_terminal(tmp_path, fake_clients):
    console = Script()
    assert wizard.discover_command(write_config(tmp_path), args(token="cyberleek"), console, can_prompt=False) == 2


def test_traders_command_adds_lists_and_removes(tmp_path):
    path = write_config(tmp_path)
    wallet = addr("someone")
    console = Script()
    assert wizard.traders_command(path, args(action="add", address=wallet, label="Friend"), console) == 0
    assert wizard.traders_command(path, args(action="add", address=wallet), console) == 0
    assert "already on the watch list" in console.text
    assert wizard.traders_command(path, args(action="list"), console) == 0
    assert f"{wallet}  Friend" in console.text and "Helius credits a month" in console.text
    assert wizard.traders_command(path, args(action="remove", address=wallet), console) == 0
    assert load_config(path, environ={}).traders.wallets == ()
    assert wizard.traders_command(path, args(action="add", address="nope"), console) == 2


def test_traders_command_sets_a_wallets_own_scope_and_minimum(tmp_path):
    path = write_config(tmp_path)
    wallet = addr("trader-1")
    console = Script()
    assert wizard.traders_command(path, args(action="add", address=wallet, label="Trader 1", tokens="major",
                                             min_usd=1.0), console) == 0
    assert wizard.traders_command(path, args(action="add", address=wallet, label="Trader One"), console) == 0
    assert "Updated" in console.text
    [entry] = load_config(path, environ={}).traders.wallets
    assert (entry.label, entry.tokens, entry.min_trade_usd) == ("Trader One", "major", 1.0)  # the rest is kept
    assert wizard.traders_command(path, args(action="list"), console) == 0
    assert "major tokens, at least $1" in console.text


def test_traders_command_with_an_evm_wallet(tmp_path, monkeypatch):
    monkeypatch.setenv("ANKR_API_KEY", ANKR_ENV["ANKR_API_KEY"])
    path = write_config(tmp_path)
    wallet = "0x" + "Ab" * 20
    console = Script()
    assert wizard.traders_command(path, args(action="add", address=wallet), console) == 2
    assert "add --chain" in console.text
    assert wizard.traders_command(path, args(action="add", address=wallet, chain="bsc", label="BNB friend"),
                                  console) == 0
    assert json.loads(path.read_text(encoding="utf-8"))["traders"]["wallets"] == [
        {"chain": "bsc", "address": wallet.lower(), "label": "BNB friend"}]
    assert wizard.traders_command(path, args(action="list"), console) == 0
    assert "1 wallet(s) (BNB Chain 1)" in console.text
    assert "about 1,400,000 Ankr API credits a month (free plan: 200,000,000)" in console.text
    assert wizard.traders_command(path, args(action="remove", address=wallet, chain="bsc"), console) == 0
    assert load_config(path, environ={}).traders.wallets == ()


def test_cli_arguments():
    parsed = holder_watch.parse_args(["discover", MINT, "--watch", "3", "--config", "x.json"])
    assert (parsed.command, parsed.token, parsed.watch, parsed.config) == ("discover", MINT, 3, "x.json")
    assert holder_watch.parse_args(["--once"]).command is None
    parsed = holder_watch.parse_args(["traders", "add", MINT, "--tokens", "major", "--min-usd", "25"])
    assert (parsed.action, parsed.tokens, parsed.min_usd) == ("add", "major", 25)
    for bad in (["traders", "add"], ["discover", "--watch", "-1"], ["--once", "setup"], ["discover", "--scan", "10"],
                ["traders", "add", MINT, "--tokens", "all"]):
        with pytest.raises(SystemExit):
            holder_watch.parse_args(bad)
