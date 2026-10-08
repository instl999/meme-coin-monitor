# Meme Coin Monitor — Smart Money & Whale Watch

**English** | [简体中文](README.zh-CN.md)

A read-only Python CLI for public wallet trades, historical trader discovery and Telegram alerts. Version **1.2.0**, Python **3.10+**. The application never requests seed phrases or private keys, signs transactions or executes trades.

## Features

- Watch buys, sells and token swaps on Solana, Ethereum, Base, BNB Chain and Arbitrum.
- Discover a token's historically profitable traders, inspect wallet scorecards, refresh and renew preset lists.
- Monitor Solana SPL holders, outflows, price and deepest-pool liquidity with configurable rules.
- Send Telegram notifications, daily heartbeats and failure/recovery messages; save trade signals as JSONL.

“Smart money” is a historical performance filter, not identity verification or a guarantee of future returns.

## Installation

Windows PowerShell:

```powershell
git clone https://github.com/instl999/meme-coin-monitor.git
cd meme-coin-monitor
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe holder_watch.py setup
.\.venv\Scripts\python.exe holder_watch.py --once
.\.venv\Scripts\python.exe holder_watch.py
```

Linux / macOS:

```bash
git clone https://github.com/instl999/meme-coin-monitor.git
cd meme-coin-monitor
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python holder_watch.py setup
.venv/bin/python holder_watch.py --once
.venv/bin/python holder_watch.py
```

The first wallet check establishes a baseline; it does not replay historical trades. Stop with `Ctrl+C`. Restart after configuration changes. The setup wizard configures Telegram, networks, provider keys and watched traders, backing up configuration before edits.

## Credentials

Use `setup`, or copy `.env.example` to `.env`. Never commit credentials or place them in `config.json`.

| Variable | Purpose |
| --- | --- |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Telegram alerts; otherwise console/log output |
| `HELIUS_API_KEY` | Solana holder queries, discovery and preset building; used automatically with default public RPC configuration |
| `ANKR_API_KEY` | EVM indexed wallet history, discovery and scorecards; configure and validate a provider for BNB Chain watching |
| `ALCHEMY_API_KEY` | Alternative history provider; verify supported networks and API access |
| `SOLANA_RPC_URL` | Custom Solana RPC |
| `ETHEREUM_RPC_URL`, `BASE_RPC_URL`, `BSC_RPC_URL`, `ARBITRUM_RPC_URL` | Custom EVM RPC endpoints; ordinary nodes may lack indexed wallet history |

Ethereum, Base and Arbitrum trade watching has a public RPC path, subject to node restrictions and availability. Solana public RPC may reject holder queries. Provider quotas, pricing and supported APIs can change; verify these in your provider dashboard.

Create a bot through Telegram's `@BotFather`, message it, then run `python holder_watch.py telegram` and `python holder_watch.py --test-alert`. If another service uses the bot's webhook, enter the chat ID manually or use a separate bot.

## Configuration

The original `config.json` is retained as a sample. It follows CYBERLEEK at Solana mint `ApZuxdpzMrbEYTGEzeY9afh5pj9d6qPRJCTgQYiipbKg` and historical whale/trader lists. These are not current recommendations; review network, addresses and watchlists before use.

| Setting | Shipped value / meaning |
| --- | --- |
| `mint` | SPL holder token; `PASTE_MINT_HERE` permits traders-only mode with at least one watched trader |
| `poll_seconds` / `traders.poll_seconds` | 30-second holder / 60-second trader checks, minimum 15 seconds |
| `top_n` | 10 non-excluded holders, range 1–20 |
| `exclude_owners` / `auto_exclude_pools` | Manual / automatic pool exclusions |
| `always_alert_owners` | Any holder outflow from listed addresses alerts |
| `rules.holder_drop_pct` | Individual holder drops at least 20% |
| `rules.combined_drop_pct` | Combined watched holdings drop at least 10% |
| `rules.window_minutes` | 60-minute holder-rule window |
| `rules.min_liquidity_usd` | Deepest-pool liquidity below $100,000 |
| `rules.stop_price_usd` / `rules.trailing_stop_pct` | Price threshold / drawdown from persisted peak; `null` disables them |
| `traders.tokens` | `any`, `source` (discovery token), or `major`; wallet overrides supported |
| `traders.min_trade_usd` | $10 minimum estimated size; wallet overrides supported |
| `heartbeat` | Daily health message; sample timezone is `America/New_York` |
| `signal_log` | `signals.jsonl`; `null` disables recording |

