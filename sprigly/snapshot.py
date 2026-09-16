"""Everything the picker is allowed to know, read in one pass.

Scoring is pure: it never touches the database. This module is the only bridge between the two, so
the weights stay fittable offline against replayed history.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from .store import utcnow


@dataclass(frozen=True)
class Candidate:
    """A lesson the picker may offer. Everything scoring is allowed to see about it."""

    id: int
    topic: str
    domain: str
    tags: frozenset[str] = frozenset()
    prereqs: frozenset[str] = frozenset()
    est_minutes: int = 20
    track_id: int | None = None
    evidence_level: str | None = None


@dataclass
class Snapshot:
    now: str
    mastered_tags: set[str] = field(default_factory=set)
    due_tags: set[str] = field(default_factory=set)
    # Tags the learner has actually met, mastered or not. The prerequisite gate needs this to tell
    # "you tried this and it has lapsed" apart from "you have never been near this".
    seen_tags: set[str] = field(default_factory=set)
    domain_last_seen: dict[str, str] = field(default_factory=dict)
    track_last_lesson: dict[int, str] = field(default_factory=dict)
    offers: dict[str, int] = field(default_factory=dict)
    picks: dict[str, int] = field(default_factory=dict)
    budget_minutes: int = 20
    focus_track_ids: set[int] = field(default_factory=set)
    focus_exclusive: bool = False
    # Change in quiz success rate per domain. Empty until the feedback loop exists; a signal that
    # is constant across the pool contributes nothing, so an empty dict is a safe default.
    progress: dict[str, float] = field(default_factory=dict)
    min_evidence: dict[int, str] = field(default_factory=dict)


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

SEEN = """
SELECT DISTINCT lt.tag FROM lesson_tag lt JOIN lesson l ON l.id = lt.lesson_id
WHERE lt.kind = 'tag' AND l.state = 'reviewed'
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

# Grades in order, with the domain they belong to. Everything else is derived in Python: the
# windowing is easier to read there and the volume is tiny.
GRADES = """
SELECT l.domain AS k, e.payload AS payload FROM event e JOIN lesson l ON l.id = e.lesson_id
WHERE e.kind = 'graded' AND l.domain IS NOT NULL ORDER BY e.id
"""

BY_DOMAIN = """
SELECT l.domain AS k, COUNT(*) AS n FROM event e JOIN lesson l ON l.id = e.lesson_id
WHERE e.kind = ? AND l.domain IS NOT NULL GROUP BY l.domain
"""


# A grade of Again is a lapse. Hard was still recalled, so it counts as a success — the distinction
# between Hard and Good belongs to the scheduler, not to whether you knew it.
LAPSE = "Again"


def progress_by_domain(conn: sqlite3.Connection, window: int) -> dict[str, float]:
    """Change in quiz success rate per domain, as a 0-1 signal centred on 0.5.

    ZPDES rewards learning *progress* — the derivative — rather than competence. A domain you have
    mastered stops being rewarding, and so does one you keep failing; what attracts effort is the
    place where the success rate is still moving. Absolute mastery is already handled elsewhere,
    by fsrs deciding which tags are overdue.

    A domain without enough history is absent from the result rather than being reported as zero:
    "no evidence" and "getting worse" must not look the same to the scorer.
    """
    import json

    grades: dict[str, list[int]] = {}
    for row in conn.execute(GRADES):
        try:
            rating = (json.loads(row["payload"]) or {}).get("rating")
        except (TypeError, ValueError):
            continue
        if rating:
            grades.setdefault(row["k"], []).append(int(rating != LAPSE))

    out: dict[str, float] = {}
    for domain, marks in grades.items():
        if len(marks) < window + 1:
            continue
        recent = marks[-window:]
        earlier = marks[-2 * window:-window] or marks[:-window]
        if not earlier:
            continue
        delta = (sum(recent) / len(recent)) - (sum(earlier) / len(earlier))
        out[domain] = (delta + 1.0) / 2.0
    return out


