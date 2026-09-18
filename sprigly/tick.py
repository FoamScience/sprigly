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
import logging
import shutil
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import bridge, harvester, notes, store
from .store import TS, utcnow

log = logging.getLogger(__name__)


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

def _do_picked(conn, row, cfg, report=None) -> str:
    _set(conn, row["id"], state="harvesting")
    fresh = conn.execute("SELECT * FROM lesson WHERE id=?", (row["id"],)).fetchone()
    return _do_harvesting(conn, fresh, cfg, report)


def _min_evidence(conn, row, cfg: dict) -> str:
    """The bar this lesson is judged against: its track's, or its domain's default.

    A lesson outside any track still belongs to a field, and "scientifically proven" means
    something different in business than in numerics, so the fallback is per domain rather than one
    universal bar.
    """
    if row["track_id"]:
        bar = conn.execute("SELECT min_evidence FROM track WHERE id=?",
                           (row["track_id"],)).fetchone()
        if bar:
            return bar[0]
    domains = cfg["sources"]["min_evidence_by_domain"]
    return domains.get(row["domain"] or "", cfg["sources"]["default_min_evidence"])


def _do_harvesting(conn, row, cfg, report=None) -> str:
    dest = _lesson_dir(cfg, row["id"]) / "sources"
    # A lesson that already has sources is being freshened, not harvested from scratch: keep them
    # as the baseline and look only for a few recent additions.
    known = {r[0].lower() for r in conn.execute(
        "SELECT s.url FROM source s JOIN lesson_source ls ON ls.source_id=s.id"
        " WHERE ls.lesson_id=?", (row["id"],))}
    want = cfg["delivery"]["freshen_new_sources"] if known else None
    if known and report:
        report(f"freshening: {len(known)} sources kept, looking for {want} more")
    found = harvester.gather(row["topic"], row["depth"], dest, cfg, report=report,
                             exclude=known, want=want,
                             min_evidence=_min_evidence(conn, row, cfg))
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

    total = len(known) + len(found)
    budget = cfg["depth"][str(row["depth"])]["sources"]
    if total < cfg["sources"]["min_sources"]:
        _set(conn, row["id"], state="unsourced")
        store.log_event(conn, "unsourced", row["id"], json.dumps({"found": total}))
        return "unsourced"
    thin = int(total < budget * cfg["sources"]["thin_ratio"])
    _set(conn, row["id"], state="uploading", thin=thin)
    return "uploading"


def _do_uploading(conn, row, cfg, report=None) -> str:
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
    wanted = json.loads(row["artifacts"]) if row["artifacts"] else None
    job = bridge.start(row["topic"], paths, cfg, language=row["language"],
                       instructions=note.read_text() if note.exists() else None,
                       urls=urls, depth=row["depth"], report=report, artifacts=wanted)
    _set(conn, row["id"], state="generating", job_ref=json.dumps(job),
         notebook_id=job.get("notebook_id"), polled_at=utcnow())
    store.log_event(conn, "generated", row["id"], json.dumps(job))  # carries notebook_id
    return "generating"


def _do_generating(conn, row, cfg, report=None) -> str:
    job = json.loads(row["job_ref"])
    if not bridge.ready(job, cfg, report=report):
        _set(conn, row["id"], polled_at=utcnow())
        return "generating"
    actual = None
    for a in bridge.download(job, _lesson_dir(cfg, row["id"]), cfg, report=report):
        conn.execute(
            "INSERT INTO artifact (lesson_id, kind, path, mime, bytes, duration, notebook_id)"
            " VALUES (?,?,?,?,?,?,?)",
            (row["id"], a["kind"], a["path"], a.get("mime"), a.get("bytes"), a.get("duration"),
             a.get("notebook_id")))
        # Audio is the lesson's length; a video overview is a condensed companion, so it is only
        # the fallback when audio was not generated at all.
        if a.get("duration") and (a["kind"] == "audio" or (actual is None and a["kind"] == "video")):
            actual = round(a["duration"] / 60)
    # The curator's estimate is what scoring saw before generation; keep it, and record what the
    # lesson actually turned out to be. Overwriting the estimate would erase the only evidence of
    # how wrong it was.
    if actual:
        _set(conn, row["id"], actual_minutes=actual)
    bridge.discard(job, cfg)
    _set(conn, row["id"], state="ready", polled_at=utcnow())
    store.log_event(conn, "ready", row["id"])
    out = notes.write(conn, cfg, row["id"])
    if report:
        report(f"ready in {out.parent}")
    return "ready"


