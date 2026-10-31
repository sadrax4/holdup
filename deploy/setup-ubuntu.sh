#!/usr/bin/env bash
# Prepare a fresh Ubuntu 24.04 server for HoldUp (see deploy/README.md):
#   - Docker Engine, Compose v2 and Buildx from the Ubuntu archive (no third-party repositories
#     or install scripts), plus ufw and git;
#   - a firewall that allows SSH, HTTP and HTTPS (TCP 22, 80, 443 and UDP 443 for HTTP/3);
#   - ~/work/secrets (mode 700) with holdup.env (mode 600) created from holdup.env.example, and
#     deploy/.env linked to it so that Docker Compose can read the host names.
# Safe to run again: it only adds what is missing and never overwrites an existing holdup.env.
#
# Usage: sudo bash deploy/setup-ubuntu.sh [--dry-run] [--no-firewall]
#   --dry-run       print what would be done and change nothing (works without root)
#   --no-firewall   leave the firewall alone (for example when the provider's firewall is used)
# SSH stays reachable: port 22, the ports sshd and ssh.socket use and the port of open SSH sessions
# are allowed; add another with: sudo SSH_PORT=2222 bash deploy/setup-ubuntu.sh
set -euo pipefail

DRY_RUN=0
FIREWALL=1
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --no-firewall) FIREWALL=0 ;;
    -h | --help)
      sed -n '2,14p' "$0"
      exit 0
      ;;
    *)
      echo "unknown option: $arg (try --help)" >&2
      exit 2
      ;;
  esac
done

