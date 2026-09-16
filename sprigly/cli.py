"""sprigly entrypoint."""

from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from pathlib import Path

import click

from . import config

log = logging.getLogger(__name__)


class _ConsoleWarnings(logging.Handler):
    """Mirror warnings to the terminal while a command runs.

    The file log is the record; a long command that silently drops sources is the problem. Only
    WARNING and above, so the console stays readable.
    """

    def __init__(self, console) -> None:
        super().__init__(level=logging.WARNING)
        self.console = console

    def emit(self, record: logging.LogRecord) -> None:
        mark = "[red]![/red]" if record.levelno >= logging.ERROR else "[yellow]![/yellow]"
        self.console.print(f"{mark} [dim]{record.name.removeprefix('sprigly.')}:[/dim] "
                           f"{record.getMessage()}")


def _echo_warnings(console):
    """Attach the console handler to sprigly's loggers for the duration of a command."""
    handler = _ConsoleWarnings(console)
    logging.getLogger("sprigly").addHandler(handler)
    return handler


def _setup_logging(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    logging.Formatter.converter = time.gmtime  # the Z suffix below must not lie
    logging.basicConfig(
        filename=path, level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%SZ")


@click.group()
@click.option("--config", "config_file", type=click.Path(path_type=Path),
              help="Config TOML to use instead of the XDG location.")
@click.pass_context
def main(ctx: click.Context, config_file) -> None:
    """Learn one thing at a time."""
    ctx.obj = config.load(path=config_file)
    _setup_logging(ctx.obj["paths"]["log"])


@main.command("config")
@click.pass_obj
def show_config(cfg: dict) -> None:
    """Print the resolved configuration, in the format the config file itself uses."""
    import tomli_w

    path = config.config_path()
    click.echo(f"# {path}  ({'in use' if path.is_file() else 'not present, showing defaults'})")
    click.echo(f"# copy any section below into that file to change it\n")
    # Paths are resolved absolute at load time and are derived, not settings, so they are shown
    # separately as comments rather than offered as something to paste back.
    settings = {k: v for k, v in cfg.items() if k != "paths"}
    click.echo(tomli_w.dumps(settings).rstrip())
    click.echo("\n# resolved paths")
    for name, value in sorted(cfg["paths"].items()):
        click.echo(f"#   {name:<8} {value}")


@main.command()
@click.option("--lesson", "lesson_ref", metavar="LESSON",
              help="Advance only this lesson, ignoring its backoff window.")
@click.pass_obj
def tick(cfg: dict, lesson_ref: str | None) -> None:
    """Advance every lesson one step. Safe to run from a timer."""
    from . import store, tick as ticker

    from rich.console import Console

    console = Console()
    _echo_warnings(console)
    with ticker.lock(cfg["paths"]["data"] / "tick.lock") as held:
        if not held:
            click.echo("another tick is running")
            return
        conn = store.connect(cfg["paths"]["db"])
        lesson = _ref(conn, lesson_ref) if lesson_ref else None
        started = time.monotonic()
        status = console.status("[dim]scanning[/dim]", spinner="dots")

        def step(event: str, row, detail: str) -> None:
            topic = (row["topic"] or "")[:48]
            if event == "begin":
                status.update(f"[cyan]{row['state']}[/cyan] [dim]{topic}[/dim]")
            elif event == "step":
                # Both: the line scrolls into the log above, and the spinner itself tracks the
                # current sub-step, so a long phase shows movement rather than one frozen caption.
                console.print(f"   [dim]{detail}[/dim]")
                status.update(f"[cyan]{row['state']}[/cyan] [dim]{detail[:70]}[/dim]")
            elif event == "done" and detail != row["state"]:
                console.print(f"[green]✓[/green] {row['id']:>3}  {row['state']} → {detail}"
                              f"  [dim]{topic}[/dim]")
            elif event == "done":
                console.print(f"[dim]·[/dim] {row['id']:>3}  {detail} (waiting)  [dim]{topic}[/dim]")
            else:
                console.print(f"[yellow]![/yellow] {row['id']:>3}  {row['state']} parked"
                              f"  [dim]{detail[:60]}[/dim]")

        status.start()
        try:
            moved = ticker.run(conn, cfg, on_step=step, only=lesson)
        finally:
            status.stop()
        # Only when something actually moved. Backing up an unchanged database every few minutes
        # would rotate the useful history out of the window.
        if any(moved.values()):
            store.backup(conn, cfg["paths"]["backups"], cfg["store"]["backup_keep"])
        conn.close()
    log.info("tick moved %s", dict(moved))
    summary = ", ".join(f"{v} -> {k}" for k, v in sorted(moved.items()) if v)
    console.print(f"[dim]{summary or 'nothing to do'} in {time.monotonic() - started:.0f}s[/dim]")


@main.command()
@click.argument("lesson_ref", metavar="LESSON", required=False)
@click.pass_obj
def status(cfg: dict, lesson_ref: str | None) -> None:
    """Pipeline state, parked failures and review load. With an id, inspect one lesson."""
    from rich.console import Console
    from rich.table import Table

    from . import store

    conn = store.connect(cfg["paths"]["db"])
    console = Console()

    if lesson_ref is not None:
        _show_lesson(conn, cfg, console, _ref(conn, lesson_ref))
        conn.close()
        return

    counts = store.counts_by_state(conn)
    table = Table("state", "lessons", title="pipeline")
    for state in store.STATES:
        if counts.get(state):
            table.add_row(state, str(counts[state]))
    console.print(table if counts else "[dim]no lessons yet[/dim]")

    rows = store.troubled(conn)
    if rows:
        t = Table("lesson", "topic", "state", "tries", "next try", "error",
                  title="needs attention")
        for r in rows:
            t.add_row(r["slug"] or str(r["id"]), r["topic"], r["state"], str(r["retry_count"]),
                      r["next_attempt_at"] or "-", (r["last_error"] or "")[:60])
        console.print(t)

    due, nxt = store.due_cards(conn)
    console.print(f"cards due now: {due}" + (f" · next due {nxt}" if nxt else ""))
    conn.close()


def _choose(menu: list, cfg: dict) -> list[int]:
    """Fuzzy-pick from the menu, falling back to a numbered table. Returns 1-based indices.

    fzf needs a terminal on both ends, so a piped or redirected stdin — a script, a test — has to
    keep working rather than blowing up inside the subprocess.
    """
    lines = [f"{i:>2}  {o.kind:<6} {o.candidate.domain:<15} {o.candidate.est_minutes:>3}m "
             f"{o.score:.2f}  {o.candidate.topic}"
             f"   ({o.candidate.slug or o.candidate.id})   [{_why(o, cfg)}]"
             for i, o in enumerate(menu, 1)]
    if sys.stdin.isatty() and sys.stdout.isatty():
        from iterfzf import iterfzf

        picked = iterfzf(lines, prompt="pick> ", exact=False, multi=True,
                         header="tab to mark several, enter to take, esc to skip all")
        return [int(line.split()[0]) for line in (picked or [])]

    from rich.console import Console
    from rich.table import Table

    table = Table("#", "kind", "lesson", "domain", "min", "score", "why")
    for i, o in enumerate(menu, 1):
        table.add_row(str(i), o.kind, o.candidate.slug or o.candidate.topic, o.candidate.domain,
                      str(o.candidate.est_minutes), f"{o.score:.2f}", _why(o, cfg))
    Console().print(table)
    raw = click.prompt("pick", default="", show_default=False,
                       prompt_suffix=" (numbers, comma-separated; empty to skip all): ")
    out = []
    for part in str(raw).replace(",", " ").split():
        if part.isdigit() and 1 <= int(part) <= len(menu):
            out.append(int(part))
    return out


def _why(offer, cfg: dict) -> str:
    """The two signals contributing most to this score. The full breakdown is in the event log."""
    from . import picker

    w = cfg["scoring"]
    ranked = sorted(picker.SIGNALS, key=lambda s: -w[s] * offer.signals[s])[:2]
    return " ".join(f"{s.replace('_', '-')} {offer.signals[s]:.2f}" for s in ranked)


@main.command()
@click.option("--budget", type=int, help="Minutes available today.")
@click.option("-k", "k", type=int, help="How many to offer (default from config).")
@click.option("--all", "show_all", is_flag=True,
              help="Offer every eligible candidate instead of a selected menu.")
@click.pass_obj
def next(cfg: dict, budget: int | None, k: int | None, show_all: bool) -> None:
    """Offer the next lessons and record which one you take."""
    from rich.console import Console
    from rich.table import Table

    from . import picker, snapshot, store

    conn = store.connect(cfg["paths"]["db"])
    snap = snapshot.load(conn, cfg)
    if budget:
        snap.budget_minutes = budget
    pool, due = snapshot.candidates(conn), snapshot.due_reviews(conn)
    if k:
        cfg["offer"]["k"] = k
    menu = picker.offer_all(pool, due, snap, cfg) if show_all \
        else picker.offer(pool, due, snap, cfg)
    if not menu:
        click.echo("nothing to offer — run `sprigly curate` to propose lessons,"
                   " or `sprigly status` to see what is in flight")
        conn.close()
        return

    # One id per invocation, stamped on every event of this offering. Without it the fit cannot
    # tell which candidates competed against each other, and the choice sets are unrecoverable.
    choice_set = uuid.uuid4().hex[:12]
    for o in menu:
        store.log_event(conn, "offered", o.candidate.id,
                        json.dumps({"choice_set": choice_set, "kind": o.kind, "score": o.score,
                                     "signals": o.signals}))

    for idx in _choose(menu, cfg):
        taken = menu[idx - 1]
        store.log_event(conn, "picked", taken.candidate.id,
                        json.dumps({"choice_set": choice_set, "kind": taken.kind}))
        if taken.kind == "new":
            conn.execute("UPDATE lesson SET state='picked', score=?, updated_at=? WHERE id=?",
                         (taken.score, store.utcnow(), taken.candidate.id))
            name = taken.candidate.slug or taken.candidate.id
            click.echo(f"picked {name}: {taken.candidate.topic}"
                       f" — run `sprigly tick` to harvest and generate it")
        else:
            click.echo(f"review {taken.candidate.id}: {taken.candidate.topic}")
    conn.close()


@main.command()
@click.option("--track", type=int, help="Decompose this track instead of prospecting.")
@click.option("-n", type=int, help="How many lessons to ask for.")
@click.option("--lang", help="Language for the generated material, e.g. de. Default from config.")
@click.option("--dry-run", is_flag=True, help="Print the prompt without calling the agent.")
@click.pass_obj
def curate(cfg: dict, track: int | None, n: int | None, lang: str | None, dry_run: bool) -> None:
    """Ask the agent for candidate lessons."""
    from . import curator, store

    conn = store.connect(cfg["paths"]["db"])
    if lang:
        cfg = {**cfg, "lesson": {**cfg["lesson"], "default_language": lang}}
    if dry_run:
        prompt, role, _ = curator.build_prompt(conn, cfg, track, n)
        backend = cfg["agent"]["backend"]
        model = cfg["agent"]["models"].get(backend, {}).get(role, "?")
        click.echo(f"# backend={backend} role={role} model={model}\n")
        click.echo(prompt)
    else:
        from rich.console import Console
        from rich.table import Table

        console = Console()
        _echo_warnings(console)
        backend = cfg["agent"]["backend"]
        _, role, _ = curator.build_prompt(conn, cfg, track, n)
        model = cfg["agent"]["models"].get(backend, {}).get(role, "?")
        goal = conn.execute("SELECT goal FROM track WHERE id=?", (track,)).fetchone() if track else None
        console.print(f"[dim]·[/dim] {'decomposing ' + goal['goal'] if goal else 'prospecting'}"
                      f" [dim]via {backend} {model}[/dim]")

        started = time.monotonic()
        opening = f"decomposing {goal['goal']}" if goal else "exploring topics"
        status = console.status(f"[dim]{opening}[/dim]", spinner="dots")

        def say(msg: str) -> None:
            console.print(f"   [dim]{msg}[/dim]")
            status.update(f"[cyan]{msg[:70]}[/cyan]")

        status.start()
        try:
            ids = curator.propose(
                conn, cfg, track, n,
                on_retry=lambda attempt, err: status.update(
                    f"[yellow]retry {attempt}[/yellow] [dim]{err[:60]}[/dim]"),
                report=say)
        except curator.CuratorError as err:
            raise click.ClickException(str(err))
        finally:
            status.stop()

        console.print(f"[green]✓[/green] {len(ids)} proposed in {time.monotonic() - started:.0f}s")
        table = Table("lesson", "topic", "domain", "depth", box=None, pad_edge=False)
        for r in conn.execute(
                f"SELECT id, slug, topic, COALESCE(domain,'') AS domain, depth FROM lesson"
                f" WHERE id IN ({','.join('?' * len(ids))})", ids):
            table.add_row(r["slug"] or str(r["id"]), r["topic"], r["domain"], str(r["depth"]))
        console.print(table)
        console.print("[dim]run `sprigly next` to pick a lesson[/dim]")
    conn.close()


def _show_lesson(conn, cfg: dict, console, lesson_id: int) -> None:
    """What this lesson is carrying, and what the next tick would actually have to work with."""
    from rich.table import Table

    row = conn.execute("SELECT * FROM lesson WHERE id=?", (lesson_id,)).fetchone()
    if not row:
        raise click.ClickException(f"no lesson {lesson_id}")

    console.print(f"[bold]{row['slug'] or row['id']}[/bold]  {row['topic']}"
                  f"  [cyan]{row['state']}[/cyan]  [dim]#{row['id']}[/dim]")
    facts = Table(box=None, show_header=False, pad_edge=False)
    for label, value in (
            ("domain", row["domain"]), ("track", row["track_id"]), ("depth", row["depth"]),
            ("language", row["language"]),
            ("minutes", f"est {row['est_minutes']} / actual {row['actual_minutes']}"),
            ("thin", bool(row["thin"])), ("notebook", row["notebook_id"]),
            ("retries", row["retry_count"]), ("next try", row["next_attempt_at"]),
            ("last error", row["last_error"])):
        if value not in (None, "", 0, False) or label in ("retries", "minutes"):
            facts.add_row(f"[dim]{label}[/dim]", str(value))
    console.print(facts)

    for kind in ("tag", "prereq"):
        vals = [r[0] for r in conn.execute(
            "SELECT tag FROM lesson_tag WHERE lesson_id=? AND kind=? ORDER BY tag",
            (lesson_id, kind))]
        if vals:
            console.print(f"[dim]{kind}s[/dim] {', '.join(vals)}")

    srcs = conn.execute(
        "SELECT s.* FROM source s JOIN lesson_source ls ON ls.source_id=s.id"
        " WHERE ls.lesson_id=? ORDER BY s.tier, s.year DESC", (lesson_id,)).fetchall()
    if srcs:
        table = Table("tier", "evidence", "file", "year", "title", title="sources")
        files = urls = 0
        for s in srcs:
            path = Path(s["local_path"]) if s["local_path"] else None
            if path and path.exists():
                mark, files = f"[green]{path.stat().st_size // 1024}k[/green]", files + 1
            elif path:
                mark = "[red]missing[/red]"
            else:
                mark, urls = "[yellow]url only[/yellow]", urls + 1
            table.add_row(s["tier"], s["evidence_level"], mark, str(s["year"] or "-"),
                          (s["title"] or s["url"])[:58])
        console.print(table)
        # This is what `tick` would hand the bridge: files upload directly, the rest go up as links.
        console.print(f"[dim]uploadable: {files} file(s) + {urls} url(s) of {len(srcs)}[/dim]")

    note = cfg["paths"]["lessons"] / str(lesson_id) / "brief.md"
    console.print(f"[dim]brief.md[/dim] {'present' if note.exists() else '[red]absent[/red]'}")

    arts = conn.execute("SELECT kind, path, mime, bytes, duration FROM artifact"
                        " WHERE lesson_id=? ORDER BY kind", (lesson_id,)).fetchall()
    if arts:
        table = Table("kind", "size", "mime", "path", title="artifacts")
        for a in arts:
            table.add_row(a["kind"], f"{(a['bytes'] or 0) // 1024}k", a["mime"] or "-",
                          str(a["path"]))
        console.print(table)

    if row["job_ref"]:
        console.print(f"[dim]jobs[/dim] {row['job_ref']}")


@main.command()
@click.argument("lesson_ref", metavar="LESSON")
@click.option("--from", "stage", type=click.Choice(["harvest", "upload", "freshen"]),
              default="harvest",
              show_default=True, help="Which phase to run again.")
@click.option("--lang", help="Regenerate in this language, e.g. de.")
@click.pass_obj
def redo(cfg: dict, lesson_ref: str, stage: str, lang: str | None) -> None:
    """Rewind a lesson so the next tick re-runs a phase.

    `--from harvest` throws away its sources and brief and searches again. `--from upload` keeps the
    sources but discards the notebook and artifacts. Either way the old notebook is deleted, so a
    redo does not quietly consume the account's notebook cap.
    """
    from rich.console import Console

    from . import store, tick as ticker

    conn = store.connect(cfg["paths"]["db"])
    _echo_warnings(Console())
    try:
        state = ticker.redo(conn, cfg, lesson_id, stage)
    except ValueError as err:
        raise click.ClickException(str(err))
    if lang:
        conn.execute("UPDATE lesson SET language=? WHERE id=?", (lang, lesson_id))
        click.echo(f"language set to {lang}")
    _, discarded = ticker.REDO_STAGES[stage]
    click.echo(f"lesson {lesson_id} rewound to {state}; discarded {discarded}")
    click.echo(f"run `sprigly tick --lesson {lesson_id}` to run that phase again")
    conn.close()


@main.command()
@click.argument("lesson_ref", metavar="LESSON")
@click.argument("question", nargs=-1, required=True)
@click.pass_obj
def ask(cfg: dict, lesson_ref: str, question: tuple[str, ...]) -> None:
    """Ask a question about a lesson, answered from its own sources.

    Notebooks are deleted once their artifacts are downloaded, so the first question about an older
    lesson rebuilds one from the sources still on disk. That is slower than the rest, and cheaper
    than keeping every notebook alive against the account's cap.
    """
    from rich.console import Console
    from rich.markdown import Markdown

    from . import bridge, store

    console = Console()
    _echo_warnings(console)
    conn = store.connect(cfg["paths"]["db"])
    lesson_id = _ref(conn, lesson_ref)
    row = conn.execute("SELECT * FROM lesson WHERE id=?", (lesson_id,)).fetchone()

    notebook = row["notebook_id"]
    if not notebook or not bridge.alive(notebook, cfg):
        srcs = conn.execute(
            "SELECT s.local_path, s.url FROM source s JOIN lesson_source ls ON ls.source_id=s.id"
            " WHERE ls.lesson_id=?", (lesson_id,)).fetchall()
        if not srcs:
            raise click.ClickException(f"lesson {lesson_id} has no sources to answer from")
        with console.status("[dim]rebuilding the notebook from its sources[/dim]", spinner="dots"):
            notebook = bridge.upload_only(
                row["topic"],
                [s["local_path"] for s in srcs if s["local_path"]],
                [s["url"] for s in srcs if not s["local_path"]], cfg)
        conn.execute("UPDATE lesson SET notebook_id=? WHERE id=?", (notebook, lesson_id))
        store.log_event(conn, "asked", lesson_id, json.dumps({"notebook_id": notebook}))

    text = " ".join(question)
    with console.status("[dim]asking[/dim]", spinner="dots"):
        answer = bridge.ask(notebook, text, cfg)
    console.print(Markdown(str(getattr(answer, "answer", None) or answer)))
    store.log_event(conn, "asked", lesson_id, json.dumps({"question": text}))
    conn.close()


def _ref(conn, ref: str, table: str = "lesson") -> int:
    """Resolve what the user typed, turning an ambiguous reference into a useful error."""
    from . import refs

    try:
        return refs.resolve(conn, ref, table)
    except refs.Ambiguous as err:
        raise click.ClickException(str(err)) from None
    except LookupError as err:
        raise click.ClickException(str(err)) from None


def _mark_consumed(conn, lesson_id: int, via: str) -> bool:
    """Move a ready lesson on. Returns whether it actually moved."""
    from . import store

    row = conn.execute("SELECT state FROM lesson WHERE id=?", (lesson_id,)).fetchone()
    if not row:
        raise click.ClickException(f"no lesson {lesson_id}")
    store.log_event(conn, "consumed", lesson_id, json.dumps({"via": via}))
    if row["state"] == "ready":
        conn.execute("UPDATE lesson SET state='consumed', updated_at=? WHERE id=?",
                     (store.utcnow(), lesson_id))
        return True
    return False


@main.command()
@click.argument("lesson_ref", metavar="LESSON")
@click.option("--kind", default="audio", show_default=True,
              help="Which artifact to open: audio, video, slides, quiz.")
@click.pass_obj
def play(cfg: dict, lesson_ref: str, kind: str) -> None:
    """Open a lesson's artifact and record that you consumed it.

    This is the machine-side counterpart of deleting the file on the phone. Opening is taken as
    consumption because you asked for it by name — a lesson never advances on its own.
    """
    import shutil
    import subprocess

    from . import store

    conn = store.connect(cfg["paths"]["db"])
    lesson_id = _ref(conn, lesson_ref)
    art = conn.execute("SELECT path FROM artifact WHERE lesson_id=? AND kind=?",
                       (lesson_id, kind)).fetchone()
    if not art:
        have = [r[0] for r in conn.execute(
            "SELECT kind FROM artifact WHERE lesson_id=?", (lesson_id,))]
        raise click.ClickException(
            f"lesson {lesson_id} has no {kind}" + (f"; it has {', '.join(have)}" if have else ""))
    path = Path(art["path"])
    if not path.exists():
        raise click.ClickException(f"{path} is missing — try `sprigly redo {lesson_id} --from upload`")

    opener = shutil.which("xdg-open") or shutil.which("open")
    if opener:
        subprocess.Popen([opener, str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        click.echo(f"opened {path}")
    else:
        click.echo(str(path))
    if _mark_consumed(conn, lesson_id, f"play:{kind}"):
        click.echo(f"lesson {lesson_id} marked consumed — `sprigly done {lesson_id}` to rate it")
    conn.close()


@main.command()
@click.argument("lesson_ref", metavar="LESSON")
@click.option("--rating", type=click.IntRange(1, 5), help="How useful it was, 1 to 5.")
@click.option("--note", default="", help="Anything worth remembering about it.")
@click.pass_obj
def done(cfg: dict, lesson_ref: str, rating: int | None, note: str) -> None:
    """Record what you thought of a lesson, and mark it consumed if it was not already."""
    from . import store

    conn = store.connect(cfg["paths"]["db"])
    lesson_id = _ref(conn, lesson_ref)
    _mark_consumed(conn, lesson_id, "done")
    store.log_event(conn, "rated", lesson_id,
                    json.dumps({"rating": rating, "note": note.strip() or None}))
    click.echo(f"recorded for lesson {lesson_id}"
               + (f": rating {rating}" if rating else "")
               + (f" — {note.strip()}" if note.strip() else ""))
    conn.close()


RATING_KEYS = {"1": "Again", "2": "Hard", "3": "Good", "4": "Easy"}


def _drill(conn, console, cards, cfg) -> int:
    """Ask each card, take a self-grade, schedule it. Returns how many were graded."""
    from rich.panel import Panel

    from . import review

    graded = 0
    for i, card in enumerate(cards, 1):
        console.print(Panel(card["front"], title=f"{i}/{len(cards)}", border_style="cyan"))
        click.prompt("press enter to reveal", default="", show_default=False, prompt_suffix="")
        console.print(Panel(card["back"], border_style="green"))
        choice = click.prompt("  1 again  2 hard  3 good  4 easy  (q to stop)",
                              default="3", show_default=False)
        if choice.strip().lower().startswith("q"):
            break
        rating = RATING_KEYS.get(choice.strip(), "Good")
        when = review.grade(conn, card, rating)
        console.print(f"  [dim]{rating.lower()}, due {when[:10]}[/dim]\n")
        graded += 1
    return graded


@main.command()
@click.argument("lesson_ref", metavar="LESSON")
@click.pass_obj
def quiz(cfg: dict, lesson_ref: str) -> None:
    """Work through a lesson's questions and schedule them for review.

    The questions come from NotebookLM, generated from the same sources as the lesson. Grading is
    yours: only you know whether an answer was recalled or reconstructed.
    """
    from rich.console import Console

    from . import review, store

    console = Console()
    conn = store.connect(cfg["paths"]["db"])
    lesson_id = _ref(conn, lesson_ref)
    added = review.import_cards(conn, cfg, lesson_id)
    if added:
        console.print(f"[dim]imported {added} cards[/dim]")
    cards = conn.execute("SELECT * FROM card WHERE lesson_id=? ORDER BY id",
                         (lesson_id,)).fetchall()
    if not cards:
        raise click.ClickException(
            f"lesson {lesson_id} has no questions — is 'quiz' in bridge.artifacts, and has it "
            f"finished generating? `sprigly status {lesson_id}` shows what arrived")

    graded = _drill(conn, console, cards, cfg)
    if graded:
        review.finish(conn, lesson_id)
        console.print(f"[green]✓[/green] graded {graded} of {len(cards)}")
    conn.close()


@main.command("review")
@click.option("--limit", type=int, default=20, show_default=True, help="Most cards to drill.")
@click.pass_obj
def review_due(cfg: dict, limit: int) -> None:
    """Drill every card that has come due, across all lessons."""
    from rich.console import Console

    from . import review, store

    console = Console()
    conn = store.connect(cfg["paths"]["db"])
    cards = review.due(conn)[:limit]
    if not cards:
        nxt = store.due_cards(conn)[1]
        console.print("nothing due" + (f" — next on {nxt[:10]}" if nxt else ""))
        conn.close()
        return
    graded = _drill(conn, console, cards, cfg)
    console.print(f"[green]✓[/green] graded {graded} of {len(cards)} due")
    conn.close()


@main.command()
@click.argument("goal", required=False)
@click.option("--depth", type=click.IntRange(1, 5), help="How fine-grained the lessons should be.")
@click.option("--lang", help="Language for this track's material.")
@click.option("--only", is_flag=True, help="Offer nothing outside the active tracks.")
@click.option("--shared", is_flag=True, help="Undo --only.")
@click.option("--min-evidence", help="peer-reviewed, preprint, institutional, practitioner.")
@click.option("--off", is_flag=True, help="Deactivate: the named track, or all of them.")
@click.pass_obj
def focus(cfg: dict, goal: str | None, depth: int | None, lang: str | None, only: bool,
          shared: bool, min_evidence: str | None, off: bool) -> None:
    """Activate a track, re-tune it, or show what is active.

    Several tracks may be active at once — parallel interests are the normal case, and the picker's
    track-debt signal keeps them advancing fairly without a scheduler. With no arguments this lists
    the tracks and changes nothing.
    """
    from rich.console import Console
    from rich.table import Table

    from . import store

    console = Console()
    conn = store.connect(cfg["paths"]["db"])

    if off:
        if goal:
            n = conn.execute("UPDATE track SET active=0 WHERE goal=?", (goal,)).rowcount
            click.echo(f"deactivated {n} track(s) matching {goal!r}")
        else:
            conn.execute("UPDATE track SET active=0")
            click.echo("all tracks deactivated")
    elif goal:
        row = conn.execute("SELECT id FROM track WHERE goal=?", (goal,)).fetchone()
        if row:
            tid = row["id"]
            conn.execute("UPDATE track SET active=1 WHERE id=?", (tid,))
        else:
            tid = conn.execute(
                "INSERT INTO track (goal, depth, active, language, min_evidence)"
                " VALUES (?,?,1,?,?)",
                (goal, depth or cfg["lesson"]["default_depth"],
                 lang or cfg["lesson"]["default_language"],
                 min_evidence or cfg["sources"]["default_min_evidence"])).lastrowid
            click.echo(f"track {tid}: {goal}")
        _retune(conn, tid, depth, lang, only, shared, min_evidence)
    else:
        targets = [r["id"] for r in conn.execute("SELECT id FROM track WHERE active=1")]
        if any(v is not None and v is not False for v in (depth, lang, min_evidence)) or only or shared:
            if not targets:
                raise click.ClickException("no active track to re-tune — name one")
            for tid in targets:
                _retune(conn, tid, depth, lang, only, shared, min_evidence)

    table = Table("id", "goal", "depth", "active", "only", "lang", "evidence", "lessons")
    for r in conn.execute(
            "SELECT t.*, (SELECT count(*) FROM lesson l WHERE l.track_id=t.id) AS n,"
            " (SELECT count(*) FROM lesson l WHERE l.track_id=t.id AND l.state='proposed') AS waiting,"
            " (SELECT count(*) FROM lesson l WHERE l.track_id=t.id AND l.state='reviewed') AS done"
            " FROM track t ORDER BY t.active DESC, t.id"):
        finished = r["n"] and not r["waiting"] and r["done"] == r["n"]
        table.add_row(str(r["id"]), r["goal"] + ("  [green]done[/green]" if finished else ""),
                      str(r["depth"]), "yes" if r["active"] else "", "yes" if r["exclusive"] else "",
                      r["language"], r["min_evidence"], f"{r['done']}/{r['n']}")
    console.print(table)
    conn.close()


def _retune(conn, track_id: int, depth, lang, only, shared, min_evidence) -> None:
    """Apply the flags to one track. Changing depth clears pending proposals for it.

    Lessons already reviewed are untouched; only what has not been learned yet is regenerated, so
    re-tuning granularity never throws away work.
    """
    from . import store

    if depth:
        conn.execute("UPDATE track SET depth=? WHERE id=?", (depth, track_id))
        stale = conn.execute(
            "SELECT id FROM lesson WHERE track_id=? AND state='proposed'", (track_id,)).fetchall()
        for r in stale:
            conn.execute("UPDATE lesson SET state='expired' WHERE id=?", (r["id"],))
            store.log_event(conn, "expired", r["id"], '{"why": "depth changed"}')
        if stale:
            click.echo(f"depth {depth}; dropped {len(stale)} pending proposal(s) —"
                       f" run `sprigly curate --track {track_id}` to redecompose")
    if lang:
        conn.execute("UPDATE track SET language=? WHERE id=?", (lang, track_id))
    if min_evidence:
        conn.execute("UPDATE track SET min_evidence=? WHERE id=?", (min_evidence, track_id))
    if only:
        conn.execute("UPDATE track SET exclusive=1 WHERE id=?", (track_id,))
    if shared:
        conn.execute("UPDATE track SET exclusive=0 WHERE id=?", (track_id,))


@main.command()
@click.argument("lesson_ref", metavar="LESSON")
@click.option("-n", type=int, help="How many children to ask for.")
@click.pass_obj
def deeper(cfg: dict, lesson_ref: str, n: int | None) -> None:
    """Split a lesson into finer children, one depth level down."""
    from rich.console import Console

    from . import curator, store

    console = Console()
    _echo_warnings(console)
    conn = store.connect(cfg["paths"]["db"])
    lesson_id = _ref(conn, lesson_ref)
    status = console.status("[dim]decomposing[/dim]", spinner="dots")

    def say(msg: str) -> None:
        console.print(f"   [dim]{msg}[/dim]")
        status.update(f"[cyan]{msg[:70]}[/cyan]")

    status.start()
    try:
        ids = curator.deepen(conn, cfg, lesson_id, n, report=say)
    except curator.CuratorError as err:
        raise click.ClickException(str(err))
    finally:
        status.stop()
    console.print(f"[green]✓[/green] {len(ids)} finer lessons under {lesson_id}"
                  f" — run `sprigly next` to pick one")
    conn.close()


@main.command()
@click.option("--prune", is_flag=True, help="Offer unused notebooks for deletion, one at a time.")
@click.pass_obj
def notebooks(cfg: dict, prune: bool) -> None:
    """List the notebooks on your NotebookLM account and which lesson uses each.

    Sprigly never deletes a notebook on its own. Generating a lesson leaves one behind, and a redo
    abandons one; both stay until you say otherwise here.
    """
    from rich.console import Console
    from rich.table import Table

    from . import bridge, store

    console = Console()
    conn = store.connect(cfg["paths"]["db"])
    used = {r["notebook_id"]: r["id"] for r in conn.execute(
        "SELECT id, notebook_id FROM lesson WHERE notebook_id IS NOT NULL")}
    # Every notebook sprigly has ever created, from the event log. Anything outside this set was
    # made by the user, and pruning must never offer it: "not used by a lesson" is not the same as
    # "ours to delete".
    ours = set()
    for r in conn.execute("SELECT payload FROM event WHERE kind IN ('generated','asked','redo')"):
        try:
            nb = (json.loads(r["payload"]) or {}).get("notebook_id")
        except (TypeError, ValueError):
            nb = None
        if nb:
            ours.add(nb)
    ours |= set(used)

    with console.status("[dim]listing notebooks[/dim]", spinner="dots"):
        rows = bridge.listing(cfg)

    table = Table("notebook", "sources", "owner", "title")
    for nb in rows:
        lesson = used.get(nb["id"])
        if lesson:
            owner = f"lesson {lesson}"
        elif nb["id"] in ours:
            owner = "[yellow]sprigly, unused[/yellow]"
        else:
            owner = "[dim]yours[/dim]"
        table.add_row(nb["id"][:8], str(nb["sources"]), owner, nb["title"][:48])
    console.print(table)

    unused = [nb for nb in rows if nb["id"] in ours and nb["id"] not in used]
    if not prune:
        if unused:
            console.print(f"[dim]{len(unused)} created by sprigly and no longer used — "
                          f"`sprigly notebooks --prune` to review them[/dim]")
        conn.close()
        return
    if not unused:
        console.print("[dim]nothing of sprigly's is unused; your own notebooks are never offered[/dim]")
        conn.close()
        return

    for nb in unused:
        label = f"{nb['title'][:48] or '(untitled)'} — {nb['sources']} sources"
        if click.confirm(f"delete {nb['id'][:8]}  {label}?", default=False):
            bridge.delete(nb["id"], cfg)
            click.echo(f"  deleted {nb['id'][:8]}")
    conn.close()


def _selfcheck() -> None:
    """Exercise every command's wiring without touching the network.

    The module self-checks cover their own logic; nothing covered this file, so a stale name in one
    command went unnoticed until it was run by hand. `--help` on every command import-checks its
    body, and the read-only commands are run for real against a scratch store.
    """
    import tempfile

    from click.testing import CliRunner

    runner = CliRunner()
    commands = sorted(main.commands)
    assert {"config", "curate", "next", "tick", "status", "quiz", "review", "ask", "play",
            "done", "redo", "deeper", "focus", "notebooks"} <= set(commands), commands

    for name in commands:
        result = runner.invoke(main, [name, "--help"])
        assert result.exit_code == 0, f"{name} --help: {result.output[-300:]}"

    with tempfile.TemporaryDirectory() as td:
        env = {"XDG_DATA_HOME": td, "XDG_CONFIG_HOME": td}
        for argv in (["config"], ["status"], ["focus"], ["next"], ["review"]):
            result = runner.invoke(main, argv, env=env, input="\n")
            assert result.exit_code == 0, f"{argv}: {result.exception or result.output[-300:]}"
        # The config is printed as TOML so it can be pasted straight back into the file.
        import tomllib

        printed = runner.invoke(main, ["config"], env=env).output
        parsed = tomllib.loads("\n".join(
            l for l in printed.splitlines() if not l.startswith("#")))
        assert parsed["bridge"]["artifacts"] == ["audio", "video", "quiz"]
        assert parsed["scoring"]["prereq"] == 0.25
        assert "paths" not in parsed, "resolved paths are shown as comments, not as settings"

        # An unknown reference is a message, not a traceback.
        result = runner.invoke(main, ["status", "nothing-like-this"], env=env)
        assert result.exit_code != 0 and "no lesson" in result.output, result.output

    print("cli selfcheck ok")


if __name__ == "__main__":
    _selfcheck()
