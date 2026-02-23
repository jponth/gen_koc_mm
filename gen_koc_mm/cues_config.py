from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List

import importlib.resources as pkg_resources


@dataclass(frozen=True)
class CuesConfig:
    cues: dict[str, list[re.Pattern[str]]]


def _load_text_from_package(filename: str) -> str:
    return pkg_resources.files(__package__).joinpath(filename).read_text(encoding="utf-8")


def load_section_cues() -> CuesConfig:
    """Load and compile section cue regexes.

    Default source: `gen_koc_mm/section_cues.json` inside the package.

    Override (optional): set env var `GEN_KOC_MM_SECTION_CUES_PATH` to a JSON file path.

    JSON format:
        {
          "section key": ["regex1", "regex2", ...],
          ...
        }
    where the section key is the normalized heading key used by the chunker,
    e.g. "treasurer’s report".
    """

    override = os.environ.get("GEN_KOC_MM_SECTION_CUES_PATH", "").strip()
    if override:
        raw = Path(override).read_text(encoding="utf-8")
    else:
        raw = _load_text_from_package("section_cues.json")

    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("section_cues.json must be an object mapping section keys to lists of regex strings")

    def _norm_key(k: str) -> str:
        # Canonical key format: snake_case (lowercase, underscores)
        k = k.strip().lower().replace("’", "").replace("'", "")
        k = re.sub(r"[^a-z0-9]+", "_", k)
        k = re.sub(r"_+", "_", k).strip("_")
        return k

    compiled: dict[str, list[re.Pattern[str]]] = {}
    for key, patterns in data.items():
        if not isinstance(key, str) or not isinstance(patterns, list) or not all(isinstance(p, str) for p in patterns):
            raise ValueError(f"Invalid section cues entry for {key!r}")
        norm_key = _norm_key(key)
        compiled[norm_key] = [re.compile(p, re.IGNORECASE) for p in patterns]

    return CuesConfig(cues=compiled)
