"""Model-backed classifiers for HoldUp's guardrail endpoint (``holdup.guard``).

During a request that carries the ``checker-injection`` guardrail, Otari posts
the input to HoldUp's ``POST /validate``, which asks a classifier. The one built
here uses open-weight safety models through Otari:

* ``checker-injection``: gpt-oss-safeguard-120b with HoldUp's written policy
  (``prompts/injection_policy.txt``) as the system message; that model is made
  to apply a policy supplied with the request;
* ``safety``: llama-guard-4-12b with its own built-in hazard categories.

Only the forwarded text is judged: when the input holds HoldUp's untrusted-data
markers, the classifier looks at the text between them and not at HoldUp's own
instructions around it. Answers are cached by profile and text hash, and these
requests carry no guardrails themselves, so a check never starts another check.
"""

from __future__ import annotations

import hashlib
import re
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any

from . import prompts
from .otari import INJECTION_GUARD_MODEL, SAFETY_GUARD_MODEL, OtariClient
from .pipeline import untrusted
from .prompts.contracts import PolicyVerdict

Classifier = Callable[[str, str], Awaitable[dict[str, Any]]]

MAX_CLASSIFIED_CHARS = 20_000
_MARKED = re.compile(r"<<<BEGIN UNTRUSTED (m-[0-9a-f]+)>>>\n(.*?)\n<<<END UNTRUSTED \1>>>", re.DOTALL)


def untrusted_parts(text: str) -> str:
    """The text between HoldUp's untrusted-data markers, or all of ``text`` when it has none."""
    parts = [match.group(2) for match in _MARKED.finditer(text)]
    return "\n\n".join(parts) if parts else text


def make_classifier(client: OtariClient, *, cache_size: int = 512) -> Classifier:
    """``async (profile, text) -> {"valid", "explanation", "score"}`` for ``holdup.guard.build_router``.

    Errors from the gateway propagate, so the router can fall back to its rules.
    """
    cache: OrderedDict[tuple[str, str], dict[str, Any]] = OrderedDict()

    async def classify(profile: str, text: str) -> dict[str, Any]:
        content = untrusted_parts(text)[:MAX_CLASSIFIED_CHARS]
        key = (profile, hashlib.sha256(content.encode("utf-8", "surrogatepass")).hexdigest())
        if key in cache:
            cache.move_to_end(key)
            return dict(cache[key])
        if profile == "checker-injection":
            verdict = await _injection(client, content)
        elif profile == "safety":
            verdict = await _safety(client, content)
        else:
            raise ValueError(f"unknown guardrail profile {profile!r}")
        cache[key] = verdict
        while len(cache) > cache_size:
            cache.popitem(last=False)
        return dict(verdict)

    return classify


async def _injection(client: OtariClient, content: str) -> dict[str, Any]:
    prompt = prompts.load("injection_policy")
    messages = [
        {"role": "system", "content": prompt.text},
        {"role": "user", "content": untrusted("CONTENT TO CLASSIFY", content)},
    ]
    verdict, _ = await client.chat(
        "guard", INJECTION_GUARD_MODEL, messages,
        output=PolicyVerdict,
        metadata={"app": "holdup", "step": "guard"},
        max_tokens=1_000,  # the model reasons before it answers; small limits end with no answer
        prompt_sha256=prompt.sha256,
    )
    violation = verdict.violation == 1
    return {"valid": not violation, "explanation": verdict.rationale or None, "score": 1.0 if violation else 0.0}


async def _safety(client: OtariClient, content: str) -> dict[str, Any]:
    # Llama Guard answers "safe", or "unsafe" and a line of category codes such as S2.
    answer, _ = await client.chat(
        "guard", SAFETY_GUARD_MODEL, [{"role": "user", "content": content}],
        metadata={"app": "holdup", "step": "guard"},
        max_tokens=20,
    )
    words = answer.split()
    if not words or words[0].lower() not in ("safe", "unsafe"):
        return {"valid": None, "explanation": "the safety model gave no verdict", "score": None}
    if words[0].lower() == "safe":
        return {"valid": True, "explanation": None, "score": 0.0}
    categories = ",".join(words[1:])[:60]
    return {"valid": False, "explanation": f"unsafe: {categories}" if categories else "unsafe", "score": 1.0}
