#!/usr/bin/env bash
# Install or update holder_watch as a systemd service on a Linux server, then verify that it runs
# and that its Telegram alerts are delivered. See DEPLOY.md.
#
# Run from the extracted package folder:
#   sudo ./deploy/install.sh --env-file /tmp/holder-watch.env   first install, with the owner's .env
#   sudo ./deploy/install.sh                                    update: keeps .env, config.json, state, logs
#
# Options:
#   --env-file PATH    install PATH as /opt/holder-watch/.env (mode 600), then delete PATH
#   --chains "LIST"    also watch the built-in traders of these chains, e.g. --chains "base bsc ethereum"
#                      (solana ethereum base bsc arbitrum); a missing list is built first, which needs the
#                      keys in .env and takes several minutes per chain
#   --no-auto-renew    don't enable the monthly renewal of the built-in trader lists (holder-watch-presets.timer)
#   --replace-config   use the package's config.json even if the server has its own (old one backed up)
#   --skip-tests       don't run the test suite
#   --no-test-alert    don't send the Telegram test message at the end
#   --no-start         install only, don't start the service
#   --check            only check the prerequisites
# Environment: PYTHON=python3.12 picks the interpreter (default: python3).
# Exit codes: 0 = running and alerts verified, 1 = install failed, 3 = installed but a check needs fixing.
set -euo pipefail

APP_DIR=/opt/holder-watch
APP_USER=holderwatch
SERVICE=holder-watch
UNIT_PATH=/etc/systemd/system/$SERVICE.service
RENEW_TIMER=$SERVICE-presets.timer
PYTHON=${PYTHON:-python3}
SRC_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
STAMP=$(date +%Y%m%d-%H%M%S)

CHECK_ONLY=0
ENV_FILE=""
CHAINS=""
AUTO_RENEW=1
REPLACE_CONFIG=0
RUN_TESTS=1
TEST_ALERT=1
START=1
PROBLEMS=0

say()  { printf '==> %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
# Run a command as the service user, from the app folder.
as_app() { (cd "$APP_DIR" && runuser -u "$APP_USER" -- env HOME="$APP_DIR" "$@"); }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env-file)
      [[ $# -ge 2 ]] || die "--env-file needs a path"
      ENV_FILE=$2
      shift ;;
    --chains)
      [[ $# -ge 2 ]] || die "--chains needs a list, e.g. --chains \"base bsc\""
      CHAINS=${2//,/ }
      for chain in $CHAINS; do
        [[ $chain =~ ^(solana|ethereum|base|bsc|arbitrum)$ ]] \
          || die "unknown chain '$chain' (choose from: solana ethereum base bsc arbitrum)"
      done
      shift ;;
    --replace-config) REPLACE_CONFIG=1 ;;
    --skip-tests) RUN_TESTS=0 ;;
    --no-test-alert) TEST_ALERT=0 ;;
    --no-start) START=0 ;;
    --check) CHECK_ONLY=1 ;;
    --no-auto-renew) AUTO_RENEW=0 ;;
    -h|--help) sed -n '2,21p' "$0"; exit 0 ;;
    *) die "unknown option: $1 (see --help)" ;;
  esac
  shift
done

preflight() {
  [[ $EUID -eq 0 ]] || die "run as root: sudo $0"
  [[ -f "$SRC_DIR/holder_watch.py" && -d "$SRC_DIR/watcher" && -f "$SRC_DIR/requirements.txt" ]] \
    || die "$SRC_DIR doesn't look like the extracted holder-watch package"
  [[ "$SRC_DIR" != "$APP_DIR" ]] || die "run the installer from the extracted package folder, not from $APP_DIR"
  if [[ ! -d /run/systemd/system ]] || ! command -v systemctl >/dev/null; then
    die "systemd is not running on this machine"
  fi
  command -v runuser >/dev/null || die "runuser (util-linux) not found"
  command -v "$PYTHON" >/dev/null \
    || die "$PYTHON not found. Debian/Ubuntu: apt-get install -y python3 python3-venv"
  "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
    || die "Python 3.10+ is required but $PYTHON is $("$PYTHON" -V 2>&1); install a newer one and re-run with PYTHON=python3.X (DEPLOY.md, step 3)"
  local probe
  probe=$(mktemp -d)
  if ! "$PYTHON" -m venv "$probe/venv" >/dev/null 2>&1; then
    rm -rf "$probe"
    die "$PYTHON can't create virtualenvs. Debian/Ubuntu: apt-get install -y python3-venv (or python3.X-venv)"
  fi
  rm -rf "$probe"
  if [[ -n "$ENV_FILE" && ! -f "$ENV_FILE" ]]; then
    die "--env-file $ENV_FILE not found"
  fi
  if command -v timedatectl >/dev/null \
     && [[ "$(timedatectl show -p NTPSynchronized --value 2>/dev/null || true)" == "no" ]]; then
    warn "the clock is not NTP-synchronised (the daily heartbeat relies on it): sudo timedatectl set-ntp true"
  fi
  local host
  for host in mainnet.helius-rpc.com rpc.ankr.com rpc.mevblocker.io mainnet.base.org arb1.arbitrum.io \
              bsc-rpc.publicnode.com api.dexscreener.com api.geckoterminal.com api.telegram.org; do
    if command -v getent >/dev/null && ! getent hosts "$host" >/dev/null; then
      warn "cannot resolve $host: check DNS and outbound network access"
    fi
  done
  say "preflight OK: $("$PYTHON" -V 2>&1), systemd $(systemctl --version | awk 'NR == 1 {print $2}'), package $SRC_DIR"
}

