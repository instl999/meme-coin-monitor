"""config.json validation, secrets from the environment, and the .env loader."""

import json
from pathlib import Path

import pytest

from tests.fakes import MINT, addr
from watcher.chains import SOLANA
from watcher.config import ConfigError, load_config, load_dotenv

PROJECT = Path(__file__).resolve().parent.parent


def write(tmp_path, data):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data) if not isinstance(data, str) else data, encoding="utf-8")
    return path


def problems(tmp_path, data, environ=None):
    with pytest.raises(ConfigError) as caught:
        load_config(write(tmp_path, data), environ={} if environ is None else environ)
    return "\n".join(caught.value.problems)


def test_installed_config_is_valid():
    """The config.json next to the code must load. Only validity is checked, so a server whose owner has
    edited config.json still passes the installer's test run."""
    cfg = load_config(PROJECT / "config.json", environ={})
    assert cfg.mint and cfg.telegram_token is None


def test_minimal_config_gets_defaults(tmp_path):
    cfg = load_config(write(tmp_path, {"mint": MINT}), environ={})
    assert cfg.poll_seconds == 120 and cfg.top_n == 10 and cfg.rules.window_minutes == 60
    assert cfg.rules.holder_drop_pct is None  # every rule is off unless set
    assert cfg.cooldowns["always_alert_owners"] == 0 and cfg.cooldowns["holder_drop_pct"] == 30
    assert cfg.state_file == tmp_path / "state.json"


def test_placeholder_mint_is_rejected(tmp_path):
    assert "replace 'PASTE_MINT_HERE'" in problems(tmp_path, {"mint": "PASTE_MINT_HERE"})


def test_all_problems_are_reported_at_once(tmp_path):
    text = problems(tmp_path, {
        "mint": "not-a-mint",
        "pol_seconds": 60,
        "top_n": 50,
        "exclude_owners": ["xyz"],
        "rules": {"trailing_stop_pct": 150, "holder_drop_pct": "20"},
        "alert_cooldown_minutes": {"holder_drop": 5},
    })
    for expected in ("mint: 'not-a-mint' is not a valid Solana address",
                     "pol_seconds: unknown setting (did you mean 'poll_seconds'?)",
                     "top_n: expected a whole number from 1 to 20",
                     "exclude_owners[0]: 'xyz' is not a valid Solana address",
                     "rules.trailing_stop_pct: expected a percentage between 0 and 100",
                     'rules.holder_drop_pct: expected a percentage above 0 and at most 100, or null to turn the rule off, got "20"',
                     "alert_cooldown_minutes.holder_drop: unknown setting (did you mean 'holder_drop_pct'?)"):
        assert expected in text


def test_wallet_cannot_be_both_excluded_and_always_alert(tmp_path):
    wallet = addr("creator")
    assert "in both exclude_owners and always_alert_owners" in problems(
        tmp_path, {"mint": MINT, "exclude_owners": [wallet], "always_alert_owners": [wallet]})


def test_invalid_json_points_at_the_line(tmp_path):
    # Python 3.14 points at the trailing comma; older versions point at the closing brace.
    text = problems(tmp_path, '{\n  "mint": "x",\n}')
    assert "not valid JSON" in text
    assert "line 2" in text or "line 3" in text


def test_literal_api_key_in_rpc_url_is_rejected(tmp_path):
    text = problems(tmp_path, {"mint": MINT, "rpc_url": "https://mainnet.helius-rpc.com/?api-key=abc123def456"})
    assert "literal API key" in text and "HELIUS_API_KEY" in text


def test_rpc_url_env_placeholders(tmp_path):
    data = {"mint": MINT, "rpc_url": "https://rpc.example.com/?api-key=${MY_RPC_KEY}"}
    assert "rpc_url: needs MY_RPC_KEY" in problems(tmp_path, data)
    cfg = load_config(write(tmp_path, data), environ={"MY_RPC_KEY": "sekret-value-42"})
    assert cfg.rpc_url == "https://rpc.example.com/?api-key=sekret-value-42"
    assert cfg.rpc_label == "rpc.example.com (key from MY_RPC_KEY)"
    assert "sekret-value-42" in cfg.secrets and "sekret" not in repr(cfg)


def test_helius_key_upgrades_the_public_default(tmp_path):
    cfg = load_config(write(tmp_path, {"mint": MINT}), environ={"HELIUS_API_KEY": "helius-key-123456"})
    assert cfg.rpc_url == "https://mainnet.helius-rpc.com/?api-key=helius-key-123456"
    assert cfg.rpc_label == "mainnet.helius-rpc.com (key from HELIUS_API_KEY)"
    assert not cfg.uses_public_rpc and "helius-key-123456" in cfg.secrets


def test_solana_rpc_url_env_overrides_config(tmp_path):
    url = "https://example.quiknode.pro/0123456789abcdef/"
    cfg = load_config(write(tmp_path, {"mint": MINT}), environ={"SOLANA_RPC_URL": url, "HELIUS_API_KEY": "x" * 10})
    assert cfg.rpc_url == url and "0123456789abcdef" in cfg.secrets
    assert cfg.rpc_label == "example.quiknode.pro (key from SOLANA_RPC_URL)"


