"""Human-facing names for lessons and tracks.

Integers are a lookup table you have to keep in your head. `meshless.rbf-fd-stencils` is not.

The integer primary key stays — it is stable, foreign keys point at it, and renaming a topic must
not break the graph — so a slug is a second name resolved on the way in, never a replacement.
"""

from __future__ import annotations

import re
import sqlite3

# Dropped from slugs: they carry no identity and make every name longer.
FILLER = {
    "a", "an", "the", "and", "or", "but", "of", "in", "on", "at", "to", "for", "from", "with",
    "by", "as", "is", "are", "was", "were", "be", "been", "it", "its", "this", "that", "these",
    "those", "how", "why", "what", "when", "where", "which", "who", "does", "do", "did", "can",
    "into", "onto", "about", "over", "under", "between", "within", "via", "using", "use", "uses",
    "they", "them", "their", "we", "you", "our", "there", "here", "so", "than", "then", "not",
}


def words(text: str) -> list[str]:
    return [w for w in re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).split() if w]


def condense(text: str, limit: int = 4, drop: set[str] | None = None) -> str:
    """A short readable name: filler removed, at most `limit` words.

    Filler is dropped only when something survives it — an all-filler phrase keeps its words rather
    than condensing to nothing. `drop` removes words the context already supplies, so a lesson
    inside a track is not named after the track twice.
    """
    parts = words(text)
    meaningful = [w for w in parts if w not in FILLER] or parts
    if drop:
        trimmed = [w for w in meaningful if w not in drop]
        meaningful = trimmed or meaningful
    return "-".join(meaningful[:limit])


def unique(conn: sqlite3.Connection, table: str, candidate: str, exclude_id: int | None = None) -> str:
    """Append a counter until the slug is free. Collisions are rare and must still be handled."""
    base, n = candidate or table, 1
    slug = base
    while True:
        row = conn.execute(f"SELECT id FROM {table} WHERE slug=?", (slug,)).fetchone()
        if row is None or row["id"] == exclude_id:
            return slug
        n += 1
        slug = f"{base}-{n}"


def for_track(conn: sqlite3.Connection, goal: str, track_id: int | None = None) -> str:
    return unique(conn, "track", condense(goal, 2), track_id)


def for_lesson(conn: sqlite3.Connection, topic: str, track_id: int | None,
               lesson_id: int | None = None) -> str:
    """`track.topic` where the lesson belongs to a track, plain `topic` where it does not."""
    prefix, drop = "", None
    if track_id:
        row = conn.execute("SELECT slug FROM track WHERE id=?", (track_id,)).fetchone()
        if row and row["slug"]:
            prefix = f"{row['slug']}."
            drop = set(row["slug"].split("-"))
    return unique(conn, "lesson", prefix + condense(topic, drop=drop), lesson_id)


def backfill(conn: sqlite3.Connection) -> int:
    """Name anything that predates slugs. Tracks first, so lessons can be prefixed."""
    filled = 0
    for row in conn.execute("SELECT id, goal FROM track WHERE slug IS NULL").fetchall():
        conn.execute("UPDATE track SET slug=? WHERE id=?",
                     (for_track(conn, row["goal"], row["id"]), row["id"]))
        filled += 1
    for row in conn.execute("SELECT id, topic, track_id FROM lesson WHERE slug IS NULL").fetchall():
        conn.execute("UPDATE lesson SET slug=? WHERE id=?",
                     (for_lesson(conn, row["topic"], row["track_id"], row["id"]), row["id"]))
        filled += 1
    return filled


class Ambiguous(LookupError):
    def __init__(self, ref: str, matches: list[tuple[int, str]]):
        self.matches = matches
        super().__init__(
            f"{ref!r} matches {len(matches)}: "
            + ", ".join(slug for _id, slug in matches[:6])
            + (" ..." if len(matches) > 6 else ""))


