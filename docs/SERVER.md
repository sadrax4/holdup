# The server runner: how it works

`scripts/server.sh` installs and runs HoldUp on one Ubuntu server that also runs other things.
This page explains its design for people changing it; the operator's guide is
[deploy/README.md](../deploy/README.md).

## The server it was built for

An Ubuntu 24.04 machine with 2 CPUs and 4 GB of memory, used by one ordinary account without
sudo or Docker. The same machine runs a PDF bot in a tmux session, a remote-control helper in
another tmux session, VPN services and a database, and other programs listen on ports 22, 443,
8080, 8443 and others. The owner's rules follow from that:

1. HoldUp listens on `127.0.0.1` only, on unusual ports from 47800-47999 that are checked free
   first and remembered.
2. The internet reaches it only through outbound Cloudflare quick tunnels; there is no domain, and
   no firewall, sudo, Docker, systemd or crontab change.
3. Nothing that HoldUp did not start is ever stopped, signalled or changed.
4. Secrets live only in `~/work/secrets` (mode 600), and are never printed, put on a command line
   or written to a log.

The sections below show how each rule is kept.

## Pieces

| File | Role |
|---|---|
| `scripts/server.sh` | the commands people run: install, setup, testnet, start, stop, status, ... |
| `scripts/server_tools.py` | standard-library Python used by the script: env files, ports, process records, state, and the supervisor (`supervise`) |
| `deploy/backup.sh` | the backup archive |
| `tests/test_server_runner.py`, `tests/fakes/` | tests with a fake cloudflared, a fake uv and a fake Bot API |

`server_tools.py` runs with the system `python3` (3.12 or newer), so it works before
`uv sync` has built `.venv`. The script calls it for anything fiddlier than a few lines of bash.

## Processes

```
tmux -L holdup: session "holdup", window "run"
└── bash scripts/server.sh run  -->  exec python3 scripts/server_tools.py supervise
    ├── cloudflared tunnel ... --metrics 127.0.0.1:<TUNNEL_METRICS_PORT> --url http://127.0.0.1:<HOLDUP_PORT>
    ├── cloudflared tunnel ... --metrics 127.0.0.1:<FIXTURE_METRICS_PORT> --url http://127.0.0.1:<HOLDUP_PORT>
    └── uv run --locked --no-dev ... holdup serve --network testnet --host 127.0.0.1 --port <HOLDUP_PORT>
        └── python .venv/bin/holdup serve ...   (uv passes SIGTERM, SIGINT and SIGHUP on to it)
```

- HoldUp has a tmux server of its own: every tmux command is `tmux -L holdup -f /dev/null ...`
  (`HOLDUP_TMUX_SOCKET` changes the name). The owner's tmux server, with its `pdfbot` and `rc`
  sessions, is never even asked about, and no `~/.tmux.conf` (remain-on-exit, say) changes how
  HoldUp's session behaves. tmux itself runs with `env -i HOME PATH LANG [TMUX_TMPDIR]`: the
  client that starts a tmux server gives it its whole environment, which every later session
  there inherits, so a key exported in the owner's shell never reaches it.
- `start` creates the session with `tmux new-session -d -s holdup -n run -- env -i <settings>
  bash scripts/server.sh run` and marks it with the session option `@holdup_runner`. The
  supervisor gets an environment of its own: paths and tuning values only, never a secret. If
  the marked session is still open because of windows the owner opened in it, `start` adds the
  `run` window to it instead; a session named `holdup` without the mark is someone else's, and
  `start` refuses to use it.
- Each child runs in a session and process group of its own (`start_new_session`), so a Ctrl-c
  typed in the tmux window reaches the supervisor only, and the supervisor stops the children in
  order.
- The supervisor holds an exclusive `flock` on `run/supervisor.lock` for its whole life; a second
  supervisor refuses to start and leaves the first one's files alone.

## Supervision

The supervisor checks the children every quarter second and their health every 10 seconds
(`HOLDUP_CHECK_INTERVAL`).

| Event | Action |
|---|---|
| a tunnel gets its hostname | read from `GET 127.0.0.1:<metrics>/quicktunnel` (`{"hostname": "<name>.trycloudflare.com"}`, no scheme); anything that is not a `*.trycloudflare.com` name is refused |
| a tunnel is connected | `GET /ready` answers 200 (503 while no edge connection is up) |
| both tunnels connected | `holdup serve` starts with `PUBLIC_BASE_URL` and `FIXTURE_BASE_URL` set to them |
| the app exits, or `/health` fails 3 times in a row after it was healthy | the app alone restarts, so the public URL stays the same |
| the app does not answer `/health` within 120 s of starting | it is restarted |
| a tunnel exits, gets no hostname or does not connect within 90 s, or `/ready` fails for 120 s | that tunnel is replaced (new hostname); once the new one is connected, the app restarts with the new URL |
| `https://<host>/health` fails from outside 3 times (every 5 minutes) | the main tunnel is replaced |
| the two tunnels are not both connected for 90 s and the app is not running | the app starts without public URLs, so the Telegram bot keeps working; the training pages are then served only to `localhost`, never under a tunnel's hostname; it restarts with the URLs once the tunnels are up |

