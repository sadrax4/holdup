"""The near-kit client HoldUp uses, with retries for rate limits and server errors.

near-kit 1.3.0 talks to one RPC URL and retries transport errors and JSON-RPC errors it
classifies as transient. Two answers a public endpoint gives under load slip through: a 429 (or
5xx) whose body is not JSON is raised at once, and one whose JSON body has no ``error`` field
comes back as a ``None`` result, which later fails in odd places. This transport treats any 429
or 5xx as transient whatever its body, honours ``Retry-After`` (capped), and backs off 1, 2, 4
and 8 seconds between its five attempts.

Resending a signed transaction is safe: nearcore recognises its hash and does not run it twice.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
from near import AsyncNear
from near.errors import RpcError
from near.rpc import AsyncRpcTransport

log = logging.getLogger(__name__)

MAX_RETRY_AFTER_S = 30.0


class RpcBusy(RpcError):
    """The endpoint answered 429 or 5xx; worth another try."""

    def __init__(self, message: str, *, method: str, retry_after: float | None) -> None:
        super().__init__(message, method=method, retryable=True)
        self.retry_after = retry_after


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after", "").strip()
    if not value:
        return None
    try:
        return min(max(float(value), 0.0), MAX_RETRY_AFTER_S)
    except ValueError:
        return None  # an HTTP date; the backoff will do


class RetryingTransport(AsyncRpcTransport):
    """near-kit's async transport, also retrying any 429 or 5xx answer."""

    sleep = staticmethod(asyncio.sleep)

    @classmethod
    def wrapping(cls, transport: AsyncRpcTransport) -> RetryingTransport:
        """The same URL, settings and HTTP client as ``transport``, with these retry rules."""
        retrying = cls.__new__(cls)
        retrying.__dict__.update(vars(transport))
        return retrying

    def _handle(self, response: httpx.Response, method: str) -> Any:
        status = response.status_code
        host = httpx.URL(self.rpc_url).host  # messages name the host only: the query may carry an API key
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if status != 429 and status < 500:
            if payload is None:
                raise RpcError(f"{method}: HTTP {status} without JSON from {host}", method=method)
            return super()._handle(response, method)
        reason = f"HTTP {status}"
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict):
            cause = (error.get("cause") or {}).get("name") or error.get("name") or ""
            reason = f"{reason} {cause}: {error.get('message') or ''}".rstrip(": ")
        raise RpcBusy(f"{method}: {reason} from {host}", method=method, retry_after=_retry_after(response))

    async def call(self, method: str, params: Any) -> Any:
        last_error: RpcError | None = None
        for attempt in range(self.retries + 1):
            if attempt:
                delay = self.retry_initial_delay * 2 ** (attempt - 1)
                if isinstance(last_error, RpcBusy) and last_error.retry_after is not None:
                    delay = max(delay, last_error.retry_after)
                log.info("RPC %s failed (%s); retrying in %.1f s", method, last_error, delay)
                await self.sleep(delay)
            try:
                response = await self._client.post(self.rpc_url, json=self._body(method, params))
                return self._handle(response, method)
            except httpx.HTTPError as exc:
                last_error = RpcError(f"RPC request failed: {exc}", method=method, retryable=True)
            except RpcError as exc:
                if not exc.retryable:
                    raise
                last_error = exc
        assert last_error is not None
        raise last_error


def make_client(network: str | None, rpc_url: str | None, *, timeout: float = 30.0, retries: int = 4) -> AsyncNear:
    """An ``AsyncNear`` for ``rpc_url`` (or the network's default) whose transport retries 429 and 5xx."""
    near = AsyncNear(network, rpc_url=rpc_url, timeout=timeout, retries=retries)
    near._transport = RetryingTransport.wrapping(near._transport)  # noqa: SLF001
    return near
