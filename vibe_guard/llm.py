"""Thin LLM client wrapper.

Uses the OpenAI-compatible SDK pointed at DeepSeek. The MVP keeps a single
``chat_json`` helper that asks the model for strict JSON and parses it
defensively (DeepSeek occasionally wraps JSON in markdown fences).
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any

from openai import OpenAI

DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-chat"


class LLMClient:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
    ) -> None:
        self.api_key = api_key or os.environ.get("DEEPSEEK_API_KEY") or os.environ.get(
            "OPENAI_API_KEY", ""
        )
        self.base_url = base_url or os.environ.get("DEEPSEEK_BASE_URL", DEFAULT_BASE_URL)
        self.model = model or os.environ.get("VIBE_GUARD_MODEL", DEFAULT_MODEL)
        if not self.api_key:
            raise RuntimeError(
                "No LLM API key. Set DEEPSEEK_API_KEY (or pass --api-key)."
            )
        self.client = OpenAI(api_key=self.api_key, base_url=self.base_url)
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0

    # ------------------------------------------------------------------ #
    def chat(
        self,
        system: str,
        user: str,
        *,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        force_json: bool = False,
        retries: int = 3,
    ) -> str:
        kwargs: dict[str, Any] = dict(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
        )
        if force_json:
            kwargs["response_format"] = {"type": "json_object"}

        last_err: Exception | None = None
        for attempt in range(retries):
            try:
                resp = self.client.chat.completions.create(**kwargs)
                self.calls += 1
                if resp.usage:
                    self.prompt_tokens += resp.usage.prompt_tokens or 0
                    self.completion_tokens += resp.usage.completion_tokens or 0
                return resp.choices[0].message.content or ""
            except Exception as e:  # noqa: BLE001 — surface after retries
                last_err = e
                time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"LLM call failed after {retries} retries: {last_err}")

    # ------------------------------------------------------------------ #
    def chat_json(
        self,
        system: str,
        user: str,
        *,
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> Any:
        raw = self.chat(
            system,
            user,
            temperature=temperature,
            max_tokens=max_tokens,
            force_json=True,
        )
        return _extract_json(raw)


def _extract_json(raw: str) -> Any:
    raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    # strip markdown fences
    fence = re.search(r"```(?:json)?\s*(.*?)```", raw, re.DOTALL)
    if fence:
        try:
            return json.loads(fence.group(1).strip())
        except json.JSONDecodeError:
            pass
    # grab first {...} or [...] balanced-ish blob
    for opener, closer in (("{", "}"), ("[", "]")):
        start = raw.find(opener)
        end = raw.rfind(closer)
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(raw[start : end + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError(f"Could not parse JSON from LLM output:\n{raw[:500]}")
