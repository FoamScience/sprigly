"""The pipeline driver.

One pass advances every lesson at most one state. Safe to run from a timer: a lock keeps overlapping
runs out, and a failure parks its lesson in place rather than aborting the pass.

Failures park where they are, not in `failed`. A retryable error leaves the state untouched and sets
`next_attempt_at`; only exhausting `max_retries` moves a lesson to terminal `failed`. That way the
retry needs no record of which state to return to.
"""

from __future__ import annotations

import fcntl
import json
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import bridge, harvester, store
from .store import TS, utcnow


@contextmanager
def lock(path: Path):
    """Exclusive, non-blocking. Yields False when another pass already holds it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = path.open("w")
    try:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        fh.close()


def _set(conn, lesson_id: int, **fields) -> None:
    fields["updated_at"] = utcnow()
    cols = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE lesson SET {cols} WHERE id=?", (*fields.values(), lesson_id))


def _park(conn, row, cfg, err: Exception) -> str:
    """Record a failure. Back off, or give up once the retry budget is spent."""
    attempts = row["retry_count"] + 1
    limit = cfg["bridge"]["max_retries"]
    if attempts >= limit:
        _set(conn, row["id"], state="failed", retry_count=attempts, last_error=str(err))
        store.log_event(conn, "failed", row["id"], json.dumps({"error": str(err), "attempts": attempts}))
        return "failed"
    backoff = cfg["bridge"]["retry_backoff_seconds"] * 2 ** (attempts - 1)
    nxt = (datetime.now(timezone.utc) + timedelta(seconds=backoff)).strftime(TS)
    _set(conn, row["id"], retry_count=attempts, last_error=str(err), next_attempt_at=nxt)
    return row["state"]


def _lesson_dir(cfg, lesson_id: int) -> Path:
    return cfg["paths"]["lessons"] / str(lesson_id)


def _generated_today(conn) -> int:
    day = utcnow()[:10]
    return conn.execute(
        "SELECT count(*) FROM event WHERE kind='generated' AND created_at LIKE ?", (day + "%",)
    ).fetchone()[0]


# --- one function per state that has work to do ----------------------------------------------

def _do_picked(conn, row, cfg) -> str:
    _set(conn, row["id"], state="harvesting")
    return _do_harvesting(conn, conn.execute("SELECT * FROM lesson WHERE id=?", (row["id"],)).fetchone(), cfg)


def _do_harvesting(conn, row, cfg) -> str:
    dest = _lesson_dir(cfg, row["id"]) / "sources"
    found = harvester.gather(row["topic"], row["depth"], dest, cfg)
    for s in found:
        conn.execute(
            "INSERT OR IGNORE INTO source (url, doi, title, venue, year, work_type, tier,"
            " evidence_level, oa_status, retracted, local_path)"
            " VALUES (:url,:doi,:title,:venue,:year,:work_type,:tier,:evidence_level,"
            ":oa_status,:retracted,:local_path)",
            {"venue": None, "year": None, "work_type": None, "oa_status": None,
             "retracted": 0, "doi": None, "local_path": None, **s})
        sid = conn.execute("SELECT id FROM source WHERE url=?", (s["url"],)).fetchone()[0]
        conn.execute("INSERT OR IGNORE INTO lesson_source VALUES (?,?)", (row["id"], sid))

    budget = cfg["depth"][str(row["depth"])]["sources"]
    if len(found) < cfg["sources"]["min_sources"]:
        _set(conn, row["id"], state="unsourced")
        store.log_event(conn, "unsourced", row["id"], json.dumps({"found": len(found)}))
        return "unsourced"
    thin = int(len(found) < budget * cfg["sources"]["thin_ratio"])
    _set(conn, row["id"], state="uploading", thin=thin)
    return "uploading"


def _do_uploading(conn, row, cfg) -> str:
    if _generated_today(conn) >= cfg["bridge"]["max_generations_per_day"]:
        return "uploading"  # quota spent; try again tomorrow, no retry counted against it
    rows = conn.execute(
        "SELECT s.local_path, s.url FROM source s JOIN lesson_source ls ON ls.source_id=s.id"
        " WHERE ls.lesson_id=?", (row["id"],)).fetchall()
    paths = [r["local_path"] for r in rows if r["local_path"]]
    # Anything that could not be downloaded still goes up as a URL — the bridge accepts both, which
    # is also why Tier C video needs no separate downloader.
    urls = [r["url"] for r in rows if not r["local_path"]]
    note = _lesson_dir(cfg, row["id"]) / "brief.md"
    job = bridge.start(row["topic"], paths, cfg, language=row["language"],
                       instructions=note.read_text() if note.exists() else None,
                       urls=urls, depth=row["depth"])
    _set(conn, row["id"], state="generating", job_ref=json.dumps(job),
         notebook_id=job.get("notebook_id"), polled_at=utcnow())
    store.log_event(conn, "generated", row["id"], json.dumps(job))
    return "generating"


def _do_generating(conn, row, cfg) -> str:
    job = json.loads(row["job_ref"])
    if not bridge.ready(job, cfg):
        _set(conn, row["id"], polled_at=utcnow())
        return "generating"
    actual = None
    for a in bridge.download(job, _lesson_dir(cfg, row["id"]), cfg):
        conn.execute(
            "INSERT INTO artifact (lesson_id, kind, path, mime, bytes, duration, notebook_id)"
            " VALUES (?,?,?,?,?,?,?)",
            (row["id"], a["kind"], a["path"], a.get("mime"), a.get("bytes"), a.get("duration"),
             a.get("notebook_id")))
        if a["kind"] == "audio" and a.get("duration"):
            actual = round(a["duration"] / 60)
    # The curator's estimate is what scoring saw before generation; keep it, and record what the
    # lesson actually turned out to be. Overwriting the estimate would erase the only evidence of
    # how wrong it was.
    if actual:
        _set(conn, row["id"], actual_minutes=actual)
    if not cfg["bridge"]["keep_notebooks"]:
        bridge.discard(job, cfg)
    _set(conn, row["id"], state="ready", polled_at=utcnow())
    store.log_event(conn, "ready", row["id"])
    return "ready"


HANDLERS = {
    "picked": _do_picked,
    "harvesting": _do_harvesting,
    "uploading": _do_uploading,
    "generating": _do_generating,
}
# `proposed` waits for a human pick; `ready` onwards waits for a consumption signal.


def expire_candidates(conn, cfg) -> int:
    """Unpicked candidates do not accumulate forever."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=cfg["lesson"]["candidate_ttl_days"])).strftime(TS)
    rows = conn.execute("SELECT id FROM lesson WHERE state='proposed' AND created_at < ?", (cutoff,)).fetchall()
    for r in rows:
        _set(conn, r["id"], state="expired")
        store.log_event(conn, "expired", r["id"])
    return len(rows)


