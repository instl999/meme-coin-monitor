# Task brief: deploy holder_watch 1.2.0 to the owner's VPS

You are deploying an already-built, already-tested program. Don't change its code or settings; if a
step fails in a way this brief and [DEPLOY.md](DEPLOY.md) don't cover, stop and report.

## What it is

holder_watch is a **read-only** crypto monitor (Python, runs as a systemd service). It watches a Solana
token's holders, price and liquidity, and the trades of chosen wallets on Solana, Ethereum, Base, BNB
Chain and Arbitrum, and sends Telegram alerts. It never trades, signs or holds keys to funds. A
systemd timer renews its built-in trader lists once a month.

## Inputs

| Input | Where |
|---|---|
| Package | `dist/holder-watch-1.2.0.tar.gz` and `.sha256` in the project folder on the owner's PC (rebuild first: `python deploy/package.py`) |
| Secrets | `.env` in the project folder (Helius, Ankr and Telegram keys) |
| Server | from the owner: SSH host, user with sudo, and how to log in (key or password) |
| Owner's choices | already inside the package's `config.json`; nothing to choose |

## Rules

1. **Secrets:** never print, `cat`, `grep`, echo, paste or log the contents of `.env`, and never put its
   values into commands, files, commits or your messages. Copy the file only with `scp`, to
   `/tmp/holder-watch.env`; the installer moves it into place (mode 600) and deletes the copy.
2. **No code or config edits** on the server or the PC, except the documented fallbacks in DEPLOY.md
   ("If something fails"), and only when that exact message appears.
3. **No extra runs:** don't start `holder-watch-presets.service` by hand (it rebuilds lists for up to two
   hours and may change what is watched), and don't run the monitor on the PC (two copies = duplicate
   alerts).
4. **Telegram:** the installer sends two messages to the owner's chat (started, test alert). That is
   expected; don't send others.
5. Run everything on the server with `sudo` as shown. Don't open firewall ports; the program needs
   none (outbound HTTPS only).

## Steps

Each step's commands and expected output are in DEPLOY.md; the numbers match.

1. **On the PC** (PowerShell, project folder):
   ```powershell
   python deploy\package.py
   scp dist\holder-watch-1.2.0.tar.gz dist\holder-watch-1.2.0.tar.gz.sha256 USER@VPS:/tmp/
   scp .env USER@VPS:/tmp/holder-watch.env
   ```
   `package.py` must end with "checked 3 secret value(s) from .env: none packaged".
2. **On the server:** check, unpack, syntax-check, preflight:
   ```bash
   cd /tmp && sha256sum -c holder-watch-1.2.0.tar.gz.sha256
   tar -xzf holder-watch-1.2.0.tar.gz && cd holder-watch-1.2.0
   bash -n deploy/install.sh && echo "installer syntax OK"
   sudo ./deploy/install.sh --check
   ```
3. If `--check` says Python is missing or too old: DEPLOY.md step 3.
4. **Back up** an existing `/opt/holder-watch` (DEPLOY.md step 4). Note the backup's file name.
5. **Install:**
   ```bash
   sudo ./deploy/install.sh --env-file /tmp/holder-watch.env --replace-config
   ```
   Expect `228 passed`, `config OK … watched traders: Solana 2, BNB Chain 1, Base 1, Ethereum 1`,
   `… read through: Ankr (ANKR_API_KEY)` for BNB Chain, Base and Ethereum, a "next run" date for the
   monthly renewal, and finally `RESULT: holder-watch is running and its Telegram alerts work`
   (exit code 0).
6. **Verify** with the commands in DEPLOY.md step 6. All must hold:
   - `holder-watch` is active and enabled; `holder-watch-presets.timer` is enabled and lists the 1st of
     next month as its next run;
   - `systemd-analyze verify` on the three unit files prints nothing;
   - the journal shows `cycle ok` and `trader check ok` (not `had errors`);
   - `holder_watch.py traders` lists 5 wallets: Trader 1, Solana pro #1, BNB Chain pro #1, Base pro #1,
     Ethereum pro #1.
7. **Ask the owner** to confirm the two Telegram messages arrived.

## If it goes wrong

- Exit code 3 or 1 from the installer: find the `WARNING:`/`ERROR:` line in DEPLOY.md's "If something
  fails" table, apply that fix only, re-run step 5.
- Anything else, or a fix that doesn't work: stop, leave the service as it is, and report. To undo
  completely: DEPLOY.md "Rollback" (restores the step 4 backup).

## Report back

- server OS and version, Python version, systemd version (`systemctl --version | head -1`);
- the installer's last 20 lines and exit code (they contain no secrets);
- the step 6 results, and the renewal timer's next run;
- the backup file name from step 4 and any `config.json.bak-…` the installer made;
- anything you changed under "If something fails", and why.