install_files() {
  if ! id -u "$APP_USER" >/dev/null 2>&1; then
    say "creating system user $APP_USER"
    useradd --system --user-group --home-dir "$APP_DIR" --no-create-home \
      --shell "$(command -v nologin || echo /usr/sbin/nologin)" "$APP_USER"
  fi
  install -d -m 750 -o "$APP_USER" -g "$APP_USER" "$APP_DIR"

  say "copying the program to $APP_DIR"
  rm -rf "$APP_DIR/watcher" "$APP_DIR/tests" "$APP_DIR/deploy"
  cp -R "$SRC_DIR/watcher" "$SRC_DIR/tests" "$SRC_DIR/deploy" "$APP_DIR/"
  local name
  for name in holder_watch.py presets.json requirements.txt requirements-dev.txt README.md DEPLOY.md CODEX_DEPLOY.md \
              .env.example; do
    if [[ -f "$SRC_DIR/$name" ]]; then
      cp "$SRC_DIR/$name" "$APP_DIR/$name"
    fi
  done

  if [[ ! -f "$APP_DIR/config.json" ]]; then
    cp "$SRC_DIR/config.json" "$APP_DIR/config.json"
  elif cmp -s "$SRC_DIR/config.json" "$APP_DIR/config.json"; then
    :
  elif [[ $REPLACE_CONFIG -eq 1 ]]; then
    cp -p "$APP_DIR/config.json" "$APP_DIR/config.json.bak-$STAMP"
    cp "$SRC_DIR/config.json" "$APP_DIR/config.json"
    say "replaced config.json (the previous one is config.json.bak-$STAMP)"
  else
    cp "$SRC_DIR/config.json" "$APP_DIR/config.json.new"
    warn "kept the server's config.json; the package's version is config.json.new (--replace-config to use it)"
  fi

  if [[ -n "$ENV_FILE" ]]; then
    if [[ -f "$APP_DIR/.env" ]]; then
      cp -p "$APP_DIR/.env" "$APP_DIR/.env.bak-$STAMP"
    fi
    install -m 600 -o "$APP_USER" -g "$APP_USER" "$ENV_FILE" "$APP_DIR/.env"
    shred -u "$ENV_FILE" 2>/dev/null || rm -f "$ENV_FILE"
    say "installed $APP_DIR/.env (mode 600) and deleted the uploaded copy"
  elif [[ ! -f "$APP_DIR/.env" ]]; then
    install -m 600 -o "$APP_USER" -g "$APP_USER" "$SRC_DIR/.env.example" "$APP_DIR/.env"
    warn "created $APP_DIR/.env from the template; it needs the owner's values (DEPLOY.md, step 5)"
  fi

  chown -R "$APP_USER:$APP_USER" "$APP_DIR"
  chmod 750 "$APP_DIR"
  chmod 600 "$APP_DIR"/.env*
}

setup_venv() {
  if [[ ! -x "$APP_DIR/.venv/bin/python" ]] \
     || ! "$APP_DIR/.venv/bin/python" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    say "creating the virtualenv with $("$PYTHON" -V 2>&1)"
    rm -rf "$APP_DIR/.venv"
    as_app "$(command -v "$PYTHON")" -m venv "$APP_DIR/.venv"
  fi
  local requirements="requirements.txt"
  if [[ $RUN_TESTS -eq 1 ]]; then
    requirements="requirements-dev.txt"
  fi
  say "installing Python packages from $requirements"
  as_app "$APP_DIR/.venv/bin/python" -m pip install --quiet --disable-pip-version-check --no-cache-dir \
    -r "$APP_DIR/$requirements"
}

