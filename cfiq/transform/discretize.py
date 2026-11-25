"""Discretization and concept hierarchies.

DMDW topics:
  * discretization        - continuous rating -> categorical rating band
  * concept hierarchies   - tag -> tag category (dp -> algorithms, ...),
                            raw compiler name -> language family,
                            contest name -> division
All boundaries and mappings come from config (config/cfiq.yaml and
config/tag_hierarchy.yaml); nothing is hard-coded here.
"""

import re
from pathlib import Path

import yaml

OTHER_CATEGORY = "other"


def rating_to_band(rating: float | None, bands: list[dict]) -> str | None:
    """Return the band `code` for a rating, or None if the rating is missing.

    `bands` is ordered by ascending `lower` (inclusive); the first band may have
    lower = None, meaning "no lower bound". A rating falls in the last band
    whose lower bound it reaches.
    """
    if rating is None:
        return None
    chosen = None
    for band in bands:
        if band["lower"] is None or rating >= band["lower"]:
            chosen = band["code"]
        else:
            break
    return chosen


def load_tag_hierarchy(path: str | Path) -> dict[str, str]:
    """Read config/tag_hierarchy.yaml (category -> [tags]) into a tag -> category dict."""
    with open(path, encoding="utf-8") as f:
        categories = yaml.safe_load(f)
    return {tag: cat for cat, tags in categories.items() for tag in tags}


def tag_to_category(tag: str, hierarchy: dict[str, str]) -> str:
    """Roll a tag up one level in the concept hierarchy (unknown tags -> 'other')."""
    return hierarchy.get(tag, OTHER_CATEGORY)


def first_match(value: str | None, rules: list[dict], key: str, default: str) -> str:
    """Return rule[key] for the first rule whose regex `pattern` matches `value`."""
    if not value:
        return default
    for rule in rules:
        if re.search(rule["pattern"], value):
            return rule[key]
    return default


def contest_division(name: str, tcfg: dict) -> str:
    """'Codeforces Round 900 (Div. 2)' -> 'Div. 2' (rules in config)."""
    return first_match(name, tcfg["division_rules"], "division", tcfg["default_division"])


def language_family(raw_name: str, tcfg: dict) -> str:
    """'C++20 (GCC 13-64)' -> 'C++', 'PyPy 3-64' -> 'Python' (rules in config)."""
    return first_match(raw_name, tcfg["language_rules"], "family", tcfg["default_language_family"])


def problem_index_letter(index: str) -> str:
    """'C1' -> 'C', 'D' -> 'D'. Groups sub-problems with their parent letter."""
    m = re.match(r"[A-Z]+", str(index))
    return m.group(0) if m else str(index)
