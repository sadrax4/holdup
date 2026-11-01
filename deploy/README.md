# Running HoldUp on your own server

One command installs HoldUp on an Ubuntu server and keeps it running around the clock:

```bash
cd ~/work/holdup
bash scripts/server.sh install
```

There is nothing to open in a firewall and nothing to run as root. HoldUp listens on
`127.0.0.1` only, and the internet reaches it through two outbound
[Cloudflare quick tunnels](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/)
(free `*.trycloudflare.com` addresses, no account or domain needed). The Telegram bot gets its
messages by long polling, so Telegram never has to connect to the server either.

How it works inside, and why each choice was made: [docs/SERVER.md](../docs/SERVER.md).

## What runs

```
tmux session "holdup", in a tmux server of its own (tmux -L holdup)
└── supervisor            python3 scripts/server_tools.py supervise
    ├── cloudflared       quick tunnel: https://<random>.trycloudflare.com -> 127.0.0.1:<app port>
    ├── cloudflared       quick tunnel for the training pages (second hostname)
    └── uv run holdup serve --network testnet --host 127.0.0.1 --port <app port>
            web pages, Telegram bot (polling), near-intel MCP (/mcp), guardrail (/validate), keeper
```

The supervisor starts the two tunnels, reads their hostnames, starts `holdup serve` with
`PUBLIC_BASE_URL` and `FIXTURE_BASE_URL` set to them, and then checks every few seconds:

| What happens | What the supervisor does |
|---|---|
| `holdup serve` exits, or `/health` fails 3 times in a row | restarts the app; the tunnels and their hostnames stay |
| a tunnel exits, cannot connect, or stays disconnected for 2 minutes | starts a new one (new hostname), then restarts the app with the new URL |
| the public URL does not answer from the internet 3 times in a row (checked every 5 minutes) | replaces the main tunnel |
| the two tunnels are not both up for 90 s | runs the app without a public URL meanwhile: the Telegram bot keeps working, and the training pages are not public |
| Ctrl-c, `stop`, SIGTERM or SIGHUP | stops the app and the tunnels, then exits |

Restarts back off: 5 s, then 10, 20, 40 and at most 60 s between attempts, and back to 5 s once
things have been healthy for 10 minutes.

The second tunnel exists because the training pages (`/fixtures/...`) imitate scam pages, with
fictional brands, for the demo. Cloudflare's terms let it block content that looks like phishing
and suspend access without notice; on their own hostname, a report against those pages cannot
take down the bot's address, which Otari also needs for its calls to `/mcp` and `/validate`.

## Requirements

