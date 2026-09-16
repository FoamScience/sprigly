"""NotebookLM bridge.

Placeholder. The real implementation wraps notebooklm-py; these three functions are the seam
`tick` is written against, and generation is polled rather than waited on.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def start(topic: str, source_paths: list[str], cfg: dict, language: str = "en") -> dict[str, Any]:
    """Create a notebook, upload sources, request generation. Returns a job reference."""
    return {"notebook_id": f"stub-{abs(hash(topic)) % 10**8}", "jobs": {"audio": "j1", "slides": "j2"}}


def ready(job_ref: dict, cfg: dict) -> bool:
    """Has generation finished? Cheap, called once per tick per generating lesson."""
    return True


def download(job_ref: dict, dest: Path, cfg: dict) -> list[dict[str, Any]]:
    """Fetch the artifacts, returning one record each. Container and MIME come from the download."""
    dest.mkdir(parents=True, exist_ok=True)
    out = []
    for kind, name, mime, secs in (("audio", "podcast.m4a", "audio/mp4", 1380.0),
                                   ("slides", "slides.pdf", "application/pdf", None)):
        path = dest / name
        path.write_text(f"stub {kind} for {json.dumps(job_ref)}\n")
        out.append({"kind": kind, "path": str(path), "mime": mime, "bytes": path.stat().st_size,
                    "duration": secs, "notebook_id": job_ref.get("notebook_id")})
    return out


def discard(job_ref: dict, cfg: dict) -> None:
    """Delete the notebook once artifacts are downloaded, unless configured to keep it."""
    return None