def load(conn: sqlite3.Connection, cfg: dict, now: str | None = None) -> Snapshot:
    now = now or utcnow()
    tracks = conn.execute("SELECT id, exclusive FROM track WHERE active = 1").fetchall()
    return Snapshot(
        now=now,
        mastered_tags={r[0] for r in conn.execute(MASTERED, (now,))},
        due_tags={r[0] for r in conn.execute(DUE, (now,))},
        seen_tags={r[0] for r in conn.execute(SEEN)},
        domain_last_seen={r["k"]: r["t"] for r in conn.execute(LAST_SEEN.format(col="domain"))},
        track_last_lesson={r["k"]: r["t"] for r in conn.execute(LAST_SEEN.format(col="track_id"))},
        offers={r["k"]: r["n"] for r in conn.execute(BY_DOMAIN, ("offered",))},
        picks={r["k"]: r["n"] for r in conn.execute(BY_DOMAIN, ("picked",))},
        budget_minutes=cfg["lesson"]["budget_minutes"],
        min_evidence={r["id"]: r["min_evidence"] for r in
                      conn.execute("SELECT id, min_evidence FROM track")},
        focus_track_ids={r["id"] for r in tracks},
        focus_exclusive=any(r["exclusive"] for r in tracks),
        progress=progress_by_domain(conn, cfg["scoring"]["progress_window"]),
    )


CANDIDATE_COLS = """
SELECT l.id, l.topic, COALESCE(l.domain,'') AS domain, l.est_minutes, l.track_id,
       (SELECT group_concat(tag) FROM lesson_tag WHERE lesson_id=l.id AND kind='tag')  AS tags,
       (SELECT group_concat(tag) FROM lesson_tag WHERE lesson_id=l.id AND kind='prereq') AS prereqs,
       (SELECT s.evidence_level FROM source s JOIN lesson_source ls ON ls.source_id=s.id
        WHERE ls.lesson_id=l.id ORDER BY s.tier LIMIT 1) AS evidence_level
FROM lesson l
"""


def _to_candidate(r: sqlite3.Row) -> Candidate:
    return Candidate(
        id=r["id"], topic=r["topic"], domain=r["domain"],
        tags=frozenset((r["tags"] or "").split(",")) - {""},
        prereqs=frozenset((r["prereqs"] or "").split(",")) - {""},
        est_minutes=r["est_minutes"] or 20,
        track_id=r["track_id"], evidence_level=r["evidence_level"],
    )


def candidates(conn: sqlite3.Connection) -> list[Candidate]:
    """Lessons awaiting a pick."""
    return [_to_candidate(r) for r in conn.execute(CANDIDATE_COLS + " WHERE l.state='proposed'")]


def due_reviews(conn: sqlite3.Connection, now: str | None = None) -> list[Candidate]:
    """Lessons already learned whose cards have come due. A separate pool, not a candidate."""
    now = now or utcnow()
    return [_to_candidate(r) for r in conn.execute(
        CANDIDATE_COLS + " WHERE l.state='reviewed' AND EXISTS ("
        " SELECT 1 FROM card c WHERE c.lesson_id=l.id AND c.due IS NOT NULL AND c.due<=?)", (now,))]


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

        # Learning progress: the derivative, not the level.
        import json as _j

        def graded(lesson, ratings):
            for r in ratings:
                conn.execute("INSERT INTO event (lesson_id, kind, payload) VALUES (?,'graded',?)",
                             (lesson, _j.dumps({"rating": r})))

        improving = lesson("optimisation", "operations-research", "reviewed")
        graded(improving, ["Again", "Again", "Again", "Good", "Good", "Good"])
        plateaued = lesson("regression", "statistics", "reviewed")
        graded(plateaued, ["Good"] * 6)
        slipping = lesson("topology", "mathematics", "reviewed")
        graded(slipping, ["Good", "Good", "Good", "Again", "Again", "Again"])

        prog = progress_by_domain(conn, 3)
        assert prog["operations-research"] > 0.5, "a domain getting better attracts effort"
        assert abs(prog["statistics"] - 0.5) < 1e-9, "a mastered domain is neutral, not attractive"
        assert prog["mathematics"] < 0.5, "a domain slipping is not where progress is"
        assert "numerics" not in prog, "a domain with no grades is absent, not zero"

        thin_history = lesson("sheaves", "category-theory", "reviewed")
        graded(thin_history, ["Good", "Good"])
        assert "category-theory" not in progress_by_domain(conn, 3), \
            "too little history is no evidence"

        cfg["scoring"]["progress_window"] = 3
        assert load(conn, cfg, now=now).progress == prog, \
            "the snapshot carries it, at the configured window"
        conn.close()
    print("snapshot selfcheck ok")


if __name__ == "__main__":
    _selfcheck()
