"""Defaults merged with an optional TOML file, plus XDG path resolution.

Every knob the design calls configurable lives here. Nothing in this file should ever need a code
change to re-tune behaviour.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

APP = "sprigly"

DEFAULTS: dict[str, Any] = {
    "agent": {
        # opencode | claude
        "backend": "opencode",
        # Cheap model for bulk passes, stronger one for judgement calls.
        "model_bulk": "openrouter/free",
        "model_judgement": "sonnet",
        "max_retries": 2,
    },
    "lesson": {
        "default_language": "en",
        "budget_minutes": 20,
        "candidate_ttl_days": 30,
        "max_syllabus": 25,
        "default_depth": 3,
    },
    # Signal weights. Domain spread is deliberately absent: it is a set-level calibration
    # constraint in `offer`, not a per-candidate penalty.
    "scoring": {
        "prereq": 0.25,
        "review": 0.15,
        "learning_progress": 0.20,
        "track_debt": 0.15,
        "effort": 0.10,
        "preference": 0.15,
        "track_debt_saturation_days": 21.0,
        "progress_window": 5,
        # Signals are rescaled within the candidate pool, but only when the pool actually
        # separates them; below this spread the signal stays flat rather than amplifying noise.
        "pool_epsilon": 0.02,
        # Sample revealed preference from its Beta posterior (Thompson sampling) instead of
        # taking the mean.
        "thompson": True,
    },
    "offer": {
        "k": 5,
        # Share of the k slots reserved for lessons with due cards, whenever any exist.
        "review_lane": 0.4,
        # Target share of the offered set per domain. Empty means uniform over what exists.
        "domain_mix": {},
        "calibration_weight": 0.3,
        # Focus shrinks tag similarity rather than fighting the relevance score.
        "focus_similarity_scale": 0.3,
        "prereq_floor_min_prereqs": 2,
    },
    # depth -> scope hint for the curator, NotebookLM audio length, source budget
    "depth": {
        "1": {"scope": "the whole field, one orientation pass", "audio": "brief", "sources": 5},
        "2": {"scope": "major families", "audio": "brief", "sources": 8},
        "3": {"scope": "how one method actually works", "audio": "deep-dive", "sources": 10},
        "4": {"scope": "a single design decision inside a method", "audio": "deep-dive", "sources": 12},
        "5": {"scope": "one derivation, one paper, one failure mode", "audio": "deep-dive", "sources": 15},
    },
    "sources": {
        "open_access_only": True,
        "allowed_types": ["article", "review", "book-chapter", "preprint", "report", "dissertation"],
        "institutional_suffixes": [".edu", ".gov", ".ac.uk", ".ac.jp"],
        # Tier C is per-channel, never "YouTube" as a whole.
        "channel_allowlist": [],
        # Composition per lesson, the real quality lever. Thresholds are provisional until the
        # yield spike measures them.
        "min_tier_a": 1,
        "max_preprint_ratio": 0.4,
        "require_review_at_depth_upto": 3,
        "thin_ratio": 0.6,
        "min_sources": 2,
        "default_min_evidence": "peer-reviewed",
    },
    "bridge": {
        "max_generations_per_day": 4,
        "keep_notebooks": False,
        "poll_seconds": 60,
        "max_retries": 5,
        "retry_backoff_seconds": 60,
    },
    "store": {
        "backup_keep": 7,
    },
    "delivery": {
        "retention_video_days": 30,
        "freshen_new_sources": 3,
    },
    # Relative entries resolve under the data directory; absolute ones are taken as given.
    "paths": {
        "db": "sprigly.db",
        "lessons": "lessons",
        "drop": "drop",
        "backups": "backups",
        "log": "sprigly.log",
    },
}


def _xdg(var: str, fallback: str) -> Path:
    return Path(os.environ.get(var) or Path.home() / fallback) / APP


def config_path() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config") / "config.toml"


def data_dir() -> Path:
    return _xdg("XDG_DATA_HOME", ".local/share")


def _merge(base: dict, over: dict) -> dict:
    """Recursive overlay. Only dicts merge; lists and scalars are replaced wholesale."""
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load(path: Path | None = None, root: Path | None = None) -> dict[str, Any]:
    """Defaults overlaid with the TOML file, with every path resolved absolute."""
    path = path or config_path()
    cfg = DEFAULTS
    if path.is_file():
        cfg = _merge(cfg, tomllib.loads(path.read_text()))
    else:
        cfg = _merge(cfg, {})  # copy, so callers never mutate DEFAULTS
    root = root or data_dir()
    cfg["paths"] = {k: (root / v).resolve() for k, v in cfg["paths"].items()}
    cfg["paths"]["data"] = root.resolve()
    return cfg


def _selfcheck() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        cfg = load(path=td / "missing.toml", root=td / "data")
        assert cfg["lesson"]["budget_minutes"] == 20, "defaults apply when no file exists"
        assert cfg["paths"]["db"].is_absolute(), "paths resolve absolute"
        assert cfg["paths"]["db"].parent == (td / "data").resolve()

        (td / "c.toml").write_text('[lesson]\nbudget_minutes = 45\n[paths]\ndb = "other.db"\n')
        cfg2 = load(path=td / "c.toml", root=td / "data")
        assert cfg2["lesson"]["budget_minutes"] == 45, "file overrides the default"
        assert cfg2["lesson"]["default_language"] == "en", "siblings survive a partial override"
        assert cfg2["scoring"]["prereq"] == 0.25, "untouched sections survive"
        assert "diversity" not in cfg2["scoring"], "spread is a calibration constraint, not a signal"
        assert cfg2["paths"]["db"].name == "other.db"

        assert DEFAULTS["lesson"]["budget_minutes"] == 20, "DEFAULTS must not be mutated"
        assert isinstance(DEFAULTS["paths"]["db"], str), "DEFAULTS must not be mutated"

    print("config selfcheck ok")


if __name__ == "__main__":
    _selfcheck()