HANDLERS = {
    "picked": _do_picked,
    "harvesting": _do_harvesting,
    "uploading": _do_uploading,
    "generating": _do_generating,
}
# `proposed` waits for a human pick; `ready` onwards waits for a consumption signal.


REDO_STAGES = {
    # stage -> (state to return to, what gets thrown away)
    "harvest": ("picked", "sources, brief and any notebook"),
    "upload": ("uploading", "the notebook and any artifacts"),
    "freshen": ("picked", "the notebook and the audio, keeping every source as a baseline"),
}


def redo(conn, cfg, lesson_id: int, stage: str) -> str:
    """Rewind one lesson so a later tick re-runs a phase.

    Re-running is not idempotent from where the lesson stands — a half-harvested lesson has source
    rows and files that would be counted again — so rewinding means clearing what that phase
    produced, not just setting the state back.
    """
    row = conn.execute("SELECT * FROM lesson WHERE id=?", (lesson_id,)).fetchone()
    if not row:
        raise ValueError(f"no lesson {lesson_id}")
    if stage not in REDO_STAGES:
        raise ValueError(f"unknown stage {stage!r}, expected one of {sorted(REDO_STAGES)}")
    state, _ = REDO_STAGES[stage]

    if row["notebook_id"]:
        # Detached, not deleted. A redo abandons the old notebook, but removing it from the
        # account is the user's call — `sprigly notebooks --prune` lists what is no longer used.
        log.info("lesson %s no longer uses notebook %s; "
                 "`sprigly notebooks --prune` can remove it", lesson_id, row["notebook_id"])

    lesson_dir = _lesson_dir(cfg, lesson_id)
    for a in conn.execute("SELECT path FROM artifact WHERE lesson_id=?", (lesson_id,)):
        Path(a["path"]).unlink(missing_ok=True)
    conn.execute("DELETE FROM artifact WHERE lesson_id=?", (lesson_id,))

    if stage == "harvest":
        conn.execute("DELETE FROM lesson_source WHERE lesson_id=?", (lesson_id,))
        shutil.rmtree(lesson_dir / "sources", ignore_errors=True)
        (lesson_dir / "brief.md").unlink(missing_ok=True)

    # Freshening regenerates the audio alone. The video and slides are the expensive parts and
    # nothing about them has changed; the sources stay as the baseline the refresher builds on.
    artifacts = json.dumps(["audio"]) if stage == "freshen" else None
    _set(conn, lesson_id, state=state, job_ref=None, notebook_id=None, polled_at=None,
         retry_count=0, last_error=None, next_attempt_at=None, thin=0, artifacts=artifacts)
    store.log_event(conn, "redo", lesson_id, json.dumps({"stage": stage, "from": row["state"]}))
    return state


