"""The lesson's own record of what it was built from.

Written next to the artifacts, so a lesson directory explains itself without the database.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import store
from .store import TS

log = logging.getLogger(__name__)
NOTES = "notes.md"


def _level_index(level: str) -> int:
    from .gate import LEVEL_ORDER

    return LEVEL_ORDER.index(level) if level in LEVEL_ORDER else 0


def render(conn, cfg: dict, lesson_id: int) -> str:
    """What the lesson was built from, in the order the gate ranked it."""
    row = conn.execute("SELECT * FROM lesson WHERE id=?", (lesson_id,)).fetchone()
    lines = [f"# {row['topic']}", ""]
    if row["domain"]:
        lines.append(f"{row['domain']} · depth {row['depth']}"
                     + (" · thin" if row["thin"] else ""))
        lines.append("")
    srcs = conn.execute(
        "SELECT s.tier, s.evidence_level, s.title, s.url, s.year FROM source s"
        " JOIN lesson_source ls ON ls.source_id=s.id WHERE ls.lesson_id=?"
        " ORDER BY s.tier, s.year DESC", (lesson_id,)).fetchall()
    if srcs:
        weakest = min((s["evidence_level"] for s in srcs), key=_level_index)
        lines += [f"Evidence: {weakest}", "", "## Sources", ""]
        for s in srcs:
            lines.append(f"- [{s['tier']}] {s['title'] or s['url']} ({s['year'] or 'n.d.'})")
            lines.append(f"  {s['url']}")
    brief = cfg["paths"]["lessons"] / str(lesson_id) / "brief.md"
    if brief.exists():
        lines += ["", "## Brief", "", brief.read_text().strip()]
    return "\n".join(lines) + "\n"


def write(conn, cfg: dict, lesson_id: int) -> Path:
    out = cfg["paths"]["lessons"] / str(lesson_id) / NOTES
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(conn, cfg, lesson_id))
    return out


def prune_video(conn, cfg: dict) -> int:
    """Drop the video once it is old enough. Audio and slides are small and stay indefinitely.

    Off by default (`retention_video_days` is 0). Writes a `pruned` event so the removal is
    distinguishable from anything the learner did.
    """
    days = cfg["delivery"]["retention_video_days"]
    if not days:
        return 0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(TS)
    rows = conn.execute(
        "SELECT id, lesson_id, path FROM artifact WHERE kind='video' AND created_at < ?",
        (cutoff,)).fetchall()
    for r in rows:
        Path(r["path"]).unlink(missing_ok=True)
        conn.execute("DELETE FROM artifact WHERE id=?", (r["id"],))
        store.log_event(conn, "pruned", r["lesson_id"], '{"kind": "video"}')
        log.info("pruned the video for lesson %s", r["lesson_id"])
    return len(rows)


def _selfcheck() -> None:
    import tempfile

    from . import config

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = config.load(path=root / "absent.toml", root=root)
        conn = store.connect(cfg["paths"]["db"])

        lid = conn.execute(
            "INSERT INTO lesson (topic, domain, state, depth) VALUES"
            " ('how rbf-fd builds a stencil','numerics','ready',3)").lastrowid
        for url, title, tier, level, year in (
                ("https://example.org/a", "A stencil paper", "A", "peer-reviewed", 2017),
                ("https://example.org/b", "A preprint", "A-", "preprint", None)):
            sid = conn.execute(
                "INSERT INTO source (url, title, year, tier, evidence_level) VALUES (?,?,?,?,?)",
                (url, title, year, tier, level)).lastrowid
            conn.execute("INSERT INTO lesson_source VALUES (?,?)", (lid, sid))

        d = cfg["paths"]["lessons"] / str(lid)
        d.mkdir(parents=True)
        (d / "brief.md").write_text("framing for the podcast")

        out = write(conn, cfg, lid)
        text = out.read_text()
        assert out.name == NOTES and out.parent == d, "notes live beside the artifacts"
        assert "A stencil paper" in text and "https://example.org/a" in text
        assert "Evidence: preprint" in text, "the notes report the weakest source, like the gate"
        assert "framing for the podcast" in text

        (d / "video.mp4").write_text("v")
        conn.execute("INSERT INTO artifact (lesson_id, kind, path, created_at) VALUES (?,?,?,?)",
                     (lid, "video", str(d / "video.mp4"), "2020-01-01T00:00:00Z"))
        assert prune_video(conn, cfg) == 0, "retention of 0 days means never prune"
        cfg["delivery"]["retention_video_days"] = 30
        assert prune_video(conn, cfg) == 1
        assert not (d / "video.mp4").exists()
        assert "pruned" in {r[0] for r in conn.execute(
            "SELECT kind FROM event WHERE lesson_id=?", (lid,))}
        conn.close()

    print("notes selfcheck ok")


if __name__ == "__main__":
    _selfcheck()
