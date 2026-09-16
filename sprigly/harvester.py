"""Source gathering.

Placeholder. Real implementation lands with the credibility gate and the literature adapters; the
signature is what `tick` depends on and is not expected to change.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def gather(topic: str, depth: int, dest: Path, cfg: dict) -> list[dict[str, Any]]:
    """Return source records for a topic. Fewer than `min_sources` means the topic is unsourceable."""
    dest.mkdir(parents=True, exist_ok=True)
    budget = cfg["depth"][str(depth)]["sources"]
    return [
        {
            "url": f"https://example.invalid/{topic.replace(' ', '-')}/{i}",
            "doi": None,
            "title": f"Placeholder source {i} for {topic}",
            "tier": "A",
            "evidence_level": "peer-reviewed",
            "local_path": str(dest / f"source-{i}.txt"),
        }
        for i in range(budget)
    ]
