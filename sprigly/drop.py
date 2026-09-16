"""The drop folder: what actually reaches the phone, and how it reports back.

`data/lessons/<id>/` holds the originals and is never touched by delivery. `data/drop/<slug>/` is a
projection of it — the only directory Syncthing mirrors. Deleting a projected file is the signal
that the lesson was consumed, so only the projection is ever removed; the originals stay.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import store
from .store import TS, utcnow

log = logging.getLogger(__name__)
NOTES = "notes.md"


def slug(lesson_id: int, topic: str) -> str:
    """Zero-padded id first, so the folder sorts in the order lessons were learned."""
    words = re.sub(r"[^a-z0-9]+", "-", (topic or "").lower()).strip("-")[:60]
    return f"{lesson_id:04d}-{words}" if words else f"{lesson_id:04d}"


def folder(cfg: dict, lesson_id: int, topic: str) -> Path:
    return cfg["paths"]["drop"] / slug(lesson_id, topic)


def notes(conn, cfg: dict, lesson_id: int) -> str:
    """What the lesson was built from, readable on a phone with no app but a text viewer."""
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


def _level_index(level: str) -> int:
    from .gate import LEVEL_ORDER

    return LEVEL_ORDER.index(level) if level in LEVEL_ORDER else 0


def _link(src: Path, dst: Path) -> None:
    """Hard link where the filesystem allows it, copy when it does not.

    A link costs no disk for what may be a 40MB video, and deleting the projection still leaves the
    original intact — the two names simply point at the same data.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def project(conn, cfg: dict, lesson_id: int) -> Path:
    """Publish a ready lesson into the drop folder."""
    row = conn.execute("SELECT topic FROM lesson WHERE id=?", (lesson_id,)).fetchone()
    out = folder(cfg, lesson_id, row["topic"])
    out.mkdir(parents=True, exist_ok=True)
    for a in conn.execute("SELECT kind, path FROM artifact WHERE lesson_id=?", (lesson_id,)):
        src = Path(a["path"])
        if src.exists():
            _link(src, out / src.name)
    (out / NOTES).write_text(notes(conn, cfg, lesson_id))
    store.log_event(conn, "dropped", lesson_id, json.dumps({"folder": str(out)}))
    return out


def media_left(cfg: dict, lesson_id: int, topic: str) -> int:
    """Projected files still present, not counting the notes.

    The notes alone do not keep a lesson open: a podcast app that deletes after playback leaves
    them behind, and that is exactly the case that should count as consumed.
    """
    out = folder(cfg, lesson_id, topic)
    if not out.is_dir():
        return 0
    return sum(1 for p in out.iterdir() if p.is_file() and p.name != NOTES)


def prune_video(conn, cfg: dict) -> int:
    """Drop the video once it is old enough. Audio and slides are small and stay indefinitely.

    Writes a `pruned` event so the removal is never mistaken for someone consuming the lesson.
    """
    days = cfg["delivery"]["retention_video_days"]
    if not days:
        return 0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(TS)
    rows = conn.execute(
        "SELECT a.id, a.lesson_id, a.path, l.topic FROM artifact a JOIN lesson l ON l.id=a.lesson_id"
        " WHERE a.kind='video' AND a.created_at < ?", (cutoff,)).fetchall()
    for r in rows:
        Path(r["path"]).unlink(missing_ok=True)
        dropped = folder(cfg, r["lesson_id"], r["topic"]) / Path(r["path"]).name
        dropped.unlink(missing_ok=True)
        conn.execute("DELETE FROM artifact WHERE id=?", (r["id"],))
        store.log_event(conn, "pruned", r["lesson_id"], json.dumps({"kind": "video"}))
        log.info("pruned the video for lesson %s", r["lesson_id"])
    return len(rows)


def consumed(conn, cfg: dict, lesson_id: int, topic: str) -> bool:
    """Has everything projected for this lesson been deleted?"""
    was_dropped = conn.execute(
        "SELECT 1 FROM event WHERE lesson_id=? AND kind='dropped' LIMIT 1", (lesson_id,)).fetchone()
    return bool(was_dropped) and media_left(cfg, lesson_id, topic) == 0