def run(conn, cfg, on_step=None) -> Counter:
    """One pass. Returns a count of the transitions made.

    `on_step(event, row, detail)` reports progress as it happens — a pass that harvests and
    generates can take minutes, and a silent command that long is indistinguishable from a hang.
    Events are "begin", "done" (detail is the new state, equal to the old one when the lesson is
    waiting) and "failed" (detail is the error).
    """
    moved: Counter = Counter()
    expired = expire_candidates(conn, cfg)
    if expired:
        moved["expired"] = expired
    now = utcnow()
    # A lesson advances at most once per pass. Without this, a lesson moved into `uploading` would
    # be picked up again by the `uploading` handler later in the same loop and run the whole
    # pipeline in one go, which defeats both the poll and the daily generation cap.
    seen: set[int] = set()
    for state, handler in HANDLERS.items():
        rows = conn.execute(
            "SELECT * FROM lesson WHERE state=? AND (next_attempt_at IS NULL OR next_attempt_at<=?)",
            (state, now)).fetchall()
        for row in rows:
            if row["id"] in seen:
                continue
            seen.add(row["id"])
            if on_step:
                on_step("begin", row, state)
            try:
                new = handler(conn, row, cfg)
                if new != state:
                    _set(conn, row["id"], retry_count=0, last_error=None, next_attempt_at=None)
                    moved[new] += 1
                if on_step:
                    on_step("done", row, new)
            except Exception as err:  # one bad lesson must not end the pass
                # A park is not a transition. Counting it under the state it stayed in reads as
                # progress in the summary when nothing actually moved.
                outcome = _park(conn, row, cfg, err)
                moved["failed" if outcome == "failed" else "parked"] += 1
                if on_step:
                    on_step("failed", row, str(err))
    return moved


