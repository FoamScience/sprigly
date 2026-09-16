"""Proposing what could be taught next.

Two modes, one code path: unfocused it prospects unrelated candidates, focused it decomposes one
topic into an ordered syllabus. The prompts are text files, not code — they are the part that gets
tuned, and they should be editable without a release.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

from . import store, tags

log = logging.getLogger(__name__)
PROMPTS = Path(__file__).parent / "prompts"


class CuratorError(RuntimeError):
    pass


def render(name: str, **ctx) -> str:
    """Placeholders are {{name}}, not str.format — the templates are full of JSON braces."""
    text = (PROMPTS / f"{name}.md").read_text()
    for key, value in ctx.items():
        text = text.replace("{{" + key + "}}", str(value))
    left = re.findall(r"\{\{(\w+)\}\}", text)
    if left:
        raise CuratorError(f"unfilled placeholders: {sorted(set(left))}")
    return text


# Both CLIs already emit newline-delimited JSON events, so watching an agent work needs no SDK
# and no extra dependency — only the right flag and a parser per backend.
STREAM_FLAGS = {"claude": ["--output-format", "stream-json", "--verbose"],
                "opencode": ["--format", "json"]}


def _argv(backend: str, model: str, prompt: str, streaming: bool) -> list[str]:
    if backend == "claude":
        argv = ["claude", "-p", prompt, "--model", model]
    elif backend == "opencode":
        argv = ["opencode", "run", "-m", model, prompt]
    else:
        raise CuratorError(f"unknown agent backend {backend!r}")
    return argv + (STREAM_FLAGS[backend] if streaming else [])


def _event(backend: str, ev: dict) -> tuple[str, str | None]:
    """One streamed event -> (text it contributes, a line worth showing).

    The two backends disagree about everything except being NDJSON, so the differences are absorbed
    here rather than leaking into the caller.
    """
    kind = ev.get("type")
    if backend == "opencode":
        part = ev.get("part") or {}
        if kind == "text":
            return part.get("text") or "", None
        if kind == "reasoning":
            return "", "thinking"
        if kind == "tool":
            return "", f"tool {part.get('tool') or part.get('name') or '?'}"
        if kind == "step_finish":
            tok = (part.get("tokens") or {}).get("total")
            return "", f"finished, {tok} tokens" if tok else "finished"
        return "", None
    if kind == "assistant":
        text, note = "", None
        for block in (ev.get("message") or {}).get("content") or []:
            if block.get("type") == "text":
                text += block.get("text") or ""
            elif block.get("type") == "thinking":
                note = "thinking"
            elif block.get("type") == "tool_use":
                note = f"tool {block.get('name')}"
        return text, note
    if kind == "result":
        usage = ev.get("usage") or {}
        tok = (usage.get("input_tokens") or 0) + (usage.get("output_tokens") or 0)
        return "", f"finished, {tok} tokens" if tok else "finished"
    return "", None


def run_agent(prompt: str, cfg: dict, role: str = "bulk", report=None) -> str:
    """Shell out to whichever agent CLI is configured. No SDK; the prompt is the product.

    With a `report` callback the run is streamed, so a several-minute agent call shows what it is
    doing instead of looking like a hang. Without one it is a plain blocking call.
    """
    a = cfg["agent"]
    backend = a["backend"]
    try:
        model = a["models"][backend][role]
    except KeyError:
        raise CuratorError(f"no model configured for backend {backend!r} role {role!r}") from None
    argv = _argv(backend, model, prompt, streaming=bool(report))
    if not shutil.which(argv[0]):
        raise CuratorError(f"{argv[0]} is not on PATH")

    if not report:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=a["timeout_seconds"])
        if done.returncode != 0:
            raise CuratorError(f"{argv[0]} exited {done.returncode}: {done.stderr.strip()[:200]}")
        return done.stdout

    report(f"asking {backend} {model}")
    deadline = time.monotonic() + a["timeout_seconds"]
    collected, shown = [], 0
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            bufsize=1)
    try:
        for line in proc.stdout:
            if time.monotonic() > deadline:
                proc.kill()
                raise CuratorError(f"{argv[0]} exceeded {a['timeout_seconds']}s")
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            text, note = _event(backend, ev)
            if text:
                collected.append(text)
                total = sum(len(t) for t in collected)
                # A preview every few hundred characters: enough to see it working, not a firehose.
                if total - shown >= 400:
                    shown = total
                    report(f"{total} chars: {text.strip()[-70:]}")
            if note:
                report(note)
        proc.wait(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
    if proc.returncode != 0:
        raise CuratorError(f"{argv[0]} exited {proc.returncode}: "
                           f"{(proc.stderr.read() or '').strip()[:200]}")
    return "".join(collected)


def extract_json(text: str) -> list[dict]:
    """Pull the JSON array out of whatever the agent wrapped it in.

    Agents narrate. Asking for bare JSON is necessary but never sufficient, so find the array
    rather than trusting the whole of stdout to parse.
    """
    fenced = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.S)
    if fenced:
        return json.loads(fenced.group(1))
    start = text.find("[")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        break
        start = text.find("[", start + 1)
    raise CuratorError("no JSON array in the agent's output")


def validate(items: list) -> list[dict]:
    """Reject rather than repair. A malformed proposal is cheap to ask for again."""
    if not isinstance(items, list) or not items:
        raise CuratorError("expected a non-empty JSON array")
    out = []
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            raise CuratorError(f"item {i} is not an object")
        topic = str(it.get("topic", "")).strip()
        if not topic:
            raise CuratorError(f"item {i} has no topic")
        depth = it.get("depth", 3)
        if not isinstance(depth, int) or not 1 <= depth <= 5:
            raise CuratorError(f"item {i} has depth {depth!r}, expected an integer 1-5")
        for key in ("tags", "prereqs"):
            if not isinstance(it.get(key, []), list):
                raise CuratorError(f"item {i} field {key} is not a list")
        out.append({
            "topic": topic,
            "domain": str(it.get("domain", "")).strip(),
            "why": str(it.get("why", "")).strip(),
            "depth": depth,
            "tags": [str(t) for t in it.get("tags", [])],
            "prereqs": [str(t) for t in it.get("prereqs", [])],
            "est_minutes": int(it.get("est_minutes") or 0) or None,
        })
    return out


def ask(prompt: str, cfg: dict, role: str = "bulk", runner=run_agent, on_retry=None,
        validator=None, report=None) -> list[dict]:
    """Ask, validate, and on malformed output ask again with the complaint attached.

    The validator is a parameter because not every prompt returns lessons — the relevance pass
    returns index picks. Defaulting it to the lesson schema silently rejected every reply and left
    the caller's fallback path to swallow it.
    """
    validator = validator or validate
    attempt, last = 0, None
    while attempt <= cfg["agent"]["max_retries"]:
        text = runner(prompt if attempt == 0 else
                      f"{prompt}\n\nYour previous reply was rejected: {last}\nReturn only the JSON array.",
                      cfg, role, report=report)
        try:
            return validator(extract_json(text))
        except (CuratorError, json.JSONDecodeError) as err:
            last = str(err)
            log.warning("curator attempt %d rejected: %s", attempt + 1, last)
            attempt += 1
            if on_retry:
                on_retry(attempt, last)
    raise CuratorError(f"agent returned unusable output {attempt} times: {last}")


def _context(conn: sqlite3.Connection, cfg: dict, track_id: int | None) -> dict:
    scope = " AND track_id=?" if track_id else ""
    args = (track_id,) if track_id else ()
    recent = [r[0] for r in conn.execute(
        "SELECT topic FROM lesson WHERE state IN ('ready','consumed','reviewed')" + scope
        + " ORDER BY updated_at DESC LIMIT 20", args)]
    # Candidates already waiting count as covered ground too. Without them every curate run
    # re-proposes the standing pool at a slightly different grain.
    pending = [r[0] for r in conn.execute(
        "SELECT topic FROM lesson WHERE state IN ('proposed','picked','harvesting','uploading',"
        "'generating') " + scope.replace(" AND", "AND") + " ORDER BY id DESC LIMIT 40", args)]
    known = tags.known_tags(conn)
    return {
        "recent": "\n".join(f"- {t}" for t in recent) or "- nothing yet",
        "pending": "\n".join(f"- {t}" for t in pending) or "- nothing yet",
        "known_tags": ", ".join(known) or "none yet",
        "minutes": cfg["lesson"]["budget_minutes"],
    }


def build_prompt(conn: sqlite3.Connection, cfg: dict, track_id: int | None = None,
                 n: int | None = None) -> tuple[str, str, str]:
    """The prompt, the model role it should run at, and the language of what it proposes."""
    ctx = _context(conn, cfg, track_id)
    if track_id:
        row = conn.execute("SELECT goal, depth, language FROM track WHERE id=?", (track_id,)).fetchone()
        if not row:
            raise CuratorError(f"no track {track_id}")
        depth = row["depth"]
        prompt = render("syllabus", goal=row["goal"], depth=depth,
                        scope=cfg["depth"][str(depth)]["scope"],
                        n=n or cfg["lesson"]["max_syllabus"], **ctx)
        return prompt, "judgement", row["language"]
    else:
        goals = [f"- {r['goal']} (depth {r['depth']})" for r in
                 conn.execute("SELECT goal, depth FROM track WHERE status='open'")]
        prompt = render("prospect", tracks="\n".join(goals) or "- none yet",
                        n=n or 8, **ctx)
        return prompt, "bulk", cfg["lesson"]["default_language"]


def propose(conn: sqlite3.Connection, cfg: dict, track_id: int | None = None,
            n: int | None = None, runner=run_agent, on_retry=None, report=None) -> list[int]:
    """Write proposed lessons. Focused on a track it decomposes; otherwise it prospects."""
    prompt, role, language = build_prompt(conn, cfg, track_id, n)
    items = ask(prompt, cfg, role, runner, on_retry, report=report)
    ids = []
    for it in items:
        lid = conn.execute(
            "INSERT INTO lesson (topic, domain, track_id, depth, language, est_minutes)"
            " VALUES (?,?,?,?,?,?)",
            (it["topic"], tags.normalize(it["domain"]) or None, track_id,
             it["depth"], language, it["est_minutes"])).lastrowid
        tags.write(conn, lid, it["tags"], "tag", cfg)
        tags.write(conn, lid, it["prereqs"], "prereq", cfg)
        store.log_event(conn, "proposed", lid, json.dumps({"why": it["why"]}))
        ids.append(lid)
    log.info("curator proposed %d lessons for track %s", len(ids), track_id)
    return ids


def _selfcheck() -> None:
    import tempfile

    from . import config

    # --- extraction survives the ways an agent actually replies
    bare = '[{"topic": "a", "depth": 3}]'
    assert extract_json(bare)[0]["topic"] == "a"
    assert extract_json("Sure! Here you go:\n```json\n" + bare + "\n```\nHope that helps")[0]["topic"] == "a"
    assert extract_json("prose [not json] then\n" + bare)[0]["topic"] == "a", \
        "a bracketed aside before the array must not derail it"
    assert extract_json('[{"topic": "a [b] c", "depth": 3}]')[0]["topic"] == "a [b] c", \
        "brackets inside strings are not nesting"
    try:
        extract_json("no array here")
        raise AssertionError("must refuse output with no array")
    except CuratorError:
        pass

    # --- validation rejects rather than repairs
    for bad, why in (
        ([], "empty array"),
        ([{"depth": 3}], "missing topic"),
        ([{"topic": "a", "depth": 9}], "depth out of range"),
        ([{"topic": "a", "depth": "3"}], "depth not an integer"),
        ([{"topic": "a", "depth": 3, "tags": "rbf"}], "tags not a list"),
        (["a string"], "item not an object"),
    ):
        try:
            validate(bad)
            raise AssertionError(f"should have rejected: {why}")
        except CuratorError:
            pass
    assert validate([{"topic": " a ", "depth": 3}])[0]["topic"] == "a"

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = config.load(path=root / "absent.toml", root=root)
        conn = store.connect(cfg["paths"]["db"])

        calls = []

        def flaky(prompt, cfg, role, report=None):
            calls.append(prompt)
            if len(calls) == 1:
                return "I'd suggest a few things, but here is no array."
            return ('```json\n[{"topic": "how RBF-FD builds a stencil", "domain": "Numerics",'
                    ' "why": "because", "depth": 3, "tags": ["RBF-FD", "Stencils"],'
                    ' "prereqs": ["Linear Algebra"], "est_minutes": 22}]\n```')

        ids = propose(conn, cfg, runner=flaky)
        assert len(calls) == 2, "a rejected reply is retried with the complaint attached"
        assert "nothing yet" in calls[0], "an empty store has no pending list"
        assert "previous reply was rejected" in calls[1]
        assert len(ids) == 1

        row = conn.execute("SELECT * FROM lesson WHERE id=?", (ids[0],)).fetchone()
        assert row["state"] == "proposed" and row["est_minutes"] == 22
        assert row["domain"] == "numerics", "domains are normalised like tags"
        got = {r[0] for r in conn.execute("SELECT tag FROM lesson_tag WHERE kind='tag'")}
        assert got == {"rbf-fd", "stencils"}, f"tags normalised on write, got {got}"
        assert {r[0] for r in conn.execute("SELECT tag FROM lesson_tag WHERE kind='prereq'")} \
            == {"linear-algebra"}
        assert conn.execute("SELECT count(*) FROM event WHERE kind='proposed'").fetchone()[0] == 1

        # --- an agent that never complies gives up instead of looping
        cfg["agent"]["max_retries"] = 1
        try:
            propose(conn, cfg, runner=lambda *a, **k: "never any json")
            raise AssertionError("must give up")
        except CuratorError as err:
            assert "unusable output" in str(err)

        # --- the syllabus path needs a track, and refuses a missing one
        t = conn.execute("INSERT INTO track (goal, depth) VALUES ('meshless methods', 4)").lastrowid
        seen = {}

        def capture(prompt, cfg, role, report=None):
            seen["prompt"], seen["role"] = prompt, role
            return '[{"topic": "shape parameter choice", "domain": "numerics", "depth": 4}]'

        ids = propose(conn, cfg, track_id=t, runner=capture)
        assert seen["role"] == "judgement", "decomposition uses the stronger model"
        assert "meshless methods" in seen["prompt"] and "single design decision" in seen["prompt"]
        assert conn.execute("SELECT track_id FROM lesson WHERE id=?", (ids[0],)).fetchone()[0] == t
        try:
            propose(conn, cfg, track_id=999, runner=capture)
            raise AssertionError("must refuse an unknown track")
        except CuratorError:
            pass

        # --- known tags are fed back so the vocabulary converges instead of drifting
        assert "rbf-fd" in seen["prompt"]
        # --- and so are the candidates already waiting, or every run re-proposes the same ground
        assert "how RBF-FD builds a stencil".lower() in seen["prompt"].lower()
        conn.close()

    # Streamed events from either backend fold into (text, note); everything else is noise.
    assert _event("opencode", {"type": "text", "part": {"text": "hi"}}) == ("hi", None)
    assert _event("opencode", {"type": "step_finish", "part": {"tokens": {"total": 42}}}) \
        == ("", "finished, 42 tokens")
    assert _event("opencode", {"type": "tool", "part": {"tool": "read"}}) == ("", "tool read")
    assert _event("claude", {"type": "assistant",
                             "message": {"content": [{"type": "text", "text": "hi"}]}}) == ("hi", None)
    assert _event("claude", {"type": "assistant",
                             "message": {"content": [{"type": "thinking", "thinking": ""}]}}) \
        == ("", "thinking")
    assert _event("claude", {"type": "system", "subtype": "hook_started"}) == ("", None), \
        "hook chatter is not progress"
    assert _event("claude", {"type": "result", "usage": {"input_tokens": 10, "output_tokens": 5}}) \
        == ("", "finished, 15 tokens")
    assert _event("opencode", {"type": "unheard_of"}) == ("", None)

    assert _argv("opencode", "m", "p", streaming=True)[-2:] == ["--format", "json"]
    assert "--output-format" in _argv("claude", "m", "p", streaming=True)
    assert _argv("claude", "m", "p", streaming=False) == ["claude", "-p", "p", "--model", "m"], \
        "without a reporter the call stays a plain blocking one"

    try:
        render("prospect", n=1)
        raise AssertionError("an unfilled placeholder must be caught, not sent to the agent")
    except CuratorError as err:
        assert "unfilled placeholders" in str(err)

    print("curator selfcheck ok")


if __name__ == "__main__":
    _selfcheck()
