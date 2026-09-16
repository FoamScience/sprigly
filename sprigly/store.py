"""SQLite store: schema, forward-only migrations, connection setup.

The database is also the queue. `lesson.state` drives the whole pipeline, so it carries a CHECK
constraint: a typo in a state name should fail at the write, not silently strand a lesson.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

TS = "%Y-%m-%dT%H:%M:%SZ"


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime(TS)

STATES = (
    "proposed", "picked", "harvesting", "uploading", "generating",
    "ready", "consumed", "reviewed",
    "expired", "unsourced", "failed",  # terminal
)
TERMINAL = {"expired", "unsourced", "failed", "reviewed"}

_STATE_LIST = ", ".join(f"'{s}'" for s in STATES)
NOW = "strftime('%Y-%m-%dT%H:%M:%SZ','now')"

# Forward-only. Append a new (version, sql) pair; never edit a shipped one.
MIGRATIONS: list[tuple[int, str]] = [
    (1, f"""
    CREATE TABLE track (
        id           INTEGER PRIMARY KEY,
        goal         TEXT NOT NULL,
        status       TEXT NOT NULL DEFAULT 'open',
        depth        INTEGER NOT NULL DEFAULT 3 CHECK (depth BETWEEN 1 AND 5),
        active       INTEGER NOT NULL DEFAULT 0,
        exclusive    INTEGER NOT NULL DEFAULT 0,
        min_evidence TEXT NOT NULL DEFAULT 'peer-reviewed',
        language     TEXT NOT NULL DEFAULT 'en',
        created_at   TEXT NOT NULL DEFAULT ({NOW}),
        updated_at   TEXT NOT NULL DEFAULT ({NOW})
    );

    CREATE TABLE lesson (
        id              INTEGER PRIMARY KEY,
        topic           TEXT NOT NULL,
        domain          TEXT,
        track_id        INTEGER REFERENCES track(id) ON DELETE SET NULL,
        parent_id       INTEGER REFERENCES lesson(id) ON DELETE SET NULL,
        depth           INTEGER NOT NULL DEFAULT 3 CHECK (depth BETWEEN 1 AND 5),
        state           TEXT NOT NULL DEFAULT 'proposed' CHECK (state IN ({_STATE_LIST})),
        score           REAL,
        language        TEXT NOT NULL DEFAULT 'en',
        est_minutes     INTEGER,
        actual_minutes  INTEGER,
        thin            INTEGER NOT NULL DEFAULT 0,
        notebook_id     TEXT,
        job_ref         TEXT,
        polled_at       TEXT,
        retry_count     INTEGER NOT NULL DEFAULT 0,
        last_error      TEXT,
        next_attempt_at TEXT,
        created_at      TEXT NOT NULL DEFAULT ({NOW}),
        updated_at      TEXT NOT NULL DEFAULT ({NOW})
    );
    CREATE INDEX lesson_state_idx ON lesson(state, next_attempt_at);
    CREATE INDEX lesson_track_idx ON lesson(track_id);

    -- Tags and prerequisites share a table; `kind` separates them. Values are normalised on write
    -- (US English, lowercase, no abbreviations, hyphen-separated) because every scoring signal
    -- works by set intersection.
    CREATE TABLE lesson_tag (
        lesson_id INTEGER NOT NULL REFERENCES lesson(id) ON DELETE CASCADE,
        tag       TEXT NOT NULL,
        kind      TEXT NOT NULL CHECK (kind IN ('tag','prereq')),
        PRIMARY KEY (lesson_id, tag, kind)
    );
    CREATE INDEX lesson_tag_tag_idx ON lesson_tag(tag, kind);

    CREATE TABLE source (
        id             INTEGER PRIMARY KEY,
        url            TEXT NOT NULL,
        doi            TEXT,
        title          TEXT,
        venue          TEXT,
        year           INTEGER,
        work_type      TEXT,
        tier           TEXT NOT NULL CHECK (tier IN ('A','A-','B','C','M')),
        evidence_level TEXT NOT NULL,
        oa_status      TEXT,
        retracted      INTEGER NOT NULL DEFAULT 0,
        local_path     TEXT,
        created_at     TEXT NOT NULL DEFAULT ({NOW})
    );
    -- Global dedup: a paper harvested for a second lesson is referenced, not re-downloaded.
    CREATE UNIQUE INDEX source_url_idx ON source(url);
    CREATE UNIQUE INDEX source_doi_idx ON source(doi) WHERE doi IS NOT NULL;

    CREATE TABLE lesson_source (
        lesson_id INTEGER NOT NULL REFERENCES lesson(id) ON DELETE CASCADE,
        source_id INTEGER NOT NULL REFERENCES source(id) ON DELETE CASCADE,
        PRIMARY KEY (lesson_id, source_id)
    );

    CREATE TABLE artifact (
        id          INTEGER PRIMARY KEY,
        lesson_id   INTEGER NOT NULL REFERENCES lesson(id) ON DELETE CASCADE,
        kind        TEXT NOT NULL,
        path        TEXT NOT NULL,
        mime        TEXT,
        bytes       INTEGER,
        duration    REAL,
        notebook_id TEXT,
        created_at  TEXT NOT NULL DEFAULT ({NOW})
    );
    CREATE INDEX artifact_lesson_idx ON artifact(lesson_id);

    CREATE TABLE card (
        id         INTEGER PRIMARY KEY,
        lesson_id  INTEGER NOT NULL REFERENCES lesson(id) ON DELETE CASCADE,
        track_id   INTEGER REFERENCES track(id) ON DELETE SET NULL,
        front      TEXT NOT NULL,
        back       TEXT NOT NULL,
        front_hash TEXT NOT NULL,
        fsrs_state TEXT,
        due        TEXT,
        created_at TEXT NOT NULL DEFAULT ({NOW})
    );
    -- Deduped within a track. COALESCE because SQLite treats NULLs as distinct in a UNIQUE index,
    -- which would let trackless lessons accumulate duplicate cards.
    CREATE UNIQUE INDEX card_dedup_idx ON card(COALESCE(track_id, -1), front_hash);
    CREATE INDEX card_due_idx ON card(due);

    -- Append-only. The picker's only training data; nothing deletes from here.
    CREATE TABLE event (
        id         INTEGER PRIMARY KEY,
        lesson_id  INTEGER REFERENCES lesson(id) ON DELETE SET NULL,
        kind       TEXT NOT NULL,
        payload    TEXT,
        created_at TEXT NOT NULL DEFAULT ({NOW})
    );
    CREATE INDEX event_kind_idx ON event(kind, created_at);
    CREATE INDEX event_lesson_idx ON event(lesson_id);
    """),
]

SCHEMA_VERSION = MIGRATIONS[-1][0]


def migrate(conn: sqlite3.Connection) -> int:
    """Apply every migration newer than the file's user_version. Idempotent."""
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for version, sql in MIGRATIONS:
        if version > current:
            conn.executescript(sql)
            conn.execute(f"PRAGMA user_version = {version}")
            current = version
    conn.commit()
    return current


