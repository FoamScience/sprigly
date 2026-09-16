"""Source gathering: search, gate, rank for relevance, fetch.

The split is deliberate. The gate decides credibility from metadata, in code. The agent decides
only relevance, among sources that already passed — it never gets a vote on rigour.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from pathlib import Path
from typing import Any

from . import curator, gate, sources
from .sources import Work

log = logging.getLogger(__name__)


def rank_relevance(topic: str, depth: int, works: list[Work], cfg: dict,
                   runner=None, report=None) -> tuple[list[Work], list[tuple[Work, str]]]:
    """Drop sources that merely share vocabulary with the topic.

    The gate cannot catch these: a paper can be peer-reviewed, open access and entirely about
    something else. Searching "meshfree radial basis function" on arXiv returns "Radial velocity
    follow-up of CoRoT transiting exoplanets" — impeccable, and about exoplanets.

    Degrades open. If the agent is unreachable or unusable, every source is kept and a note says
    so: the gate has already established these are credible, and silently harvesting nothing would
    be a worse failure than harvesting a few loose ones.
    """
    if not works:
        return [], []
    listing = "\n".join(
        f"{i}. {w.title} — {w.venue or 'unknown venue'}, {w.year or 'n.d.'} [{w.work_type or '?'}]"
        for i, w in enumerate(works, 1))
    prompt = curator.render("relevance", topic=topic, depth=depth,
                            scope=cfg["depth"][str(depth)]["scope"], candidates=listing)
    try:
        picks = curator.ask(prompt, cfg, "judgement", runner or curator.run_agent,
                            validator=_validate_picks, report=report)
    except Exception as err:
        # Deliberately broad. Degrading open is the whole point of this pass, and narrowing it to
        # CuratorError let a subprocess timeout through, which parked the lesson after 871 seconds
        # instead of harvesting the credible sources it already had.
        log.warning("relevance pass unusable, keeping every source: %s", err)
        return works, []
    keep_idx = {p["n"] for p in picks if isinstance(p.get("n"), int)}
    keep = [w for i, w in enumerate(works, 1) if i in keep_idx]
    dropped = [(w, "off topic") for i, w in enumerate(works, 1) if i not in keep_idx]
    return (keep or works), dropped


def _validate_picks(items: list) -> list[dict]:
    """The relevance pass returns index picks, not lessons, so it needs its own schema."""
    if not isinstance(items, list):
        raise curator.CuratorError("expected a JSON array of picks")
    out = []
    for i, it in enumerate(items):
        if not isinstance(it, dict) or not isinstance(it.get("n"), int):
            raise curator.CuratorError(f"pick {i} has no integer 'n'")
        out.append({"n": it["n"], "why": str(it.get("why", ""))})
    return out


def _slug(text: str, limit: int = 60) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:limit] or "source"


def _is_pdf(head: bytes) -> bool:
    """A PDF starts with %PDF. Publishers answer pdf_url with paywall and consent pages that are
    perfectly valid HTML, and saving one under a .pdf name feeds NotebookLM an error page as a
    source — which looks like a successful harvest right up until the podcast is about nothing."""
    return head.lstrip()[:4] == b"%PDF"


def _save_text(w: Work, dest: Path, text: str, cfg: dict) -> str | None:
    """Extracted prose is only worth keeping if there is enough of it to teach from.

    A landing page yields an abstract and a cookie banner. Below the floor the source is better
    sent to NotebookLM as a URL, which can do its own retrieval, than as a stub file."""
    limit = cfg["sources"]["max_bytes"]
    if len(text) < cfg["sources"]["min_text_bytes"]:
        log.info("only %d chars extracted from %s, sending it as a url instead", len(text), w.url)
        return None
    path = dest / f"{_slug(w.title)}.txt"
    path.write_text(f"# {w.title}\n\nSource: {w.url}\n\n{text[:limit]}")
    return str(path)


def fetch(w: Work, dest: Path, cfg: dict) -> str | None:
    """Pull the source down so the bridge uploads a file, not a URL.

    Files beat URLs twice over: the upload is more reliable, and the lesson survives link rot.
    """
    import httpx

    limit = cfg["sources"]["max_bytes"]
    timeout = cfg["sources"]["fetch_timeout_seconds"]
    dest.mkdir(parents=True, exist_ok=True)
    stem = _slug(w.title)

    if w.pdf_url:
        path = dest / f"{stem}.pdf"
        try:
            with httpx.stream("GET", w.pdf_url, timeout=timeout, follow_redirects=True) as r:
                r.raise_for_status()
                written, first, ok = 0, True, True
                with path.open("wb") as fh:
                    for chunk in r.iter_bytes():
                        if first:
                            first = False
                            if not _is_pdf(chunk):
                                log.warning("%s answered with %s, not a pdf", w.pdf_url,
                                            r.headers.get("content-type", "unknown content"))
                                ok = False
                                break
                        written += len(chunk)
                        if written > limit:
                            log.warning("%s exceeds max_bytes, abandoning", w.pdf_url)
                            ok = False
                            break
                        fh.write(chunk)
            if ok and written:
                return str(path)
            path.unlink(missing_ok=True)
        except Exception as err:
            log.warning("pdf fetch failed for %s: %s", w.pdf_url, err)
            path.unlink(missing_ok=True)

    try:
        import trafilatura

        raw = trafilatura.fetch_url(w.url)
        text = trafilatura.extract(raw) if raw else None
    except Exception as err:
        log.warning("html extract failed for %s: %s", w.url, err)
        text = None
    return _save_text(w, dest, text, cfg) if text else None


def brief(topic: str, depth: int, kept: list[Work], admission: gate.Admission, cfg: dict) -> str:
    """The framing note that becomes the NotebookLM prompt, written next to the sources."""
    lines = [f"# {topic}", "",
             f"Depth {depth}: {cfg['depth'][str(depth)]['scope']}.",
             f"Target length: {cfg['depth'][str(depth)]['audio']}.", "",
             f"Evidence level: {admission.evidence or 'unknown'}.", ""]
    if admission.notes:
        lines += ["Caveats:"] + [f"- {n}" for n in admission.notes] + [""]
    lines += ["Sources:"]
    lines += [f"- {w.title} ({w.venue or 'unknown venue'}, {w.year or 'n.d.'})" for w in kept]
    return "\n".join(lines) + "\n"


def gather(topic: str, depth: int, dest: Path, cfg: dict, runner=None,
           report=None) -> list[dict[str, Any]]:
    """Search, gate, rank, fetch. Returns source records for the store."""
    say = report or (lambda _msg: None)
    budget = cfg["depth"][str(depth)]["sources"]
    found = sources.search(topic, limit=budget * 3, cfg=cfg, report=report)
    log.info("harvest %r: %d candidates", topic, len(found))
    say(f"{len(found)} candidates")

    say("ranking for relevance")
    relevant, off_topic = rank_relevance(topic, depth, found, cfg, runner, report)
    admission = gate.admit(relevant, depth, cfg)
    for w, why in off_topic:
        log.info("dropped %s: %s", w.title[:60], why)
    for w, why in admission.rejected:
        log.info("rejected %s: %s", w.title[:60], why)
    say(f"{len(off_topic)} off topic, {len(admission.rejected)} rejected, "
        f"{len(admission.accepted)} admitted ({admission.evidence or 'unknown'})")
    for note in admission.notes:
        say(note)

    records = []
    for w in admission.accepted:
        tier = gate.tier_of(w, cfg)
        records.append({
            "url": w.url, "doi": w.doi, "title": w.title, "venue": w.venue, "year": w.year,
            "work_type": w.work_type, "tier": tier, "evidence_level": gate.TIER_LEVEL[tier],
            "oa_status": "oa" if w.is_oa else None, "retracted": int(w.retracted),
            "local_path": None,
        })
        records[-1]["local_path"] = fetch(w, dest, cfg)
        got = records[-1]["local_path"]
        size = f"{Path(got).stat().st_size // 1024}k" if got and Path(got).exists() else \
            ("saved" if got else "url only")
        say(f"[{tier}] {size}  {w.title[:64]}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    (dest.parent / "brief.md").write_text(brief(topic, depth, admission.accepted, admission, cfg))
    log.info("harvest %r: %d admitted, status %s", topic, len(records), admission.status)
    return records


def _selfcheck() -> None:
    import tempfile

    from . import config

    cfg = config.load(path=Path("/nonexistent.toml"))
    depth = 3

    def work(title, **kw):
        kw.setdefault("venue", "JCP")
        kw.setdefault("venue_type", "journal")
        kw.setdefault("work_type", "article")
        kw.setdefault("is_oa", True)
        return Work(title=title, url=f"https://example.org/{_slug(title)}",
                    doi=f"10.1/{_slug(title)}", **kw)

    on_topic = work("how rbf-fd builds a stencil")
    off_topic = work("radial velocity follow-up of corot transiting exoplanets")

    keep_first = lambda prompt, cfg, role, report=None: '[{"n": 1, "why": "directly on topic"}]'
    kept, dropped = rank_relevance("rbf-fd stencils", depth, [on_topic, off_topic], cfg, keep_first)
    assert kept == [on_topic] and len(dropped) == 1, "an off-topic credible paper is dropped"

    # Degrade open: a broken relevance pass must not silently harvest nothing.
    for failure in (curator.CuratorError("agent down"),
                    subprocess.TimeoutExpired("opencode", 600),
                    OSError("no such binary"),
                    RuntimeError("something new")):
        boom = (lambda exc: lambda *a, **k: (_ for _ in ()).throw(exc))(failure)
        kept, dropped = rank_relevance("x", depth, [on_topic, off_topic], cfg, boom)
        assert kept == [on_topic, off_topic] and dropped == [], \
            f"a {type(failure).__name__} must keep everything, not park the lesson"

    keep_none = lambda *a, **k: '[{"n": 99, "why": "nothing matches"}]'
    kept, _ = rank_relevance("x", depth, [on_topic, off_topic], cfg, keep_none)
    assert len(kept) == 2, "an agent that rejects everything is not believed"

    assert rank_relevance("x", depth, [], cfg, boom) == ([], []), "no candidates, no agent call"

    # A publisher answering pdf_url with a consent page is the common case, not an edge one.
    assert _is_pdf(b"%PDF-1.5\n...")
    assert _is_pdf(b"\n  %PDF-1.4")
    assert not _is_pdf(b"<!DOCTYPE html>\n<html>")
    assert not _is_pdf(b"")

    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        assert _save_text(on_topic, d, "x" * 50, cfg) is None, "a stub extraction is not a source"
        kept = _save_text(on_topic, d, "x" * (cfg["sources"]["min_text_bytes"] + 10), cfg)
        assert kept and Path(kept).exists() and Path(kept).suffix == ".txt"

    with tempfile.TemporaryDirectory() as td:
        dest = Path(td) / "lessons" / "1" / "sources"
        catalogue = [work(f"paper {i}") for i in range(6)] + [off_topic]

        real_search, sources.search = sources.search, lambda q, limit, cfg, report=None: catalogue
        real_fetch = globals()["fetch"]
        globals()["fetch"] = lambda w, dest, cfg: str(dest / f"{_slug(w.title)}.pdf")
        try:
            keep_all = lambda *a, **k: json.dumps(
                [{"n": i, "why": "ok"} for i in range(1, len(catalogue))])
            got = gather("rbf-fd stencils", depth, dest, cfg, keep_all)
        finally:
            sources.search, globals()["fetch"] = real_search, real_fetch

        assert len(got) == 6, f"six on-topic papers admitted, got {len(got)}"
        assert all(r["tier"] == "A" and r["evidence_level"] == "peer-reviewed" for r in got)
        assert all(r["local_path"] and r["url"] and r["title"] for r in got)
        assert {"venue", "year", "work_type", "oa_status", "retracted"} <= set(got[0]), \
            "the store's columns are all populated, not just the ones the gate needed"

        note = (dest.parent / "brief.md").read_text()
        assert "rbf-fd stencils" in note and "peer-reviewed" in note
        assert "paper 0" in note and "exoplanets" not in note, \
            "the brief lists what was admitted, not what was searched"

    print("harvester selfcheck ok")


if __name__ == "__main__":
    _selfcheck()