say() { printf '\n==> %s\n' "$*"; }
note() { printf '    %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}
run() {
  if [[ $DRY_RUN -eq 1 ]]; then
    printf '    [dry-run] %s\n' "$*"
  else
    "$@"
  fi
}

# ----------------------------------------------------------------------------------------------
# Where things go
# ----------------------------------------------------------------------------------------------

if [[ $EUID -ne 0 && $DRY_RUN -eq 0 ]]; then
  die "run this as root: sudo bash $0"
fi

os_id="$(. /etc/os-release && printf '%s' "${ID:-}")"
os_version="$(. /etc/os-release && printf '%s' "${VERSION_ID:-}")"
[[ $os_id == ubuntu ]] || die "this script is for Ubuntu; found '${os_id:-unknown}'"
[[ $os_version == 24.04 ]] || warn "written for Ubuntu 24.04, found $os_version; continuing"

TARGET_USER="${SUDO_USER:-$(id -un)}"
TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
[[ -n $TARGET_HOME && -d $TARGET_HOME ]] || die "cannot find the home directory of $TARGET_USER"
TARGET_GROUP="$(id -gn "$TARGET_USER")"

WORK="$TARGET_HOME/work"
SECRETS="$WORK/secrets"
ENV_FILE="$SECRETS/holdup.env"
DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$DEPLOY_DIR")"
EXAMPLE="$DEPLOY_DIR/holdup.env.example"

say "Setting up for user $TARGET_USER (home $TARGET_HOME)"
[[ $DRY_RUN -eq 1 ]] && note "dry run: nothing will be changed"

# ----------------------------------------------------------------------------------------------
# Packages from the Ubuntu archive
# ----------------------------------------------------------------------------------------------

export DEBIAN_FRONTEND=noninteractive
APT=(apt-get -o DPkg::Lock::Timeout=300 -y)

has_candidate() {
  local policy
  policy="$(apt-cache policy "$1" 2>/dev/null || true)"
  [[ $policy =~ Candidate:\ [^\(] ]]
}

is_installed() {
  local status
  status="$(dpkg-query -W -f='${Status}' "$1" 2>/dev/null || true)"
  [[ $status == "install ok installed" ]]
}

say "Updating the package lists"
run "${APT[@]}" update

if ! has_candidate docker-compose-v2; then
  say "Enabling the 'universe' component (docker.io and docker-compose-v2 are there)"
  if ! command -v add-apt-repository >/dev/null 2>&1; then
    run "${APT[@]}" install software-properties-common
  fi
  run add-apt-repository -y universe
  run "${APT[@]}" update
fi

packages=(git curl ca-certificates openssl python3)
if is_installed docker-ce; then
  warn "docker-ce (Docker's own packages) is installed; keeping it instead of Ubuntu's docker.io"
else
  packages+=(docker.io docker-compose-v2 docker-buildx)
fi
[[ $FIREWALL -eq 1 ]] && packages+=(ufw)

say "Installing: ${packages[*]}"
run "${APT[@]}" install "${packages[@]}"

say "Starting Docker"
run systemctl enable --now docker

if [[ $TARGET_USER != root ]]; then
  if [[ " $(id -nG "$TARGET_USER") " == *" docker "* ]]; then
    note "$TARGET_USER is already in the docker group"
  else
    say "Adding $TARGET_USER to the docker group (log out and back in for it to apply)"
    run usermod -aG docker "$TARGET_USER"
  fi
fi

# ----------------------------------------------------------------------------------------------
# Firewall
# ----------------------------------------------------------------------------------------------

if [[ $FIREWALL -eq 1 ]]; then
  say "Configuring ufw: SSH, 80/tcp, 443/tcp and 443/udp in; everything else in is dropped"
  # SSH must stay reachable: port 22, sshd's configured ports, the ports of the ssh.socket unit
  # (Ubuntu 24.04 starts sshd through socket activation), the port of open SSH sessions, SSH_PORT.
  ssh_ports=(22)
  if command -v sshd >/dev/null 2>&1; then
    while read -r port; do
      ssh_ports+=("$port")
    done < <(sshd -T 2>/dev/null | awk '$1 == "port" { print $2 }' || true)
  fi
  while read -r port; do
    ssh_ports+=("$port")
  done < <(systemctl show -p Listen --value ssh.socket 2>/dev/null |
    grep -oE ':[0-9]+ \(Stream\)' | grep -oE '[0-9]+' || true)
  while read -r port; do
    ssh_ports+=("$port")
  done < <(ss -Htnp state established 2>/dev/null |
    awk '/"sshd/ { n = split($3, part, ":"); print part[n] }' || true)
  [[ -n ${SSH_PORT:-} ]] && ssh_ports+=("$SSH_PORT")

  declare -A seen=()
  for port in "${ssh_ports[@]}"; do
    [[ $port =~ ^[0-9]+$ && $port -ge 1 && $port -le 65535 ]] || die "not a port number: $port"
    [[ -n ${seen[$port]:-} ]] && continue
    seen[$port]=1
    note "SSH port $port stays open"
    run ufw allow "$port/tcp" comment 'SSH'
  done
  run ufw allow 80/tcp comment 'HTTP: ACME challenges and redirect to HTTPS'
  run ufw allow 443/tcp comment 'HTTPS'
  run ufw allow 443/udp comment 'HTTP/3'
  run ufw default deny incoming
  run ufw default allow outgoing
  run ufw --force enable
  note "Docker-published ports bypass ufw; docker-compose.yml publishes only 80 and 443 (Caddy)."
else
  say "Leaving the firewall alone (--no-firewall); open TCP 22, 80, 443 and UDP 443 yourself"
fi

# ----------------------------------------------------------------------------------------------
# ~/work/secrets/holdup.env and deploy/.env
# ----------------------------------------------------------------------------------------------

# The server's public IPv4 address, if the default route uses one (not behind NAT).
public_ipv4() {
  local ip
  ip="$(ip -4 route get 1.1.1.1 2>/dev/null |
    awk '{ for (i = 1; i < NF; i++) if ($i == "src") { print $(i + 1); exit } }')" || return 1
  [[ $ip =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || return 1
  case "$ip" in
    10.* | 127.* | 169.254.* | 192.168.* | 172.1[6-9].* | 172.2[0-9].* | 172.3[01].*) return 1 ;;
    100.6[4-9].* | 100.[7-9][0-9].* | 100.1[01][0-9].* | 100.12[0-7].*) return 1 ;;
  esac
  printf '%s\n' "$ip"
}

say "Creating $SECRETS (mode 700)"
if [[ ! -d $WORK ]]; then
  run install -d -m 755 -o "$TARGET_USER" -g "$TARGET_GROUP" "$WORK"
fi
run install -d -m 700 -o "$TARGET_USER" -g "$TARGET_GROUP" "$SECRETS"

ip_address="$(public_ipv4 || true)"
holdup_host=""
fixture_host=""
if [[ -n $ip_address ]]; then
  holdup_host="holdup.${ip_address//./-}.sslip.io"
  fixture_host="fixtures.${ip_address//./-}.sslip.io"
fi

if [[ -f $ENV_FILE ]]; then
  say "$ENV_FILE already exists; leaving its contents unchanged"
  run chown "$TARGET_USER:$TARGET_GROUP" "$ENV_FILE"
  run chmod 600 "$ENV_FILE"
elif [[ ! -f $EXAMPLE ]]; then
  warn "$EXAMPLE not found; create $ENV_FILE by hand (see deploy/README.md)"
else
  say "Creating $ENV_FILE (mode 600) with a fresh HOLDUP_SECRET and TELEGRAM_WEBHOOK_SECRET"
  [[ -n $holdup_host ]] && note "host names: $holdup_host and $fixture_host"
  if [[ $DRY_RUN -eq 0 ]]; then
    umask 077
    tmp="$(mktemp "$SECRETS/.holdup.env.XXXXXX")"
    sed -e "s/^HOLDUP_SECRET=\$/HOLDUP_SECRET=$(openssl rand -hex 32)/" \
      -e "s/^TELEGRAM_WEBHOOK_SECRET=\$/TELEGRAM_WEBHOOK_SECRET=$(openssl rand -hex 32)/" \
      -e "s/^HOLDUP_HOST=\$/HOLDUP_HOST=$holdup_host/" \
      -e "s/^FIXTURE_HOST=\$/FIXTURE_HOST=$fixture_host/" \
      "$EXAMPLE" >"$tmp"
    chown "$TARGET_USER:$TARGET_GROUP" "$tmp"
    chmod 600 "$tmp"
    mv "$tmp" "$ENV_FILE"
  fi
fi

if [[ $REPO != "$WORK/holdup" ]]; then
  warn "the repository is at $REPO; docker-compose.yml expects $WORK/holdup (it reads ../../secrets/holdup.env)"
fi
link="$DEPLOY_DIR/.env"
target="../../secrets/holdup.env"
if [[ -L $link ]]; then
  if [[ "$(readlink "$link")" == "$target" ]]; then
    note "deploy/.env already links to $target"
  else
    warn "deploy/.env links to $(readlink "$link"), not $target; leaving it"
  fi
elif [[ -e $link ]]; then
  warn "deploy/.env is a regular file, so Compose reads it instead of $ENV_FILE; move it away and run this again"
else
  say "Linking deploy/.env to $target (Compose reads the host names from it)"
  run ln -s "$target" "$link"
  run chown -h "$TARGET_USER:$TARGET_GROUP" "$link"
fi

# ----------------------------------------------------------------------------------------------
# Summary
# ----------------------------------------------------------------------------------------------

if [[ $DRY_RUN -eq 0 ]]; then
  say "Installed versions"
  docker --version || true
  docker compose version || warn "the Compose plugin is missing ('docker compose' does not work)"
  docker buildx version || warn "the Buildx plugin is missing; builds fall back to the legacy builder"
  if [[ $FIREWALL -eq 1 ]]; then
    ufw status verbose || true
  fi
fi

host_hint="${holdup_host:-holdup.<your-ip-with-dashes>.sslip.io}"
relogin="Log out and back in, so that $TARGET_USER can run docker without sudo."
[[ $TARGET_USER == root ]] && relogin="(Running as root: docker works without logging in again.)"
cat <<EOF

Next steps (details in deploy/README.md):

  1. $relogin
  2. Fill in $ENV_FILE:
       HOLDUP_HOST, FIXTURE_HOST   e.g. $host_hint
       ACME_EMAIL                  your email address, for the certificate authority
       TELEGRAM_BOT_TOKEN          from @BotFather
       NEAR settings               python3 $DEPLOY_DIR/credentials_to_env.py <credentials.json> $ENV_FILE
       OTARI_API_KEY               optional; without it recorded responses are replayed
  3. Start:            cd $DEPLOY_DIR && docker compose up -d --build
  4. Check:            curl -fsS https://$host_hint/health
  5. Telegram webhook: $DEPLOY_DIR/set-webhook.sh
  6. Back up HOLDUP_SECRET (in $ENV_FILE) somewhere off this server.
EOF
