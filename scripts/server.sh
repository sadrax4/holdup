#!/usr/bin/env bash
# HoldUp on your own Ubuntu server: install it, keep it running 24/7 in tmux, and look after it.
#
#   bash scripts/server.sh install [--offline]    setup, the testnet step (first time only), start, status
#   bash scripts/server.sh setup                  check tools, ask for the keys, write settings, install packages
#   bash scripts/server.sh testnet [--force-new]  testnet accounts with free test money, then deploy the contracts
#   bash scripts/server.sh start [--offline]      start HoldUp in tmux (session "holdup" on its own socket)
#   bash scripts/server.sh stop                   stop HoldUp and everything it started
#   bash scripts/server.sh restart                stop, then start the same way again
#   bash scripts/server.sh status                 what runs, where, how healthy, testnet balances
#   bash scripts/server.sh url                    print the public URL
#   bash scripts/server.sh logs [app|tunnel|fixtures|supervisor] [-f]
#   bash scripts/server.sh update                 git pull --ff-only, uv sync, restart if it was running
#   bash scripts/server.sh topup [NEAR]           ask the testnet faucet for more test NEAR for the treasury
#   bash scripts/server.sh backup [DIR]           archive the secrets and the database (deploy/backup.sh)
#
# No sudo, Docker, systemd or crontab. Everything HoldUp runs listens on 127.0.0.1, on ports from
# 47800-47999 that are checked free first, and is reached from the internet only through outbound
# Cloudflare quick tunnels. Only processes this script started (and verified) are ever signalled;
# HoldUp's tmux session lives in a tmux server of its own: tmux -L holdup attach -t holdup.
# Guide: deploy/README.md. How it works: docs/SERVER.md.
if [ -z "${BASH_VERSION:-}" ]; then
  echo "error: run this with bash: bash scripts/server.sh <command>" >&2
  exit 2
fi
set -euo pipefail
set +x # tracing would print the secrets this script handles
umask 077

SESSION=holdup
WINDOW=run
TREASURY_MIN_NEAR=5 # what the testnet step leaves in the treasury for onboarding the first users
UV_FLAGS=(--locked --no-dev --no-managed-python --no-python-downloads)
# Supervisor settings passed on to the tmux session when they are set (tests and fine tuning).
TUNING_VARS=(HOLDUP_PORT_MIN HOLDUP_PORT_MAX HOLDUP_CHECK_INTERVAL HOLDUP_BACKOFF_MIN HOLDUP_BACKOFF_MAX
  HOLDUP_STABLE_AFTER HOLDUP_PUBLIC_CHECK_INTERVAL HOLDUP_STOP_TIMEOUT HOLDUP_TUNNEL_TIMEOUT
  HOLDUP_APP_TIMEOUT HOLDUP_READY_GRACE HOLDUP_LOG_MAX_BYTES HOLDUP_TUNNEL_PROTOCOL
  HOLDUP_TUNNEL_EDGE_IP_VERSION)
# Settings that setup copies into holdup.env when the file lacks them, from HOLDUP_<NAME> in the
# environment (TELEGRAM_API_BASE_URL too, which setup also uses for its getMe check).
OPTIONAL_SETTINGS=(OTARI_BASE_URL OTARI_USER_KEYS NEAR_RPC_URL)