Each child has its own backoff: 5 s doubling to 60 s (`HOLDUP_BACKOFF_MIN`, `HOLDUP_BACKOFF_MAX`),
reset after 10 minutes of good health. The provisioning endpoint publishes no rate limit, so a
tunnel that keeps failing is asked for at most once a minute.

On SIGINT, SIGTERM or SIGHUP the supervisor sends SIGTERM to the three children, waits up to
40 s (`HOLDUP_STOP_TIMEOUT`; cloudflared gets `--grace-period 5s`, and `holdup serve` usually
stops within a second, but may take 10 s for open requests and 20 s for a keeper round in
progress), then SIGKILLs what is left, including any process left in a child's process group.
`stop` sends SIGTERM to the recorded supervisor itself (verified, through a pidfd), gives it 50 s
and then SIGKILL, and then stops whatever is left of the recorded children. It never types into
a tmux window (a pane in copy mode would swallow a Ctrl-c) and never kills a session: the
supervisor's window closes when it exits, and windows the owner opened stay. If a supervisor
holds the lock without a valid record, `stop` says so and leaves it alone.

A child is started only if its record can be written (a full disk, for example): otherwise its
new process group is killed at once and the start is retried after the backoff, so no child ever
runs unrecorded.

`holdup serve` leaves successful `GET /health` requests out of its access log, so the
supervisor's checks every 10 seconds do not bury everything else in `logs app`.

The state is written atomically to `run/state.json` (status, URLs, PIDs, ports, the last
`/health` answer, the public check); `start`, `status` and `url` read it. The status is
`starting`, `running` (both tunnels connected and the app healthy with their URLs), `degraded`
(the app serves, but without the current public URLs), `restarting`, `stopping`, `stopped` or
`failed` (a configuration problem, with the reason in `message`).

## Ports

Three ports: `HOLDUP_PORT` (the app), `TUNNEL_METRICS_PORT` and `FIXTURE_METRICS_PORT` (the
tunnels' metrics servers, which must not be reachable from outside because they serve
`/debug/pprof` and `/diag`). A port is free when nothing listens on it on any address (from
`ss -Htln` when installed, and from `/proc/net/tcp` and `/proc/net/tcp6`) and `127.0.0.1` can
bind it with `SO_REUSEADDR`, as servers do, so a port in TIME_WAIT counts as free. Saved ports
are kept while they are free; taken ones are replaced at random from the range and saved in
`ports.env`. The supervisor checks again right before it starts each child. Overrides
(`HOLDUP_PORT_MIN`, `HOLDUP_PORT_MAX`) must stay inside 47800-47999.

Any local user can read a port from cloudflared's command line, and cloudflared opens its metrics
port only after Cloudflare has created the tunnel, so another user's program could listen there
first and name a hostname of its own. The supervisor therefore trusts a local answer only when
the listening socket is ours: in `/proc/net/tcp` (and `tcp6`) every listener on that port must
have our uid and an inode found among the `socket:[...]` links in `/proc/<pid>/fd` of the
recorded child (or its verified process group). This is checked before `/quicktunnel` is read
and before the app's first `/health` counts; once our process holds a port, no other user can
bind it. While the app does not hold its port (it is restarting), the supervisor looks every
second: if someone else listens there, it kills both tunnels at once, so no public request
reaches that program, and moves the app to another port (new hostnames follow).

## Stopping only what is ours

Right after starting a child, the supervisor writes `run/<name>.pid.json`: the PID, the start
time from `/proc/<pid>/stat` (field 22), the process group, the user, the working directory and
words that must appear in the command line (`holdup serve <port>`, or `tunnel --metrics
127.0.0.1:<port>`). Before any signal, `server_tools.py` opens a pidfd for the process, checks
all of these again, and signals through the pidfd, so a PID that was reused by another program is
never hit. Members of a recorded process group (the `holdup serve` that uv started) are signalled
only if their user, command line and directory match too. tmux is never used to signal anything:
it is only asked about `=holdup` (an exact name; plain `holdup` would also match `holdup2`) and
`=holdup:=run`, on HoldUp's own tmux server, and the only window it ever closes is `run` once
its pane is dead (with remain-on-exit), which signals no process. `status` reports the session
as HoldUp's only when the pane of `run` is the recorded supervisor. There is no `pkill`,
`killall`, `kill-session` or `kill-server`.

## Secrets

