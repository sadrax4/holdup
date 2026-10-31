# Deploying HoldUp on one server

One Ubuntu 24.04 server runs the whole service with Docker Compose:

| Container | What it does |
|---|---|
| `holdup` | `python -m holdup serve`: web pages, the Telegram webhook (`/tg/<secret>`), the MCP server (`/mcp`), the guardrail endpoint (`/validate`), `/health`, and the keeper that releases due holds. Port 8000, reachable only inside the Compose network. |
| `caddy` | Caddy 2.11.7 on ports 80 and 443: certificates for two host names and a reverse proxy to `holdup:8000`. |

Where things live:

```
~/work/holdup/                 this repository
~/work/holdup/deploy/.env      symlink to ../../secrets/holdup.env (created by setup-ubuntu.sh)
~/work/secrets/holdup.env      every setting and secret (mode 600, outside the repository)
~/work/backups/                archives written by deploy/backup.sh
```

Docker volumes: `holdup-data` (the SQLite database, `/data/holdup.sqlite3`), `holdup-caddy-data`
(certificates and ACME account keys) and `holdup-caddy-config`.

Files in this directory:

| File | Purpose |
|---|---|
| `Dockerfile` | python:3.12-slim image; uv from PyPI (pinned version and hashes, `requirements-uv.txt`); `uv sync --frozen --no-dev`; runs as an unprivileged user with a health check on `/health` |
| `Dockerfile.dockerignore` | sends only what the image needs to the builder; `.env` files and `.holdup/` never reach it |
| `docker-compose.yml` | the two services, volumes and log rotation |
| `Caddyfile` | HTTPS for `HOLDUP_HOST` (the app) and `FIXTURE_HOST` (only the `/fixtures/` training pages) |
| `holdup.env.example` | every setting, one line of explanation each |
| `setup-ubuntu.sh` | one-time server preparation (Docker, firewall, `~/work/secrets`) |
| `credentials_to_env.py` | copies the output of `holdup deploy` into `holdup.env` |
| `set-webhook.sh` | registers the Telegram webhook with a secret token |
| `backup.sh` | online backup of the database plus the secrets file |

## 1. Host names without buying a domain

