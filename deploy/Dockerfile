# HoldUp server image: web pages, Telegram webhook, MCP server, guardrail endpoint and the
# keeper, all in one process (`python -m holdup serve`). Build it from the repository root:
#
#   docker build -f deploy/Dockerfile -t holdup:local .
#
# deploy/docker-compose.yml does this for you. deploy/Dockerfile.dockerignore limits what is
# sent to the builder, so local secrets (.env, .holdup/) never reach the image.

FROM python:3.12-slim

# uv from PyPI, pinned by version and hash.
COPY deploy/requirements-uv.txt /tmp/requirements-uv.txt
RUN pip install --no-cache-dir --disable-pip-version-check --root-user-action=ignore \
        --require-hashes --only-binary=:all: -r /tmp/requirements-uv.txt \
    && rm /tmp/requirements-uv.txt \
    && uv --version

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1 \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first, in their own layer: code changes do not reinstall them.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# The application. uv installs it in editable mode, so `holdup` is imported from /app/holdup
# and finds fixtures/ and res/ next to it, as in a checkout.
COPY README.md ./
COPY holdup ./holdup
COPY fixtures ./fixtures
# Contract builds (res/*.wasm and their hash lists): verify-deploy and the /prove page compare
# the vault's on-chain code hash with res/holdvault.wasm.
COPY res ./res
RUN uv sync --frozen --no-dev \
    && test -s res/holdvault.wasm \
    && test -s res/demo_token.wasm

# Unprivileged runtime user. /data is the named volume (SQLite database); /app/.holdup points
# there too, so the app's default relative paths (.holdup/...) also land on the volume.
RUN groupadd --system --gid 10001 holdup \
    && useradd --system --uid 10001 --gid holdup --create-home --home-dir /home/holdup \
        --shell /usr/sbin/nologin holdup \
    && install -d -o holdup -g holdup -m 0750 /data \
    && ln -s /data /app/.holdup

ENV PATH="/app/.venv/bin:${PATH}" \
    HOLDUP_DB=/data/holdup.sqlite3

USER holdup
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status != 200)"]

# 0.0.0.0 so that Caddy, in its own container, can reach the app. Port 8000 is not published
# on the host (see docker-compose.yml).
CMD ["python", "-m", "holdup", "serve", "--host", "0.0.0.0", "--port", "8000"]
