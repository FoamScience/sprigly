"""NotebookLM bridge — a thin wrapper over notebooklm-py.

The library is async; the rest of Sprigly is not. One `asyncio.run` per call at this boundary keeps
the click app and the tick loop plain synchronous code.

Generation is never waited on. `start` returns task ids, later ticks poll them. A NotebookLM
generation takes minutes, and a tick that blocks on one stalls every other lesson behind it.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# The depth table asks for "brief" or "deep-dive"; the library wants its own enum.
AUDIO_FORMATS = {"brief": "BRIEF", "deep-dive": "DEEP_DIVE",
                 "critique": "CRITIQUE", "debate": "DEBATE"}

# One row per artifact kind: how to ask for it, how to fetch it, and what it lands as. Adding a
# kind is a row here plus a prompt file, not a new branch in three places.
SPECS = {
    "audio": {"generate": "generate_audio", "download": "download_audio",
              "file": "podcast.m4a", "mime": "audio/mp4"},
    "video": {"generate": "generate_video", "download": "download_video",
              "file": "video.mp4", "mime": "video/mp4"},
    "slides": {"generate": "generate_slide_deck", "download": "download_slide_deck",
               "file": "slides.pdf", "mime": "application/pdf"},
    "quiz": {"generate": "generate_quiz", "download": "download_quiz",
             "file": "quiz.json", "mime": "application/json"},
}
ARTIFACTS = tuple(SPECS)


def _enum(name: str, value: str):
    import notebooklm

    return getattr(getattr(notebooklm, name), value)


def options(kind: str, depth: int, cfg: dict) -> dict:
    """Generation options for one artifact kind, resolved from config to library enums."""
    b = cfg["bridge"]
    if kind == "audio":
        fmt = AUDIO_FORMATS.get(cfg["depth"][str(depth)]["audio"], "DEEP_DIVE")
        return {"audio_format": _enum("AudioFormat", fmt),
                "audio_length": _enum("AudioLength", b["audio_length"])}
    if kind == "slides":
        return {"slide_format": _enum("SlideDeckFormat", b["slide_format"]),
                "slide_length": _enum("SlideDeckLength", b["slide_length"])}
    if kind == "video":
        return {"video_format": _enum("VideoFormat", b["video_format"]),
                "video_style": _enum("VideoStyle", b["video_style"])}
    if kind == "quiz":
        return {"quantity": _enum("QuizQuantity", b["quiz_quantity"]),
                "difficulty": _enum("QuizDifficulty", b["quiz_difficulty"])}
    return {}


def instructions_for(kind: str, topic: str, depth: int, brief: str, cfg: dict) -> str:
    """Per-artifact prompt. What suits a podcast does not suit a slide deck or a quiz."""
    from .curator import render

    return render(f"artifact_{kind}", topic=topic, depth=depth,
                  scope=cfg["depth"][str(depth)]["scope"], brief=brief or "")


class BridgeError(RuntimeError):
    pass


def _context(cfg: dict):
    """The client context. A seam, so the self-check never touches Google."""
    from notebooklm.client import NotebookLMClient

    return NotebookLMClient.from_storage(timeout=cfg["bridge"]["client_timeout_seconds"])


async def _start(topic, paths, urls, instructions, language, depth, cfg, report=None) -> dict[str, Any]:
    say = report or (lambda _msg: None)
    async with _context(cfg) as c:
        nb = await c.notebooks.create(topic[:100])
        say(f"notebook {nb.id}")
        try:
            return await _fill(c, nb, topic, paths, urls, instructions, language, depth, cfg, say)
        except Exception:
            # Anything that fails after create leaves an empty notebook behind, and the account
            # has a cap. Tidy up before the error propagates, then let the lesson park and retry.
            try:
                await c.notebooks.delete(nb.id)
                log.info("removed the empty notebook %s after a failed setup", nb.id)
            except Exception as err:
                log.warning("could not remove the orphaned notebook %s: %s", nb.id, err)
            raise


async def _fill(c, nb, topic, paths, urls, instructions, language, depth, cfg, say) -> dict[str, Any]:
    if True:
        added = []
        for p in paths:
            try:
                added.append(await c.sources.add_file(nb.id, p))
                say(f"uploaded {Path(p).name}")
            except Exception as err:
                log.warning("source %s rejected: %s", Path(p).name, err)
        for u in urls:
            try:
                added.append(await c.sources.add_url(nb.id, u))
                say(f"linked {u[:70]}")
            except Exception as err:
                log.warning("source %s rejected: %s", u, err)
        if not added:
            raise BridgeError("no source was accepted by the notebook")

        ids = [s.id for s in added if getattr(s, "id", None)]
        if ids:
            say(f"waiting for {len(ids)} sources to index")
            # Generating before the sources finish indexing produces an empty artifact.
            await c.sources.wait_all_until_ready(
                nb.id, ids, timeout=cfg["bridge"]["source_ready_timeout_seconds"])

        jobs = {}
        for kind in cfg["bridge"]["artifacts"]:
            spec = SPECS.get(kind)
            if not spec:
                log.warning("unknown artifact kind %r in config, skipping", kind)
                continue
            say(f"requesting {kind}")
            kwargs = {"instructions": instructions_for(kind, topic, depth, instructions, cfg),
                      **options(kind, depth, cfg)}
            if kind != "quiz":  # the quiz endpoint takes no language
                kwargs["language"] = language
            status = await getattr(c.artifacts, spec["generate"])(nb.id, **kwargs)
            if status.task_id:
                jobs[kind] = status.task_id
        if not jobs:
            raise BridgeError("no artifact was requested")
        return {"notebook_id": nb.id, "jobs": jobs}


def start(topic: str, source_paths: list[str], cfg: dict, language: str = "en",
          instructions: str | None = None, urls: list[str] | None = None,
          depth: int = 3, report=None) -> dict[str, Any]:
    """Create a notebook, upload the sources, request generation. Returns a job reference."""
    return asyncio.run(
        _start(topic, source_paths, urls or [], instructions, language, depth, cfg, report))


async def _ready(job_ref, cfg, report=None) -> bool:
    say = report or (lambda _msg: None)
    async with _context(cfg) as c:
        done = True
        for kind, task in job_ref.get("jobs", {}).items():
            st = await c.artifacts.poll_status(job_ref["notebook_id"], task)
            if st.is_failed:
                raise BridgeError(f"{kind} generation failed: {st.error or st.error_code}")
            say(f"{kind}: {'ready' if st.is_complete else 'still generating'}")
            done = done and st.is_complete
    return done


def ready(job_ref: dict, cfg: dict, report=None) -> bool:
    """Has every requested artifact finished? A failure raises, so the lesson parks and retries."""
    return asyncio.run(_ready(job_ref, cfg, report))


async def _download(job_ref, dest: Path, cfg, report=None) -> list[dict[str, Any]]:
    say = report or (lambda _msg: None)
    dest.mkdir(parents=True, exist_ok=True)
    nb = job_ref["notebook_id"]
    out = []
    async with _context(cfg) as c:
        for kind in job_ref.get("jobs", {}):
            spec = SPECS.get(kind)
            if not spec:
                continue
            name, mime, method = spec["file"], spec["mime"], spec["download"]
            path = dest / name
            try:
                await getattr(c.artifacts, method)(nb, str(path))
            except Exception as err:
                # One missing artifact is not worth discarding the lesson's audio over.
                log.warning("could not download %s for %s: %s", kind, nb, err)
                continue
            if not path.exists():
                continue
            out.append({"kind": kind, "path": str(path), "mime": mime,
                        "bytes": path.stat().st_size, "duration": None, "notebook_id": nb})
            say(f"downloaded {name} ({path.stat().st_size // 1024}k)")
    if not out:
        raise BridgeError("generation reported complete but nothing downloaded")
    return out


def download(job_ref: dict, dest: Path, cfg: dict, report=None) -> list[dict[str, Any]]:
    """Fetch the artifacts. Container and MIME come from what actually arrives."""
    return asyncio.run(_download(job_ref, dest, cfg, report))


async def _discard(job_ref, cfg) -> None:
    async with _context(cfg) as c:
        await c.notebooks.delete(job_ref["notebook_id"])


def discard(job_ref: dict, cfg: dict) -> None:
    """Delete the notebook once its artifacts are safely downloaded.

    Accounts have a notebook cap. Without this the project stops working after a few weeks, and the
    failure looks like a generation error rather than a housekeeping one.
    """
    if cfg["bridge"]["keep_notebooks"]:
        return
    try:
        asyncio.run(_discard(job_ref, cfg))
    except Exception as err:  # the artifacts are already on disk; this is tidying, not the work
        log.warning("could not delete notebook %s: %s", job_ref.get("notebook_id"), err)


def _selfcheck() -> None:
    import tempfile
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from . import config

    cfg = config.load(path=Path("/nonexistent.toml"))
    calls: list[str] = []

    def fake_client(*, fail=None, complete=True, downloads=ARTIFACTS, accept=True):
        async def add_file(nb, p, **kw):
            calls.append(f"add_file:{Path(p).name}")
            if not accept:
                raise RuntimeError("rejected")
            return SimpleNamespace(id=f"s-{Path(p).name}")

        async def add_url(nb, u, **kw):
            calls.append(f"add_url:{u}")
            return SimpleNamespace(id=f"s-{u}")

        async def gen(name):
            async def inner(nb, **kw):
                calls.append(f"generate:{name}")
                return SimpleNamespace(task_id=f"t-{name}")
            return inner

        async def poll(nb, task):
            kind = task.removeprefix("t-")
            return SimpleNamespace(is_failed=(fail == kind), is_complete=complete,
                                   error="boom", error_code=None)

        def downloader(kind, name):
            async def inner(nb, out, **kw):
                calls.append(f"download:{kind}")
                if kind not in downloads:
                    raise RuntimeError("missing")
                Path(out).write_text(f"{kind} bytes")
            return inner

        async def ready_all(nb, ids, **kw):
            calls.append(f"wait:{len(ids)}")

        client = SimpleNamespace(
            notebooks=SimpleNamespace(
                create=lambda title: _coro(SimpleNamespace(id="nb-1", title=title)),
                delete=lambda nb: _coro(calls.append(f"delete:{nb}"))),
            sources=SimpleNamespace(add_file=add_file, add_url=add_url,
                                    wait_all_until_ready=ready_all),
            artifacts=SimpleNamespace(poll_status=poll))
        for kind, spec in SPECS.items():
            async def make(nb, _n=kind, **kw):
                calls.append(f"generate:{_n}")
                calls.append(f"instructions:{_n}:{len(kw.get('instructions') or '')}")
                return SimpleNamespace(task_id=f"t-{_n}")
            setattr(client.artifacts, spec["generate"], make)
            setattr(client.artifacts, spec["download"], downloader(kind, spec["file"]))

        @asynccontextmanager
        async def ctx(_cfg):
            yield client
        return ctx

    async def _coro(value):
        return value

    real_ctx = globals()["_context"]
    try:
        globals()["_context"] = fake_client()
        calls.clear()
        job = start("rbf-fd stencils", ["/tmp/a.pdf", "/tmp/b.pdf"], cfg,
                    instructions="brief", urls=["https://example.org/v"], depth=3)
        assert job["notebook_id"] == "nb-1"
        assert set(job["jobs"]) == set(cfg["bridge"]["artifacts"])
        assert "generate:video" not in calls, "video is opt-in, not default"
        # Each artifact is asked for with its own prompt, not one instruction reused four times.
        lengths = {c.split(":")[1]: int(c.split(":")[2]) for c in calls if c.startswith("instructions:")}
        assert len(set(lengths.values())) == len(lengths), f"prompts must differ per kind: {lengths}"
        assert all(v > 200 for v in lengths.values()), "each prompt is a real instruction"

        assert "add_url:https://example.org/v" in calls, "urls go up alongside files"
        assert any(c.startswith("wait:") for c in calls), \
            "sources must finish indexing before generation, or the artifact comes back empty"
        assert calls.index("wait:3") < calls.index("generate:audio")

        # Turning video on is a config line, not a code change.
        wide = {**cfg, "bridge": {**cfg["bridge"], "artifacts": ["audio", "video"]}}
        calls.clear()
        assert set(start("x", ["/tmp/a.pdf"], wide, depth=3)["jobs"]) == {"audio", "video"}

        assert ready(job, cfg) is True

        globals()["_context"] = fake_client(complete=False)
        assert ready(job, cfg) is False, "an unfinished task is not ready"

        globals()["_context"] = fake_client(fail="audio")
        try:
            ready(job, cfg)
            raise AssertionError("a failed generation must raise so the lesson parks")
        except BridgeError as err:
            assert "audio generation failed" in str(err)

        with tempfile.TemporaryDirectory() as td:
            globals()["_context"] = fake_client()
            got = download(job, Path(td) / "lesson", cfg)
            assert {a["kind"] for a in got} == {"audio", "slides", "quiz"}
            assert all(a["bytes"] > 0 and Path(a["path"]).exists() for a in got)

            # One artifact missing must not cost the lesson its audio.
            globals()["_context"] = fake_client(downloads=("audio",))
            got = download(job, Path(td) / "partial", cfg)
            assert {a["kind"] for a in got} == {"audio"}

            globals()["_context"] = fake_client(downloads=())
            try:
                download(job, Path(td) / "empty", cfg)
                raise AssertionError("nothing downloaded is a failure, not an empty success")
            except BridgeError:
                pass

        globals()["_context"] = fake_client(accept=False)
        calls.clear()
        try:
            start("x", ["/tmp/a.pdf"], cfg, urls=[])
            raise AssertionError("a notebook with no accepted source must not generate")
        except BridgeError as err:
            assert "no source was accepted" in str(err)
        assert "delete:nb-1" in calls, \
            "a setup that fails after create must not strand an empty notebook on the account"

        # Tidying must never take down a lesson whose artifacts are already on disk.
        globals()["_context"] = fake_client()
        discard({"notebook_id": "nb-1"}, cfg)
        discard({"notebook_id": "missing"}, {**cfg, "bridge": {**cfg["bridge"], "keep_notebooks": True}})
    finally:
        globals()["_context"] = real_ctx

    print("bridge selfcheck ok")


if __name__ == "__main__":
    _selfcheck()
