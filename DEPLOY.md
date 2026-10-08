# Deploying holder_watch to a Linux VPS

**Goal:** run the monitor 24/7 as a systemd service, renew its built-in trader lists once a month, and
confirm that its Telegram alerts arrive. An AI agent handed this job gets its brief in
[CODEX_DEPLOY.md](CODEX_DEPLOY.md); this file is the full procedure and reference.

holder_watch watches the Solana token CYBERLEEK (mint `ApZuxdpzMrbEYTGEzeY9afh5pj9d6qPRJCTgQYiipbKg`):
top holders, five whale wallets, price and liquidity. It also watches the trades of wallets on
**Solana, Ethereum, Base, BNB Chain and Arbitrum**: "Trader 1" (major tokens only) and the built-in
traders the owner chose. It sends alerts to the owner's Telegram. It only reads public data (Helius,
Ankr, the EVM chains' public RPCs, DEX Screener, GeckoTerminal), never trades, and needs no open ports.

| | |
|---|---|
| Package | `holder-watch-1.2.0.tar.gz` and `holder-watch-1.2.0.tar.gz.sha256` (built on the owner's PC: `python deploy/package.py`) |
| Installs into | `/opt/holder-watch`, running as the system user `holderwatch` |
| Service | `holder-watch` (systemd): starts at boot, restarts 30 s after a crash |
| Timer | `holder-watch-presets.timer`: on the 1st of each month (~05:00 UTC) runs `holder-watch-presets.service`, which renews the built-in trader lists and restarts the monitor when its watch list changed |
| Server needs | Linux with systemd (a VM/VPS, not a plain container), Python 3.10+ with `venv`, outbound HTTPS to `mainnet.helius-rpc.com`, `rpc.ankr.com`, `rpc.mevblocker.io`, `mainnet.base.org`, `arb1.arbitrum.io`, `bsc-rpc.publicnode.com` (and the backups in `watcher/chains.py`), `api.dexscreener.com`, `api.geckoterminal.com`, `api.telegram.org` |
| Secrets | the owner's `.env` (`HELIUS_API_KEY`, `ANKR_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`), stored as `/opt/holder-watch/.env` with mode 600. Never print, copy into chat or logs, or commit its values. |
| Owner's choices | already in the package's `config.json`: Trader 1 (major tokens) plus the built-in traders of Solana, BNB Chain, Base and Ethereum, $10 minimum trade, checks every 60 s |

The installer does the work: it creates the user, installs the program and its Python packages, runs
the test suite, validates the config, installs and starts the service and the monthly timer, and
checks that alerts reach Telegram. It is safe to run again, for example after fixing something.

## 1. Copy the files to the server

From the project folder on the owner's Windows PC (PowerShell), with the package freshly built:

```powershell
python deploy\package.py                    # prints the archive's sha256 and "none packaged" for the secrets
scp dist\holder-watch-1.2.0.tar.gz dist\holder-watch-1.2.0.tar.gz.sha256 USER@VPS:/tmp/
scp .env USER@VPS:/tmp/holder-watch.env
```

The package never contains `.env`; it travels separately and the installer deletes the uploaded copy.

## 2. Unpack and check

```bash
cd /tmp
sha256sum -c holder-watch-1.2.0.tar.gz.sha256      # expect: holder-watch-1.2.0.tar.gz: OK
tar -xzf holder-watch-1.2.0.tar.gz
cd holder-watch-1.2.0
bash -n deploy/install.sh && echo "installer syntax OK"
sudo ./deploy/install.sh --check                  # prerequisites only; changes nothing
```

## 3. Install Python (if `--check` asked for it)

Debian / Ubuntu: `sudo apt-get update && sudo apt-get install -y python3 python3-venv ca-certificates`
(`python3 -V` must say 3.10 or newer). Fedora / RHEL / Alma / Rocky: `sudo dnf install -y python3`.

If the system Python is older than 3.10, install a newer one and pass it with `PYTHON=`:

- Ubuntu 20.04: `sudo add-apt-repository -y ppa:deadsnakes/ppa && sudo apt-get install -y python3.12 python3.12-venv`, then `sudo PYTHON=python3.12 ./deploy/install.sh …`
- RHEL / Alma / Rocky 8–9: `sudo dnf install -y python3.11`, then `sudo PYTHON=python3.11 ./deploy/install.sh …`
- Debian 11: upgrade to Debian 12, or install Python 3.11+ another way and pass `PYTHON=`.

## 4. Back up an existing installation

An older version (1.0.0 or 1.1.0) may already run there. Keep a copy before changing anything:

```bash
if [ -d /opt/holder-watch ]; then
  sudo tar -czf /root/holder-watch-backup-$(date +%Y%m%d-%H%M%S).tar.gz --exclude=holder-watch/.venv -C /opt holder-watch
  sudo ls -l /root/holder-watch-backup-*.tar.gz
fi
```

## 5. Run the installer

```bash
sudo ./deploy/install.sh --env-file /tmp/holder-watch.env --replace-config
```

- `--env-file` installs the owner's secrets as `/opt/holder-watch/.env` (mode 600) and deletes the
  uploaded copy. The installer prints names and counts, never secret values.
- `--replace-config` makes the package's `config.json` (the owner's current choices) the server's; an
  existing one is first saved as `config.json.bak-<date>-<time>`. Leave it out only if the owner says
  the server's own `config.json` must stay (the package's is then saved as `config.json.new`).
