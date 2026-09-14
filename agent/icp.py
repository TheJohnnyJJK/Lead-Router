"""Loads the business profile from business.yaml and exposes it as the same
module-level names the rest of the codebase already depends on.

This is the file that makes "point this at a different business" a config
edit instead of a code change: business.yaml is plain YAML anyone can open
and edit, and everything downstream - the LLM prompt (agent/scoring.py),
the offline fallback scorer, the cost-saving prefilter (agent/graph.py),
and the dashboard's title (dashboard/generate_dashboard.py) - reads it
through the names defined here, unchanged since before this file loaded
from YAML at all. Swapping business.yaml is the entire migration.
"""
from __future__ import annotations

import os
from pathlib import Path

import yaml

_DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "business.yaml"
CONFIG_PATH = Path(os.environ.get("BUSINESS_CONFIG", str(_DEFAULT_CONFIG_PATH)))

_REQUIRED_KEYS = (
    "business_name", "ideal_customer", "routing_rules", "keywords", "free_email_domains"
)


def _load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"business.yaml not found at {CONFIG_PATH} - this file defines who the "
            "agent is qualifying leads for, so there's no safe default to fall back to. "
            "Restore it, write your own, or set BUSINESS_CONFIG to point at one."
        )
    with open(CONFIG_PATH, encoding="utf-8") as f:
        config = yaml.safe_load(f)
    missing = [k for k in _REQUIRED_KEYS if k not in config]
    if missing:
        raise ValueError(f"{CONFIG_PATH} is missing required key(s): {', '.join(missing)}")
    return config


_config = _load_config()

BUSINESS_NAME: str = _config["business_name"]
ICP_DESCRIPTION: str = _config["ideal_customer"]
ROUTING_RUBRIC: str = _config["routing_rules"]

# Used by the heuristic fallback (agent/scoring.py) and the graph's free
# prefilter (agent/graph.py) when no LLM is available.
HOT_KEYWORDS: list[str] = _config["keywords"].get("hot", [])
RESIDENTIAL_KEYWORDS: list[str] = _config["keywords"].get("residential", [])
SPAM_KEYWORDS: list[str] = _config["keywords"].get("spam", [])
FREE_EMAIL_DOMAINS: set[str] = set(_config["free_email_domains"])
