from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal, Optional

from openai import OpenAI

LLMProvider = Literal["openai", "ollama"]


@dataclass(frozen=True)
class LLMConfig:
    provider: LLMProvider
    api_key: str
    model: str
    base_url: Optional[str] = None


def load_llm_config(*, provider: str = "openai", model: Optional[str] = None) -> LLMConfig:
    """Load LLM config.

    - provider=openai requires OPENAI_API_KEY
    - provider=ollama uses an OpenAI-compatible endpoint (default http://localhost:11434/v1)
    """

    prov = (provider or "openai").strip().lower()
    if prov not in ("openai", "ollama"):
        raise ValueError(f"Unsupported provider: {provider!r} (expected 'openai' or 'ollama')")

    if prov == "openai":
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is not set. Set it in your environment or a .env file.")

        m = (model or os.environ.get("OPENAI_MODEL") or "gpt-5-mini").strip()
        return LLMConfig(provider="openai", api_key=api_key, model=m, base_url=None)

    # Ollama local (OpenAI-compatible API)
    base_url = (os.environ.get("OLLAMA_BASE_URL") or "http://localhost:11434/v1").strip()
    # The OpenAI client requires an api_key string even if the server ignores it.
    api_key = (os.environ.get("OLLAMA_API_KEY") or "ollama").strip()
    m = (model or os.environ.get("OLLAMA_MODEL") or "gpt-oss:20b").strip()
    return LLMConfig(provider="ollama", api_key=api_key, model=m, base_url=base_url)


def generate_minutes(*, system_prompt: str, user_prompt: str, provider: str, model: str) -> str:
    cfg = load_llm_config(provider=provider, model=model)

    if cfg.provider == "ollama":
        client = OpenAI(api_key=cfg.api_key, base_url=cfg.base_url)
    else:
        client = OpenAI(api_key=cfg.api_key)

    resp = client.chat.completions.create(
        model=cfg.model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.2,
    )
    return (resp.choices[0].message.content or "").strip() + "\n"
