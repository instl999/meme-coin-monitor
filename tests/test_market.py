"""DEX Screener pool selection."""

from tests.fakes import MINT, FakeResponse, pair
from watcher.market import fetch_market, select_market


def test_picks_the_deepest_pool_where_the_token_is_the_base():
    pairs = [
        pair(MINT, 0.0021, 8_000, dex="meteora", labels=["DLMM"]),
        pair(MINT, 0.0017, 695_000, dex="raydium", labels=["CPMM"], pair_address="DeepPool"),
        pair(MINT, 0.00000034, 9_000_000, base=False),       # our token is the quote: other token's price
        pair(MINT, 0.5, 50_000_000, chain_id="base"),         # another chain
        pair(MINT, None, 90_000_000),                          # no price
    ]
    market = select_market(pairs, MINT)
    assert market.found and market.pair_address == "DeepPool"
    assert market.price_usd == 0.0017 and market.liquidity_usd == 695_000
    assert market.venue == "Raydium CPMM TEST/SOL"
    assert market.pool_count == 2                     # priced Solana pools with our token as base
    assert "DeepPool" in market.pair_addresses        # every pool address, for pool-wallet detection


def test_no_pair_found():
    market = select_market([pair(MINT, 0.1, 1000, base=False)], MINT)
    assert not market.found and market.price_usd is None


def test_missing_liquidity_is_reported_as_unknown():
    market = select_market([pair(MINT, 0.002, None, dex="pumpfun", labels=[])], MINT)
    assert market.found and market.liquidity_usd is None and market.venue == "Pumpfun TEST/SOL"


def test_hidden_direction_characters_are_removed_and_flagged():
    spoof = pair(MINT, 0.002, 1000, name="‮kaeLrebyC", symbol="CYBER​LEEK")
    market = select_market([spoof], MINT)
    assert market.name == "kaeLrebyC" and market.symbol == "CYBERLEEK" and market.name_had_hidden_chars


class OneResponse:
    def __init__(self, payload):
        self.payload = payload

    def request(self, method, url, *, what, json=None):
        assert url == f"https://api.dexscreener.com/token-pairs/v1/solana/{MINT}"
        return FakeResponse(200, self.payload)


def test_fetch_accepts_the_documented_array_and_the_older_pairs_object():
    assert fetch_market(OneResponse([pair(MINT, 0.002, 1000)]), MINT).price_usd == 0.002
    assert fetch_market(OneResponse({"schemaVersion": "1.0.0", "pairs": [pair(MINT, 0.003, 1000)]}), MINT).price_usd == 0.003
    assert not fetch_market(OneResponse({"pairs": None}), MINT).found