- `holdup.env` is parsed as `KEY='value'` lines, never sourced, and written atomically at mode
  600. A quoted value ends at its closing quote, and only a comment after a space may follow it
  (`KEY='value'  # note`); anything else is an error naming the line, because a value that kept
  its quotes would silently change `HOLDUP_SECRET`. Values with quotes or line breaks are
  refused. `setup` adds missing values and never replaces one.
- The script never runs with `set -x` (it turns tracing off first). Secrets travel in shell
  variables and reach other programs only as environment variables set by builtins, or on
  standard input: the bot token goes to curl inside a `curl --config -` file on stdin.
- Only `holdup serve` gets the settings from `holdup.env`; the supervisor reads the file when it
  starts the app and does not keep it in its own environment. cloudflared runs with `HOME`,
  `PATH` and `LANG` only. The testnet commands run in an emptied environment with just
  `HOLDUP_CREDENTIALS`, `HOLDUP_NETWORK`, `HOLDUP_SECRET` and `NEAR_RPC_URL`, so a stray
  `HOLDUP_TREASURY_KEY` in the owner's shell cannot change what gets deployed.
- `holdup serve` sets the `httpx` loggers to WARNING (Bot API URLs contain the token) and
  filters configured secrets out of its log lines.
- Errors about secrets name the line or the setting, never the value.
- `setup` reads settings from the environment only as `HOLDUP_<NAME>` (`HOLDUP_TELEGRAM_BOT_TOKEN`,
  `HOLDUP_OTARI_API_KEY`, ...): this account runs another Telegram bot, whose
  `TELEGRAM_BOT_TOKEN` must not become HoldUp's (its poller would take that bot's updates).
- `start` and the supervisor set the secrets folder back to 700 and `holdup.env`,
  `holdup-credentials.json` and the `.old` copies back to 600, and refuse files of another user.
  `start` on testnet refuses a `HOLDUP_SECRET` that differs from the deployment's.

## cloudflared

Every flag has a reason, found in the cloudflared 2026.10.7 source and Cloudflare's docs:

| Flag | Why |
|---|---|
| `tunnel --url http://127.0.0.1:<port>` | a quick tunnel: an HTTPS POST to `api.trycloudflare.com` creates it, its credentials stay in memory, and nothing is written to `~/.cloudflared` |
| `--metrics 127.0.0.1:<port>` | without it cloudflared takes the first free port of 20241-20245 (all interfaces in container builds); an explicit address that cannot be bound stops it with "Error opening metrics server listener" |
| `--no-autoupdate` | the updater is active when stdout is not a terminal; an update makes cloudflared exit with code 11 and come back with a new hostname and PID |
| `--protocol http2` | HTTP/2 makes outbound TCP connections to port 7844 only. QUIC (the default of a quick tunnel, and tried first by `auto`) binds a UDP socket on every interface (`net.ListenUDP` with no address in `connection/quic.go`), which this server's rules forbid, so `HOLDUP_TUNNEL_PROTOCOL` accepts only `http2` |
| `--edge-ip-version 4` | the default became `auto` in 2026.4.0; IPv4 is predictable (`HOLDUP_TUNNEL_EDGE_IP_VERSION`) |
| `--config <data>/cloudflared/config.yml` | an existing `~/.cloudflared/config.yml` would still apply, and its `ingress:` rules win over `--url`; an explicit file of our own (`{}`) avoids that, and `HOME` points at an empty directory too |
| `--loglevel info` | `debug` logs every request header, including the bearer tokens Otari sends to `/mcp` |
| `--grace-period 5s` | the default 30 s would not fit in the stop timeout |

Logs go to `logs/tunnel.log` and `logs/fixtures.log` through standard output, and the supervisor
rotates every log above 20 MB (`HOLDUP_LOG_MAX_BYTES`): it copies the file to `.1` (keeping two
copies) and empties it; the children write with `O_APPEND` and carry on at the start.

Quick tunnels have no uptime guarantee, allow 200 requests in flight, do not carry Server-Sent
Events, and get a new random hostname on every start. Every start logs Cloudflare's notice that
account-less tunnels are subject to its Online Services Terms.

## Telegram

The bot long-polls `getUpdates` from inside `holdup serve` (`TELEGRAM_MODE='polling'`), so no
inbound connection or webhook is needed and the bot keeps working without a tunnel. Starting to
poll removes any webhook set for the token. Two processes polling one token make Telegram answer
409 Conflict; `/health` then reports `"conflict": true` and `status` says so. The server only
needs outbound HTTPS to `api.telegram.org`.

## NEAR testnet