def resolve(conn: sqlite3.Connection, ref: str, table: str = "lesson") -> int:
    """Turn whatever the user typed into a row id.

    Accepts the integer id, the exact slug, a unique prefix of one, or a unique substring. An
    ambiguous reference is an error naming the candidates — picking one silently is how you end up
    grading the wrong lesson.
    """
    ref = str(ref).strip()
    if ref.isdigit():
        row = conn.execute(f"SELECT id FROM {table} WHERE id=?", (int(ref),)).fetchone()
        if row:
            return row["id"]
        raise LookupError(f"no {table} {ref}")

    exact = conn.execute(f"SELECT id FROM {table} WHERE slug=?", (ref,)).fetchone()
    if exact:
        return exact["id"]

    for pattern in (ref + "%", "%" + ref + "%"):
        rows = conn.execute(
            f"SELECT id, slug FROM {table} WHERE slug LIKE ? ORDER BY id", (pattern,)).fetchall()
        if len(rows) == 1:
            return rows[0]["id"]
        if len(rows) > 1:
            raise Ambiguous(ref, [(r["id"], r["slug"]) for r in rows])
    raise LookupError(f"no {table} matching {ref!r}")


def _selfcheck() -> None:
    import tempfile
    from pathlib import Path

    from . import config, store

    assert condense("How RBF-FD builds a stencil") == "rbf-fd-builds-stencil"
    assert condense("why meshless methods exist and where they fit") == "meshless-methods-exist-fit"
    assert condense("why meshless methods exist and where they fit",
                    drop={"meshless", "methods"}) == "exist-fit", "the track is not repeated"
    assert condense("meshless methods", 2) == "meshless-methods"
    assert condense("why it works") == "works", "filler goes when something survives"
    assert condense("how does it do that") == "how-does-it-do", \
        "an all-filler phrase keeps its words rather than condensing to nothing"
    assert condense("") == ""

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = config.load(path=root / "absent.toml", root=root)
        conn = store.connect(cfg["paths"]["db"])

        t = conn.execute("INSERT INTO track (goal) VALUES ('meshless methods')").lastrowid
        for topic, track in (("why meshless methods exist and where they fit", t),
                             ("rbf-fd stencil construction", t),
                             ("the IKEA effect in consumer behavior", None)):
            conn.execute("INSERT INTO lesson (topic, track_id) VALUES (?,?)", (topic, track))
        assert backfill(conn) == 4, "tracks and lessons both get named"
        assert backfill(conn) == 0, "and only once"

        slugs = {r["id"]: r["slug"] for r in conn.execute("SELECT id, slug FROM lesson")}
        assert slugs[1] == "meshless-methods.exist-fit", \
            f"a lesson is not named after its track twice: {slugs[1]}"
        assert slugs[3] == "ikea-effect-consumer-behavior", "untracked lessons do not"
        assert conn.execute("SELECT slug FROM track").fetchone()[0] == "meshless-methods"

        assert resolve(conn, "1") == 1, "integers still work"
        assert resolve(conn, slugs[2]) == 2, "so does the exact slug"
        assert resolve(conn, "rbf") == 2, "and a unique substring"
        assert resolve(conn, "meshless-methods.rbf") == 2, "and a prefix"
        assert resolve(conn, "meshless-methods", "track") == t

        try:
            resolve(conn, "meshless")
            raise AssertionError("an ambiguous reference must not silently pick one")
        except Ambiguous as err:
            assert len(err.matches) == 2 and "matches 2" in str(err)

        for missing in ("999", "nothing-like-this"):
            try:
                resolve(conn, missing)
                raise AssertionError(f"{missing} should not resolve")
            except LookupError:
                pass

        # A second lesson with the same condensed name gets a counter, not a collision.
        conn.execute("INSERT INTO lesson (topic) VALUES ('the IKEA effect in consumer behavior')")
        backfill(conn)
        both = [r["slug"] for r in conn.execute(
            "SELECT slug FROM lesson WHERE slug LIKE 'ikea%' ORDER BY id")]
        assert both == ["ikea-effect-consumer-behavior", "ikea-effect-consumer-behavior-2"], both
        conn.close()

    print("refs selfcheck ok")


if __name__ == "__main__":
    _selfcheck()
