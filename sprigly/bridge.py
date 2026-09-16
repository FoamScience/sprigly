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
            # The notebook is left behind deliberately. It is empty and useless, but it is on the
            # user's account, and deleting anything there without being asked is not ours to do.
            # `sprigly notebooks --prune` offers it for removal.
            log.warning("notebook %s was created but not filled; "
                        "remove it with `sprigly notebooks --prune` if you want it gone", nb.id)
            raise


async def _fill(c, nb, topic, paths, urls, instructions, language, depth, cfg, say,
                generate: bool = True) -> dict[str, Any]:
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

        if not generate:
            return {"notebook_id": nb.id, "jobs": {}}

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
          depth: int = 3, report=None, artifacts: list[str] | None = None) -> dict[str, Any]:
    """Create a notebook, upload the sources, request generation. Returns a job reference."""
    if artifacts:
        cfg = {**cfg, "bridge": {**cfg["bridge"], "artifacts": artifacts}}
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
        # One listing, matched by task id: the artifact objects carry duration_seconds, and asking
        # the API is cheaper and more accurate than parsing a container for it.
        durations = {}
        try:
            for a in await c.artifacts.list(nb):
                if getattr(a, "duration_seconds", None):
                    durations[a.id] = float(a.duration_seconds)
        except Exception as err:
            log.info("could not read artifact durations for %s: %s", nb, err)

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
                        "bytes": path.stat().st_size, "notebook_id": nb,
                        "duration": durations.get(job_ref["jobs"][kind])})
            say(f"downloaded {name} ({path.stat().st_size // 1024}k)")
    if not out:
        raise BridgeError("generation reported complete but nothing downloaded")
    return out


def download(job_ref: dict, dest: Path, cfg: dict, report=None) -> list[dict[str, Any]]:
    """Fetch the artifacts. Container and MIME come from what actually arrives."""
    return asyncio.run(_download(job_ref, dest, cfg, report))


async def _upload_only(topic, paths, urls, cfg) -> str:
    async with _context(cfg) as c:
        nb = await c.notebooks.create(topic[:100])
        try:
            out = await _fill(c, nb, topic, paths, urls, None, "en", 3, cfg,
                              lambda _m: None, generate=False)
            return out["notebook_id"]
        except Exception:
            try:
                await c.notebooks.delete(nb.id)
            except Exception as err:
                log.warning("could not remove the orphaned notebook %s: %s", nb.id, err)
            raise


def upload_only(topic: str, paths: list[str], urls: list[str], cfg: dict) -> str:
    """A notebook holding the sources and nothing else, for asking questions of."""
    return asyncio.run(_upload_only(topic, paths, urls or [], cfg))


async def _alive(notebook_id, cfg) -> bool:
    async with _context(cfg) as c:
        return await c.notebooks.get_or_none(notebook_id) is not None


def alive(notebook_id: str, cfg: dict) -> bool:
    """Is this notebook still on the account? They are deleted after download by default."""
    try:
        return asyncio.run(_alive(notebook_id, cfg))
    except Exception as err:
        log.info("could not check notebook %s: %s", notebook_id, err)
        return False


async def _ask(notebook_id, question, cfg) -> Any:
    async with _context(cfg) as c:
        return await c.chat.ask(notebook_id, question)


def ask(notebook_id: str, question: str, cfg: dict) -> Any:
    """Grounded question answering against a notebook's sources."""
    return asyncio.run(_ask(notebook_id, question, cfg))


async def _share_url(notebook_id, cfg) -> str:
    async with _context(cfg) as c:
        return await c.notebooks.get_share_url(notebook_id)


def share_url(notebook_id: str, cfg: dict) -> str:
    """A link to open the notebook, where live audio and video sessions live."""
    return asyncio.run(_share_url(notebook_id, cfg))


async def _discard(job_ref, cfg) -> None:
    async with _context(cfg) as c:
        await c.notebooks.delete(job_ref["notebook_id"])


def delete(notebook_id: str, cfg: dict) -> None:
    """Delete one notebook. Only ever called because someone asked for it."""
    asyncio.run(_discard({"notebook_id": notebook_id}, cfg))


async def _listing(cfg) -> list:
    async with _context(cfg) as c:
        out = []
        for nb in await c.notebooks.list():
            ids = await c.notebooks.get_source_ids(nb.id)
            out.append({"id": nb.id, "title": getattr(nb, "title", ""), "sources": len(ids)})
        return out


