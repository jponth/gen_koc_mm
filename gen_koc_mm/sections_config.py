from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

import importlib.resources as pkg_resources


@dataclass(frozen=True)
class SectionDef:
    key: str
    heading: str


@dataclass(frozen=True)
class SectionsConfig:
    sections: list[SectionDef]


def _load_text_from_package(filename: str) -> str:
    return pkg_resources.files(__package__).joinpath(filename).read_text(encoding="utf-8")


def load_sections_config() -> SectionsConfig:
    """Load section definitions (key + heading).

    Default source: `gen_koc_mm/section_headings.json` inside the package.

    Override (optional): set env var `GEN_KOC_MM_SECTIONS_PATH` to a JSON file path.

    JSON format:
    {
      "sections": [
        {"key": "treasurers report", "heading": "Treasurer’s Report:"},
        ...
      ]
    }
    """

    override = os.environ.get("GEN_KOC_MM_SECTIONS_PATH", "").strip()
    if override:
        raw = Path(override).read_text(encoding="utf-8")
    else:
        raw = _load_text_from_package("section_headings.json")

    data = json.loads(raw)
    if not isinstance(data, dict) or "sections" not in data:
        raise ValueError("section_headings.json must be an object with a 'sections' array")

    secs = data["sections"]
    if not isinstance(secs, list):
        raise ValueError("'sections' must be a list")

    def _norm_key(k: str) -> str:
        # Canonical key format: snake_case (lowercase, underscores)
        k = k.strip().lower().replace("’", "").replace("'", "")
        k = re.sub(r"[^a-z0-9]+", "_", k)
        k = re.sub(r"_+", "_", k).strip("_")
        return k

    out: list[SectionDef] = []
    for s in secs:
        if not isinstance(s, dict):
            raise ValueError("each section must be an object")
        key = s.get("key")
        heading = s.get("heading")
        if not isinstance(key, str) or not isinstance(heading, str):
            raise ValueError("each section must have string 'key' and 'heading'")
        nk = _norm_key(key)
        out.append(SectionDef(key=nk, heading=heading))

    if not out:
        raise ValueError("sections list is empty")

    return SectionsConfig(sections=out)
