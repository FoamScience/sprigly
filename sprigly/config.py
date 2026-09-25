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
        # Model ids are backend-specific — opencode wants provider/model, the claude CLI wants a
        # short alias — so they are keyed by backend rather than shared. The opencode backend runs
        # everything on the free router; the split between bulk work and judgement calls (syllabus
        # decomposition, relevance ranking) only buys anything on the claude backend. Check
        # available ids with `opencode models`.
        "models": {
            "opencode": {"bulk": "openrouter/openrouter/free", "judgement": "openrouter/openrouter/free"},
            "claude": {"bulk": "sonnet", "judgement": "opus"},
        },
        "max_retries": 2,
        "timeout_seconds": 600,
        # The relevance pass degrades open, so waiting the full budget only to keep everything is
        # wasted time. It gets a shorter leash than a curate call, whose output cannot be guessed.
        "relevance_timeout_seconds": 180,
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
    "tags": {
        # Abbreviations are expanded only from this map. Nothing is guessed.
        "expansions": {},
        # How close a new tag must be to an existing one before it snaps onto it.
        "snap_cutoff": 0.92,
    },
    "sources": {
        # Identifies you to OpenAlex and Crossref for their faster "polite pool". Left empty
        # because sending your address to a third party should be a deliberate choice.
        "mailto": "",
        "open_access_only": True,
        "allowed_types": ["article", "review", "conference-paper", "book-chapter", "preprint",
                          "report", "dissertation"],
        # Tier B. University and government suffixes, then the bodies that publish the primary
        # material in business and policy: working papers, regulator filings, standards and
        # national statistics. Consultancy whitepapers and trade magazines are deliberately absent
        # — they have no recognised venue either, so they stay out as primary sources.
        "institutional_suffixes": [".edu", ".gov", ".ac.uk", ".ac.jp", ".int", "europa.eu",
                                   "nber.org", "ssrn.com", "repec.org", "bis.org", "imf.org",
                                   "oecd.org", "worldbank.org", "ecb.europa.eu", "iso.org",
                                   "nist.gov", "who.int"],
        # Tier C is per-channel, never "YouTube" as a whole.
        "channel_allowlist": [],
        # Composition per lesson, the real quality lever. Measured over numerics, human sciences
        # and business at depth 3 and 5 (tasks-8wk.10): the gate admits 79-90% of what the search
        # returns and fills the budget every time, so none of these bind on a healthy field. They
        # are floors for a sparse one. Peer-reviewed sources per lesson ran 10-15, so 3 is well
        # under the observed minimum and still refuses a lesson built out of preprints alone.
        "min_tier_a": 3,
        # Never approached: 0% at depth 3 and at most 7% at depth 5, because tier A sorts ahead of
        # preprints and the budget fills first. Left at its original value rather than tuned — six
        # observations are not grounds for moving a guard that never fired.
        "max_preprint_ratio": 0.4,
        # Satisfiable, contrary to the earlier reading. Reviews exist for every field measured
        # (2 for the numerics query, 80 business, 2316 human sciences) and one was ranked into the
        # accepted set at depth 3 in all three. Thin in numerics, so the note can still fire there.
        "require_review_at_depth_upto": 3,
        "thin_ratio": 0.6,
        "min_sources": 2,
        "default_min_evidence": "peer-reviewed",
        # A lesson with no track is judged by its domain. "Scientifically proven" maps onto physics
        # and numerics and poorly onto business, where the good primary material is working papers,
        # regulator filings and statistics rather than journal articles. A track's own
        # min_evidence always wins over this.
        "min_evidence_by_domain": {"business": "institutional"},
        # No adapter may stall a harvest indefinitely.
        "http_timeout_seconds": 30.0,
        # How many candidates the relevance agent is asked to judge in one prompt. The gate bounds
        # the final count anyway, and a long listing is what a small model chokes on.
        "rank_limit": 20,
        # Source downloads are independent and mostly waiting, so they run together.
        "fetch_workers": 6,
        "max_bytes": 20_000_000,
        # Below this many characters an extraction is a landing page, not a paper.
        "min_text_bytes": 6_000,
        "fetch_timeout_seconds": 30,
        # When every pdf link OpenAlex carried has failed, ask Unpaywall and Semantic Scholar
        # whether they know a copy it missed. Turn this off to keep your dois off those services.
        "oa_fallbacks": True,
    },
    "bridge": {
        # Which artifacts each lesson gets. The video overview is narrated slides, so it covers
        # what a static deck would and adds the moving explanation; add "slides" alongside it if
        # you also want a PDF to skim.
        "artifacts": ["audio", "video", "quiz"],
        # Generation options, by enum name from notebooklm-py. The audio format comes from the
        # depth table instead, since it is a property of how broad the lesson is.
        "audio_length": "DEFAULT",
        "slide_format": "DETAILED_DECK",
        "slide_length": "DEFAULT",
        "video_format": "EXPLAINER",
        "video_style": "AUTO_SELECT",
        "quiz_quantity": "STANDARD",
        "quiz_difficulty": "MEDIUM",
        "max_generations_per_day": 4,
        # Sprigly never deletes a notebook on its own. Deleting something on your Google account
        # is not a decision a background timer gets to make, so cleanup is `sprigly notebooks
        # --prune`, which asks first. Set this true only if you want unattended deletion.
        "delete_notebooks": False,
        "poll_seconds": 60,
        "client_timeout_seconds": 60.0,
        "source_ready_timeout_seconds": 300.0,
        "max_retries": 5,
        "retry_backoff_seconds": 60,
    },
    "store": {
        "backup_keep": 7,
    },
    "delivery": {
        # Video is a primary artifact here, not a bulky extra, so nothing prunes it by default.
        # Set a number of days if disk becomes the problem.
        "retention_video_days": 0,
        "freshen_new_sources": 3,
    },
    # Relative entries resolve under the data directory; absolute ones are taken as given.
    "paths": {
        "db": "sprigly.db",
        "lessons": "lessons",
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
        backend = cfg2["agent"]["backend"]
        assert set(cfg2["agent"]["models"][backend]) == {"bulk", "judgement"}, \
            "the active backend must have a model for both roles"
        assert cfg2["paths"]["db"].name == "other.db"

        assert DEFAULTS["lesson"]["budget_minutes"] == 20, "DEFAULTS must not be mutated"
        assert isinstance(DEFAULTS["paths"]["db"], str), "DEFAULTS must not be mutated"

    print("config selfcheck ok")


if __name__ == "__main__":
    _selfcheck()