def connect(path: Path | str) -> sqlite3.Connection:
    """Open the store, applying any pending migration. WAL so tick and the CLI can overlap."""
    path = Path(path)
    if path.parent != Path("."):
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    migrate(conn)
    return conn


def backup(conn: sqlite3.Connection, dest_dir: Path, keep: int = 7) -> Path:
    """Snapshot the database, keeping the newest `keep` copies.

    The event log is the picker's only training data and cannot be regenerated, which is the whole
    reason this exists.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    # Microseconds, not seconds: two backups in the same second must not land on one filename.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%f")
    out = dest_dir / f"sprigly-{stamp}.db"
    with sqlite3.connect(out) as target:
        conn.backup(target)
    stale = sorted(dest_dir.glob("sprigly-*.db"))[:-keep] if keep > 0 else []
    for old in stale:
        old.unlink()
    return out


def counts_by_state(conn: sqlite3.Connection) -> dict[str, int]:
    return {r["state"]: r["n"] for r in
            conn.execute("SELECT state, count(*) AS n FROM lesson GROUP BY state ORDER BY state")}


def troubled(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Lessons that failed outright, or are parked mid-retry."""
    return conn.execute(
        "SELECT id, topic, state, retry_count, next_attempt_at, last_error FROM lesson"
        " WHERE last_error IS NOT NULL AND state != 'reviewed' ORDER BY state, id").fetchall()


def due_cards(conn: sqlite3.Connection, now: str | None = None) -> tuple[int, str | None]:
    """How many cards are due, and when the next one comes up."""
    now = now or utcnow()
    n = conn.execute("SELECT count(*) FROM card WHERE due IS NOT NULL AND due <= ?",
                     (now,)).fetchone()[0]
    nxt = conn.execute("SELECT MIN(due) FROM card WHERE due > ?", (now,)).fetchone()[0]
    return n, nxt


def log_event(conn: sqlite3.Connection, kind: str, lesson_id: int | None = None,
              payload: str | None = None) -> None:
    conn.execute("INSERT INTO event (lesson_id, kind, payload) VALUES (?,?,?)",
                 (lesson_id, kind, payload))


