"""The chains the trader watch, discovery and the built-in traders support.

Solana is read through its own RPC (Helius). The EVM chains are read through Ankr (ANKR_API_KEY, one
free key for all of them) or Alchemy, or, for the trader watch on Ethereum, Base and Arbitrum, through
their public RPCs without any key (evm.source). On every chain, prices, pools and token search come
from DEX Screener and a pool's recent trades from GeckoTerminal, neither of which needs a key.

Every EVM token address below was checked against DEX Screener on 2026-10-07 (symbol, name, pools).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .known import MAJOR_TOKENS, STABLE_MINTS, WSOL_MINT
from .util import is_pubkey

EVM_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
# How pools that trade the coin itself (not its wrapped token) name it, e.g. Uniswap v4 on GeckoTerminal.
NATIVE_PLACEHOLDERS = frozenset({"0x0000000000000000000000000000000000000000",
                                 "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"})


@dataclass(frozen=True)
class Chain:
    id: str             # as written in config.json
    name: str
    kind: str           # "solana" or "evm"
    native: str         # the chain's coin, which pays fees and most trades
    decimals: int       # of the native coin
    wrapped: str        # its wrapped token (WSOL, WETH, WBNB)
    stables: frozenset  # USD stablecoins trades are paid in
    dexscreener: str    # DEX Screener chainId
    geckoterminal: str  # GeckoTerminal network id
    explorer: str
    alchemy: str | None = None  # Alchemy network name, for EVM chains
    ankr: str | None = None     # Ankr network name (RPC and Advanced API), for EVM chains
    # Keyless RPCs, checked 2026-10-07. All of them answer the per-minute transaction counts; the first
    # also serves the keyless trader watch when log_span > 0: wallet-filtered eth_getLogs over
    # log_span blocks, and balances of the last minutes.
    public_rpcs: tuple = ()
    log_span: int = 0
    block_seconds: float = 1.0  # typical time between blocks (chains speed up: EVM windows measure it)
    majors: dict = field(default_factory=dict)  # major tokens: address -> symbol

    @property
    def evm(self) -> bool:
        return self.kind == "evm"

    @property
    def quotes(self) -> frozenset:
        """What tokens are bought and sold with here."""
        return frozenset({self.wrapped, *self.stables})

    def normalize(self, address: str) -> str:
        return address.lower() if self.evm else address

    def valid(self, address) -> bool:
        if not isinstance(address, str):
            return False
        return bool(EVM_ADDRESS.match(address)) if self.evm else is_pubkey(address)

    def tx_url(self, signature: str) -> str:
        return f"{self.explorer}/tx/{signature}"

    def wallet_url(self, address: str) -> str:
        return f"{self.explorer}/{'address' if self.evm else 'account'}/{address}"

    def token_url(self, token: str) -> str:
        return f"{self.explorer}/token/{token}"


SOLANA = Chain(
    id="solana", name="Solana", kind="solana", native="SOL", decimals=9, wrapped=WSOL_MINT, stables=STABLE_MINTS,
    dexscreener="solana", geckoterminal="solana", explorer="https://solscan.io", block_seconds=0.4,
    majors=dict(MAJOR_TOKENS))

ETHEREUM = Chain(
    id="ethereum", name="Ethereum", kind="evm", native="ETH", decimals=18,
    wrapped="0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
    stables=frozenset({"0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",    # USDC
                       "0xdac17f958d2ee523a2206206994597c13d831ec7",    # USDT
                       "0x6b175474e89094c44da98b954eedeac495271d0f",    # DAI
                       "0x4c9edd5852cd905f086c759e8383e09bff1e68b3",    # USDe
                       "0x6c3ea9036406852006290770bedfcaba0e23a0e8",    # PYUSD
                       "0xdc035d45d973e3ec169d2276ddab16f1e407384f",    # USDS
                       "0x8d0d000ee44948fc98c9b98a4fa4921476f08b0d",    # USD1
                       "0xc5f0f7b66764f6ec8c8dff7ba683102295e16409",    # FDUSD
                       "0x8292bb45bf1ee4d140127049757c2e0ff06317ed",    # RLUSD
                       "0xe343167631d89b6ffc58b88d6b7fb0228795491d",    # USDG
                       "0x40d16fc0246ad3160ccc09b8d0d3a2cd28ae6c2f",    # GHO
                       "0xf939e0a03fb07f59a73314e73794be0e57ac1b4e",    # crvUSD
                       "0x853d955acef822db058eb8505911ed77f175b99e"}),  # FRAX
    dexscreener="ethereum", geckoterminal="eth", explorer="https://etherscan.io", alchemy="eth-mainnet", ankr="eth",
    public_rpcs=("https://rpc.mevblocker.io", "https://eth.blockrazor.xyz", "https://ethereum-rpc.publicnode.com"),
    log_span=150, block_seconds=12,
    majors={"0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2": "ETH",
            "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599": "WBTC",
            "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf": "cbBTC",
            "0x7f39c581f595b53c5cb19bd0b3f8da6c935e2ca0": "wstETH",
            "0x514910771af9ca656af840dff83e8264ecf986ca": "LINK",
            "0x1f9840a85d5af5bf1d1762f925bdaddc4201f984": "UNI",
            "0x7fc66500c84a76ad7e9c93437bfc5ac33e2ddae9": "AAVE",
            "0xfaba6f8e4a5e8ab82f62fe7c39859fa577269be3": "ONDO",
            "0x57e114b691db790c35207b2e685d4a43181e6061": "ENA",
            "0x6982508145454ce325ddbe47a25d4ec3d2311933": "PEPE",
            "0x95ad61b0a150d79219dcf64e1e6cc01f0b64c4ce": "SHIB"})

BASE = Chain(
    id="base", name="Base", kind="evm", native="ETH", decimals=18,
    wrapped="0x4200000000000000000000000000000000000006",
    stables=frozenset({"0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",    # USDC
                       "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca",    # USDbC
                       "0xfde4c96c8593536e31f229ea8f37b2ada2699bb2",    # USDT
                       "0x50c5725949a6f0c72e6c4a641f24049a917db0cb",    # DAI
                       "0x5d3a1ff2b6bab83b63cd9ad0787074081a52ef34",    # USDe
                       "0x820c137fa70c8691f0e44dc420a5e53c168921dc",    # USDS
                       "0x6bb7a212910682dcfdbd5bcbb3e28fb4e8da10ee"}),  # GHO
    dexscreener="base", geckoterminal="base", explorer="https://basescan.org", alchemy="base-mainnet", ankr="base",
    public_rpcs=("https://mainnet.base.org", "https://base-rpc.publicnode.com", "https://base.drpc.org"),
    log_span=300, block_seconds=2,
    majors={"0x4200000000000000000000000000000000000006": "ETH",
            "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf": "cbBTC",
            "0x2ae3f1ec7f1f5012cfeab0185bfc7aa3cf0dec22": "cbETH",
            "0x940181a94a35a4569e4529a3cdfb74e38fd98631": "AERO",
            "0x0b3e328455c4059eeb9e3f84b5543f74e24e7e1b": "VIRTUAL"})

BSC = Chain(
    id="bsc", name="BNB Chain", kind="evm", native="BNB", decimals=18,
    wrapped="0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c",
    stables=frozenset({"0x55d398326f99059ff775485246999027b3197955",    # USDT
                       "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d",    # USDC
                       "0x8d0d000ee44948fc98c9b98a4fa4921476f08b0d",    # USD1
                       "0xc5f0f7b66764f6ec8c8dff7ba683102295e16409",    # FDUSD
                       "0xe9e7cea3dedca5984780bafc599bd69add087d56",    # BUSD
                       "0x5d3a1ff2b6bab83b63cd9ad0787074081a52ef34",    # USDe
                       "0xb3b02e4a9fb2bd28cc2ff97b0ab3f6b3ec1ee9d2"}),  # USDf
    dexscreener="bsc", geckoterminal="bsc", explorer="https://bscscan.com", alchemy="bnb-mainnet", ankr="bsc",
    # No keyless BNB Chain RPC answers wallet-filtered eth_getLogs or keeps more than ~60 s of state.
    public_rpcs=("https://bsc-rpc.publicnode.com", "https://bsc-dataseed.bnbchain.org"),
    block_seconds=0.45,  # measured 2026-10-07; time windows use the measured rate (EvmRPC.block_seconds)
    majors={"0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c": "BNB",
            "0x7130d2a12b9bcbfae4f2634d864a1ee1ce3ead9c": "BTCB",
            "0x2170ed0880ac9a755fd29b2688956bd959f933f8": "ETH",
            "0x570a5d26f7765ecb712c0924e4de545b89fd43df": "SOL",
            "0x1d2f0da169ceb9fc7b3144628db156f3f6c60dbe": "XRP",
            "0xba2ae424d960c26247dd6c32edc70b295c744c43": "DOGE",
            "0x0e09fabb73bd3ade0a17ecc321fd13a19e81ce82": "CAKE"})

ARBITRUM = Chain(
    id="arbitrum", name="Arbitrum", kind="evm", native="ETH", decimals=18,
    wrapped="0x82af49447d8a07e3bd95bd0d56f35241523fbab1",
    stables=frozenset({"0xaf88d065e77c8cc2239327c5edb3a432268e5831",    # USDC
                       "0xff970a61a04b1ca14834a43f5de4533ebddb5cc8",    # USDC.e
                       "0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9",    # USDT0
                       "0xda10009cbd5d07dd0cecc66161fc93d7c9000da1",    # DAI
                       "0x5d3a1ff2b6bab83b63cd9ad0787074081a52ef34",    # USDe
                       "0x6491c05a82219b8d1479057361ff1654749b876b",    # USDS
                       "0x7dff72693f6a4149b17e7c6314655f6a9f7c8b33"}),  # GHO
    dexscreener="arbitrum", geckoterminal="arbitrum", explorer="https://arbiscan.io", alchemy="arb-mainnet",
    ankr="arbitrum", public_rpcs=("https://arb1.arbitrum.io/rpc", "https://arbitrum-one-rpc.publicnode.com"),
    log_span=5000, block_seconds=0.25,
    majors={"0x82af49447d8a07e3bd95bd0d56f35241523fbab1": "ETH",
            "0x2f2a2543b76a4166549f7aab2e75bef0aefc5b0f": "WBTC",
            "0x912ce59144191c1204e64559fe8253a0e49e6548": "ARB",
            "0xfc5a1a6eb076a2c7ad06ed22c90d7e710e35ad0a": "GMX",
            "0xf97f4df75117a78c1a5a0dbb814af92458539fb4": "LINK",
            "0x0c880f6761f1af8d9aa9c466984b80dab9a8c9e8": "PENDLE"})

CHAINS = {chain.id: chain for chain in (SOLANA, ETHEREUM, BASE, BSC, ARBITRUM)}
EVM_CHAINS = tuple(chain.id for chain in CHAINS.values() if chain.evm)


def get(chain_id: str) -> Chain:
    return CHAINS[chain_id]