- Ubuntu (24.04 tested) and an ordinary user account; no sudo, Docker, systemd or crontab.
- bash 4.4 or newer, tmux, curl, Python 3.12 or newer as `python3`,
  [uv](https://docs.astral.sh/uv/getting-started/installation/), and the `cloudflared` binary at
  `~/work/bin/cloudflared` (or set `HOLDUP_CLOUDFLARED`). `ss` is used when installed.
- About 1 GB of free disk for the Python environment and the logs, and a few hundred MB of
  memory (the app itself uses about 100 MB).
- Outbound connections only:

  | To | Port | For |
  |---|---|---|
  | `api.trycloudflare.com` | TCP 443 | creating each quick tunnel |
  | Cloudflare's edge (`region1.v2.argotunnel.com`, `region2.v2.argotunnel.com`) | TCP 7844 | the tunnels themselves (HTTP/2) |
  | `api.telegram.org` | TCP 443 | the bot |
  | `test.rpc.fastnear.com`, the NEAR faucet (`docs-mcp-f18b.onrender.com`) | TCP 443 | NEAR testnet |
  | `api.otari.ai` | TCP 443 | the AI checks |
  | PyPI (`pypi.org`, `files.pythonhosted.org`) | TCP 443 | `uv sync`, during setup and update |

- A Telegram bot token from [@BotFather](https://t.me/BotFather) (`/newbot`), and an Otari API key
  (`otk_...`). Without an Otari key HoldUp checks payments with its deterministic rules only.

## The one command

`bash scripts/server.sh install` runs these steps; each can also be run on its own.

1. **setup** checks the tools and prints their versions, creates `~/work/secrets` and
   `~/work/holdup-data` (mode 700), asks for the Otari key and the bot token (typing is not shown;
   they are asked only when missing), checks the token with Telegram's `getMe` and shows the
   bot's name, generates `HOLDUP_SECRET` and `TELEGRAM_WEBHOOK_SECRET`, writes
   `~/work/secrets/holdup.env` (mode 600), picks three free ports, and runs
   `uv sync --locked --no-dev` with the system Python.
2. **testnet** explains what it is about to do and waits for you to type `yes`. It then asks for
   the name of your root account (press Enter for a suggestion such as
   `holdup-k3x9qa.testnet`), gets free test NEAR from NEAR's faucet, creates the account,
   deploys the contracts (`holdup deploy --network testnet --profile hosted`), checks them
   (`verify-deploy`), shows that the oracle, keeper and user keys cannot do anything else
   (`prove`) and asks the faucet for a little more test NEAR for the first users. Testnet NEAR is
   test money with no value; no real money is involved at any point. This step runs only when
   there is no deployment yet.
3. **start** checks that the secrets are still private (it sets `~/work/secrets` back to 700 and
   HoldUp's files there to 600 if they were changed), starts the tmux session, waits until both
   tunnels are connected and `/health` answers, and prints the public URL and the bot's Telegram
   link.
4. **status** shows what runs (see below).

`install` is safe to run again: it keeps every saved value, never redeploys over existing
credentials, and leaves a running HoldUp alone.

**Run the testnet step at least three days before judges try the bot**, and do not use
`--force-new` after that. The deployment creates the accounts the training samples pay: the scam
payees, and ten practice friends (`ravi-0` ... `ravi-9` under the root; each person always pays
the same one, so people trying the samples do not all pay one account). On testnet the rules read
their real age, and for 72 hours after the deployment an ordinary payment to a practice friend is
braked as a payment to a brand-new account. A new deployment starts that clock again.

The root account name has at most 48 characters, so that every account created under it fits
NEAR's 64. Payments use HUSD, the demo token every new user is funded with. Circle's testnet
USDC becomes a token of the vault only when `HOLDUP_USDC_TOKEN` names it in `holdup.env` before
the testnet step (the vault's token list is fixed when it is deployed); without it, "send 50 USDC"
is paid in HUSD, and the bot says so.

A rehearsal without NEAR: `bash scripts/server.sh install --offline` runs everything except the
testnet step, with the in-memory demo vault (the real bot, real tunnels). New payees wait 5 minutes
there, as on the hosted testnet bot (`holdup serve --offline --profile hosted`), so that a live AI
check (17-57 s) ends before the payment is due. A later `bash scripts/server.sh install` adds the
testnet step and switches the running HoldUp over.

Without a terminal (for scripts), give the keys as `HOLDUP_OTARI_API_KEY` and
`HOLDUP_TELEGRAM_BOT_TOKEN` in the environment; the testnet step reads its answers from standard
input (`yes`, then the name). `setup` reads a setting from the environment only under such a
`HOLDUP_` name (`HOLDUP_NEAR_RPC_URL`, `HOLDUP_TELEGRAM_API_BASE_URL`, ...), never the plain one:
another bot on this account may export its own `TELEGRAM_BOT_TOKEN`, and HoldUp must not take
over that bot. In a terminal it asks instead.

## Commands

| Command | What it does |
|---|---|
| `install [--offline]` | setup, the testnet step (first time only), start, status |
| `setup` | check the tools, ask for missing keys, write the settings, pick ports, `uv sync` |
| `testnet [--force-new]` | testnet accounts with free test money, deploy and check the contracts |
| `start [--offline]` | start HoldUp in the tmux session `holdup` and wait for the public URL |
| `stop` | stop HoldUp and everything it started (it signals the recorded supervisor, never a tmux window) |
| `restart` | `stop`, then `start` the same way again (after editing `holdup.env`) |
| `status` | session, processes, ports, public URLs, `/health`, testnet balances, last log lines |
| `url` | print the public URL |
| `logs [app\|tunnel\|fixtures\|supervisor] [-f]` | the last 100 lines of a log; `-f` follows it |
| `update` | `git pull --ff-only`, `uv sync`, and a restart if HoldUp was running |
| `topup [NEAR]` | ask the testnet faucet until the treasury holds that much test NEAR (default 5) |
| `backup [DIR]` | a mode 600 archive of HoldUp's secrets and database (see below) |

Every command is `bash scripts/server.sh <command>`, run from anywhere as the server's user.
`testnet --force-new` deploys a new set of contracts under a new root account, also after an
unfinished one; the old credentials file is renamed to `holdup-credentials.json.<time>.old`,
never deleted.

## Where things are

```
~/work/
├── holdup/                         this repository; scripts/server.sh uses its own checkout
├── bin/cloudflared                 the cloudflared binary (HOLDUP_CLOUDFLARED)
├── secrets/                        mode 700
│   ├── holdup.env                  settings and secrets, mode 600 (deploy/holdup.env.example)
│   └── holdup-credentials.json     NEAR accounts and keys from the testnet step, mode 600
├── holdup-data/                    mode 700
│   ├── holdup.sqlite3              the database: users, holds, evidence
│   ├── ports.env                   the three ports, all on 127.0.0.1
│   ├── logs/                       app.log, tunnel.log, fixtures.log, supervisor.log
│   ├── run/                        state.json, the process records, the supervisor lock
│   └── cloudflared/                cloudflared's own empty home and config file
└── backups/                        archives written by `backup`
```

`HOLDUP_HOME` (default `~/work`), `HOLDUP_SECRETS_DIR`, `HOLDUP_DATA_DIR` and `HOLDUP_CLOUDFLARED`
move these. Logs are rotated at 20 MB, keeping two older copies of each.

## Everyday use

```bash
bash scripts/server.sh status        # is everything up? which URL? what does /health say?
bash scripts/server.sh url           # the public URL only
bash scripts/server.sh logs app -f   # follow the app's log (Ctrl-c stops following, not HoldUp)
tmux -L holdup attach -t holdup      # the supervisor's own output; leave with Ctrl-b d
```

HoldUp's session lives in a tmux server of its own (`-L holdup`), so a plain `tmux ls` does not
list it, and HoldUp never touches the sessions there (`pdfbot`, `rc`). In HoldUp's window, Ctrl-c
stops HoldUp, as `stop` does. Windows you open in that session are yours: `stop` leaves them
open and running, and the next `start` adds HoldUp's window next to them.

**Changing a setting.** Edit `~/work/secrets/holdup.env` (one `KEY='value'` per line, no quotes
inside values; a comment may follow after a space, `KEY='value'  # note`) and run
`bash scripts/server.sh restart`. `setup` never overwrites a value that is
already there, so this is also how to replace the bot token or the Otari key.

**Updating.** `bash scripts/server.sh update` pulls the current branch with `--ff-only` (it stops
if the checkout has local changes), runs `uv sync --locked --no-dev` and restarts HoldUp if it was
running and anything changed.

**After a reboot.** Nothing starts by itself (no systemd units or crontab entries by design):
run `bash scripts/server.sh start`.

**Test money.** The treasury (the root account, whose full-access key is in
`holdup-credentials.json`) funds each new user and tops up the gas of users, the oracle and the
keeper when the network refuses one of their transactions for lack of balance. HoldUp asks the
faucet for more test NEAR by itself when the treasury runs low (not before the time a rate-limited
faucet names). `bash scripts/server.sh topup 10` does it by hand; `status` shows the balances.

## Backups

```bash
bash scripts/server.sh backup        # -> ~/work/backups/holdup-20261031T120000Z.tar.gz (mode 600)
```

The archive holds `holdup.env` (with `HOLDUP_SECRET`, from which every user's NEAR account and
key are derived), `holdup-credentials.json` (the treasury, oracle and keeper keys, plus older
credentials files) and a consistent copy of the database, made while HoldUp keeps running.
Nothing is uploaded anywhere; copy the archive to private storage yourself, for example
`scp 'server:work/backups/holdup-*.tar.gz' .`

To restore, on this server or a new one prepared with `setup`:

```bash
bash scripts/server.sh stop
bash scripts/server.sh status        # must say "Supervisor   not running"
# The database runs in WAL mode: after an unclean stop, a leftover -wal file would be replayed
# onto the restored copy and undo the restore. Remove it (and -shm) first.
rm -f ~/work/holdup-data/holdup.sqlite3-wal ~/work/holdup-data/holdup.sqlite3-shm
dir="$(mktemp -d)" && tar -C "$dir" -xzf holdup-20261031T120000Z.tar.gz
install -m 600 "$dir"/secrets/holdup.env "$dir"/secrets/holdup-credentials.json ~/work/secrets/
install -m 600 "$dir"/holdup.sqlite3 ~/work/holdup-data/
rm -rf "$dir"
bash scripts/server.sh start
```

## Limits of quick tunnels

- **The hostname is random and changes** whenever a tunnel starts again: on `restart`, `update`,
  a reboot, or when the supervisor replaces a tunnel. HoldUp then restarts with the new URL by
  itself, but links in older Telegram messages point to the old address. `url` prints the
  current one.
- **No uptime guarantee.** Quick tunnels are account-less, subject to Cloudflare's Online
  Services Terms, and Cloudflare may look into how they are used. A tunnel handles up to 200
  requests at once (more get HTTP 429) and does not carry Server-Sent Events (HoldUp's `/mcp`
  answers with plain JSON).
- **Public.** Anyone with the URL can open it; that is needed, because Otari calls `/mcp` and
  `/validate`. (Cloudflare's protected quick tunnels need a browser login, which Otari cannot do.)

## Rules this setup keeps

- Everything listens on `127.0.0.1` only, on ports from 47800-47999. Each port is checked free
  first (nothing listening on any address, and `127.0.0.1` can bind it), saved in `ports.env`,
  and checked again before every start; a port someone else took is replaced and saved.
- cloudflared always gets `--metrics 127.0.0.1:<port>`, `--no-autoupdate`, `--protocol http2`,
  `--loglevel info` and a config file of its own, so no `~/.cloudflared/config.yml` can redirect
  it.
- Nothing runs as root or with sudo, and the script refuses to use a `HOLDUP_HOME` that belongs
  to another user.
- Only processes this script started are ever stopped: each one is recorded with its start time,
  command line and working directory, and checked again right before any signal. `stop` signals
  the recorded supervisor itself; nothing is ever typed into a tmux window or sent through tmux,
  and no tmux session is ever killed. HoldUp's session lives in a tmux server of its own
  (`tmux -L holdup`), started with an environment that holds no secret. There is no `pkill` or
  `killall` anywhere.
- An answer on a local port counts only if the process HoldUp started holds that port: a tunnel's
  hostname is read only from a metrics socket that cloudflared itself holds, and `/health` only
  from the app's own socket. If another program takes the app's port while the app restarts, the
  tunnels are stopped at once, so no public request reaches it, and HoldUp moves to another port.
- Secrets live only in `~/work/secrets` (mode 600 files). They are never printed, never put on a
  command line (the bot token reaches curl on standard input), never written to a log, and only
  `holdup serve` receives them, as environment variables. `start` and the supervisor set the
  folder back to 700 and HoldUp's files to 600 if their modes changed, and refuse files that
  belong to another user. `start` also refuses a `HOLDUP_SECRET` that differs from the one the
  deployment was made with, which would give every user a new, empty account.

## Troubleshooting

- **`tunnel got no hostname`** in `logs supervisor`: the server cannot reach
  `api.trycloudflare.com` on port 443, or Cloudflare did not create the tunnel; `logs tunnel`
  shows its answer. The supervisor keeps retrying, at most once a minute.
- **`did not connect to Cloudflare`**: outbound TCP port 7844 is blocked. QUIC (UDP 7844) is not
  an option here: cloudflared's QUIC client binds a UDP socket on every interface, which this
  server's rules forbid, so `HOLDUP_TUNNEL_PROTOCOL` accepts only `http2`.
- **`status` shows `CONFLICT: another process uses this bot token`**: something else polls
  Telegram with the same token (the bot started on a laptop, for example). Stop it; one token
  serves one running bot.
- **`Telegram rejected the bot token`**: get the token again from @BotFather, put it into
  `holdup.env` and `restart`.
- **`HoldUp is not fully up after 120 s`**: `start` gave up waiting, the supervisor did not; see
  `status` and `logs supervisor`.
- **`there is a tmux session 'holdup' ... that this script did not start`**: someone created a
  session with that name on HoldUp's tmux server. HoldUp leaves it alone; rename it
  (`tmux -L holdup rename-session -t =holdup old-holdup`) or close it, then `start`.
- **The testnet step stops half-way**: run `bash scripts/server.sh testnet` again; it resumes with
  the same root account, and the deployment carries on from the transaction where it stopped (its
  keys and progress are in `holdup-credentials.json`). `holdup deploy` prints its budget before
  sending anything and refuses, sending nothing, when the root account cannot pay for what is left;
  the testnet step then says `nothing was sent; see the message above`.
  Only when it says an account under the root `already exists, but without this deployment's key`
  does `bash scripts/server.sh testnet --force-new` help: it starts over under a new root account
  (the old credentials file is kept as `.old`) and needs three more faucet drops.
- **`the faucet is rate limited`**: NEAR's faucet refuses for hours after a few requests (10 hours
  after six, on 2026-10-31). The message says when to try again; run `testnet` or `topup` again
  after that time, not before.
- **`uv sync failed`**: `uv.lock needs to be updated` means the checkout differs from what was
  committed (`git -C ~/work/holdup status`). `uv lock --check` tells, without changing anything.
- **A process seems left over**: `stop` again; it finds HoldUp's processes from their records.
  Do not use `pkill`: other programs on this server may match the same names.

## Moving on later

**A stable address with a named Cloudflare tunnel.** With a Cloudflare account and a domain on
Cloudflare, a named tunnel keeps the same hostnames across restarts:

```bash
~/work/bin/cloudflared tunnel login
~/work/bin/cloudflared tunnel create holdup
~/work/bin/cloudflared tunnel route dns holdup bot.example.com
~/work/bin/cloudflared tunnel route dns holdup training.example.com
```

Its configuration sends both hostnames to `http://127.0.0.1:<HOLDUP_PORT>` (the port is in
`~/work/holdup-data/ports.env`), and HoldUp then runs with `PUBLIC_BASE_URL=https://bot.example.com`
and `FIXTURE_BASE_URL=https://training.example.com`. The supervisor manages quick tunnels only:
moving it to `cloudflared tunnel run` with fixed URLs is a small change in
`scripts/server_tools.py` (`start_tunnel` and `wanted_urls`). With a stable https address the
bot can also switch to Telegram's webhook (`TELEGRAM_MODE='webhook'`; the webhook secret is
already in `holdup.env`).

**A server of its own.** On a VPS the same `install` works unchanged. A machine with a public
address can instead serve HTTPS itself behind a reverse proxy on port 443; the Docker and Caddy
setup used before quick tunnels is in the git history of `deploy/`.