Set rules to `null` to disable them. `labels` supplies wallet names; `alert_cooldown_minutes` controls repeat reminders. Startup validates settings and exits with code 2 on invalid configuration. `_`-prefixed fields are comments.

## Commands

Use your virtual environment's Python executable for these commands. Replace address placeholders; chain names are `solana`, `ethereum`, `base`, `bsc`, `arbitrum`.

```bash
python holder_watch.py traders
python holder_watch.py traders add WALLET_ADDRESS --chain base --label "My watch" --tokens any --min-usd 50
python holder_watch.py traders remove WALLET_ADDRESS --chain base
python holder_watch.py discover TOKEN_ADDRESS --chain base
python holder_watch.py discover TOKEN_ADDRESS --chain solana --watch 3
python holder_watch.py scorecard WALLET_ADDRESS --chain ethereum --days 30
python holder_watch.py presets list
python holder_watch.py presets add base bsc --build
python holder_watch.py presets refresh base --days 30 --top 5 --add
python holder_watch.py presets renew
python holder_watch.py --list
python holder_watch.py --config /path/to/config.json -v
```

Specify the network for EVM addresses. `discover --watch N` saves the top N without interactive selection; `--json` exports rankings. Discovery uses realized and unrealized profit and depends on available history and valuation. Presets filter historical profitability, holding times, activity and some bot/scalper behavior. Refreshing can take considerable time and API quota; renewal preserves manually added wallets.

## Files, tests and deployment

`holder_watch.py` is the entry point; `watcher/` implements monitoring, `tests/` contains offline tests and fixtures, and `deploy/` supplies packaging, installer and systemd units. `presets.json` holds shipped historical lists. Runtime state, signals, logs, local presets, `.env`, backups, virtual environments and build outputs are Git-ignored. Run only one monitor against the same state files.

```bash
python -m pip install -r requirements-dev.txt
python -m pytest tests -q
python deploy/package.py
```

Packaging generates `dist/holder-watch-1.2.0.tar.gz` and a SHA-256 checksum, excluding `.env` and runtime data. Upload archive, checksum and your own `.env` separately to a Linux server with Python 3.10+, venv, systemd and outbound HTTPS.

```bash
cd /tmp
sha256sum -c holder-watch-1.2.0.tar.gz.sha256
tar -xzf holder-watch-1.2.0.tar.gz
cd holder-watch-1.2.0
sudo ./deploy/install.sh --check
sudo ./deploy/install.sh --env-file /tmp/holder-watch.env
sudo systemctl status holder-watch
sudo journalctl -u holder-watch -n 50 --no-pager
```

Installation uses `/opt/holder-watch` and user `holderwatch`, enabling monthly preset renewal by default. Add `--replace-config` only to replace server settings with the package configuration (old settings are backed up); `--no-auto-renew` disables renewal. The installer deletes the temporary environment file supplied to it; keep a secure original. See [DEPLOY.md](DEPLOY.md) for full options. [CODEX_DEPLOY.md](CODEX_DEPLOY.md) is the original deployment brief, provided as documentation. Windows/macOS local runs do not use systemd.

## Troubleshooting and limits

- HTTP 429 / missing holder data: check keys and quotas, configure a provider and reduce polling frequency.
- Missing EVM discovery history: configure Ankr / Alchemy with the required indexed API and network permissions.
- Telegram `chat not found`: message your bot, then run `telegram` again.
- No trade alerts: check the baseline, watchlist, token scope and minimum size.
- Windows timezone errors: reinstall `requirements.txt` in the active environment; it includes `tzdata`.

Polling does not guarantee immediate or complete capture. Solana holder discovery is constrained by the RPC's top 20 token accounts. Keeper/solver execution, DCA, limit orders and some routes may be missed; EVM trades of the same token in one block may be combined. Classification and profit estimates are best effort. Market data comes from third parties; verify linked transactions before acting.

## License

The supplied archive contains no open-source license. This publication adds no licensing terms on the owner's behalf. Obtain rights-holder permission before reuse or redistribution.
