"""Everything the picker is allowed to know, read in one pass.

Scoring is pure: it never touches the database. This module is the only bridge between the two, so
the weights stay fittable offline against replayed history.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from .store import utcnow


@dataclass
class Snapshot:
    now: str
    mastered_tags: set[str] = field(default_factory=set)
    due_tags: set[str] = field(default_factory=set)
    domain_last_seen: dict[str, str] = field(default_factory=dict)
    track_last_lesson: dict[int, str] = field(default_factory=dict)
    offers: dict[str, int] = field(default_factory=dict)
    picks: dict[str, int] = field(default_factory=dict)
    budget_minutes: int = 20
    focus_track_ids: set[int] = field(default_factory=set)
    focus_exclusive: bool = False


# A tag counts as mastered while at least one card carrying it is still not overdue. Decay comes
# free from fsrs that way — no second forgetting model. A reviewed lesson that produced no cards
# stays mastered, since nothing contradicts it.
MASTERED = """
SELECT DISTINCT lt.tag FROM lesson_tag lt JOIN lesson l ON l.id = lt.lesson_id
WHERE lt.kind = 'tag' AND l.state = 'reviewed' AND (
    EXISTS (SELECT 1 FROM card c WHERE c.lesson_id = l.id AND (c.due IS NULL OR c.due > ?))
    OR NOT EXISTS (SELECT 1 FROM card c WHERE c.lesson_id = l.id)
)
"""

DUE = """
SELECT DISTINCT lt.tag FROM lesson_tag lt JOIN card c ON c.lesson_id = lt.lesson_id
WHERE lt.kind = 'tag' AND c.due IS NOT NULL AND c.due <= ?
"""

# 'ready' is when a lesson was actually taught. lesson.updated_at would drift on any later write.
LAST_SEEN = """
SELECT l.{col} AS k, MAX(e.created_at) AS t FROM event e JOIN lesson l ON l.id = e.lesson_id
WHERE e.kind = 'ready' AND l.{col} IS NOT NULL GROUP BY l.{col}
"""

BY_DOMAIN = """
SELECT l.domain AS k, COUNT(*) AS n FROM event e JOIN lesson l ON l.id = e.lesson_id
WHERE e.kind = ? AND l.domain IS NOT NULL GROUP BY l.domain
"""


def load(conn: sqlite3.Connection, cfg: dict, now: str | None = None) -> Snapshot:
    now = now or utcnow()
    tracks = conn.execute("SELECT id, exclusive FROM track WHERE active = 1").fetchall()
    return Snapshot(
        now=now,
        mastered_tags={r[0] for r in conn.execute(MASTERED, (now,))},
        due_tags={r[0] for r in conn.execute(DUE, (now,))},
        domain_last_seen={r["k"]: r["t"] for r in conn.execute(LAST_SEEN.format(col="domain"))},
        track_last_lesson={r["k"]: r["t"] for r in conn.execute(LAST_SEEN.format(col="track_id"))},
        offers={r["k"]: r["n"] for r in conn.execute(BY_DOMAIN, ("offered",))},
        picks={r["k"]: r["n"] for r in conn.execute(BY_DOMAIN, ("picked",))},
        budget_minutes=cfg["lesson"]["budget_minutes"],
        focus_track_ids={r["id"] for r in tracks},
        focus_exclusive=any(r["exclusive"] for r in tracks),
    )


def _selfcheck() -> None:
    import tempfile
    from pathlib import Path

    from . import config, store

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = config.load(path=root / "absent.toml", root=root)
        conn = store.connect(cfg["paths"]["db"])
        now = "2026-09-16T12:00:00Z"
        past, future = "2026-09-01T00:00:00Z", "2026-12-01T00:00:00Z"

        t = conn.execute("INSERT INTO track (goal, active) VALUES ('meshless methods', 1)").lastrowid
        def lesson(topic, domain, state, track=None):
            return conn.execute(
                "INSERT INTO lesson (topic, domain, state, track_id) VALUES (?,?,?,?)",
                (topic, domain, state, track)).lastrowid
        def tag(lid, t_, kind="tag"):
            conn.execute("INSERT INTO lesson_tag VALUES (?,?,?)", (lid, t_, kind))

        fresh = lesson("rbf-fd stencils", "numerics", "reviewed", t)
        tag(fresh, "radial-basis-functions")
        conn.execute("INSERT INTO card (lesson_id, track_id, front, back, front_hash, due)"
                     " VALUES (?,?,'q','a','h1',?)", (fresh, t, future))

        stale = lesson("sph kernels", "numerics", "reviewed", t)
        tag(stale, "smoothed-particle-hydrodynamics")
        conn.execute("INSERT INTO card (lesson_id, track_id, front, back, front_hash, due)"
                     " VALUES (?,?,'q','a','h2',?)", (stale, t, past))

        cardless = lesson("finite elements", "numerics", "reviewed")
        tag(cardless, "finite-element-method")

        snap = load(conn, cfg, now=now)
        assert "radial-basis-functions" in snap.mastered_tags
        assert "smoothed-particle-hydrodynamics" not in snap.mastered_tags, "an overdue card ends mastery"
        assert "smoothed-particle-hydrodynamics" in snap.due_tags
        assert "finite-element-method" in snap.mastered_tags, "no cards means nothing contradicts mastery"
        assert "radial-basis-functions" not in snap.due_tags

        # A lesson with one overdue and one fresh card still counts as mastered.
        conn.execute("INSERT INTO card (lesson_id, track_id, front, back, front_hash, due)"
                     " VALUES (?,?,'q2','a','h3',?)", (stale, t, future))
        assert "smoothed-particle-hydrodynamics" in load(conn, cfg, now=now).mastered_tags

        # Recency comes from the 'ready' event, not from lesson.updated_at.
        taught = lesson("cash flow", "business", "consumed")
        conn.execute("INSERT INTO event (lesson_id, kind, created_at) VALUES (?,'ready',?)",
                     (taught, past))
        conn.execute("UPDATE lesson SET updated_at=? WHERE id=?", (now, taught))
        snap = load(conn, cfg, now=now)
        assert snap.domain_last_seen["business"] == past, "a later write must not reset recency"
        assert "numerics" not in snap.domain_last_seen, "never-taught domains are absent"

        conn.execute("INSERT INTO event (lesson_id, kind, created_at) VALUES (?,'ready',?)",
                     (fresh, now))
        assert load(conn, cfg, now=now).track_last_lesson[t] == now

        for kind, lid in (("offered", fresh), ("offered", stale), ("picked", fresh),
                          ("offered", taught)):
            store.log_event(conn, kind, lid)
        snap = load(conn, cfg, now=now)
        assert snap.offers == {"numerics": 2, "business": 1}
        assert snap.picks == {"numerics": 1}

        assert snap.focus_track_ids == {t} and not snap.focus_exclusive
        conn.execute("UPDATE track SET exclusive=1 WHERE id=?", (t,))
        t2 = conn.execute("INSERT INTO track (goal, active) VALUES ('game theory', 1)").lastrowid
        snap = load(conn, cfg, now=now)
        assert snap.focus_track_ids == {t, t2}, "several tracks may be active at once"
        assert snap.focus_exclusive, "one exclusive active track makes the offer exclusive"

        conn.execute("UPDATE track SET active=0")
        assert load(conn, cfg, now=now).focus_track_ids == set()

        assert snap.budget_minutes == cfg["lesson"]["budget_minutes"]
        conn.close()
    print("snapshot selfcheck ok")


if __name__ == "__main__":
    _selfcheck()
