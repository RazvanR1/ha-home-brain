"""Un singur client Ollama pentru tot add-on-ul: apeluri serializate, același model și context."""
from __future__ import annotations

import asyncio
import json
import logging
import time

import aiohttp

log = logging.getLogger("home_brain.llm")


class LLM:
    def __init__(self, cfg: dict) -> None:
        self.url = (cfg.get("ollama_url") or "").rstrip("/")
        self.model = cfg.get("model") or "llama3.1:8b"
        # Același num_ctx ca integrarea Ollama din HA: altfel Ollama reîncarcă modelul.
        self.num_ctx = int(cfg.get("num_ctx", 8192))
        self.lock = asyncio.Lock()
        self.calls = 0
        self.last_seconds = 0.0

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    async def chat_json(self, messages: list[dict], timeout: float = 180) -> tuple[dict | None, str]:
        """Întoarce (json, text brut). json e None dacă modelul n-a respectat formatul."""
        if not self.enabled:
            return None, "Modelul AI nu e configurat."
        payload = {"model": self.model, "messages": messages, "stream": False, "format": "json",
                   "think": False, "keep_alive": -1, "options": {"num_ctx": self.num_ctx}}
        async with self.lock:
            t0 = time.monotonic()
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as s:
                    async with s.post(f"{self.url}/api/chat", json=payload) as r:
                        r.raise_for_status()
                        body = await r.json()
            finally:
                self.calls += 1
                self.last_seconds = time.monotonic() - t0
        content = body.get("message", {}).get("content", "")
        try:
            data = json.loads(content)
            return (data if isinstance(data, dict) else {"value": data}), content
        except json.JSONDecodeError:
            return None, content

    def budget_chars(self, share: float = 0.6) -> int:
        return int(self.num_ctx * share * 3.5)
