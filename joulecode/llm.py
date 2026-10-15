"""LLM connection (Ollama) with structured JSON output.

Taken over from v1: responses are always streamed, even if only the finished
answer is returned to the caller. Without streaming no bytes flow during a
generation lasting several minutes, and some network layers (firewalls,
university networks, Docker Desktop) cut such connections.

New: `format` receives a JSON schema (Ollama ≥ 0.5), and `unload()` unloads
the model from VRAM before measuring – the LLM and measurements must never run
at the same time on the measurement machine.
"""

from __future__ import annotations

import json
import os
import time
from typing import Protocol

import httpx


class LLMError(RuntimeError):
    pass


class LLM(Protocol):
    name: str

    def generate_json(
        self, system: str, prompt: str, schema: dict, *, max_tokens: int | None = None
    ) -> dict: ...

    def unload(self) -> None: ...


class OllamaLLM:
    def __init__(
        self,
        host: str | None = None,
        model: str | None = None,
        *,
        timeout_s: float = 900.0,
        num_ctx: int = 16384,
        num_predict: int = 4096,
        temperature: float = 0.2,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.host = (host or os.environ.get("JOULECODE_OLLAMA_HOST", "http://localhost:11434")).rstrip("/")
        self.model = model or os.environ.get("JOULECODE_OLLAMA_MODEL", "qwen2.5-coder:14b")
        self.timeout_s = timeout_s
        self.num_ctx = num_ctx
        self.num_predict = num_predict
        self.temperature = temperature
        self.name = f"ollama:{self.model}"
        self._transport = transport  # for tests

    def _client(self, timeout: float) -> httpx.Client:
        return httpx.Client(timeout=timeout, transport=self._transport)

    def check(self) -> list[str]:
        try:
            with self._client(10) as client:
                tags = client.get(f"{self.host}/api/tags").json()
        except (httpx.HTTPError, ValueError) as exc:
            return [f"Ollama at {self.host} not reachable: {exc}"]
        names = {m.get("name") for m in tags.get("models", [])}
        if self.model not in names and f"{self.model}:latest" not in names:
            return [f"Model {self.model} is not available in Ollama (ollama pull {self.model})."]
        return []

    def _generate(
        self, system: str, prompt: str, schema: dict, max_tokens: int | None = None
    ) -> tuple[str, dict]:
        payload = {
            "model": self.model,
            "system": system,
            "prompt": prompt,
            "stream": True,
            "format": schema,
            "options": {
                "temperature": self.temperature,
                "num_ctx": self.num_ctx,
                "num_predict": max_tokens or self.num_predict,
            },
        }
        chunks: list[str] = []
        meta: dict = {}
        try:
            with (
                self._client(self.timeout_s) as client,
                client.stream("POST", f"{self.host}/api/generate", json=payload) as response,
            ):
                response.raise_for_status()
                for line in response.iter_lines():
                    if not line.strip():
                        continue
                    event = json.loads(line)
                    if event.get("error"):
                        raise LLMError(f"Ollama error: {event['error']}")
                    chunks.append(event.get("response", ""))
                    if event.get("done"):
                        meta = {"done_reason": event.get("done_reason")}
        except httpx.HTTPError as exc:
            raise LLMError(f"Ollama request failed ({self.host}): {exc}") from exc
        return "".join(chunks), meta

    def generate_json(self, system: str, prompt: str, schema: dict, *, max_tokens: int | None = None) -> dict:
        text, meta = self._generate(system, prompt, schema, max_tokens)
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            hint = " (cut off: increase num_predict/num_ctx)" if meta.get("done_reason") == "length" else ""
            raise LLMError(f"Answer was not valid JSON{hint}: {text[:300]}") from exc

    def unload(self) -> None:
        """Unload the model from (V)RAM immediately."""
        try:
            with self._client(60) as client:
                client.post(
                    f"{self.host}/api/generate", json={"model": self.model, "keep_alive": 0}
                ).raise_for_status()
        except httpx.HTTPError as exc:
            raise LLMError(f"Model could not be unloaded: {exc}") from exc


def wait_for_idle_gpu(threshold: int = 5, timeout_s: float = 120.0, poll_s: float = 2.0) -> bool:
    """Wait until no GPU is busy any more. True = idle (or no GPU)."""
    from joulecode.hostcheck import gpu_utilization

    deadline = time.monotonic() + timeout_s
    while True:
        util = gpu_utilization()
        if util is None or all(u <= threshold for u in util):
            return True
        if time.monotonic() > deadline:
            return False
        time.sleep(poll_s)