run_tests() {
  say "running the test suite"
  as_app "$APP_DIR/.venv/bin/python" -m pytest -q -p no:cacheprovider \
    || die "tests failed (output above); fix the cause, or re-run with --skip-tests"
}

# Watches the built-in traders of --chains (building a missing list first, with the keys in .env).
add_presets() {
  say "watching the built-in traders of: $CHAINS"
  # shellcheck disable=SC2086  # one argument per chain
  as_app "$APP_DIR/.venv/bin/python" "$APP_DIR/holder_watch.py" presets add $CHAINS --build \
    || warn "couldn't add the built-in traders (output above); later: holder_watch.py presets add $CHAINS --build"
}

# Validates config.json with the real .env and prints a summary (never secret values), plus the
# markers MISSING_RPC_KEY / MISSING_EVM_KEY / MISSING_TELEGRAM when those settings are absent.
# Exit code 2 = invalid.
check_config() {
  as_app "$APP_DIR/.venv/bin/python" - <<'PY'
import sys
from pathlib import Path
from watcher.chains import CHAINS
from watcher.config import ConfigError, load_config, load_dotenv
from watcher.evm import source

load_dotenv(Path(".env"))
try:
    cfg = load_config(Path("config.json"), require_mint=False)
    if cfg.mint is None and not cfg.traders.wallets:
        load_config(Path("config.json"))  # nothing to run: report the missing token
except ConfigError as exc:
    print(exc)
    sys.exit(2)
chains = {}
for wallet in cfg.traders.wallets:
    chains[wallet.chain] = chains.get(wallet.chain, 0) + 1
watched = ", ".join(f"{CHAINS[c].name} {n}" for c, n in chains.items()) or "none"
print(f"config OK: token monitor {cfg.mint or 'off (traders only)'}, Solana RPC {cfg.rpc_label}, "
      f"Telegram {'configured' if cfg.telegram_token else 'NOT configured'}, "
      f"{len(cfg.always_alert_owners)} always-alert wallet(s), watched traders: {watched}, "
      f"checks every {cfg.poll_seconds}s (traders every {cfg.traders.poll_seconds}s)")
for note in cfg.notes:
    print(f"note: config.json: {note}")
if cfg.uses_public_rpc and (cfg.mint or "solana" in chains):
    print("MISSING_RPC_KEY")
for chain_id in chains:
    if CHAINS[chain_id].evm:
        src = source(CHAINS[chain_id])
        print(f"{CHAINS[chain_id].name} read through: {src.label if src else 'nothing (needs ANKR_API_KEY)'}")
if any(CHAINS[c].evm and source(CHAINS[c]) is None for c in chains):
    print("MISSING_EVM_KEY")
if not cfg.telegram_token:
    print("MISSING_TELEGRAM")
PY
}

install_unit() {
  say "installing $UNIT_PATH and the monthly renewal ($RENEW_TIMER)"
  install -m 644 "$APP_DIR/deploy/holder-watch.service" "$UNIT_PATH"
  install -m 644 "$APP_DIR/deploy/$SERVICE-presets.service" "/etc/systemd/system/$SERVICE-presets.service"
  install -m 644 "$APP_DIR/deploy/$RENEW_TIMER" "/etc/systemd/system/$RENEW_TIMER"
  systemctl daemon-reload
  systemctl enable "$SERVICE" >/dev/null 2>&1
  if [[ $AUTO_RENEW -eq 1 ]]; then
    systemctl enable --now "$RENEW_TIMER" >/dev/null 2>&1 || warn "could not enable $RENEW_TIMER"
    say "built-in trader lists renew monthly; next run: $(systemctl show "$RENEW_TIMER" -p NextElapseUSecRealtime --value 2>/dev/null || echo '?')"
  else
    systemctl disable --now "$RENEW_TIMER" >/dev/null 2>&1 || true
    say "monthly renewal of the built-in trader lists is off (--no-auto-renew)"
  fi
}