def _selfcheck() -> None:
    import tempfile

    from . import config

    assert slug(3, "How RBF-FD builds a stencil!") == "0003-how-rbf-fd-builds-a-stencil"
    assert slug(12, "") == "0012"
    assert slug(7, "a" * 200).startswith("0007-") and len(slug(7, "a" * 200)) <= 66

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = config.load(path=root / "absent.toml", root=root)
        conn = store.connect(cfg["paths"]["db"])

        lid = conn.execute(
            "INSERT INTO lesson (topic, domain, state, depth) VALUES"
            " ('how rbf-fd builds a stencil','numerics','ready',3)").lastrowid
        sid = conn.execute(
            "INSERT INTO source (url, title, year, tier, evidence_level)"
            " VALUES ('https://example.org/a','A stencil paper',2017,'A','peer-reviewed')").lastrowid
        conn.execute("INSERT INTO lesson_source VALUES (?,?)", (lid, sid))
        sid2 = conn.execute(
            "INSERT INTO source (url, title, tier, evidence_level)"
            " VALUES ('https://example.org/b','A preprint','A-','preprint')").lastrowid
        conn.execute("INSERT INTO lesson_source VALUES (?,?)", (lid, sid2))

        lesson_dir = cfg["paths"]["lessons"] / str(lid)
        lesson_dir.mkdir(parents=True)
        (lesson_dir / "brief.md").write_text("framing for the podcast")
        for name, kind in (("podcast.m4a", "audio"), ("slides.pdf", "slides")):
            (lesson_dir / name).write_text(name)
            conn.execute("INSERT INTO artifact (lesson_id, kind, path) VALUES (?,?,?)",
                         (lid, kind, str(lesson_dir / name)))

        assert not consumed(conn, cfg, lid, "how rbf-fd builds a stencil"), \
            "a lesson that was never dropped is not consumed"

        out = project(conn, cfg, lid)
        assert out.name == "0001-how-rbf-fd-builds-a-stencil"
        assert (out / "podcast.m4a").exists() and (out / "slides.pdf").exists()
        text = (out / NOTES).read_text()
        assert "A stencil paper" in text and "https://example.org/a" in text
        assert "Evidence: preprint" in text, "the notes report the weakest source, like the gate"
        assert "framing for the podcast" in text

        topic = "how rbf-fd builds a stencil"
        assert media_left(cfg, lid, topic) == 2
        assert not consumed(conn, cfg, lid, topic)

        (out / "podcast.m4a").unlink()
        assert media_left(cfg, lid, topic) == 1, "deleting one file is not finishing the lesson"
        assert not consumed(conn, cfg, lid, topic)
        assert (lesson_dir / "podcast.m4a").exists(), "the original survives the projection's death"

        (out / "slides.pdf").unlink()
        assert consumed(conn, cfg, lid, topic), "notes alone do not keep a lesson open"

        shutil.rmtree(out)
        assert consumed(conn, cfg, lid, topic), "a folder removed wholesale counts too"

        # Pruning a video is housekeeping, and must not read as consumption.
        l2 = conn.execute("INSERT INTO lesson (topic, state) VALUES ('old','ready')").lastrowid
        d2 = cfg["paths"]["lessons"] / str(l2)
        d2.mkdir(parents=True)
        for name, kind in (("podcast.m4a", "audio"), ("video.mp4", "video")):
            (d2 / name).write_text(name)
            conn.execute("INSERT INTO artifact (lesson_id, kind, path, created_at)"
                         " VALUES (?,?,?,?)", (l2, kind, str(d2 / name), "2020-01-01T00:00:00Z"))
        project(conn, cfg, l2)
        assert media_left(cfg, l2, "old") == 2
        assert prune_video(conn, cfg) == 0, "retention of 0 days means never prune"
        cfg["delivery"]["retention_video_days"] = 30
        assert prune_video(conn, cfg) == 1
        assert media_left(cfg, l2, "old") == 1, "the video goes, the audio stays"
        assert not (d2 / "video.mp4").exists() and (d2 / "podcast.m4a").exists()
        assert not consumed(conn, cfg, l2, "old"), "pruning is not consuming"
        kinds = [r[0] for r in conn.execute(
            "SELECT kind FROM event WHERE lesson_id=? ORDER BY id", (l2,))]
        assert "pruned" in kinds

        assert prune_video(conn, cfg) == 0, "nothing left to prune"
        conn.close()

    print("drop selfcheck ok")


if __name__ == "__main__":
    _selfcheck()