- The monthly timer is enabled by default; `--no-auto-renew` leaves it off.
- No `--chains` is needed: the built-in traders are already in the package's `config.json`.

A successful run ends like this (`…` varies):

```
==> running the test suite
228 passed in …s
==> config OK: token monitor ApZuxdpzMrbEYTGEzeY9afh5pj9d6qPRJCTgQYiipbKg, Solana RPC mainnet.helius-rpc.com (key from HELIUS_API_KEY), Telegram configured, 6 always-alert wallet(s), watched traders: Solana 2, BNB Chain 1, Base 1, Ethereum 1, checks every 30s (traders every 60s)
==> BNB Chain read through: Ankr (ANKR_API_KEY)
==> Base read through: Ankr (ANKR_API_KEY)
==> Ethereum read through: Ankr (ANKR_API_KEY)
==> installing /etc/systemd/system/holder-watch.service and the monthly renewal (holder-watch-presets.timer)
==> built-in trader lists renew monthly; next run: … 1 … 05:… UTC
==> starting holder-watch and waiting for the first monitoring cycle (up to 60 s)
…
==> first check OK: price $0.00… | liquidity $… | top 10 hold …% | watching 11 wallets | 0 rule hit(s), 0 sent, 0 in cooldown
==> sending a Telegram test alert
==> Telegram test alert delivered
==> RESULT: holder-watch is running and its Telegram alerts work
```

Exit code `0` means done. `3` means the service is installed but a check found a problem, printed as a
`WARNING:` line. `1` means the install failed, with an `ERROR:` line. For either, see "If something
fails", fix the cause, and run the installer again.

## 6. Verify

```bash
systemctl is-active holder-watch                                   # active
systemctl is-enabled holder-watch holder-watch-presets.timer       # enabled, enabled
systemctl list-timers holder-watch-presets.timer --no-pager        # NEXT: the 1st of next month
sudo systemd-analyze verify /etc/systemd/system/holder-watch.service /etc/systemd/system/holder-watch-presets.service /etc/systemd/system/holder-watch-presets.timer
sudo journalctl -u holder-watch --since "10 min ago" --no-pager | grep -E "cycle (ok|had errors)|trader check (ok|had errors)"
cd /opt/holder-watch && sudo -u holderwatch .venv/bin/python holder_watch.py traders
```