def test_telegram_needs_both_variables(tmp_path):
    assert "set both TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID" in problems(
        tmp_path, {"mint": MINT}, environ={"TELEGRAM_BOT_TOKEN": "123:abc"})
    cfg = load_config(write(tmp_path, {"mint": MINT}),
                      environ={"TELEGRAM_BOT_TOKEN": "123456:abcdefghijkl", "TELEGRAM_CHAT_ID": "42"})
    assert cfg.telegram_chat_id == "42" and "123456:abcdefghijkl" in cfg.secrets
    assert "abcdefghijkl" not in repr(cfg)


def test_window_is_required_for_holder_rules(tmp_path):
    text = problems(tmp_path, {"mint": MINT, "rules": {"holder_drop_pct": 20, "window_minutes": None}})
    assert "rules.window_minutes: required" in text


def test_bad_heartbeat_settings(tmp_path):
    text = problems(tmp_path, {"mint": MINT, "heartbeat": {"time": "25:00", "timezone": "Mars/Base"}})
    assert "heartbeat.time: expected HH:MM" in text
    text = problems(tmp_path, {"mint": MINT, "heartbeat": {"timezone": "Mars/Base"}})
    assert "unknown time zone 'Mars/Base'" in text
    cfg = load_config(write(tmp_path, {"mint": MINT, "heartbeat": {"enabled": False}}), environ={})
    assert cfg.heartbeat is None


def test_traders_section_is_validated(tmp_path):
    good = addr("trader")
    text = problems(tmp_path, {"mint": MINT, "traders": {
        "wallets": [good, {"address": good}, {"address": "xyz"}, {"address": addr("b"), "lable": "x"}, 5],
        "poll_seconds": 5, "tokens": "all", "alert_buys": "yes", "min_trade": 1}})
    for expected in (f"traders.wallets[1]: {good} is listed more than once",
                     "traders.wallets[2].address: 'xyz' is not a valid Solana address",
                     "traders.wallets[3].lable: unknown setting (did you mean 'label'?)",
                     "traders.wallets[4]: expected a wallet address or an object",
                     "traders.poll_seconds: expected a whole number of seconds, at least 15, got 5",
                     'traders.tokens: expected "any"',
                     "traders.alert_buys: expected true or false",
                     "traders.min_trade: unknown setting (did you mean 'min_trade_usd'?)"):
        assert expected in text


def test_trader_wallets_get_defaults_and_their_labels_show_in_alerts(tmp_path):
    a, b = addr("a"), addr("b")
    cfg = load_config(write(tmp_path, {"mint": MINT, "labels": {a: "Mine"}, "traders": {"wallets": [
        {"address": a, "label": "TEST #1"},
        {"address": b, "label": "TEST #2", "source_mint": MINT, "pnl_usd": 350}]}}), environ={})
    assert [w.address for w in cfg.traders.wallets] == [a, b]
    assert cfg.labels == {a: "Mine", b: "TEST #2"}  # explicit labels win
    assert cfg.traders.wallets[1].pnl_usd == 350 and cfg.traders.tokens == "any" and cfg.traders.poll_seconds == 60
    assert cfg.trader_state_file == tmp_path / "trader_state.json"


def test_per_wallet_scope_and_the_major_token_settings(tmp_path):
    a = addr("a")
    text = problems(tmp_path, {"mint": MINT, "traders": {
        "wallets": [{"address": a, "tokens": "majors", "min_trade_usd": -1}],
        "major_mints": ["x"], "major_min_market_cap_usd": 0}})
    for expected in ("traders.wallets[0].tokens: expected \"any\"",
                     "traders.wallets[0].min_trade_usd: expected USD >= 0",
                     "traders.major_mints[0]: 'x' is not a token address on a supported chain",
                     "traders.major_min_market_cap_usd: expected a USD amount above 0"):
        assert expected in text
    extra = addr("extra-major")
    cfg = load_config(write(tmp_path, {"mint": MINT, "traders": {
        "wallets": [{"address": a, "tokens": "major", "min_trade_usd": 2}, addr("b")],
        "major_mints": [extra], "major_min_market_cap_usd": None}}), environ={})
    major, plain_wallet = cfg.traders.wallets
    assert cfg.traders.scope(major) == "major" and cfg.traders.minimum(major) == 2
    assert cfg.traders.scope(plain_wallet) == "any" and cfg.traders.minimum(plain_wallet) == 10
    assert cfg.traders.listed_major(SOLANA, extra) and cfg.traders.listed_major(SOLANA, SOLANA.wrapped)
    assert cfg.traders.major_min_market_cap_usd is None


