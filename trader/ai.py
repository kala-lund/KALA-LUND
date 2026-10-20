from __future__ import annotations

"""Muse adapter (Anthropic Messages API). Optional, fail-closed, schema-strict."""

import json
import time
from dataclasses import dataclass

KEYS = ("action", "confidence", "observations", "evidence", "abstain", "invalidate")
ACTS = ("BUY", "SELL", "HOLD", "ABSTAIN")


@dataclass
class View:
    action: str
    confidence: float
    observations: str
    evidence: list
    abstain: list
    invalidate: str
    ms: int
    model: str


def prompt(summary: dict) -> str:
    return ("Decision-support analyst, not a trading authority. Reply STRICT JSON only: "
            "action (BUY|SELL|HOLD|ABSTAIN), confidence (0..1), observations (string), "
            "evidence (list), abstain (list), invalidate (string). Abstain when unsure.\n"
            + json.dumps(summary, default=str)[:6000])


def clean(p: object) -> dict | None:
    if not isinstance(p, dict) or any(k not in p for k in KEYS):
        return None
    if p["action"] not in ACTS:
        return None
    try:
        c = float(p["confidence"])
    except (TypeError, ValueError):
        return None
    if not (0.0 <= c <= 1.0) or not isinstance(p["observations"], str) \
            or not isinstance(p["evidence"], list) or not isinstance(p["abstain"], list) \
            or not isinstance(p["invalidate"], str):
        return None
    return {"action": p["action"], "confidence": c, "observations": p["observations"][:2000],
            "evidence": [str(e)[:500] for e in p["evidence"][:10]],
            "abstain": [str(e)[:500] for e in p["abstain"][:10]],
            "invalidate": p["invalidate"][:1000]}


class Muse:
    def __init__(self, on: bool, key: str, model: str, base: str, timeout: int, maxt: int) -> None:
        self.on, self.key, self.model = on, key, model
        self.base, self.timeout, self.maxt = base, timeout, maxt
        self.err, self._fails, self._cool = "", 0, 0.0

    @property
    def available(self) -> bool:
        return bool(self.on and self.key)

    def analyze(self, summary: dict) -> View | None:
        if not self.available or time.time() < self._cool:
            return None
        try:
            from anthropic import Anthropic
            cl = Anthropic(api_key=self.key, base_url=self.base or None, timeout=self.timeout, max_retries=1)
            t0 = time.time()
            m = cl.messages.create(model=self.model, max_tokens=self.maxt,
                                   messages=[{"role": "user", "content": prompt(summary)}])
            txt = "".join(b.text for b in m.content if getattr(b, "type", "") == "text")
            v = clean(json.loads(txt))
            if v is None:
                raise ValueError("schema reject")
            self._fails = 0
            return View(ms=int((time.time() - t0) * 1000), model=self.model, **v)
        except Exception as exc:  # noqa: BLE001
            self.err = type(exc).__name__[:200]
            self._fails += 1
            self._cool = time.time() + min(300, 10 * 2 ** min(self._fails, 4))
            return None