def expire_candidates(conn, cfg) -> int:
    """Unpicked candidates do not accumulate forever."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=cfg["lesson"]["candidate_ttl_days"])).strftime(TS)
    rows = conn.execute("SELECT id FROM lesson WHERE state='proposed' AND created_at < ?", (cutoff,)).fetchall()
    for r in rows:
        _set(conn, r["id"], state="expired")
        store.log_event(conn, "expired", r["id"])
    return len(rows)


def run(conn, cfg, on_step=None, only: int | None = None) -> Counter:
    """One pass. Returns a count of the transitions made.

    `on_step(event, row, detail)` reports progress as it happens — a pass that harvests and
    generates can take minutes, and a silent command that long is indistinguishable from a hang.
    Events are "begin", "done" (detail is the new state, equal to the old one when the lesson is
    waiting) and "failed" (detail is the error).
    """
    moved: Counter = Counter()
    if only is None:
        expired = expire_candidates(conn, cfg)
        if expired:
            moved["expired"] = expired
        pruned = notes.prune_video(conn, cfg)
        if pruned:
            moved["pruned"] = pruned
    now = utcnow()
    # A lesson advances at most once per pass. Without this, a lesson moved into `uploading` would
    # be picked up again by the `uploading` handler later in the same loop and run the whole
    # pipeline in one go, which defeats both the poll and the daily generation cap.
    seen: set[int] = set()
    for state, handler in HANDLERS.items():
        # A targeted run ignores the backoff window: asking for one lesson by name is an explicit
        # instruction, not the timer coming round again.
        rows = conn.execute(
            "SELECT * FROM lesson WHERE state=? AND id=?", (state, only)).fetchall() if only \
            else conn.execute(
                "SELECT * FROM lesson WHERE state=?"
                " AND (next_attempt_at IS NULL OR next_attempt_at<=?)", (state, now)).fetchall()
        for row in rows:
            if row["id"] in seen:
                continue
            seen.add(row["id"])
            if on_step:
                on_step("begin", row, state)
            report = (lambda msg: on_step("step", row, msg)) if on_step else None
            try:
                new = handler(conn, row, cfg, report)
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
        seen_bars: list[str] = []

        def fake_gather(topic, depth, dest, cfg, runner=None, report=None,
                        exclude=None, want=None, min_evidence=None):
            seen_bars.append(min_evidence)
            dest.mkdir(parents=True, exist_ok=True)
            if report:  # the real harvester narrates each source; prove the wiring carries it
                report(f"searching for {topic}")
            n = want if want is not None else cfg["depth"][str(depth)]["sources"]
            offset = len(exclude or ())
            return [{"url": f"https://example.org/{topic.replace(' ', '-')}/{i}",
                     "title": f"source {i}", "tier": "A", "evidence_level": "peer-reviewed",
                     "local_path": str(dest / f"source-{i}.txt")}
                    for i in range(offset, offset + n)]

        real_gather, harvester.gather = harvester.gather, fake_gather

        # The real bridge talks to Google; this check stays offline.
        real_bridge = (bridge.start, bridge.ready, bridge.download, bridge.discard)
        bridge.start = lambda topic, paths, cfg, **kw: {
            "notebook_id": f"nb-{abs(hash(topic)) % 1000}", "jobs": {"audio": "t1"}}
        bridge.ready = lambda job, cfg, report=None: True

        def fake_download(job, dest, cfg, report=None):
            dest.mkdir(parents=True, exist_ok=True)
            out = []
            for kind, name, mime, secs in (("video", "video.mp4", "video/mp4", 467.0),
                                           ("audio", "podcast.m4a", "audio/mp4", 1380.0),
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

        # The track's bar, and the domain default behind it, must reach the harvest — that is the
        # only place evidence level is decided, and it used to be judged against the global default.
        tid = conn.execute("INSERT INTO track (goal, min_evidence) VALUES ('markets','preprint')"
                           ).lastrowid
        tracked = add("bond pricing", track_id=tid, domain="business")
        run(conn, cfg)
        assert seen_bars[-1] == "preprint", f"the track's bar, got {seen_bars[-1]!r}"
        untracked = add("cash conversion cycle", domain="business")
        run(conn, cfg)
        assert seen_bars[-1] == "institutional", f"the domain default, got {seen_bars[-1]!r}"
        assert state_of(tracked) == "generating" and state_of(untracked) == "uploading"
        conn.execute("DELETE FROM lesson WHERE id IN (?,?)", (tracked, untracked))

        # Happy path: picked -> uploading -> generating -> ready, one step per pass.
        lid = add()
        assert run(conn, cfg)["uploading"] == 1
        assert state_of(lid) == "uploading"
        run(conn, cfg)
        assert state_of(lid) == "generating"
        assert json.loads(conn.execute("SELECT job_ref FROM lesson WHERE id=?", (lid,)).fetchone()[0])["notebook_id"]
        run(conn, cfg)
        assert state_of(lid) == "ready", "generation is polled, not waited on"
        assert conn.execute("SELECT count(*) FROM artifact WHERE lesson_id=?", (lid,)).fetchone()[0] == 3
        got = conn.execute("SELECT est_minutes, actual_minutes FROM lesson WHERE id=?", (lid,)).fetchone()
        assert got["actual_minutes"] == 23, \
            "the lesson's length is its audio, not the shorter video companion"
        assert got["est_minutes"] is None, "the estimate is never overwritten by the actual"
        assert conn.execute("SELECT count(*) FROM lesson_source WHERE lesson_id=?", (lid,)).fetchone()[0] == 10

        run(conn, cfg)
        assert state_of(lid) == "ready", "ready waits for a consumption signal, never auto-advances"
        assert (_lesson_dir(cfg, lid) / "notes.md").exists(), \
            "a ready lesson writes its own record of what it was built from"
        run(conn, cfg)
        assert state_of(lid) == "ready", \
            "nothing advances a ready lesson but the learner saying so"

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
        saved, harvester.gather = harvester.gather, \
            lambda t, d, dest, c, **k: saved(t, d, dest, c, **k)[:3]
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
        assert any(ev == "step" for ev, _, _ in seen_steps), \
            "sub-steps are reported too, so a long phase is not a silent one"
        saved2, harvester.gather = harvester.gather, \
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("nope"))
        try:
            seen_steps.clear()
            l11 = add("breaks")
            run(conn, cfg, on_step=lambda ev, row, detail: seen_steps.append((ev, row["state"], detail)))
            assert any(ev == "failed" and "nope" in d for ev, _, d in seen_steps)
        finally:
            harvester.gather = saved2

        # Freshening keeps every source and asks only for a few recent additions.
        fresh = add("freshenable", state="ready")
        conn.execute("INSERT INTO lesson_source VALUES (?,?)",
                     (fresh, conn.execute("SELECT id FROM source LIMIT 1").fetchone()[0]))
        assert redo(conn, cfg, fresh, "freshen") == "picked"
        assert json.loads(conn.execute("SELECT artifacts FROM lesson WHERE id=?",
                                       (fresh,)).fetchone()[0]) == ["audio"], \
            "a refresher regenerates the audio alone"
        run(conn, cfg, only=fresh)
        kept = conn.execute("SELECT count(*) FROM lesson_source WHERE lesson_id=?",
                            (fresh,)).fetchone()[0]
        assert kept == 1 + cfg["delivery"]["freshen_new_sources"], \
            f"the originals stay as the baseline and a few join them, got {kept}"

        # A redo rewinds a lesson and clears what the phase produced, so it is not counted twice.
        done = add("finished", state="ready")
        conn.execute("INSERT INTO lesson_source VALUES (?,?)",
                     (done, conn.execute("SELECT id FROM source LIMIT 1").fetchone()[0]))
        art = _lesson_dir(cfg, done)
        art.mkdir(parents=True, exist_ok=True)
        (art / "podcast.m4a").write_text("audio")
        conn.execute("INSERT INTO artifact (lesson_id, kind, path) VALUES (?,?,?)",
                     (done, "audio", str(art / "podcast.m4a")))
        _set(conn, done, job_ref='{"notebook_id": "nb-old", "jobs": {}}', notebook_id="nb-old")

        assert redo(conn, cfg, done, "upload") == "uploading"
        assert conn.execute("SELECT count(*) FROM artifact WHERE lesson_id=?",
                            (done,)).fetchone()[0] == 0, "artifacts are cleared"
        assert not (art / "podcast.m4a").exists(), "and so are their files"
        assert conn.execute("SELECT count(*) FROM lesson_source WHERE lesson_id=?",
                            (done,)).fetchone()[0] == 1, "--from upload keeps the sources"
        row = conn.execute("SELECT * FROM lesson WHERE id=?", (done,)).fetchone()
        assert row["job_ref"] is None and row["notebook_id"] is None, "the old notebook is let go"

        assert redo(conn, cfg, done, "harvest") == "picked"
        assert conn.execute("SELECT count(*) FROM lesson_source WHERE lesson_id=?",
                            (done,)).fetchone()[0] == 0, "--from harvest drops the sources too"
        try:
            redo(conn, cfg, done, "nonsense")
            raise AssertionError("an unknown stage is refused")
        except ValueError:
            pass
        try:
            redo(conn, cfg, 9999, "harvest")
            raise AssertionError("an unknown lesson is refused")
        except ValueError:
            pass

        # A targeted run touches only the lesson named, and ignores its backoff.
        a1, a2 = add("target"), add("bystander")
        _set(conn, a1, next_attempt_at="2099-01-01T00:00:00Z", retry_count=1)
        run(conn, cfg, only=a1)
        assert state_of(a1) == "uploading", "an explicit request ignores the backoff window"
        assert state_of(a2) == "picked", "and leaves everything else alone"

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
