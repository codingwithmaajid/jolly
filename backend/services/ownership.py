"""S4: which advocate may see which case.

A tool call can say which advocate it is for (body `advocate`, or the
X-Jolly-Advocate header set on the Bolna custom function). When it does, cases
belonging to another advocate are reported as not found, so nothing about
them is spoken. With no advocate given (single-advocate demo), every case of
the firm is visible, as before.

Pilot: resolve the advocate server-side from the caller's number instead of
trusting a value the voice agent passes.
"""

from __future__ import annotations

import re

from backend.models.cases import Case

_PREFIX = re.compile(r"^(adv(ocate)?|mr|mrs|ms|dr|shri|smt|thiru|tmt)\.?\s+", re.IGNORECASE)
NOT_IN_DIARY = "I can't find that matter in your diary. Could you check the case number?"


def normalise_advocate(name: str | None) -> str:
    n = (name or "").strip()
    while True:
        stripped = _PREFIX.sub("", n)
        if stripped == n:
            break
        n = stripped
    return re.sub(r"[^a-z]+", " ", n.lower()).strip()


def owns(case: Case, advocate: str | None) -> bool:
    if not advocate or not advocate.strip():
        return True
    return normalise_advocate(case.advocate) == normalise_advocate(advocate)
