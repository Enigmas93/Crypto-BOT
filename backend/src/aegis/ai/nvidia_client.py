"""NVIDIA NIM chat client (OpenAI-compatible endpoint) for the AI layer.

Built around what the free NIM tier actually does, measured live
2026-09-28: models get retired without notice (llama-3.3-70b, gpt-oss-120b
returned 410 Gone), the big ones answer "503 Service temporarily
overloaded" a large share of the time, and some just hang past 90s. So:

- a model CHAIN, tried in order; a model that fails is put on cooldown so
  the next call skips straight to one that works;
- a hard per-request timeout;
- JSON is extracted from whatever the model returns (some wrap it in
  fences or prepend reasoning text), never trusted to be clean.

Nothing in the order path ever awaits this client. AI output is written to
its own tables and read by the dashboard / confluence observation only.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass

import httpx

from aegis.logging_utils import get_logger, log_event

_LOG = get_logger("ai.nvidia")

BASE_URL = "https://integrate.api.nvidia.com/v1"
# Ordered by measured reliability/latency on 2026-09-28 (2 rounds, same
# news prompt, all three answered correctly): nemotron-3-super ~5s,
# gemma-4-31b 4-18s, gpt-oss-20b 22-31s; nemotron-3-ultra often 503.
DEFAULT_MODELS = (
    "nvidia/nemotron-3-super-120b-a12b",
    "google/gemma-4-31b-it",
    "openai/gpt-oss-20b",
    "nvidia/nemotron-3-ultra-550b-a55b",
)
_COOLDOWN_SECONDS = 300.0


@dataclass(slots=True)
class AiResult:
    data: dict
    model: str
    latency_s: float


def extract_json_object(text: str | None) -> dict | None:
    """Last balanced top-level {...} in `text` that parses as a JSON object.
    The last one wins because reasoning-style models think out loud first
    and put the answer at the end."""
    if not text:
        return None
    candidates: list[dict] = []
    depth = 0
    start = None
    in_str = False
    escape = False
    for i, ch in enumerate(text):
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    obj = json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    pass
                else:
                    if isinstance(obj, dict):
                        candidates.append(obj)
                start = None
    return candidates[-1] if candidates else None


class NvidiaClient:
    def __init__(self, api_key: str, models: tuple[str, ...] = DEFAULT_MODELS, timeout: float = 60.0,
                 http: httpx.AsyncClient | None = None) -> None:
        self.api_key = api_key
        self.models = models
        self._http = http or httpx.AsyncClient(base_url=BASE_URL, timeout=timeout)
        self._cooldown_until: dict[str, float] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    async def aclose(self) -> None:
        await self._http.aclose()

    def _available_models(self) -> list[str]:
        now = time.monotonic()
        ready = [m for m in self.models if self._cooldown_until.get(m, 0.0) <= now]
        # If everything is cooling down, still try the whole chain rather
        # than go silent - cooldown is an ordering hint, not a ban.
        return ready or list(self.models)

    async def complete_json(self, system: str, user: str, max_tokens: int = 600) -> AiResult | None:
        if not self.enabled:
            return None
        headers = {"Authorization": f"Bearer {self.api_key}"}
        for model in self._available_models():
            started = time.monotonic()
            try:
                response = await self._http.post("/chat/completions", headers=headers, json={
                    "model": model, "temperature": 0.1, "max_tokens": max_tokens,
                    "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                })
            except httpx.HTTPError as exc:
                self._cooldown_until[model] = time.monotonic() + _COOLDOWN_SECONDS
                log_event(_LOG, "model_failed", level=30, model=model, error=type(exc).__name__)
                continue
            if response.status_code != 200:
                self._cooldown_until[model] = time.monotonic() + _COOLDOWN_SECONDS
                log_event(_LOG, "model_failed", level=30, model=model, status=response.status_code)
                continue
            try:
                message = response.json()["choices"][0]["message"]
            except (ValueError, KeyError, IndexError):
                self._cooldown_until[model] = time.monotonic() + _COOLDOWN_SECONDS
                continue
            data = extract_json_object(message.get("content")) or extract_json_object(message.get("reasoning_content"))
            if data is None:
                log_event(_LOG, "model_unparseable", level=30, model=model)
                continue
            return AiResult(data=data, model=model, latency_s=round(time.monotonic() - started, 2))
        return None