def listing(cfg: dict) -> list[dict]:
    """Every notebook on the account, with how many sources each holds."""
    return asyncio.run(_listing(cfg))


def discard(job_ref: dict, cfg: dict) -> None:
    """Delete the notebook after download — only when explicitly configured to.

    Off by default. Accounts do have a notebook cap, but reaching it is a prompt to prune, not a
    licence to delete someone's data unattended.
    """
    if not cfg["bridge"]["delete_notebooks"]:
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
            chat=SimpleNamespace(ask=lambda nb, q: _coro(SimpleNamespace(answer=f"answer to {q}"))),
            notebooks=SimpleNamespace(
                create=lambda title: _coro(SimpleNamespace(id="nb-1", title=title)),
                get_or_none=lambda nb: _coro(SimpleNamespace(id=nb) if nb == "nb-1" else None),
                get_share_url=lambda nb, artifact_id=None: _coro(f"https://notebooklm/{nb}"),
                delete=lambda nb: _coro(calls.append(f"delete:{nb}"))),
            sources=SimpleNamespace(add_file=add_file, add_url=add_url,
                                    wait_all_until_ready=ready_all),
            artifacts=SimpleNamespace(
                poll_status=poll,
                list=lambda nb: _coro([
                    SimpleNamespace(id="t-audio", duration_seconds=1149.88, title="a"),
                    SimpleNamespace(id="t-video", duration_seconds=467.12, title="v"),
                    SimpleNamespace(id="t-quiz", duration_seconds=None, title="q")])))
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
        assert "generate:video" in calls, "the video overview is part of the default set"
        assert "generate:slide_deck" not in calls, "slides are opt-in alongside it"
        # Each artifact is asked for with its own prompt, not one instruction reused four times.
        lengths = {c.split(":")[1]: int(c.split(":")[2]) for c in calls if c.startswith("instructions:")}
        assert len(set(lengths.values())) == len(lengths), f"prompts must differ per kind: {lengths}"
        assert all(v > 200 for v in lengths.values()), "each prompt is a real instruction"

        assert "add_url:https://example.org/v" in calls, "urls go up alongside files"
        assert any(c.startswith("wait:") for c in calls), \
            "sources must finish indexing before generation, or the artifact comes back empty"
        assert calls.index("wait:3") < calls.index("generate:audio")

        # Which artifacts a lesson gets is a config line, not a code change.
        wide = {**cfg, "bridge": {**cfg["bridge"], "artifacts": ["audio", "slides"]}}
        calls.clear()
        assert set(start("x", ["/tmp/a.pdf"], wide, depth=3)["jobs"]) == {"audio", "slides"}

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
            assert {a["kind"] for a in got} == set(cfg["bridge"]["artifacts"])
            assert all(a["bytes"] > 0 and Path(a["path"]).exists() for a in got)
            by_kind = {a["kind"]: a for a in got}
            assert by_kind["audio"]["duration"] == 1149.88, "durations come back with the artifacts"
            assert by_kind["quiz"]["duration"] is None, "and a quiz simply has none"

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
        assert "delete:nb-1" not in calls, \
            "a failed setup leaves its empty notebook for the user to decide about"

        # A notebook can be rebuilt from the sources alone, with nothing generated.
        calls.clear()
        globals()["_context"] = fake_client()
        nb = upload_only("x", ["/tmp/a.pdf"], ["https://example.org/v"], cfg)
        assert nb == "nb-1"
        assert not any(c.startswith("generate:") for c in calls), \
            "rebuilding for a question must not spend generation quota"
        assert "add_file:a.pdf" in calls and "add_url:https://example.org/v" in calls

        assert alive("nb-1", cfg) and not alive("gone", cfg)
        assert getattr(ask("nb-1", "why does it work?", cfg), "answer") == "answer to why does it work?"
        assert share_url("nb-1", cfg).endswith("nb-1")

        # Tidying must never take down a lesson whose artifacts are already on disk.
        globals()["_context"] = fake_client()
        calls.clear()
        discard({"notebook_id": "nb-1"}, cfg)
        assert "delete:nb-1" not in calls, "nothing is deleted unless explicitly configured"
        discard({"notebook_id": "nb-1"},
                {**cfg, "bridge": {**cfg["bridge"], "delete_notebooks": True}})
        assert "delete:nb-1" in calls, "and it still works when it is"
    finally:
        globals()["_context"] = real_ctx

    print("bridge selfcheck ok")


if __name__ == "__main__":
    _selfcheck()
