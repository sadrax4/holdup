"""Sliding-window rate limits."""

from __future__ import annotations

import pytest

from holdup.bot.ratelimit import Decision, RateLimiter


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_allows_up_to_the_limit_then_warns_once() -> None:
    clock = Clock()
    limiter = RateLimiter(2, 60.0, clock=clock)
    assert [limiter.hit("a").allowed for _ in range(2)] == [True, True]
    assert limiter.hit("a") == Decision(allowed=False, warn=True)
    assert limiter.hit("a") == Decision(allowed=False, warn=False)


def test_window_slides_and_refusals_do_not_count() -> None:
    clock = Clock()
    limiter = RateLimiter(2, 60.0, clock=clock)
    limiter.hit("a")
    clock.now += 30
    limiter.hit("a")
    clock.now += 20
    assert not limiter.hit("a").allowed  # refused: does not push the window
    clock.now += 11  # the first hit is now older than 60 s
    assert limiter.hit("a").allowed
    assert not limiter.hit("a").allowed


def test_wait_says_how_long_until_the_next_action_without_counting_one() -> None:
    clock = Clock()
    limiter = RateLimiter(2, 60.0, clock=clock)
    assert limiter.wait("a") == 0
    limiter.hit("a")
    clock.now += 15
    limiter.hit("a")
    assert limiter.wait("a") == 45.0 and limiter.wait("a") == 45.0  # asking twice changes nothing
    clock.now += 45
    assert limiter.wait("a") == 0 and limiter.hit("a").allowed


def test_keys_are_independent_and_memory_is_bounded() -> None:
    limiter = RateLimiter(1, 60.0, clock=Clock(), max_keys=2)
    assert limiter.hit("a").allowed
    assert limiter.hit("b").allowed
    assert limiter.hit("c").allowed  # evicts "a", the least recently used key
    assert limiter.hit("a").allowed
    assert not limiter.hit("c").allowed


def test_rejects_nonsense_limits() -> None:
    with pytest.raises(ValueError):
        RateLimiter(0, 60.0)
    with pytest.raises(ValueError):
        RateLimiter(1, 0)