[sslip.io](https://sslip.io/) (run together with nip.io) answers DNS queries for any name that
contains an IP address with that address. Write the server's IPv4 address with dashes and put any
label in front. For a server at 203.0.113.7:

| Setting | Value |
|---|---|
| `HOLDUP_HOST` | `holdup.203-0-113-7.sslip.io` |
| `FIXTURE_HOST` | `fixtures.203-0-113-7.sslip.io` |

`getent hosts holdup.203-0-113-7.sslip.io` prints `203.0.113.7` from any machine. There is nothing
to register: Caddy obtains a certificate for each name with the HTTP-01 or TLS-ALPN-01 challenge,
which only needs ports 80 and 443 reachable from the internet. `setup-ubuntu.sh` fills in both names
when the server has a public IPv4 address.

Good to know:

- Certificates for sslip.io names count against one Let's Encrypt limit shared by all its users
  (Let's Encrypt has raised it to 250,000, according to the service's home page). If issuance is
  rate-limited, Caddy retries and falls back to ZeroSSL on its own; you can also use the same
  service under its other domain, e.g. `holdup.203-0-113-7.nip.io`.
- Why two names: the `/fixtures/` pages imitate a phishing "verify" page and supplier pages
  (fictional brands, labelled as training fixtures). Blocklists and abuse desks act on host names,
  so these pages get a host of their own. The bot's host only redirects `/fixtures/` there and never
  serves them; the fixture host serves nothing else (other paths and methods get 404) and its
  responses carry `noindex` and a policy that blocks form submissions and framing.
- Both names still share the server's IP and the sslip.io operators, who act on abuse reports
  (abuse@nip.io); their page does not say whether they act on one name or on every name for the IP.
  For complete separation, use a `FIXTURE_HOST` under a different domain; nothing else changes.
- With your own domain later: point A records at the server, change the two names in
  `holdup.env`, run `docker compose up -d` and set the webhook again (section 6).

## 2. Before you start

- A server with Ubuntu 24.04, a public IPv4 address and SSH access as a user with sudo (or root).
  If the provider has its own firewall or security group, allow inbound TCP 22, 80, 443 and
  UDP 443 there as well.
- A Telegram bot token: talk to [@BotFather](https://t.me/BotFather) and send `/newbot`.
- A testnet deployment of the contracts, done once with `holdup deploy` (section 4); you can do it
  before or after preparing the server.
- Optional: an Otari API key. Without one, the AI steps replay recorded responses.

## 3. Prepare the server (once)

```bash
ssh ubuntu@203.0.113.7                         # your user and address
sudo apt-get update && sudo apt-get install -y git
mkdir -p ~/work
git clone https://github.com/<owner>/holdup.git ~/work/holdup
sudo bash ~/work/holdup/deploy/setup-ubuntu.sh
exit                                           # log out so the docker group applies
```

`setup-ubuntu.sh` uses only the Ubuntu archive (no third-party repositories or install scripts) and
can be run again at any time:

- installs `docker.io`, `docker-compose-v2`, `docker-buildx`, `ufw`, `git`, `curl` and `python3`
  (enabling the `universe` component if needed), starts Docker and adds your user to the `docker`
  group; if Docker's own `docker-ce` packages are already installed it keeps them;
- allows TCP 22 (plus any other port that sshd, the `ssh.socket` unit or an open SSH session uses),
  80, 443 and UDP 443 (HTTP/3) in ufw, denies all other inbound traffic and enables ufw;
- creates `~/work/secrets` (mode 700) and, if it does not exist yet, `~/work/secrets/holdup.env`
  (mode 600) from `holdup.env.example` with a random `HOLDUP_SECRET` and `TELEGRAM_WEBHOOK_SECRET`;
- links `deploy/.env` to `../../secrets/holdup.env`, so that Compose reads the host names from it;
- prints the next steps.

Options: `--dry-run` prints what it would do without changing anything; `--no-firewall` leaves the
firewall to you. To keep one more SSH port open, pass it:
`sudo SSH_PORT=2222 bash ~/work/holdup/deploy/setup-ubuntu.sh`.

Docker's published ports bypass ufw. That is fine here because only Caddy publishes ports (80 and
443); keep it that way (see Troubleshooting if you want to reach port 8000 for debugging).

## 4. Configure

Log in again and edit the settings file:

```bash
nano ~/work/secrets/holdup.env
```

| Variable | What to put there |
|---|---|
| `HOLDUP_HOST`, `FIXTURE_HOST` | Two different host names for this server (section 1). Prefilled when possible. |
| `ACME_EMAIL` | Your email address, for the certificate authority account. |
| `TELEGRAM_BOT_TOKEN` | The token from @BotFather. |
| `TELEGRAM_WEBHOOK_SECRET` | Generated by the setup script. Letters, digits, `_` and `-` only. |
| `HOLDUP_SECRET` | Generated by the setup script. Every user's NEAR account and key is derived from it: back it up and never change it once people use the bot. |
| `HOLDUP_NETWORK` and the NEAR settings | From `holdup deploy`, see below. `testnet` is the setting for the hosted bot; `offline` runs the in-memory demo world, which starts from scratch after every restart. |
| `OTARI_API_KEY` | Optional. With a key the checks call the Otari gateway live; without one they replay recorded responses (`OTARI_MODE` overrides this). |

Every variable has a one-line explanation in `holdup.env.example`. You do not need to set
`PUBLIC_BASE_URL`: `docker-compose.yml` sets it to `https://$HOLDUP_HOST` (and `FIXTURE_BASE_URL`
to `https://$FIXTURE_HOST`), so links, the MCP URL and the guardrail URL given to Otari always match
the certificates.

### 4a. NEAR settings from a deployment on your workstation

On your workstation, in a checkout of the repository (the main README explains which root account
and key `holdup deploy` needs):

```bash
uv run holdup deploy --network testnet --profile hosted     # writes .holdup/credentials.json
uv run holdup verify-deploy --network testnet
scp .holdup/credentials.json ubuntu@203.0.113.7:~/work/secrets/credentials.json
```

On the server:

```bash
chmod 600 ~/work/secrets/credentials.json
python3 ~/work/holdup/deploy/credentials_to_env.py ~/work/secrets/credentials.json ~/work/secrets/holdup.env
```

The script sets `HOLDUP_NETWORK`, `NEAR_RPC_URL`, the account names, the treasury, oracle and keeper
keys and the token accounts. It keeps an existing `HOLDUP_SECRET`, prints variable names but never
values, and leaves the file at mode 600. Afterwards keep `credentials.json` in `~/work/secrets` or
delete it.

### 4b. Or deploy the contracts from the server

Put `HOLDUP_ROOT_ACCOUNT` and `HOLDUP_TREASURY_KEY` (the root account and its full-access key) in
`holdup.env`, leave `HOLDUP_NETWORK` empty, then:

```bash
cd ~/work/holdup/deploy
docker compose build holdup
docker compose run --rm --no-deps holdup python -m holdup deploy --network testnet --profile hosted
docker compose run --rm --no-deps holdup python -m holdup verify-deploy --network testnet
```

Inside the container `.holdup/` points at the data volume, so the credentials file lands in
`/data/credentials.json`, where the app reads it by default (values in `holdup.env` still win).
The oracle and keeper keys then live in the `holdup-data` volume; `backup.sh` includes that file.

## 5. Start

```bash
cd ~/work/holdup/deploy
docker compose up -d --build
docker compose ps
```

The first build takes a few minutes. Caddy requests both certificates right after it starts;
follow it with `docker compose logs -f caddy`. Then check both hosts:

```bash
curl -fsS https://holdup.203-0-113-7.sslip.io/health
curl -sS -o /dev/null -w '%{http_code}\n' https://fixtures.203-0-113-7.sslip.io/health   # 404: this host serves only /fixtures/
```

After any change to `holdup.env`, run `docker compose up -d` again; Compose recreates the
containers whose settings changed.

## 6. Connect Telegram

```bash
~/work/holdup/deploy/set-webhook.sh
```

The script reads `holdup.env`, checks that `https://$HOLDUP_HOST/health` answers, calls
[setWebhook](https://core.telegram.org/bots/api#setwebhook) with
`url=https://$HOLDUP_HOST/tg/$TELEGRAM_WEBHOOK_SECRET` and `secret_token=$TELEGRAM_WEBHOOK_SECRET`,
and prints `getWebhookInfo` with the secret masked. Telegram then posts every update to that URL and
sends the secret in the `X-Telegram-Bot-Api-Secret-Token` header. The bot token is passed to curl on
standard input, so it does not show up in the process list.

The same call by hand:

```bash
curl -sS "https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/setWebhook" \
  --data-urlencode "url=https://<HOLDUP_HOST>/tg/<TELEGRAM_WEBHOOK_SECRET>" \
  --data-urlencode "secret_token=<TELEGRAM_WEBHOOK_SECRET>"
```

Telegram accepts webhooks only over HTTPS on ports 443, 80, 88 or 8443, and a `secret_token` of
1-256 characters from `A-Z`, `a-z`, `0-9`, `_` and `-`. Open the bot in Telegram and send `/start`.

Other modes: `set-webhook.sh --info` shows the status, including Telegram's last delivery error;
`--drop-pending` discards updates queued while no webhook worked; `--delete` removes the webhook,
for example to run the bot somewhere else.

## 7. Update

```bash
cd ~/work/holdup && git pull
cd deploy && docker compose up -d --build
docker compose up -d --no-deps --force-recreate caddy   # only when deploy/Caddyfile changed
docker image prune -f                                    # optional: drop the previous image
```

`up -d --build` rebuilds the image and replaces the `holdup` container when anything changed; the
volumes keep the database and the certificates. Caddy needs a new container because Compose mounts
the single file `deploy/Caddyfile`, and `git pull` replaces files instead of editing them, so a
running container keeps the old copy. To check a changed Caddyfile first:

```bash
docker compose run --rm --no-deps caddy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
```

## 8. Logs

```bash
cd ~/work/holdup/deploy
docker compose ps                      # state and health of both containers
docker compose logs -f holdup          # the app; add --since 1h or --tail 200
docker compose logs -f caddy           # certificates and proxy errors
docker compose exec holdup python -m holdup verify-deploy    # code hash and key checks on testnet
```

Logs rotate at 10 MB, five files per container (`x-logging` in `docker-compose.yml`). Caddy's
access log is off because webhook URLs contain the secret; request lines in the app's own log can
contain it as well, so treat the logs as private.

## 9. Backups

| What | Where | Why it matters |
|---|---|---|
| `HOLDUP_SECRET` | `holdup.env` | Every user's account and key is derived from it; without it nobody can act for existing users. |
| NEAR keys | `holdup.env` (or `/data/credentials.json` after 4b) | Treasury, oracle and keeper. |
| Database | `holdup-data` volume | History, evidence documents (their hashes are on-chain), guardian invites. Losing it loses history, not money. |
| Certificates | `holdup-caddy-data` volume | Not needed: Caddy obtains new ones. |

```bash
~/work/holdup/deploy/backup.sh
# -> ~/work/backups/holdup-20261031T120000Z.tar.gz (mode 600)
```

It copies the database with SQLite's online backup API while the app keeps running, adds
`holdup.env` (and `/data/credentials.json` if present) and writes a mode 600 archive. The archive
contains secrets: copy it to encrypted storage off the server, for example
`scp 'ubuntu@203.0.113.7:work/backups/holdup-*.tar.gz' .`

Nightly, keeping two weeks (`crontab -e`):

```
17 3 * * * $HOME/work/holdup/deploy/backup.sh >/dev/null && find $HOME/work/backups -name 'holdup-*.tar.gz' -mtime +14 -delete
```

Restore, on the same server or on a new one prepared with sections 3 and 4:

```bash
restore="$(mktemp -d)"
tar -C "$restore" -xzf holdup-20261031T120000Z.tar.gz
install -m 600 "$restore/holdup.env" ~/work/secrets/holdup.env   # new server: then fix the host names
cd ~/work/holdup/deploy
docker compose up -d --build         # creates the volumes on a new server
docker compose stop holdup
docker run --rm --user 0 -v holdup-data:/data -v "$restore":/restore:ro holdup:local sh -c '
  for f in holdup.sqlite3 credentials.json; do
    if [ -f "/restore/$f" ]; then
      cp "/restore/$f" "/data/$f" && chown 10001:10001 "/data/$f" && chmod 600 "/data/$f"
    fi
  done
  rm -f /data/holdup.sqlite3-wal /data/holdup.sqlite3-shm'
docker compose start holdup
rm -rf "$restore"
```

On a new server, run `docker compose up -d` after fixing the host names and set the webhook again.

## 10. Troubleshooting

- **`required variable HOLDUP_HOST is missing a value`** (or `FIXTURE_HOST`, `ACME_EMAIL`): the value
  is empty, or `deploy/.env` is not the symlink. `ls -l ~/work/holdup/deploy/.env` should show
  `-> ../../secrets/holdup.env`; create it with
  `ln -s ../../secrets/holdup.env ~/work/holdup/deploy/.env`.
- **`permission denied while trying to connect to the Docker daemon socket`**: log out and in again
  after the setup script (docker group), or use `sudo`.
- **No certificate, or the browser shows a TLS error**: `getent hosts <HOLDUP_HOST>` must print this
  server's address, and the provider's firewall must allow TCP 80 and 443. Look at
  `docker compose logs caddy | grep -iE 'acme|challenge|error'`. Never delete the
  `holdup-caddy-data` volume: repeated issuance runs into Let's Encrypt's rate limits.
- **502 Bad Gateway**: Caddy cannot reach the app. `docker compose ps` and
  `docker compose logs --tail 100 holdup` show why, typically a missing or malformed setting.
- **The bot does not answer**: `deploy/set-webhook.sh --info` shows Telegram's `last_error_message`.
  A 401, 403 or 404 from the webhook usually means the secret in the URL no longer matches
  `TELEGRAM_WEBHOOK_SECRET`: run `docker compose up -d`, then `set-webhook.sh` again. SSL or connection
  errors point to the certificate item above.
- **`bind: address already in use` on port 80 or 443**: another web server is running. Find it with
  `sudo ss -ltnp '( sport = :80 or sport = :443 )'` and stop it, e.g.
  `sudo systemctl disable --now apache2` or `nginx`.
- **The build fails at `uv sync --frozen`**: `uv.lock` does not match `pyproject.toml`. Run `uv lock`
  in your checkout, commit, pull and build again.
- **Caddy logs `failed to sufficiently increase receive buffer size`**: harmless; it concerns HTTP/3
  performance. Adding `cap_add: [NET_ADMIN]` to the caddy service silences it.
- **The disk fills up**: `docker system df`, then `docker image prune -f`; old archives collect in
  `~/work/backups`.
- **You need port 8000 from the server itself**: publish it on localhost only
  (`ports: ["127.0.0.1:8000:8000"]` on the holdup service). A plain `8000:8000` is open to the
  internet, because Docker's published ports bypass ufw.

## Continuous integration

`.github/workflows/ci.yml` runs on every push and pull request:

- **Python tests**: uv 0.11.32 and Python 3.12, `uv sync --locked` (which also fails when `uv.lock`
  is out of date), `pytest -q` (sandbox, testnet and live Otari tests are skipped by the project's
  pytest settings).
- **Rust contracts**: the toolchain pinned in `contracts/rust-toolchain.toml` with the
  `wasm32-unknown-unknown` target, `cargo test`, then `scripts/build_contracts.sh --plain`, which
  builds both contracts and checks them against nearcore's rules.
- **Deployment files and Docker image**: `bash -n` on the scripts, `docker compose config` with the
  example settings, a build of `deploy/Dockerfile`, and a start in the in-memory demo mode that must
  reach a healthy state.

Every action is pinned to a full commit SHA; the comment next to it names the release.

## References

- Caddy automatic HTTPS: https://caddyserver.com/docs/automatic-https
- Caddy Docker image (volumes, ports, reloads): https://hub.docker.com/_/caddy
- Telegram `setWebhook`: https://core.telegram.org/bots/api#setwebhook
- sslip.io and nip.io: https://sslip.io/
- Compose `env_file` and variable interpolation: https://docs.docker.com/reference/compose-file/services/#env_file
  and https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation/
- Dockerfile-specific ignore files: https://docs.docker.com/build/concepts/context/#dockerignore-files
- Docker and ufw (published ports bypass ufw): https://docs.docker.com/engine/network/packet-filtering-firewalls/
- uv in Docker: https://docs.astral.sh/uv/guides/integration/docker/