def _selfcheck() -> None:
    import tempfile

    from . import config

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = config.load(path=root / "absent.toml", root=root)
        cfg["bridge"]["retry_backoff_seconds"] = 0
        conn = store.connect(cfg["paths"]["db"])

        # The real harvester searches the internet; this check stays offline.
        def fake_gather(topic, depth, dest, cfg, runner=None):
            dest.mkdir(parents=True, exist_ok=True)
            return [{"url": f"https://example.org/{topic.replace(' ', '-')}/{i}",
                     "title": f"source {i}", "tier": "A", "evidence_level": "peer-reviewed",
                     "local_path": str(dest / f"source-{i}.txt")}
                    for i in range(cfg["depth"][str(depth)]["sources"])]

        real_gather, harvester.gather = harvester.gather, fake_gather

        # The real bridge talks to Google; this check stays offline.
        real_bridge = (bridge.start, bridge.ready, bridge.download, bridge.discard)
        bridge.start = lambda topic, paths, cfg, **kw: {
            "notebook_id": f"nb-{abs(hash(topic)) % 1000}", "jobs": {"audio": "t1"}}
        bridge.ready = lambda job, cfg: True

        def fake_download(job, dest, cfg):
            dest.mkdir(parents=True, exist_ok=True)
            out = []
            for kind, name, mime, secs in (("audio", "podcast.m4a", "audio/mp4", 1380.0),
                                           ("slides", "slides.pdf", "application/pdf", None)):
                (dest / name).write_text(f"stub {kind}")
                out.append({"kind": kind, "path": str(dest / name), "mime": mime,
                            "bytes": (dest / name).stat().st_size, "duration": secs,
                            "notebook_id": job["notebook_id"]})
            return out

        bridge.download = fake_download
        bridge.discard = lambda job, cfg: None

        def add(topic="rbf-fd stencils", state="picked", **kw):
            cols = {"topic": topic, "state": state, **kw}
            keys = ",".join(cols)
            return conn.execute(
                f"INSERT INTO lesson ({keys}) VALUES ({','.join('?' * len(cols))})",
                tuple(cols.values())).lastrowid

        def state_of(lid):
            return conn.execute("SELECT state FROM lesson WHERE id=?", (lid,)).fetchone()[0]

        # Happy path: picked -> uploading -> generating -> ready, one step per pass.
        lid = add()
        assert run(conn, cfg)["uploading"] == 1
        assert state_of(lid) == "uploading"
        run(conn, cfg)
        assert state_of(lid) == "generating"
        assert json.loads(conn.execute("SELECT job_ref FROM lesson WHERE id=?", (lid,)).fetchone()[0])["notebook_id"]
        run(conn, cfg)
        assert state_of(lid) == "ready", "generation is polled, not waited on"
        assert conn.execute("SELECT count(*) FROM artifact WHERE lesson_id=?", (lid,)).fetchone()[0] == 2
        got = conn.execute("SELECT est_minutes, actual_minutes FROM lesson WHERE id=?", (lid,)).fetchone()
        assert got["actual_minutes"] == 23, "actual length comes from the audio artifact"
        assert got["est_minutes"] is None, "the estimate is never overwritten by the actual"
        assert conn.execute("SELECT count(*) FROM lesson_source WHERE lesson_id=?", (lid,)).fetchone()[0] == 10

        run(conn, cfg)
        assert state_of(lid) == "ready", "ready waits for a consumption signal, never auto-advances"

        # Sources dedup globally: a second lesson on the same topic reuses the rows.
        before = conn.execute("SELECT count(*) FROM source").fetchone()[0]
        l2 = add()
        run(conn, cfg)
        assert conn.execute("SELECT count(*) FROM source").fetchone()[0] == before
        assert conn.execute("SELECT count(*) FROM lesson_source WHERE lesson_id=?", (l2,)).fetchone()[0] == 10

        # A failure parks in place and backs off; it does not abort the pass or lose the state.
        l3 = add("failing topic", state="uploading")
        boom = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("upstream down"))
        real_start, bridge.start = bridge.start, boom
        try:
            run(conn, cfg)
            row = conn.execute("SELECT * FROM lesson WHERE id=?", (l3,)).fetchone()
            assert row["state"] == "uploading", "a retryable failure keeps its state"
            assert run(conn, cfg)["parked"] >= 0, "parks are counted as parks, not transitions"
            assert row["retry_count"] == 1 and row["last_error"] == "upstream down"
            assert row["next_attempt_at"] is not None

            for _ in range(cfg["bridge"]["max_retries"]):
                run(conn, cfg)
            assert state_of(l3) == "failed", "retries are capped"
        finally:
            bridge.start = real_start

        # A healthy lesson still moves while a broken one is parked.
        l4 = add("healthy")
        run(conn, cfg)
        assert state_of(l4) == "uploading"

        # Recovery clears the failure bookkeeping.
        l5 = add("recovers", state="uploading", retry_count=2, last_error="old")
        run(conn, cfg)
        row = conn.execute("SELECT * FROM lesson WHERE id=?", (l5,)).fetchone()
        assert row["state"] == "generating" and row["retry_count"] == 0 and row["last_error"] is None

        # Daily generation cap holds lessons in uploading without spending retries.
        cfg["bridge"]["max_generations_per_day"] = 0
        l6 = add("capped", state="uploading")
        run(conn, cfg)
        row = conn.execute("SELECT * FROM lesson WHERE id=?", (l6,)).fetchone()
        assert row["state"] == "uploading" and row["retry_count"] == 0, "a quota wait is not a failure"
        cfg["bridge"]["max_generations_per_day"] = 99

        # Too few sources is not a failure either; the topic is simply unsourceable.
        saved, harvester.gather = harvester.gather, lambda *a, **k: []
        try:
            l7 = add("nonexistent topic")
            run(conn, cfg)
            assert state_of(l7) == "unsourced"
        finally:
            harvester.gather = saved

        # Thin flag when the gate yields under the ratio, but still enough to generate.
        saved, harvester.gather = harvester.gather, lambda t, d, dest, c: saved(t, d, dest, c)[:3]
        try:
            l8 = add("sparse topic")
            run(conn, cfg)
            assert conn.execute("SELECT thin FROM lesson WHERE id=?", (l8,)).fetchone()[0] == 1
        finally:
            harvester.gather = saved

        # Stale candidates expire.
        old = (datetime.now(timezone.utc) - timedelta(days=99)).strftime(TS)
        l9 = add("stale", state="proposed", created_at=old)
        run(conn, cfg)
        assert state_of(l9) == "expired"

        # Progress is reported as it happens, including the waits and the failures.
        seen_steps: list[tuple] = []
        l10 = add("watched")
        run(conn, cfg, on_step=lambda ev, row, detail: seen_steps.append((ev, row["state"], detail)))
        assert ("begin", "picked", "picked") in seen_steps
        assert ("done", "picked", "uploading") in seen_steps
        saved2, harvester.gather = harvester.gather, lambda *a, **k: (_ for _ in ()).throw(RuntimeError("nope"))
        try:
            seen_steps.clear()
            l11 = add("breaks")
            run(conn, cfg, on_step=lambda ev, row, detail: seen_steps.append((ev, row["state"], detail)))
            assert any(ev == "failed" and "nope" in d for ev, _, d in seen_steps)
        finally:
            harvester.gather = saved2

        # Events survive as the picker's training data.
        kinds = {r[0] for r in conn.execute("SELECT DISTINCT kind FROM event")}
        assert {"generated", "ready", "failed", "unsourced", "expired"} <= kinds

        harvester.gather = real_gather
        bridge.start, bridge.ready, bridge.download, bridge.discard = real_bridge
        conn.close()

        # The lock keeps overlapping timer runs out.
        lock_path = root / "tick.lock"
        with lock(lock_path) as first:
            assert first
            with lock(lock_path) as second:
                assert not second, "a second pass must back off, not block"
        with lock(lock_path) as again:
            assert again, "the lock releases"

    print("tick selfcheck ok")


if __name__ == "__main__":
    _selfcheck()