`testnet` runs `scripts/testnet_tools.py bootstrap <root>` (a funding account and the root
account with faucet money, then a top-up to the hosted deployment's budget), `holdup deploy
--network testnet --profile hosted --root <root>`, `holdup verify-deploy`, `holdup prove` and a
faucet top-up of the treasury to 5 NEAR, all with `HOLDUP_CREDENTIALS` pointing at
`~/work/secrets/holdup-credentials.json` (bootstrap writes the root account's key there, and
deploy reads it from there). `holdup deploy` prints the budget line by line and refuses, before
sending anything, when the root holds less. A credentials file without a complete deployment is
resumed with the same root account: `holdup deploy` writes the deployment's fresh keys there
before its first transaction (under `unfinished_deployment`, with each transaction once it ran),
and a rerun checks on chain which steps are done and carries on from the first one that is not,
so a transaction that ran just before an interruption is not sent twice. The temporary keys of
the token and the vault leave the file once they are deleted; the funding account's key stays. An
account under the root that exists without the deployment's key (left by an attempt that did not
save its keys) is refused before anything is sent. `holdup deploy` exits with status 2 when it
refused before sending anything and 1 when it stopped halfway; the testnet step tells the two
apart and points to a rerun only for the second. A complete file is never replaced without
`--force-new`, which renames it instead of deleting it.

The token's and the vault's first transactions wait until their new keys are final: near-kit
returns from a transaction once it ran, but reads the nonce of a key it has not used yet at final
finality, a few blocks later, and finds nothing until then. `prove` leaves out the user-key
attempts while no user has been onboarded, because that account does not exist yet; its calls
attach 1 Tgas, because nearcore charges the gas a transaction buys up front before it looks at the
key's permission, and a user's account holds only about 0.03 NEAR.

NEAR's faucet is rate limited although its documentation does not say so. On 2026-10-31 it
answered `Failed to create <account>.testnet: 429 Rate limit exceeded, retry in 10 hours.` (HTTP
500) after six drops in five minutes. A clean testnet step takes three drops. A rate-limited
answer stops `bootstrap` and `topup` at once, saying when to try again, and the running service
does not ask again before that time (an hour when the faucet names none, and never more than a
day, whatever it names). A funding account the faucet already paid is used again by the next
`bootstrap` instead of asking for another drop.

The deployment also creates the payees of the training samples, among them ten practice friends
(`ravi-0` ... `ravi-9`); each person's ordinary practice payments go to one of them, chosen from
their user key (`holdup.bot.samples.friend_account`), so that eight senders in a day
(`MULE_PATTERN`) or two cancels after a warning (a 30-day report) on one shared account do not
brake everyone's ordinary payment. The rules read the payees' real age on testnet: for 72 hours
after a deployment they count as brand new, so the testnet step belongs at least three days
before judging. Every planned account id, and the longest user id (`u` + 10 characters), is
checked before anything is sent; that is why a root has at most 48 characters.

## Tests

`tests/test_server_runner.py` has unit tests for `server_tools.py` and runs the real
`scripts/server.sh` end to end:

- as an unprivileged user (`nobody` when the suite runs as root, because the script refuses root),
  with `HOME` and `HOLDUP_HOME` in a temporary directory;
- with an isolated tmux server (`HOLDUP_TMUX_SOCKET` and `TMUX_TMPDIR` in the temporary
  directory), next to decoy sessions named `pdfbot` and `holdup2` and a decoy program listening in
  the port range whose command line looks like ours;
- with `tests/fakes/cloudflared` (takes the real flags, refuses unknown ones, serves `/quicktunnel`
  and `/ready` on its `--metrics` address), `tests/fakes/uv` (checks the flags, runs `holdup serve`
  from this checkout's `.venv` and passes signals on as uv does, simulates the testnet commands)
  and `tests/fakes/telegram_api.py`; the app itself is the real `holdup serve --offline`.

They check file modes, URL discovery, `/health` on `127.0.0.1`, that every listening socket of the
service is on `127.0.0.1` in the range, that no secret shows up in any log, output, command line
or the tunnel's environment, that the bot answers a Telegram user (a message, a button press)
with links to the public URL, a replaced tunnel and a crashed app, idempotent `install`, that a
second supervisor changes nothing, that `stop` leaves no process and frees the ports while the
decoys survive (with the supervisor's pane in copy mode and a window of the owner's in the
session, which stays), that a port taken while stopped is replaced, that a `holdup` session
the script did not start is refused and left alone, that a plain `TELEGRAM_BOT_TOKEN` in the
environment is never used, that modes of the secrets are repaired at start, that a hostname
from a socket our tunnel does not hold is not trusted, that a stranger on the app's port cuts the
tunnels, and that a child whose record cannot be written is stopped again. Without tmux they
are skipped. They take about 40 seconds.

Not covered here: the real cloudflared, uv 0.12.9 and Telegram, which only the server's first run
exercises. Before it, `uv lock --check` with the server's uv is a read-only way to confirm the
lock file is accepted; CI runs it with uv 0.12.9.
