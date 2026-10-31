#!/usr/bin/env bash
# Point the Telegram bot at this server (setWebhook with a secret_token), then show the status.
#
#   deploy/set-webhook.sh                 set https://$HOLDUP_HOST/tg/<secret> and show getWebhookInfo
#   deploy/set-webhook.sh --drop-pending  the same, and drop updates that queued up meanwhile
#   deploy/set-webhook.sh --info          only show getWebhookInfo
#   deploy/set-webhook.sh --delete        remove the webhook (to run the bot somewhere else)
#
# Reads TELEGRAM_BOT_TOKEN, TELEGRAM_WEBHOOK_SECRET and HOLDUP_HOST from ~/work/secrets/holdup.env
# (or ENV_FILE=/path/to/file). The bot token reaches curl on stdin, not on the command line, and
# the webhook secret is masked in everything this prints.
set -euo pipefail

ENV_FILE="${ENV_FILE:-$HOME/work/secrets/holdup.env}"
mode=set
drop=false
for arg in "$@"; do
  case "$arg" in
    --drop-pending) drop=true ;;
    --info) mode=info ;;
    --delete) mode=delete ;;
    -h | --help)
      sed -n '2,11p' "$0"
      exit 0
      ;;
    *)
      echo "unknown option: $arg (try --help)" >&2
      exit 2
      ;;
  esac
done

die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

[[ -r $ENV_FILE ]] || die "cannot read $ENV_FILE (set ENV_FILE=... to use another file)"
command -v curl >/dev/null 2>&1 || die "curl is not installed"
command -v python3 >/dev/null 2>&1 || die "python3 is not installed"

# The last KEY=value line for a key, read the way Docker Compose does, without sourcing the file.
env_value() {
  local key=$1 line value=""
  while IFS= read -r line || [[ -n $line ]]; do
    if [[ $line =~ ^[[:space:]]*(export[[:space:]]+)?${key}[[:space:]]*=(.*)$ ]]; then
      value=${BASH_REMATCH[2]}
    fi
  done <"$ENV_FILE"
  value="${value#"${value%%[![:space:]]*}"}"
  if [[ $value =~ ^\"(.*)\"[[:space:]]*$ || $value =~ ^\'(.*)\'[[:space:]]*$ ]]; then
    value=${BASH_REMATCH[1]}
  else
    value="${value%%[[:space:]]#*}"
    value="${value%"${value##*[![:space:]]}"}"
  fi
  printf '%s' "$value"
}

TOKEN="$(env_value TELEGRAM_BOT_TOKEN)"
SECRET="$(env_value TELEGRAM_WEBHOOK_SECRET)"
HOST="$(env_value HOLDUP_HOST)"

[[ $TOKEN =~ ^[0-9]+:[A-Za-z0-9_-]+$ ]] ||
  die "TELEGRAM_BOT_TOKEN in $ENV_FILE is empty or not a bot token (digits, a colon, then letters)"

# Calls a Bot API method with the given curl config lines (key = "value"), all passed on stdin.
call() {
  local method=$1
  shift
  {
    printf 'url = "https://api.telegram.org/bot%s/%s"\n' "$TOKEN" "$method"
    local line
    for line in "$@"; do
      printf '%s\n' "$line"
    done
  } | curl --silent --show-error --max-time 30 --config - || true
}

# Prints a Bot API response; exits 1 unless it says "ok". The webhook secret is masked.
report() {
  SECRET="$SECRET" python3 -c '
import datetime, json, os, sys
raw = sys.stdin.read()
secret = os.environ.get("SECRET", "")
def mask(text):
    return text.replace(secret, "***") if secret else text
try:
    reply = json.loads(raw)
except ValueError:
    print("Unexpected response from Telegram:", mask(raw.strip())[:500] or "(empty)")
    sys.exit(1)
if not reply.get("ok"):
    print("Telegram refused:", reply.get("error_code"), mask(str(reply.get("description", ""))))
    sys.exit(1)
result = reply.get("result")
if not isinstance(result, dict):
    print("ok:", mask(str(reply.get("description") or result)))
    sys.exit(0)
for key in ("url", "pending_update_count", "max_connections", "ip_address", "allowed_updates",
            "last_error_date", "last_error_message", "last_synchronization_error_date"):
    if key not in result:
        continue
    value = result[key]
    if key.endswith("_date"):
        value = datetime.datetime.fromtimestamp(value, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"{key:>32}: {mask(str(value))}")
'
}

case "$mode" in
  info)
    call getWebhookInfo | report
    ;;
  delete)
    call deleteWebhook "data = \"drop_pending_updates=$drop\"" | report
    ;;
  set)
    [[ $SECRET =~ ^[A-Za-z0-9_-]{1,256}$ ]] ||
      die "TELEGRAM_WEBHOOK_SECRET must be 1-256 characters from A-Z a-z 0-9 _ - (Telegram's rule)"
    [[ $HOST =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ ]] ||
      die "HOLDUP_HOST must be a bare host name such as holdup.203-0-113-7.sslip.io (no https://, no path)"

    if curl --silent --fail --max-time 15 --output /dev/null "https://$HOST/health"; then
      echo "https://$HOST/health answers; setting the webhook."
    else
      echo "warning: https://$HOST/health does not answer yet; Telegram will keep retrying." >&2
      echo "         Check 'docker compose ps' and 'docker compose logs caddy holdup' in deploy/." >&2
    fi

    # allowed_updates=[] means Telegram's default set of update types.
    call setWebhook \
      "data-urlencode = \"url=https://$HOST/tg/$SECRET\"" \
      "data-urlencode = \"secret_token=$SECRET\"" \
      "data-urlencode = \"allowed_updates=[]\"" \
      "data = \"drop_pending_updates=$drop\"" | report
    echo
    call getWebhookInfo | report
    ;;
esac