say() { printf '%s\n' "$*"; }
step() { printf '\n==> %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

usage() {
  say "HoldUp on your own server. Commands:"
  grep '^#   bash scripts/server.sh' "${BASH_SOURCE[0]}" | sed 's/^#   /  /'
  say "Guide: deploy/README.md"
}

# ----------------------------------------------------------------------------------------------
# where things are
# ----------------------------------------------------------------------------------------------

configure() {
  REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
  TOOLS="$REPO/scripts/server_tools.py"
  [[ -n ${HOME:-} ]] || die "HOME is not set"
  HOLDUP_HOME="${HOLDUP_HOME:-$HOME/work}"
  HOLDUP_SECRETS_DIR="${HOLDUP_SECRETS_DIR:-$HOLDUP_HOME/secrets}"
  HOLDUP_DATA_DIR="${HOLDUP_DATA_DIR:-$HOLDUP_HOME/holdup-data}"
  HOLDUP_CLOUDFLARED="${HOLDUP_CLOUDFLARED:-$HOLDUP_HOME/bin/cloudflared}"
  local path
  for path in "$HOLDUP_HOME" "$HOLDUP_SECRETS_DIR" "$HOLDUP_DATA_DIR" "$HOLDUP_CLOUDFLARED"; do
    [[ $path == /* ]] || die "paths must be absolute (got $path)"
  done
  ENV_FILE="$HOLDUP_SECRETS_DIR/holdup.env"
  CREDS="$HOLDUP_SECRETS_DIR/holdup-credentials.json"
  LOG_DIR="$HOLDUP_DATA_DIR/logs"
  RUN_DIR="$HOLDUP_DATA_DIR/run"
  PORTS_FILE="$HOLDUP_DATA_DIR/ports.env"
  # HoldUp runs in a tmux server of its own (tmux -L holdup): the owner's other tmux sessions are
  # never even looked at, and no tmux.conf changes how HoldUp's session behaves.
  HOLDUP_TMUX_SOCKET="${HOLDUP_TMUX_SOCKET:-holdup}"
  [[ $HOLDUP_TMUX_SOCKET =~ ^[A-Za-z0-9._-]+$ ]] || die "HOLDUP_TMUX_SOCKET may only use letters, digits, '.', '_' and '-'"
  TMUX_CMD=(tmux -L "$HOLDUP_TMUX_SOCKET" -f /dev/null)
  ATTACH="tmux -L $HOLDUP_TMUX_SOCKET attach -t $SESSION"
  UV=""
}

py() { python3 "$TOOLS" "$@"; }

find_uv() {
  local candidate
  UV="${HOLDUP_UV:-}"
  if [[ -z $UV ]]; then
    UV="$(command -v uv 2>/dev/null || true)"
  fi
  if [[ -z $UV ]]; then
    for candidate in "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
      if [[ -x $candidate ]]; then
        UV=$candidate
        break
      fi
    done
  fi
  if [[ -n $UV && $UV != /* ]]; then
    UV="$(command -v -- "$UV" 2>/dev/null || true)"
  fi
  [[ -n $UV && -x $UV ]]
}

# ----------------------------------------------------------------------------------------------
# safety checks
# ----------------------------------------------------------------------------------------------

refuse_root() {
  if [[ $(id -u) -eq 0 ]]; then
    die "do not run this as root (or with sudo). Run it as the user that owns $HOLDUP_HOME."
  fi
}

owned_by_me() { [[ $(stat -c %u -- "$1") == "$(id -u)" ]]; }

check_home() { # check_home [create]
  if [[ ! -d $HOLDUP_HOME ]]; then
    [[ ${1:-} == create ]] || die "$HOLDUP_HOME does not exist; run: bash scripts/server.sh setup"
    mkdir -p -m 700 -- "$HOLDUP_HOME"
  fi
  owned_by_me "$HOLDUP_HOME" || die "$HOLDUP_HOME belongs to another user; refusing to use it"
  local dir
  for dir in "$HOLDUP_SECRETS_DIR" "$HOLDUP_DATA_DIR"; do
    if [[ -e $dir ]] && ! owned_by_me "$dir"; then
      die "$dir belongs to another user; refusing to use it"
    fi
  done
}

private_dir() {
  local dir=$1
  if [[ -d $dir ]]; then
    owned_by_me "$dir" || die "$dir belongs to another user"
    if [[ $(stat -c %a -- "$dir") != 700 ]]; then
      chmod 700 -- "$dir"
      say "  set $dir to mode 700"
    fi
  else
    mkdir -p -m 700 -- "$dir"
  fi
}

# tmux, with an environment that holds nothing secret: the client that starts a tmux server hands
# it its whole environment, and every session created there later inherits that.
tm() {
  env -i HOME="$HOME" PATH="$PATH" LANG="${LANG:-C.UTF-8}" ${TMUX_TMPDIR:+"TMUX_TMPDIR=$TMUX_TMPDIR"} \
    "${TMUX_CMD[@]}" "$@"
}

session_exists() { tm has-session -t "=$SESSION" 2>/dev/null; }

# Whether start created the session (it marks it), so that it may add its window there again.
session_is_ours() { [[ "$(tm display-message -p -t "=$SESSION:" '#{@holdup_runner}' 2>/dev/null)" == yes ]]; }

# HoldUp's window: "live" while a process runs in it, "dead" when only a dead pane is left (with
# remain-on-exit), nothing when there is no such window. Who runs there is the supervisor's
# record's business: nothing is ever signalled through tmux.
run_window() {
  local panes
  panes="$(tm list-panes -t "=$SESSION:=$WINDOW" -F '#{pane_dead}' 2>/dev/null)" || return 0
  if [[ $'\n'$panes$'\n' == *$'\n'0$'\n'* ]]; then echo live; else echo dead; fi
}

# Close HoldUp's window once nothing runs in it any more; no process gets a signal from this.
close_dead_window() {
  if [[ $(run_window) == dead ]]; then
    tm kill-window -t "=$SESSION:=$WINDOW" 2>/dev/null || true
  fi
}

# The supervisor holds this lock for its whole life, inside tmux or not.
supervisor_alive() { [[ -d $RUN_DIR ]] && ! py lock-free "$RUN_DIR/supervisor.lock"; }

# The value of one setting in holdup.env; fails when it is missing or empty.
env_get() { [[ -f $ENV_FILE ]] && py env-get "$ENV_FILE" "$1"; }

# Unset every exported variable except a few harmless ones (run in a subshell).
clean_env() {
  local name
  while IFS= read -r name; do
    case $name in
      HOME | PATH | LANG | LANGUAGE | LC_* | TERM | USER | LOGNAME | TMPDIR | TZ) ;;
      http_proxy | https_proxy | no_proxy | HTTP_PROXY | HTTPS_PROXY | NO_PROXY | SSL_CERT_FILE | SSL_CERT_DIR) ;;
      *) unset -v "$name" 2>/dev/null || true ;;
    esac
  done < <(compgen -e)
}

# Run a command of this project through uv, in a clean environment that holds only what it needs.
# Secrets reach it as environment variables set by builtins, never on a command line.
project() {
  (
    clean_env
    export HOLDUP_CREDENTIALS="$CREDS" HOLDUP_NETWORK=testnet
    if [[ -n ${PROJECT_SECRET:-} ]]; then
      export HOLDUP_SECRET="$PROJECT_SECRET"
    fi
    if [[ -n ${PROJECT_RPC:-} ]]; then
      export NEAR_RPC_URL="$PROJECT_RPC"
    fi
    cd "$REPO"
    exec "$UV" run "${UV_FLAGS[@]}" "$@"
  )
}

# ----------------------------------------------------------------------------------------------
# setup
# ----------------------------------------------------------------------------------------------

check_tools() {
  step "Checking the tools"
  if ((BASH_VERSINFO[0] < 4 || (BASH_VERSINFO[0] == 4 && BASH_VERSINFO[1] < 4))); then
    die "bash 4.4 or newer is needed (found $BASH_VERSION)"
  fi
  say "  bash         $BASH_VERSION"
  command -v tmux >/dev/null 2>&1 || die "tmux is not installed (Ubuntu package: tmux)"
  say "  tmux         $(tmux -V)"
  command -v curl >/dev/null 2>&1 || die "curl is not installed (Ubuntu package: curl)"
  say "  curl         $(curl --version 2>/dev/null | head -n 1 | cut -d' ' -f2)"
  command -v python3 >/dev/null 2>&1 || die "python3 is not installed"
  python3 -c 'import sys; sys.exit(sys.version_info < (3, 12))' ||
    die "python3 3.12 or newer is needed (found $(python3 --version 2>&1))"
  say "  python3      $(python3 -c 'import platform; print(platform.python_version())')"
  find_uv || die "uv is not installed (https://docs.astral.sh/uv/), or set HOLDUP_UV to its path"
  say "  uv           $("$UV" --version 2>/dev/null | head -n 1) ($UV)"
  if [[ ! -f $HOLDUP_CLOUDFLARED || ! -x $HOLDUP_CLOUDFLARED ]]; then
    die "cloudflared is not at $HOLDUP_CLOUDFLARED (or is not executable). Put the binary there, or set HOLDUP_CLOUDFLARED."
  fi
  say "  cloudflared  $("$HOLDUP_CLOUDFLARED" --version 2>/dev/null | head -n 1)"
  if ! command -v git >/dev/null 2>&1; then
    warn "git is not installed; 'update' will not work"
  fi
  if ! command -v ss >/dev/null 2>&1; then
    say "  (ss is not installed; ports are checked through /proc/net/tcp instead)"
  fi
  local free_kb
  free_kb="$(df -Pk -- "$HOLDUP_HOME" | awk 'NR == 2 { print $4 }')"
  if [[ $free_kb =~ ^[0-9]+$ ]] && ((free_kb < 1048576)); then
    warn "less than 1 GB is free on the disk of $HOLDUP_HOME"
  fi
}

ask_secret() { # ask_secret PROMPT: reads without echo from the terminal, prints the answer
  local answer=""
  IFS= read -rs -p "$1: " answer || true
  printf '\n' >&2
  printf '%s' "$answer"
}

# A setting from the environment, under HoldUp's own name only: HOLDUP_<NAME> (HOLDUP_SECRET as it is).
# This account may run other bots, so a plain TELEGRAM_BOT_TOKEN may well be someone else's.
from_env() {
  local name="HOLDUP_${1#HOLDUP_}"
  printf '%s' "${!name:-}"
}

valid_otari_key() { [[ $1 =~ ^otk_[A-Za-z0-9._~-]{12,}$ ]]; }
valid_bot_token() { [[ $1 =~ ^[0-9]{5,16}:[A-Za-z0-9_-]{30,}$ ]]; }

# getMe with the token in a curl config read from stdin, so it never appears on a command line.
# Sets BOT_USERNAME; returns 0 when Telegram accepted the token, 1 when it rejected it, 2 otherwise.
telegram_get_me() {
  local token=$1 base=${2%/} body code err
  BOT_USERNAME=""
  TELEGRAM_PROBLEM=""
  body="$(mktemp "$RUN_DIR/.getme.XXXXXX")"
  code="$({ printf 'url = "%s/bot%s/getMe"\n' "$base" "$token" |
    curl --silent --show-error --max-time 20 --config - --output "$body" --write-out '%{http_code}'; } 2>"$body.err")" || true
  err="$(head -c 300 -- "$body.err" 2>/dev/null || true)"
  err="${err//"$token"/[redacted]}"
  case $code in
    200)
      BOT_USERNAME="$(py telegram-username <"$body" 2>/dev/null)" || BOT_USERNAME=""
      ;;
    401 | 404) ;;
    *) TELEGRAM_PROBLEM="HTTP ${code:-000}${err:+: $err}" ;;
  esac
  rm -f -- "$body" "$body.err"
  if [[ -n $BOT_USERNAME ]]; then
    return 0
  elif [[ $code == 401 || $code == 404 ]]; then
    return 1
  fi
  return 2
}

setup_settings() {
  step "Settings and secrets ($ENV_FILE)"
  if [[ -f $ENV_FILE ]]; then
    owned_by_me "$ENV_FILE" || die "$ENV_FILE belongs to another user"
    if [[ $(stat -c %a -- "$ENV_FILE") != 600 ]]; then
      chmod 600 -- "$ENV_FILE"
      say "  set $ENV_FILE to mode 600"
    fi
    py env-check "$ENV_FILE" || die "fix $ENV_FILE (one KEY='value' per line) and run setup again"
  fi
  local interactive=0 otari otari_mode token token_saved=0 api_base holdup_secret webhook_secret mode rc=0
  if [[ -t 0 ]]; then
    interactive=1
  fi

  otari="$(env_get OTARI_API_KEY)" || otari="$(from_env OTARI_API_KEY)"
  otari_mode="$(env_get OTARI_MODE)" || otari_mode="$(from_env OTARI_MODE)"
  if [[ -z $otari && $otari_mode != off ]]; then
    if [[ -n ${OTARI_API_KEY:-} ]]; then
      say "  OTARI_API_KEY in the environment is not used; give HoldUp's key as HOLDUP_OTARI_API_KEY, or type it below."
    fi
    if ((interactive)); then
      say "  The Otari API key (otk_...) lets the open-weight models check payments. It is not shown as you type."
      otari="$(ask_secret '  Otari API key (empty: rules only)')"
    fi
    if [[ -z $otari ]]; then
      warn "no OTARI_API_KEY: payments will be checked by the deterministic rules only (OTARI_MODE=off)"
      otari_mode=off
    fi
  fi
  if [[ -n $otari ]] && ! valid_otari_key "$otari"; then
    die "that does not look like an Otari API key (it starts with otk_ and has no spaces or quotes)"
  fi
  if [[ -n $otari && $otari_mode == off ]]; then
    warn "OTARI_MODE is off, so the Otari key is not used; remove that line from $ENV_FILE to use it"
  fi

  if token="$(env_get TELEGRAM_BOT_TOKEN)"; then
    token_saved=1
  else
    token="$(from_env TELEGRAM_BOT_TOKEN)"
  fi
  if [[ -z $token && -n ${TELEGRAM_BOT_TOKEN:-} ]]; then
    say "  TELEGRAM_BOT_TOKEN in the environment is not used: it may belong to another bot on this account."
    say "  Give HoldUp's own token as HOLDUP_TELEGRAM_BOT_TOKEN, or type it below."
  fi
  if [[ -z $token ]] && ((interactive)); then
    say "  The Telegram bot token comes from @BotFather (digits, a colon, then letters). It is not shown as you type."
    token="$(ask_secret '  Telegram bot token')"
  fi
  [[ -n $token ]] || die "the bot token is needed: run setup in a terminal, or give it as HOLDUP_TELEGRAM_BOT_TOKEN"
  valid_bot_token "$token" || die "that does not look like a Telegram bot token (digits, a colon, then letters, digits, _ or -)"

  api_base="$(env_get TELEGRAM_API_BASE_URL)" || api_base="$(from_env TELEGRAM_API_BASE_URL)"
  if [[ -n $api_base && ! $api_base =~ ^https?://[A-Za-z0-9] ]]; then
    die "TELEGRAM_API_BASE_URL must be an http(s) URL"
  fi
  say "  Checking the bot token with Telegram (getMe)..."
  telegram_get_me "$token" "${api_base:-https://api.telegram.org}" || rc=$?
  if ((rc == 0)); then
    say "  Telegram accepted the token: the bot is @$BOT_USERNAME"
    printf '%s\n' "$BOT_USERNAME" >"$HOLDUP_DATA_DIR/bot_username"
  elif ((rc == 1)); then
    if ((token_saved)); then
      die "Telegram rejected the bot token saved in $ENV_FILE. Get a valid one from @BotFather and edit that file."
    fi
    die "Telegram rejected this bot token; nothing was saved. Check it with @BotFather."
  else
    warn "could not check the token with Telegram (${TELEGRAM_PROBLEM:-no answer}); continuing"
  fi

  # Every user's NEAR account and key are derived from HOLDUP_SECRET: it must stay the one the
  # deployment was made with. A credentials file brought from elsewhere carries its own.
  local deployed_secret=""
  deployed_secret="$(py credentials-secret "$CREDS" 2>/dev/null)" || deployed_secret=""
  holdup_secret="$(env_get HOLDUP_SECRET)" || holdup_secret="${deployed_secret:-$(from_env HOLDUP_SECRET)}"
  [[ -n $holdup_secret ]] || holdup_secret="$(py secret)"
  if [[ -n $deployed_secret && $deployed_secret != "$holdup_secret" ]]; then
    warn "HOLDUP_SECRET in $ENV_FILE differs from the secret in $CREDS. HoldUp uses the one in holdup.env;"
    warn "users onboarded with the other one would not find their accounts. Make them equal unless that is intended."
  fi
  webhook_secret="$(env_get TELEGRAM_WEBHOOK_SECRET)" || webhook_secret="$(from_env TELEGRAM_WEBHOOK_SECRET)"
  [[ -n $webhook_secret ]] || webhook_secret="$(py secret)"
  mode="$(env_get TELEGRAM_MODE)" || mode="$(from_env TELEGRAM_MODE)"
  mode="${mode:-polling}"
  case $mode in polling | webhook | off) ;; *) die "TELEGRAM_MODE must be polling, webhook or off" ;; esac

  local name value
  {
    printf 'OTARI_API_KEY=%s\n' "$otari"
    printf 'OTARI_MODE=%s\n' "$otari_mode"
    printf 'TELEGRAM_BOT_TOKEN=%s\n' "$token"
    printf 'TELEGRAM_MODE=%s\n' "$mode"
    printf 'TELEGRAM_WEBHOOK_SECRET=%s\n' "$webhook_secret"
    printf 'HOLDUP_SECRET=%s\n' "$holdup_secret"
    printf 'TELEGRAM_API_BASE_URL=%s\n' "$api_base"
    for name in "${OPTIONAL_SETTINGS[@]}"; do
      value="$(from_env "$name")"
      printf '%s=%s\n' "$name" "$value"
    done
  } | py env-merge "$ENV_FILE" | sed 's/^/  /' || die "could not write $ENV_FILE"
  say "  $ENV_FILE is mode $(stat -c %a -- "$ENV_FILE"); values are never printed"
  if [[ "$(env_get HOLDUP_SECRET)" != "$holdup_secret" ]]; then
    die "HOLDUP_SECRET in $ENV_FILE could not be read back"
  fi
}

setup_ports() {
  step "Ports (127.0.0.1 only, from 47800-47999)"
  if supervisor_alive; then
    say "  HoldUp is running; keeping its ports"
    sed -n 's/^\([A-Z_]*PORT\)=.\(.*\).$/  \1=\2/p' "$PORTS_FILE" 2>/dev/null || true
    return
  fi
  local out
  out="$(py ports "$PORTS_FILE")" || die "could not choose ports"
  say "$out" | sed 's/^/  /'
}

sync_packages() {
  step "Python packages: uv sync --locked --no-dev with the system Python"
  (
    unset UV_PROJECT_ENVIRONMENT VIRTUAL_ENV
    cd "$REPO"
    "$UV" sync "${UV_FLAGS[@]}"
  ) || die "uv sync failed (see above). 'uv.lock needs to be updated' means this checkout is incomplete: git -C $REPO status"
}

cmd_setup() {
  refuse_root
  check_home create
  check_tools
  step "Directories"
  private_dir "$HOLDUP_SECRETS_DIR"
  private_dir "$HOLDUP_DATA_DIR"
  private_dir "$LOG_DIR"
  private_dir "$RUN_DIR"
  private_dir "$HOLDUP_DATA_DIR/cloudflared"
  say "  secrets  $HOLDUP_SECRETS_DIR (mode 700)"
  say "  data     $HOLDUP_DATA_DIR (mode 700: database, logs/, run/)"
  setup_settings
  setup_ports
  sync_packages
  step "Setup done"
  say "  Next: bash scripts/server.sh testnet, then bash scripts/server.sh start (install does both)."
}

# ----------------------------------------------------------------------------------------------
# testnet
# ----------------------------------------------------------------------------------------------

explain_testnet() {
  cat <<EOF

This step puts HoldUp on NEAR testnet, NEAR's public test network. It will:
  1. create a funding account and ask NEAR's public faucet for free test NEAR;
  2. create your root account ($1) with that test money;
  3. deploy the HoldVault contract, the HUSD demo token and the oracle and keeper accounts
     under it (holdup deploy --network testnet --profile hosted);
  4. check the deployment (verify-deploy) and show that forbidden actions are refused (prove);
  5. ask the faucet for a little more test NEAR for onboarding the first users.
Testnet NEAR is free test money with no value: no real money is involved at any point.
The keys are saved in $CREDS (mode 600) and never printed.
It takes a few minutes; the faucet requests are 20 seconds apart.

EOF
}

cmd_testnet() {
  local force=0 arg
  for arg in "$@"; do
    case $arg in
      --force-new) force=1 ;;
      *) die "unknown option for testnet: $arg" ;;
    esac
  done
  refuse_root
  check_home
  [[ -f $ENV_FILE ]] || die "run setup first: bash scripts/server.sh setup"
  find_uv || die "uv is not installed (or set HOLDUP_UV)"
  local state root="" default answer=""
  state="$(py credentials "$CREDS" status)"
  case $state in
    complete)
      if ((!force)); then
        root="$(py credentials "$CREDS" root_account)" || root="?"
        die "a testnet deployment already exists ($CREDS, root account $root). Nothing was changed." \
          "To make a new one: bash scripts/server.sh testnet --force-new (the old file is kept)"
      fi
      ;;
    invalid) die "$CREDS is not readable JSON; move it somewhere safe (it may hold keys) and run this again" ;;
    partial) root="$(py credentials "$CREDS" root_account)" || root="" ;;
  esac
  if ((force)); then
    root="" # a new root account, whatever the old file holds
  fi
  default="${root:-$(py root-name)}"
  explain_testnet "${root:-a new account such as $default}"
  printf 'Type yes to continue: ' >&2
  IFS= read -r answer || true
  [[ $answer == yes ]] || die "not confirmed; nothing was done"
  if ((force)) && [[ $state == complete || $state == partial ]]; then
    local old
    old="$CREDS.$(date -u +%Y%m%dT%H%M%SZ).old"
    mv -- "$CREDS" "$old"
    say "Moved the old credentials to $old (kept, not deleted)."
    root=""
  fi
  if [[ -n $root ]]; then
    say "Resuming the unfinished setup of $root from $CREDS."
  else
    printf 'Root account name [%s]: ' "$default" >&2
    IFS= read -r root || true
    root="${root:-$default}"
    py check-root-name -- "$root" ||
      die "$root is not a valid name: lowercase letters and digits, with - or _ between them, ending in" \
        ".testnet, at most 48 characters in all (the accounts created under it must fit in 64)"
  fi

  PROJECT_SECRET="$(env_get HOLDUP_SECRET)" || die "HOLDUP_SECRET is missing from $ENV_FILE; run setup"
  PROJECT_RPC="$(env_get NEAR_RPC_URL)" || PROJECT_RPC=""
  local resume="run 'bash scripts/server.sh testnet' again to continue where it stopped"
  step "1/5 Test money and the root account $root (NEAR faucet)"
  project python scripts/testnet_tools.py bootstrap "$root" || die "the account setup did not finish; $resume"
  step "2/5 Deploying the contracts (holdup deploy --network testnet --profile hosted)"
  project holdup deploy --network testnet --profile hosted --root "$root" ||
    die "the deployment did not finish; $resume." \
      "If it stops again because accounts under $root already exist, start over: bash scripts/server.sh testnet --force-new"
  chmod 600 -- "$CREDS"
  step "3/5 Checking the deployment (holdup verify-deploy)"
  project holdup verify-deploy --network testnet ||
    die "the deployment does not check out (see above); do not start HoldUp on it"
  step "4/5 Trying forbidden actions with the oracle, keeper and user keys (holdup prove)"
  project holdup prove --network testnet || warn "prove did not pass every check (see above)"
  step "5/5 Test NEAR for onboarding the first users"
  project python scripts/testnet_tools.py topup "$root" --min "$TREASURY_MIN_NEAR" ||
    warn "the faucet top-up did not finish; HoldUp asks the faucet again by itself when the treasury runs low"
  PROJECT_SECRET=""
  say ""
  say "Testnet deployment ready under $root; credentials in $CREDS (mode 600)."
}

# ----------------------------------------------------------------------------------------------
# start, run, stop
# ----------------------------------------------------------------------------------------------

state_get() { py state "$RUN_DIR/state.json" "$1" 2>/dev/null; }

wait_until_running() {
  local deadline=$((SECONDS + ${HOLDUP_START_TIMEOUT:-120})) status="" last=""
  while ((SECONDS < deadline)); do
    status="$(state_get status)" || status=""
    if [[ -n $status && $status != "$last" ]]; then
      say "  $status"
      last=$status
    fi
    case $status in
      running) break ;;
      failed) die "HoldUp could not start: $(state_get message || echo 'see the supervisor log')" ;;
    esac
    if [[ $(run_window) != live ]]; then
      tail -n 20 -- "$LOG_DIR/supervisor.log" 2>/dev/null | sed 's/^/  /' >&2 || true
      die "the supervisor in tmux session '$SESSION' ended; see: bash scripts/server.sh logs supervisor"
    fi
    sleep 0.3
  done
  if [[ $status != running ]]; then
    warn "HoldUp is not fully up after ${HOLDUP_START_TIMEOUT:-120} s (${status:-no status yet}: $(state_get message || true))."
    warn "It keeps trying in the background. Look at: bash scripts/server.sh status, and logs tunnel / logs app"
    return 1
  fi
  local url username
  url="$(state_get public_url)" || url="?"
  username="$(state_get app.health.telegram.username)" ||
    username="$(cat -- "$HOLDUP_DATA_DIR/bot_username" 2>/dev/null)" || username=""
  say ""
  say "HoldUp is running."
  say "  Public URL   $url"
  if [[ -n $username ]]; then
    say "  Telegram     https://t.me/$username (@$username)"
  fi
  say "  Logs         bash scripts/server.sh logs app -f   (or: tunnel, fixtures, supervisor)"
  say "  Status       bash scripts/server.sh status"
  say "  Session      $ATTACH   (detach with Ctrl-b d)"
  say "  Stop         bash scripts/server.sh stop"
}

cmd_start() {
  local network=testnet arg
  for arg in "$@"; do
    case $arg in
      --offline) network=offline ;;
      *) die "unknown option for start: $arg" ;;
    esac
  done
  refuse_root
  check_home
  [[ -f $ENV_FILE ]] || die "run setup first: bash scripts/server.sh setup"
  py env-check "$ENV_FILE" || die "fix $ENV_FILE (one KEY='value' per line), then start again"
  [[ -x $HOLDUP_CLOUDFLARED ]] || die "cloudflared is not at $HOLDUP_CLOUDFLARED"
  command -v tmux >/dev/null 2>&1 || die "tmux is not installed"
  find_uv || die "uv is not installed (or set HOLDUP_UV)"
  if [[ $network == testnet && $(py credentials "$CREDS" status) != complete ]]; then
    die "there is no testnet deployment yet. Run: bash scripts/server.sh testnet" \
      "(or: start --offline for a rehearsal without NEAR)"
  fi
  private_dir "$LOG_DIR"
  private_dir "$RUN_DIR"
  if supervisor_alive; then
    die "HoldUp is already running. To restart it: bash scripts/server.sh restart"
  fi
  if session_exists && ! session_is_ours; then
    die "there is a tmux session '$SESSION' (tmux -L $HOLDUP_TMUX_SOCKET) that this script did not start;" \
      "HoldUp leaves it alone. Rename it (tmux -L $HOLDUP_TMUX_SOCKET rename-session -t =$SESSION <name>)" \
      "or close it, then run start again"
  fi
  close_dead_window
  if [[ $(run_window) == live ]]; then
    die "tmux session '$SESSION' already has a window '$WINDOW' in which HoldUp does not run;" \
      "close it yourself ($ATTACH), then run start again"
  fi
  local fixed
  fixed="$(py secure-secrets "$HOLDUP_SECRETS_DIR")" || die "the secrets in $HOLDUP_SECRETS_DIR are not usable (see above)"
  if [[ -n $fixed ]]; then
    printf '%s\n' "$fixed" | sed 's/^/  /'
  fi
  if [[ $network == testnet ]]; then
    check_secret_matches
  fi
  py cleanup "$RUN_DIR" --timeout 40 || die "processes from an earlier run are still there; see bash scripts/server.sh status"
  rm -f -- "$RUN_DIR/state.json"
  printf '%s\n' "$network" >"$RUN_DIR/network"

  # The supervisor gets an environment of its own (paths and settings only; it reads the secrets
  # from holdup.env itself), whatever the environment of this shell or of the tmux server is.
  local name user
  user="$(id -un)"
  local -a env_args=(
    "HOME=$HOME" "PATH=$PATH" "LANG=${LANG:-C.UTF-8}" "USER=$user" "LOGNAME=$user"
    "HOLDUP_HOME=$HOLDUP_HOME" "HOLDUP_SECRETS_DIR=$HOLDUP_SECRETS_DIR" "HOLDUP_DATA_DIR=$HOLDUP_DATA_DIR"
    "HOLDUP_CLOUDFLARED=$HOLDUP_CLOUDFLARED" "HOLDUP_UV=$UV" "HOLDUP_NETWORK=$network"
  )
  for name in "${TUNING_VARS[@]}"; do
    if [[ -n ${!name:-} ]]; then
      env_args+=("$name=${!name}")
    fi
  done
  say "Starting HoldUp ($network) in tmux session '$SESSION'; waiting up to ${HOLDUP_START_TIMEOUT:-120} s for the public URL..."
  local -a supervisor=(env -i "${env_args[@]}" bash "$REPO/scripts/server.sh" run)
  if session_exists; then
    # Our own session, still open because of windows the owner opened in it.
    tm new-window -d -t "=$SESSION:" -n "$WINDOW" -c "$REPO" -- "${supervisor[@]}" ||
      die "tmux could not open the window '$WINDOW' in session '$SESSION'"
  else
    tm new-session -d -s "$SESSION" -n "$WINDOW" -c "$REPO" -x 160 -y 48 -- "${supervisor[@]}" ||
      die "tmux could not create the session"
    tm set-option -t "=$SESSION:" @holdup_runner yes >/dev/null 2>&1 || true
  fi
  # The window should close when the supervisor ends, whatever the tmux options say.
  tm set-option -w -t "=$SESSION:=$WINDOW" remain-on-exit off >/dev/null 2>&1 || true
  wait_until_running
}

# Every user's NEAR account is derived from HOLDUP_SECRET: refuse to start with another one than
# the deployment was made with. The values are compared here and never shown.
check_secret_matches() {
  local configured deployed
  configured="$(env_get HOLDUP_SECRET)" || configured=""
  deployed="$(py credentials-secret "$CREDS" 2>/dev/null)" || deployed=""
  if [[ -n $configured && -n $deployed && $configured != "$deployed" ]]; then
    die "HOLDUP_SECRET in $ENV_FILE is not the secret in $CREDS that the deployment was made with," \
      "so every user would get a new, empty account. Put the right one back in $ENV_FILE (a quote or" \
      "a comment in the wrong place changes it), then start again."
  fi
}

cmd_run() {
  refuse_root
  check_home
  find_uv || true
  export HOLDUP_HOME HOLDUP_SECRETS_DIR HOLDUP_DATA_DIR HOLDUP_CLOUDFLARED
  export HOLDUP_UV="$UV"
  if [[ -z ${HOLDUP_NETWORK:-} ]]; then
    HOLDUP_NETWORK="$(cat -- "$RUN_DIR/network" 2>/dev/null || echo testnet)"
  fi
  export HOLDUP_NETWORK
  cd "$REPO"
  exec python3 "$TOOLS" supervise
}

cmd_stop() {
  refuse_root
  check_home
  if [[ -d $RUN_DIR ]]; then
    if supervisor_alive; then
      say "Stopping HoldUp: its supervisor stops the app and the tunnels first (up to a minute)..."
    fi
    # Only processes recorded in run/ whose start time, user, command line and directory still
    # match are signalled: the supervisor first (it stops its children), then anything left of a
    # supervisor that was killed. Nothing is signalled through tmux.
    py cleanup "$RUN_DIR" --timeout 40 || warn "some processes could not be stopped; see bash scripts/server.sh status"
    if supervisor_alive; then
      warn "a supervisor still holds $RUN_DIR/supervisor.lock but has no valid record, so it was not" \
        "signalled; stop it in its window ($ATTACH, then Ctrl-c)"
    fi
    local busy
    busy="$(py ports-busy "$PORTS_FILE")" || warn "another program now listens on a HoldUp port: $busy"
  fi
  # The supervisor's window closes when it ends; give tmux a moment to notice.
  local deadline=$((SECONDS + 3))
  while [[ $(run_window) == live ]] && ((SECONDS < deadline)); do
    sleep 0.2
  done
  if session_exists && session_is_ours; then
    close_dead_window
  fi
  if [[ $(run_window) == live ]]; then
    warn "tmux session '$SESSION' has a window '$WINDOW' in which HoldUp does not run; it was left alone"
  fi
  say "HoldUp is stopped."
}

cmd_restart() {
  local network
  network="$(cat -- "$RUN_DIR/network" 2>/dev/null || echo testnet)"
  cmd_stop
  if [[ $network == offline ]]; then
    cmd_start --offline
  else
    cmd_start
  fi
}

# ----------------------------------------------------------------------------------------------
# looking after it
# ----------------------------------------------------------------------------------------------

cmd_status() {
  refuse_root
  check_home
  local network name
  network="$(cat -- "$RUN_DIR/network" 2>/dev/null || echo testnet)"
  say "HoldUp on $(hostname) (network: $network)"
  local pid panes
  pid="$(py supervisor-pid "$RUN_DIR" 2>/dev/null)" || pid=""
  panes="$(tm list-panes -t "=$SESSION:=$WINDOW" -F '#{pane_pid}' 2>/dev/null)" || panes=""
  if [[ -n $pid && $'\n'$panes$'\n' == *$'\n'$pid$'\n'* ]]; then
    say "Session      tmux session '$SESSION' runs HoldUp's supervisor ($ATTACH)"
  elif session_exists; then
    say "Session      tmux session '$SESSION' (tmux -L $HOLDUP_TMUX_SOCKET) exists, but HoldUp's supervisor does not run in it"
  else
    say "Session      no tmux session '$SESSION' (tmux -L $HOLDUP_TMUX_SOCKET)"
  fi
  py status "$RUN_DIR" "$PORTS_FILE"
  if [[ $network == testnet ]]; then
    case "$(py credentials "$CREDS" status)" in
      complete | partial)
        say ""
        say "Testnet accounts (scripts/testnet_tools.py status):"
        if find_uv; then
          PROJECT_SECRET=""
          PROJECT_RPC="$(env_get NEAR_RPC_URL)" || PROJECT_RPC=""
          project python scripts/testnet_tools.py status 2>&1 | sed 's/^/  /' || warn "could not read the testnet accounts"
        fi
        ;;
      *) say "Testnet      no deployment yet (bash scripts/server.sh testnet)" ;;
    esac
  fi
  for name in supervisor app; do
    if [[ -s $LOG_DIR/$name.log ]]; then
      say ""
      say "Last lines of logs/$name.log:"
      tail -n 5 -- "$LOG_DIR/$name.log" | sed 's/^/  /'
    fi
  done
}

cmd_url() {
  refuse_root
  check_home
  local url
  url="$(state_get public_url)" || url=""
  if [[ -n $url ]] && supervisor_alive; then
    say "$url"
  else
    die "HoldUp has no public URL right now (bash scripts/server.sh status)"
  fi
}

cmd_logs() {
  local name=app follow=0 arg file
  for arg in "$@"; do
    case $arg in
      -f | --follow) follow=1 ;;
      app | tunnel | fixtures | supervisor) name=$arg ;;
      *) die "usage: logs [app|tunnel|fixtures|supervisor] [-f]" ;;
    esac
  done
  refuse_root
  check_home
  file="$LOG_DIR/$name.log"
  [[ -f $file ]] || die "no $name log yet ($file)"
  if ((follow)); then
    exec tail -n 100 -F -- "$file"
  fi
  tail -n 100 -- "$file"
}

cmd_update() {
  refuse_root
  check_home
  command -v git >/dev/null 2>&1 || die "git is not installed"
  find_uv || die "uv is not installed (or set HOLDUP_UV)"
  local branch before after was_running=0
  if supervisor_alive; then
    was_running=1
  fi
  branch="$(git -C "$REPO" symbolic-ref --quiet --short HEAD)" ||
    die "the checkout is not on a branch (detached HEAD); check one out first"
  git -C "$REPO" diff --quiet HEAD -- || die "the checkout has local changes; commit or undo them first (git -C $REPO status)"
  before="$(git -C "$REPO" rev-parse HEAD)"
  step "git pull --ff-only origin $branch"
  git -C "$REPO" pull --ff-only origin "$branch" || die "git pull failed (see above); nothing else was changed"
  after="$(git -C "$REPO" rev-parse HEAD)"
  sync_packages
  if [[ $before == "$after" ]]; then
    say "Already up to date (${after:0:12})."
  elif ((was_running)); then
    step "Restarting with ${after:0:12}"
    cmd_restart
  else
    say "Updated to ${after:0:12}. HoldUp was not running; start it with: bash scripts/server.sh start"
  fi
}

cmd_topup() {
  local amount=${1:-$TREASURY_MIN_NEAR}
  [[ $amount =~ ^[0-9]+([.][0-9]+)?$ ]] || die "usage: topup [NEAR], for example: topup 10"
  refuse_root
  check_home
  find_uv || die "uv is not installed (or set HOLDUP_UV)"
  local root
  root="$(py credentials "$CREDS" root_account)" ||
    die "no testnet account in $CREDS yet; run: bash scripts/server.sh testnet"
  PROJECT_SECRET=""
  PROJECT_RPC="$(env_get NEAR_RPC_URL)" || PROJECT_RPC=""
  say "Asking the testnet faucet until the treasury $root holds $amount NEAR (test money)..."
  project python scripts/testnet_tools.py topup "$root" --min "$amount"
}

cmd_backup() {
  refuse_root
  check_home
  export HOLDUP_HOME HOLDUP_SECRETS_DIR HOLDUP_DATA_DIR
  exec bash "$REPO/deploy/backup.sh" "$@"
}

cmd_install() {
  local offline=0 arg
  for arg in "$@"; do
    case $arg in
      --offline) offline=1 ;;
      *) die "unknown option for install: $arg" ;;
    esac
  done
  cmd_setup
  if ((offline)); then
    step "Offline rehearsal: no testnet step, no NEAR credentials (in-memory vault)"
  elif [[ $(py credentials "$CREDS" status) == complete ]]; then
    step "Testnet deployment found in $CREDS; skipping the testnet step"
  else
    step "NEAR testnet"
    cmd_testnet
  fi
  local wanted=testnet running_network
  if ((offline)); then
    wanted=offline
  fi
  running_network="$(cat -- "$RUN_DIR/network" 2>/dev/null || echo testnet)"
  if supervisor_alive && [[ $running_network == "$wanted" ]]; then
    step "HoldUp is already running; 'bash scripts/server.sh restart' applies new settings"
  else
    if supervisor_alive; then
      step "HoldUp runs in $running_network mode; restarting it in $wanted mode"
      cmd_stop
    else
      step "Starting"
    fi
    if ((offline)); then
      cmd_start --offline
    else
      cmd_start
    fi
  fi
  step "Status"
  cmd_status
}

main() {
  configure
  local command=${1:-help}
  shift || true
  case $command in
    install) cmd_install "$@" ;;
    setup) cmd_setup "$@" ;;
    testnet) cmd_testnet "$@" ;;
    start) cmd_start "$@" ;;
    run) cmd_run "$@" ;;
    stop) cmd_stop "$@" ;;
    restart) cmd_restart "$@" ;;
    status) cmd_status "$@" ;;
    url) cmd_url "$@" ;;
    logs) cmd_logs "$@" ;;
    update) cmd_update "$@" ;;
    topup) cmd_topup "$@" ;;
    backup) cmd_backup "$@" ;;
    help | -h | --help) usage ;;
    *)
      usage >&2
      exit 2
      ;;
  esac
}

# Everything above is only definitions, so bash has read the whole file before main runs:
# `update` can replace this file on disk without confusing the copy that is running.
main "$@"
exit