# Starts the service, waits for its first monitoring cycle and checks the startup message got to Telegram.
start_and_verify() {
  local since log=""
  since=$(date '+%Y-%m-%d %H:%M:%S')
  say "starting $SERVICE and waiting for the first monitoring cycle (up to 60 s)"
  systemctl restart "$SERVICE"
  for _ in $(seq 1 30); do
    sleep 2
    log=$(journalctl -u "$SERVICE" --since "$since" --no-pager -o cat 2>/dev/null || true)
    # "cycle ok" is the token monitor's, "trader check ok" the trader watch's (traders-only setups)
    if grep -qE '(cycle|trader check) (ok|had errors)' <<<"$log" || ! systemctl is-active --quiet "$SERVICE"; then
      break
    fi
  done
  printf '%s\n' "$log" | tail -n 20
  systemctl is-active --quiet "$SERVICE" || die "$SERVICE stopped right after starting (log above)"
  if grep -qE '(cycle|trader check) had errors' <<<"$log"; then
    warn "the first check had errors: $(grep -m1 -E '(cycle|trader check) had errors' <<<"$log" | sed -E 's/.*(cycle|trader check) had errors \| //')"
    PROBLEMS=1
  elif grep -qE '(cycle|trader check) ok' <<<"$log"; then
    say "first check OK: $(grep -m1 -E '(cycle|trader check) ok' <<<"$log" | sed -E 's/.*(cycle|trader check) ok \| //')"
  else
    warn "no check finished within 60 s; watch it with: journalctl -u $SERVICE -f"
    PROBLEMS=1
  fi
  if grep -q 'Telegram delivery failed' <<<"$log"; then
    warn "Telegram rejected the startup message: $(grep -m1 'Telegram delivery failed' <<<"$log" | sed 's/.*Telegram delivery failed: //')"
    PROBLEMS=1
  fi
}

send_test_alert() {
  local out
  say "sending a Telegram test alert"
  if out=$(as_app "$APP_DIR/.venv/bin/python" "$APP_DIR/holder_watch.py" --test-alert 2>&1); then
    say "Telegram test alert delivered"
  else
    warn "Telegram test alert failed: $(grep -m1 'Telegram delivery failed' <<<"$out" | sed 's/.*Telegram delivery failed: //')"
    PROBLEMS=1
  fi
}

preflight
if [[ $CHECK_ONLY -eq 1 ]]; then
  say "check only: nothing was changed"
  exit 0
fi

if systemctl is-active --quiet "$SERVICE"; then
  say "stopping $SERVICE for the update"
  systemctl stop "$SERVICE"
fi
install_files
setup_venv
if [[ $RUN_TESTS -eq 1 ]]; then
  run_tests
fi
if [[ -n "$CHAINS" ]]; then
  add_presets
fi
if ! report=$(check_config); then
  printf '%s\n' "$report"
  die "config.json is invalid; fix the lines above, then re-run the installer"
fi
grep -v '^MISSING_' <<<"$report" | sed 's/^/==> /' || true
install_unit

if [[ $START -eq 0 ]]; then
  say "installed (--no-start); start it with: sudo systemctl start $SERVICE"
  exit 0
fi
if grep -q '^MISSING_' <<<"$report"; then
  missing=()
  grep -q '^MISSING_RPC_KEY' <<<"$report" && missing+=("HELIUS_API_KEY (or SOLANA_RPC_URL)")
  grep -q '^MISSING_EVM_KEY' <<<"$report" && missing+=("ANKR_API_KEY (for BNB Chain)")
  grep -q '^MISSING_TELEGRAM' <<<"$report" && missing+=("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
  needed=$(printf '%s; ' "${missing[@]}")
  warn ".env needs ${needed%; } before alerts can work:"
  warn "  sudo -u $APP_USER nano $APP_DIR/.env    then re-run this installer (or: sudo systemctl start $SERVICE)"
  exit 3
fi
start_and_verify
if [[ $TEST_ALERT -eq 1 ]]; then
  send_test_alert
fi

cat <<EOF

  status:   systemctl status $SERVICE
  logs:     journalctl -u $SERVICE -f        (also $APP_DIR/logs/holder_watch.log)
  restart:  sudo systemctl restart $SERVICE  (after editing $APP_DIR/config.json or .env)
  traders:  sudo -u $APP_USER $APP_DIR/.venv/bin/python $APP_DIR/holder_watch.py traders   (presets and discover add more)
  renewal:  systemctl list-timers $RENEW_TIMER      (run now: sudo systemctl start $SERVICE-presets.service)

EOF
if [[ $PROBLEMS -eq 0 ]]; then
  say "RESULT: $SERVICE is running and its Telegram alerts work"
  exit 0
fi
warn "RESULT: $SERVICE is running, but the warnings above need fixing (DEPLOY.md, 'If something fails'); then re-run the installer"
exit 3
