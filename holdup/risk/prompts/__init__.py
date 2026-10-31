"""Versioned system prompts for the Otari steps.

Each prompt is sent to the model exactly as it is stored here, so the sha256
recorded in a step record (and, shortened, in the on-chain evidence) can be
recomputed from the repository by anyone. Everything that changes from one
payment to the next goes into the user message, never into these files.

The text must stay free of backticks and ASCII quote characters: the hosted
gateway sits behind a web application firewall that rejects some requests
containing them (otari issue #2704).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from functools import cache
from importlib import resources

PROMPT_NAMES = ("triage", "vision", "investigator", "reviewer", "gate", "injection_policy")


@dataclass(frozen=True)
class Prompt:
    name: str
    text: str

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    @property
    def version(self) -> str:
        """Short form of the hash, as recorded in the evidence JSON."""
        return self.sha256[:16]


@cache
def load(name: str) -> Prompt:
    if name not in PROMPT_NAMES:
        raise KeyError(f"unknown prompt {name!r}")
    text = resources.files(__name__).joinpath(f"{name}.txt").read_text(encoding="utf-8")
    return Prompt(name, text)


def versions() -> dict[str, str]:
    """Prompt name -> short sha256, for display and for the evidence JSON."""
    return {name: load(name).version for name in PROMPT_NAMES}