def test_evm_trader_wallets(tmp_path):
    mixed = "0x" + "aB" * 20
    text = problems(tmp_path, {"mint": MINT, "traders": {"wallets": [
        {"chain": "base", "address": "0x123"}, {"chain": "polygon", "address": mixed},
        {"chain": "base", "address": mixed}, {"chain": "base", "address": mixed.lower()}, {"address": mixed}]}})
    for expected in ("traders.wallets[0].address: '0x123' is not a valid Base address",
                     "traders.wallets[1].chain: unknown chain 'polygon' (supported: solana, ethereum, base, bsc, arbitrum)",
                     f"traders.wallets[3]: {mixed.lower()} is listed more than once on Base",
                     f"traders.wallets[4].address: '{mixed}' is not a valid Solana address (an 0x address needs "
                     '"chain": one of ethereum, base, bsc, arbitrum)'):
        assert expected in text
    cfg = load_config(write(tmp_path, {"mint": MINT, "labels": {mixed: "Mine"}, "traders": {"wallets": [
        {"chain": "base", "address": mixed}, {"chain": "bsc", "address": mixed, "label": "On BNB"}]}}), environ={})
    lower = mixed.lower()
    assert [(w.chain, w.address, w.key) for w in cfg.traders.wallets] == [("base", lower, f"base:{lower}"),
                                                                         ("bsc", lower, f"bsc:{lower}")]
    assert cfg.labels[lower] == "Mine"  # addresses are compared in lowercase on EVM chains


def test_a_1_1_config_still_loads(tmp_path):
    """1.1.0 had SOL amounts; 1.2.0 has USD ones. The old keys are accepted without effect, with a note,
    so updating a server never stops its monitor."""
    wallet = addr("trader")
    cfg = load_config(write(tmp_path, {"mint": MINT, "traders": {
        "min_trade_sol": 0.5, "wallets": [{"address": wallet, "pnl_sol": 12.5, "min_trade_sol": 1}]},
        "discovery": {"min_buy_sol": 2}}), environ={})
    assert cfg.traders.min_trade_usd == 10 and cfg.discovery.min_buy_usd == 100
    assert cfg.traders.wallets[0].address == wallet and cfg.traders.wallets[0].min_trade_usd is None
    assert cfg.notes == (
        "traders.min_trade_sol is no longer used (amounts are in USD since 1.2.0); traders.min_trade_usd applies instead",
        "traders.wallets[0].min_trade_sol is no longer used (amounts are in USD since 1.2.0); "
        "traders.wallets[0].min_trade_usd applies instead",
        "discovery.min_buy_sol is no longer used (amounts are in USD since 1.2.0); discovery.min_buy_usd applies instead")


def test_evm_keys_are_secrets(tmp_path):
    in_path, in_query = "https://bnb.example.com/rpc/0123456789abcdef", "https://arb.example.com/?apikey=fedcba9876543210"
    cfg = load_config(write(tmp_path, {"mint": MINT}), environ={
        "ALCHEMY_API_KEY": "alchemy-key-0123", "BSC_RPC_URL": in_path, "ARBITRUM_RPC_URL": in_query})
    for secret in ("alchemy-key-0123", in_path, "0123456789abcdef", in_query, "fedcba9876543210"):
        assert secret in cfg.secrets


def test_discovery_section_is_validated(tmp_path):
    text = problems(tmp_path, {"mint": MINT, "discovery": {"scan_transactions": 10, "show": 0, "lookback": 5}})
    assert "discovery.scan_transactions: expected a whole number from 50 to 50000, got 10" in text
    assert "discovery.show: expected a whole number from 1 to 100, got 0" in text
    assert "discovery.lookback: unknown setting (did you mean 'lookback_hours'?)" in text
    cfg = load_config(write(tmp_path, {"mint": MINT, "discovery": {"watch": 0, "min_buy_usd": 50}}), environ={})
    assert cfg.discovery.watch == 0 and cfg.discovery.min_buy_usd == 50 and cfg.discovery.scan_transactions == 2000


def test_comment_keys_are_allowed(tmp_path):
    cfg = load_config(write(tmp_path, {"_comment": "hi", "mint": MINT, "rules": {"_note": "x"}}), environ={})
    assert cfg.mint == MINT


def test_dotenv_loader(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("﻿# comment\r\nexport HELIUS_API_KEY=abc123  # trailing comment\r\n"
                        "TELEGRAM_CHAT_ID='-100200300'\nEMPTY=\nONLY_COMMENT= # fill me in\nHASH=ab#cd\n"
                        "ALREADY=from-file\nnot a line\n", encoding="utf-8")
    environ = {"ALREADY": "from-environment"}
    loaded = load_dotenv(env_file, environ)
    assert environ == {"ALREADY": "from-environment", "HELIUS_API_KEY": "abc123", "TELEGRAM_CHAT_ID": "-100200300",
                       "HASH": "ab#cd"}
    assert sorted(loaded) == ["HASH", "HELIUS_API_KEY", "TELEGRAM_CHAT_ID"]
    assert load_dotenv(tmp_path / "missing.env", {}) == []
