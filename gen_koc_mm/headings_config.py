from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

import importlib.resources as pkg_resources


@dataclass(frozen=True)
class HeadingsConfig:
    section_headings: list[str]


def _load_text_from_package(filename: str) -> str:
    return pkg_resources.files(__package__).joinpath(filename).read_text(encoding="utf-8")


def load_section_headings() -> HeadingsConfig:
    """Load section headings.

    Default source: `gen_koc_mm/section_headings.json` inside the package.

    Override (optional): set env var `GEN_KOC_MM_SECTION_HEADINGS_PATH` to a JSON file path.

    JSON format:
        {"section_headings": ["Heading 1:", ...]}
    """

    override = os.environ.get("GEN_KOC_MM_SECTION_HEADINGS_PATH", "").strip()
    if override:
        raw = Path(override).read_text(encoding="utf-8")
    else:
        raw = _load_text_from_package("section_headings.json")

    data = json.loads(raw)
    if not isinstance(data, dict) or "section_headings" not in data:
        raise ValueError("section_headings.json must be an object with a 'section_headings' array")

    headings = data["section_headings"]
    if not isinstance(headings, list) or not all(isinstance(h, str) for h in headings):
        raise ValueError("'section_headings' must be a list of strings")

    return HeadingsConfig(section_headings=headings)
