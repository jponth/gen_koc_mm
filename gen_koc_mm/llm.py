from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from openai import OpenAI


@dataclass
class LLMConfig:
    api_key: str
    model: str


def load_llm_config(model: Optional[str] = None) -> LLMConfig:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY is not set. Set it in your environment or a .env file."
        )

    m = (model or os.environ.get("OPENAI_MODEL") or "gpt-4o-mini").strip()
    return LLMConfig(api_key=api_key, model=m)


def generate_minutes(*, system_prompt: str, user_prompt: str, model: str) -> str:
    client = OpenAI()
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.2,
    )
    return (resp.choices[0].message.content or "").strip() + "\n"