def _selfcheck() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "nested" / "sprigly.db"
        conn = connect(db)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1

        assert migrate(conn) == SCHEMA_VERSION, "migrate must be idempotent"

        t = conn.execute("INSERT INTO track (goal) VALUES ('meshless methods')").lastrowid
        lid = conn.execute(
            "INSERT INTO lesson (topic, domain, track_id) VALUES ('rbf-fd stencils','numerics',?)",
            (t,)).lastrowid
        assert conn.execute("SELECT state FROM lesson WHERE id=?", (lid,)).fetchone()[0] == "proposed"

        try:
            conn.execute("UPDATE lesson SET state='halfway' WHERE id=?", (lid,))
            raise AssertionError("CHECK must reject an unknown state")
        except sqlite3.IntegrityError:
            pass

        try:
            conn.execute("INSERT INTO lesson (topic, track_id) VALUES ('x', 9999)")
            raise AssertionError("foreign keys must be enforced")
        except sqlite3.IntegrityError:
            pass

        sid = conn.execute(
            "INSERT INTO source (url, doi, tier, evidence_level) VALUES (?,?,'A','peer-reviewed')",
            ("https://example.org/a", "10.1/abc")).lastrowid
        conn.execute("INSERT INTO lesson_source VALUES (?,?)", (lid, sid))
        try:
            conn.execute("INSERT INTO source (url, doi, tier, evidence_level)"
                         " VALUES ('https://example.org/b','10.1/abc','A','peer-reviewed')")
            raise AssertionError("duplicate DOI must be rejected")
        except sqlite3.IntegrityError:
            pass
        # Two rows with no DOI are fine; only the URL must stay unique.
        conn.execute("INSERT INTO source (url, tier, evidence_level)"
                     " VALUES ('https://example.org/c','B','institutional')")
        conn.execute("INSERT INTO source (url, tier, evidence_level)"
                     " VALUES ('https://example.org/d','B','institutional')")

        conn.execute("INSERT INTO card (lesson_id, track_id, front, back, front_hash)"
                     " VALUES (?,?,'q','a','h1')", (lid, t))
        try:
            conn.execute("INSERT INTO card (lesson_id, track_id, front, back, front_hash)"
                         " VALUES (?,?,'q again','a','h1')", (lid, t))
            raise AssertionError("cards must dedup on front_hash within a track")
        except sqlite3.IntegrityError:
            pass

        # Trackless cards must dedup too, which a plain UNIQUE index would not do.
        l2 = conn.execute("INSERT INTO lesson (topic) VALUES ('trackless')").lastrowid
        conn.execute("INSERT INTO card (lesson_id, front, back, front_hash)"
                     " VALUES (?,'q','a','h2')", (l2,))
        try:
            conn.execute("INSERT INTO card (lesson_id, front, back, front_hash)"
                         " VALUES (?,'q','a','h2')", (l2,))
            raise AssertionError("NULL track_id must still dedup")
        except sqlite3.IntegrityError:
            pass

        log_event(conn, "proposed", lid, '{"by":"selfcheck"}')
        assert conn.execute("SELECT count(*) FROM event").fetchone()[0] == 1

        assert counts_by_state(conn) == {"proposed": 2}
        assert troubled(conn) == []
        conn.execute("UPDATE lesson SET state='failed', last_error='boom' WHERE id=?", (lid,))
        assert [r["id"] for r in troubled(conn)] == [lid]

        conn.execute("UPDATE card SET due='2020-01-01T00:00:00Z' WHERE front_hash='h1'")
        n, nxt = due_cards(conn)
        assert n == 1 and nxt is None, "nothing scheduled ahead yet"
        conn.execute("UPDATE card SET due='2099-01-01T00:00:00Z' WHERE front_hash='h2'")
        assert due_cards(conn) == (1, "2099-01-01T00:00:00Z")

        backups = Path(td) / "backups"
        for _ in range(3):
            backup(conn, backups, keep=2)
        kept = sorted(backups.glob("sprigly-*.db"))
        assert len(kept) == 2, "old backups are pruned, newest kept"
        restored = sqlite3.connect(kept[-1])
        assert restored.execute("SELECT count(*) FROM event").fetchone()[0] == 1, "backup is readable"
        restored.close()

        conn.execute("DELETE FROM lesson WHERE id=?", (lid,))
        assert conn.execute("SELECT count(*) FROM lesson_source").fetchone()[0] == 0, \
            "lesson_source must cascade"
        assert conn.execute("SELECT count(*) FROM source").fetchone()[0] == 3, \
            "sources outlive the lesson that found them"
        assert conn.execute("SELECT lesson_id FROM event").fetchone()[0] is None, \
            "events survive their lesson"

        conn.close()
    print("store selfcheck ok")


if __name__ == "__main__":
    _selfcheck()