`traders` lists 5 wallets (Trader 1, Solana pro #1, BNB Chain pro #1, Base pro #1, Ethereum pro #1)
and the estimated monthly API use. `systemd-analyze verify` prints nothing when the units are fine.
The trader check line should say `trader check ok`; a wallet's first check only notes where it is.

Don't start `holder-watch-presets.service` by hand to test it unless the owner asks: it rebuilds every
watched list (up to an hour or two, a few million Ankr credits) and may change the watch list.

## 7. Confirm with the owner

The owner should now have **two new Telegram messages** from their bot: "… monitor started" and
"… monitor: test alert". From then on they'll get:

- an alert whenever one of their rules fires (any sale or transfer by the watched whale wallets, with
  whether it was a DEX sale or a transfer and Solscan links);
- a "Trader watch" message when a watched trader buys or sells ("BUY on Base by Base pro #1 …";
  Trader 1's major-token trades are tagged "major-token monitor");
- a heartbeat every day at 10:00 New York time;
- on the 1st of each month, "Built-in traders renewed" with what changed per chain;
- "monitor failing" if 5 checks in a row fail, then "recovered".

**Only one copy may run:** the monitor must not also run on the owner's PC (duplicate alerts).

## Monthly renewal

`holder-watch-presets.timer` starts `holder-watch-presets.service` on the 1st of each month around
05:00 UTC (a run missed while the server was off happens at the next boot). It runs
`holder_watch.py presets renew` as `holderwatch`: for each chain whose built-in traders are watched it
rebuilds the list from the last 30 days, then watches the new list instead (traders that still qualify
stay with their settings, new ones are added, others dropped; wallets the owner added are never
touched; a chain where nobody passes keeps its traders). `config.json` is backed up first. When the
watch list changed, the service restarts `holder-watch`. It reports to Telegram either way.

| Task | Command |
|---|---|
| Next and last run | `systemctl list-timers holder-watch-presets.timer` |
| Last run's output | `sudo journalctl -u holder-watch-presets --no-pager -n 100` |
| Run it now | `sudo systemctl start holder-watch-presets.service` (takes up to an hour or two) |
| Turn it off / on | `sudo systemctl disable --now holder-watch-presets.timer` / `sudo systemctl enable --now holder-watch-presets.timer` |

## If something fails

| Message | Fix |
|---|---|
| `bash -n` reports a syntax error in `install.sh` | Don't improvise a rewrite: report the line to the owner (the installer couldn't be run on the PC it was written on). |
| `ERROR: Python 3.10+ is required` / `can't create virtualenvs` | Step 3. |
| `ERROR: systemd is not running` | The machine has no systemd (e.g. a plain container). Use a VPS/VM with systemd. |
| `… stopped right after starting`, with `status=226/NAMESPACE` in `systemctl status holder-watch` | Some container-based VPSes (OpenVZ/LXC) don't support the units' sandboxing. Tell the owner, then comment out the `Private*`, `Protect*` and `Restrict*` lines in `/etc/systemd/system/holder-watch.service` and `holder-watch-presets.service`, and run `sudo systemctl daemon-reload && sudo systemctl restart holder-watch`. The installer reinstalls these files, so repeat after updates. |
| `systemd-analyze verify` complains about `holder-watch-presets.*` | Report the message. Old systemd (before 235) doesn't accept `UTC` in `OnCalendar=`: remove ` UTC` from `/etc/systemd/system/holder-watch-presets.timer` (it then uses the server's time zone) and `sudo systemctl daemon-reload`. |
| `ERROR: tests failed` | Read the pytest output; usually a broken Python install. Re-run with `--skip-tests` only if the cause is understood. |
| `config.json has N problem(s)` | Each line says what to fix in `/opt/holder-watch/config.json`. |
| `note: config.json: … is no longer used (amounts are in USD since 1.2.0)` | Not an error: a 1.1.0 SOL setting is ignored and its USD replacement applies. |
| `WARNING: .env needs HELIUS_API_KEY …` / `… ANKR_API_KEY (for BNB Chain) …` | The uploaded `.env` lacked a key. Ask the owner; they fill `/opt/holder-watch/.env` (`sudo -u holderwatch nano /opt/holder-watch/.env`), then re-run the installer without `--env-file`. |
| `first check had errors: … HTTP 429 … add HELIUS_API_KEY` | No Helius key, so the public Solana RPC is used and refuses the call. |
| `… HTTP 401` from Helius | The Helius key is wrong or revoked (dashboard.helius.dev). |
| `BNB Chain: no RPC for N wallet(s); add ANKR_API_KEY to .env` | No Ankr key; BNB Chain has no keyless option. |
| Ankr `HTTP 401` (`Unauthorized`) or `403` (`API key is not allowed to access blockchain`) | Wrong key, or the owner's Ankr plan doesn't include that chain. |
| `… public RPC …` lines in the debug log | A public node didn't answer; its backups (and Ankr) are used instead. Only a concern if every check fails. |
| `Telegram … HTTP 400 (Bad Request: chat not found)` | Wrong `TELEGRAM_CHAT_ID`, or the owner never sent the bot a message. |
| `Telegram … HTTP 401` or `404` | Wrong `TELEGRAM_BOT_TOKEN`. |
| `network error` | Outbound HTTPS blocked: `curl -sI https://api.telegram.org` and check the provider's firewall. |
| `no check finished within 60 s` | `journalctl -u holder-watch -n 50`. Slow networks can need a minute more. |
| Heartbeat at the wrong time | `sudo timedatectl set-ntp true`. |
| Duplicate alerts | Two copies are running (`pgrep -af holder_watch.py`); the owner's PC must not run it too. |
| No "Trader watch" messages | `holder_watch.py traders` shows who is watched; each wallet's first check only notes where it is, so only later trades alert. Check `traders.min_trade_usd` and `traders.tokens`. |
| The renewal's Telegram summary says "not renewed … needs ANKR_API_KEY" | The key is missing or wrong in `.env`. |

