# HoldUp: common tasks. `make` or `make help` lists them.
#
# Commands run offline (in-memory vault, recorded AI answers, nothing sent anywhere) unless
# NETWORK=sandbox or NETWORK=testnet is given. Live commands need the credentials that
# `make deploy` writes to .holdup/credentials.json; without them they stop and list what is missing.

SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

PY := .venv/bin/python
PYTEST := .venv/bin/pytest
HOLDUP := $(PY) -m holdup

NETWORK ?= offline
PROFILE ?= demo
HOST ?= 127.0.0.1
PORT ?= 8000
SERVE_ARGS ?=
SANDBOX_CREDENTIALS ?= .holdup/sandbox-credentials.json
BENCH_ARGS ?= $(if $(OTARI_API_KEY),,--replay)

net_flag = $(if $(filter offline,$(NETWORK)),--offline,--network $(NETWORK))
# Offline, PROFILE sets the in-memory vault's waits; on a network the deployed vault's own configuration does.
serve_profile = $(if $(filter offline,$(NETWORK)),--profile $(PROFILE) )
# Offline, the stories replay their recorded AI answers even when OTARI_MODE=live is exported.
story_env = $(if $(filter offline,$(NETWORK)),OTARI_MODE=replay )

# The toolchain pinned in contracts/rust-toolchain.toml, or `stable` when it is that exact
# version and the pinned one is not installed, so rustup never downloads a second copy.
RUST_PIN = $(shell sed -n 's/^channel *= *"\(.*\)"/\1/p' contracts/rust-toolchain.toml)
RUST_TOOLCHAIN = $(shell rustup toolchain list 2>/dev/null | grep -q '^$(RUST_PIN)' \
	|| (rustc +stable --version 2>/dev/null | grep -q '^rustc $(RUST_PIN) ' && echo stable))
cargo = cd contracts && $(if $(RUST_TOOLCHAIN),RUSTUP_TOOLCHAIN=$(RUST_TOOLCHAIN) )cargo

.PHONY: help setup test test-contract build-contract contracts sandbox-test \
	demo-offline demo demo-sandbox deploy prove attack verify-deploy serve chat probe bench bench-rules

help: ## list the targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-15s %s\n", $$1, $$2}'
	@echo "  Variables: NETWORK=offline|sandbox|testnet (default offline), PROFILE=demo|hosted, HOST, PORT"
	@echo "  An offline serve with PROFILE=hosted holds new payees 5 min, long enough for a live AI check"
	@echo "  serve listens on 127.0.0.1; another HOST needs SERVE_ARGS=--allow-public. On a server: scripts/server.sh"

setup: ## install the Python environment exactly as uv.lock pins it (uv sync)
	uv sync --locked

test: ## Python tests, offline (sandbox, testnet and live Otari tests are skipped)
	$(PYTEST) -q

test-contract: ## Rust unit tests of the HoldVault contract and the HUSD demo token
	$(cargo) test --workspace --locked

build-contract: ## build res/*.wasm with cargo-near, check them against nearcore's rules, write hashes
	scripts/build_contracts.sh

contracts: ## contract unit tests, then the build and the wasm checks
	scripts/build_contracts.sh --test

sandbox-test: ## contract and chain tests on a running NEAR sandbox (needs NEAR_SANDBOX_URL)
	@test -n "$${NEAR_SANDBOX_URL:-}" || { echo "Set NEAR_SANDBOX_URL to the RPC of a running NEAR sandbox \
	(protocol 85 or later), e.g. http://127.0.0.1:3030." >&2; exit 2; }
	$(PYTEST) -m sandbox tests/test_contract_sandbox.py tests/test_chain_sandbox.py

demo-offline: ## the scripted story in memory with recorded AI answers; saved to docs/demo-offline.txt
	@mkdir -p docs
	OTARI_MODE=replay $(HOLDUP) demo --offline | tee docs/demo-offline.txt

demo: ## the same story on NEAR testnet: real transactions, real waits (needs `make deploy NETWORK=testnet`)
	@echo "make demo signs real transactions on NEAR testnet. It reads .holdup/credentials.json from"
	@echo "'make deploy NETWORK=testnet', or HOLDUP_SECRET, HOLDUP_TREASURY_KEY, HOLDUP_ORACLE_KEY,"
	@echo "HOLDUP_KEEPER_KEY and HOLDUP_HUSD_TOKEN from the environment. OTARI_API_KEY makes the AI checks"
	@echo "live (otherwise recorded answers are replayed). Missing settings are listed before anything is sent."
	$(HOLDUP) demo --network testnet

demo-sandbox: ## the story on a local NEAR sandbox: deploys there first if needed (needs NEAR_SANDBOX_URL)
	@test -n "$${NEAR_SANDBOX_URL:-}" || { echo "Set NEAR_SANDBOX_URL to the RPC of a running NEAR sandbox \
	(protocol 85 or later), e.g. http://127.0.0.1:3030. Its root account 'sandbox' deploys everything." >&2; exit 2; }
	@export NEAR_RPC_URL="$$NEAR_SANDBOX_URL" HOLDUP_CREDENTIALS="$(SANDBOX_CREDENTIALS)"; \
	if [ ! -f "$(SANDBOX_CREDENTIALS)" ] || grep -q '"unfinished_deployment"' "$(SANDBOX_CREDENTIALS)"; then \
	  $(HOLDUP) deploy --network sandbox --profile demo; fi; \
	$(HOLDUP) verify-deploy --network sandbox; \
	$(HOLDUP) demo --network sandbox

deploy: ## deploy vault, HUSD, oracle and keeper (NETWORK=sandbox|testnet, PROFILE=demo|hosted)
	@case "$(NETWORK)" in sandbox|testnet) ;; *) echo "Choose where to deploy: make deploy NETWORK=testnet \
	(or NETWORK=sandbox). It signs with HOLDUP_TREASURY_KEY for HOLDUP_ROOT_ACCOUNT (or NEAR CLI \
	credentials; on a sandbox, its root account) and writes .holdup/credentials.json." >&2; exit 2;; esac
	$(HOLDUP) deploy --network $(NETWORK) --profile $(PROFILE)

prove: ## try forbidden actions with the oracle and user keys; every one must be refused
	$(HOLDUP) prove $(net_flag)

attack: ## three attacks (injected message, oracle key, compromised server) and why they fail
	$(story_env)$(HOLDUP) attack $(net_flag)

verify-deploy: ## code hash and access keys of the deployment (offline: a clearly labelled sample)
	$(HOLDUP) verify-deploy $(net_flag)

serve: ## web app, web console, Telegram bot (polling), MCP, guardrail and keeper on http://HOST:PORT
	$(HOLDUP) serve $(net_flag) $(serve_profile)--host $(HOST) --port $(PORT) $(SERVE_ARGS)

chat: ## line-based console on the same engine (offline: recorded AI answers, simulated time)
	$(HOLDUP) chat $(net_flag)

probe: ## live checks of the Otari gateway (needs OTARI_API_KEY; spends at most about USD 0.20)
	$(HOLDUP) probe $(PROBE_ARGS)

bench: ## ScamBench on the triage prompt (replays recorded answers unless OTARI_API_KEY is set)
	$(PY) -m bench.run $(BENCH_ARGS)

bench-rules: ## ScamBench on the rules layer, offline and free (SPLIT=dev|test|all, default dev)
	$(PY) -m bench.run --rules --split $(or $(SPLIT),dev)