## Operating

| Task | Command |
|---|---|
| Status | `systemctl status holder-watch` |
| Live log | `sudo journalctl -u holder-watch -f` (also `/opt/holder-watch/logs/holder_watch.log`) |
| Change settings | `sudo -u holderwatch nano /opt/holder-watch/config.json`, then `sudo systemctl restart holder-watch` |
| Change secrets | `sudo -u holderwatch nano /opt/holder-watch/.env`, then `sudo systemctl restart holder-watch` |
| Test Telegram | `sudo -u holderwatch /opt/holder-watch/.venv/bin/python /opt/holder-watch/holder_watch.py --test-alert` |
| Built-in traders | `… holder_watch.py presets list` / `presets add CHAIN… [--build]` / `presets remove CHAIN…` / `presets renew`, then restart |
| Check one wallet | `… holder_watch.py scorecard ADDRESS [--chain base]` |
| Find and watch traders | `cd /opt/holder-watch && sudo -u holderwatch .venv/bin/python holder_watch.py discover <TOKEN> [--chain base]`, then restart |
| Watch / unwatch a wallet | `… holder_watch.py traders add <ADDRESS> [--chain base] --label "Name" [--tokens major] [--min-usd 100]` / `… traders remove <ADDRESS> [--chain base]`, then restart |
| Stop / start | `sudo systemctl stop holder-watch` / `sudo systemctl start holder-watch` |

(`…` = `cd /opt/holder-watch && sudo -u holderwatch .venv/bin/python`.)

Usage on the free plans (`holder_watch.py traders` prints the estimate): Helius about 430K of 1M credits
a month (token monitor at 30 s plus the Solana traders); Ankr about 4M of 200M credits a month for the
current watch list, plus a few million for each monthly renewal. The per-minute EVM checks go to free
public RPCs.

**Updating:** unpack a newer package and run `sudo ./deploy/install.sh` from it (step 4's backup
first). The update keeps `.env`, `state.json`, `trader_state.json`, `signals.jsonl`,
`presets.local.json` and the logs; `config.json` as described under step 5.

**Rollback:** `sudo systemctl stop holder-watch`, then restore the step 4 backup
(`sudo tar -xzf /root/holder-watch-backup-….tar.gz -C /opt`) and reinstall that version's package
with its own installer.

**Uninstall:**

```bash
sudo systemctl disable --now holder-watch holder-watch-presets.timer
sudo rm /etc/systemd/system/holder-watch.service /etc/systemd/system/holder-watch-presets.service /etc/systemd/system/holder-watch-presets.timer
sudo systemctl daemon-reload
sudo rm -rf /opt/holder-watch && sudo userdel holderwatch
```
