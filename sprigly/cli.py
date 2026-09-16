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
    """Print the resolved configuration and paths."""
    click.echo(f"# config file: {config.config_path()}"
               f" ({'found' if config.config_path().is_file() else 'not present, using defaults'})")
    click.echo(json.dumps(cfg, indent=2, default=str))


@main.command()
@click.option("--lesson", type=int, help="Advance only this lesson, ignoring its backoff window.")
@click.pass_obj
def tick(cfg: dict, lesson: int | None) -> None:
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
        started = time.monotonic()
        status = console.status("[dim]scanning[/dim]", spinner="dots")

        def step(event: str, row, detail: str) -> None:
            topic = (row["topic"] or "")[:48]
            if event == "begin":
                status.update(f"[cyan]{row['state']}[/cyan] [dim]{topic}[/dim]")
            elif event == "step":
                console.print(f"   [dim]{detail}[/dim]")
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
@click.argument("lesson_id", type=int, required=False)
@click.pass_obj
def status(cfg: dict, lesson_id: int | None) -> None:
    """Pipeline state, parked failures and review load. With an id, inspect one lesson."""
    from rich.console import Console
    from rich.table import Table

    from . import store

    conn = store.connect(cfg["paths"]["db"])
    console = Console()

    if lesson_id is not None:
        _show_lesson(conn, cfg, console, lesson_id)
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
        t = Table("id", "topic", "state", "tries", "next try", "error", title="needs attention")
        for r in rows:
            t.add_row(str(r["id"]), r["topic"], r["state"], str(r["retry_count"]),
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
             f"{o.score:.2f}  {o.candidate.topic}   [{_why(o, cfg)}]"
             for i, o in enumerate(menu, 1)]
    if sys.stdin.isatty() and sys.stdout.isatty():
        from iterfzf import iterfzf

        picked = iterfzf(lines, prompt="pick> ", exact=False, multi=True,
                         header="tab to mark several, enter to take, esc to skip all")
        return [int(line.split()[0]) for line in (picked or [])]

    from rich.console import Console
    from rich.table import Table

    table = Table("#", "kind", "topic", "domain", "min", "score", "why")
    for i, o in enumerate(menu, 1):
        table.add_row(str(i), o.kind, o.candidate.topic, o.candidate.domain,
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
    import json as _json

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
        click.echo("nothing to offer — run the curator, or check `sprigly status`")
        conn.close()
        return

    # One id per invocation, stamped on every event of this offering. Without it the fit cannot
    # tell which candidates competed against each other, and the choice sets are unrecoverable.
    choice_set = uuid.uuid4().hex[:12]
    for o in menu:
        store.log_event(conn, "offered", o.candidate.id,
                        _json.dumps({"choice_set": choice_set, "kind": o.kind, "score": o.score,
                                     "signals": o.signals}))

    for idx in _choose(menu, cfg):
        taken = menu[idx - 1]
        store.log_event(conn, "picked", taken.candidate.id,
                        _json.dumps({"choice_set": choice_set, "kind": taken.kind}))
        if taken.kind == "new":
            conn.execute("UPDATE lesson SET state='picked', score=?, updated_at=? WHERE id=?",
                         (taken.score, store.utcnow(), taken.candidate.id))
            click.echo(f"picked {taken.candidate.id}: {taken.candidate.topic}")
        else:
            click.echo(f"review {taken.candidate.id}: {taken.candidate.topic}")
    conn.close()


@main.command()
@click.option("--track", type=int, help="Decompose this track instead of prospecting.")
@click.option("-n", type=int, help="How many lessons to ask for.")
@click.option("--dry-run", is_flag=True, help="Print the prompt without calling the agent.")
@click.pass_obj
def curate(cfg: dict, track: int | None, n: int | None, dry_run: bool) -> None:
    """Ask the agent for candidate lessons."""
    from . import curator, store

    conn = store.connect(cfg["paths"]["db"])
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
        status = console.status("[dim]waiting on the agent[/dim]", spinner="dots")
        status.start()
        try:
            ids = curator.propose(
                conn, cfg, track, n,
                on_retry=lambda attempt, err: status.update(
                    f"[yellow]retry {attempt}[/yellow] [dim]{err[:60]}[/dim]"),
                report=lambda msg: console.print(f"   [dim]{msg}[/dim]"))
        except curator.CuratorError as err:
            raise click.ClickException(str(err))
        finally:
            status.stop()

        console.print(f"[green]✓[/green] {len(ids)} proposed in {time.monotonic() - started:.0f}s")
        table = Table("id", "topic", "domain", "depth", box=None, pad_edge=False)
        for r in conn.execute(
                f"SELECT id, topic, COALESCE(domain,'') AS domain, depth FROM lesson"
                f" WHERE id IN ({','.join('?' * len(ids))})", ids):
            table.add_row(str(r["id"]), r["topic"], r["domain"], str(r["depth"]))
        console.print(table)
        console.print("[dim]run `sprigly next`[/dim]")
    conn.close()


def _show_lesson(conn, cfg: dict, console, lesson_id: int) -> None:
    """What this lesson is carrying, and what the next tick would actually have to work with."""
    from rich.table import Table

    row = conn.execute("SELECT * FROM lesson WHERE id=?", (lesson_id,)).fetchone()
    if not row:
        raise click.ClickException(f"no lesson {lesson_id}")

    console.print(f"[bold]{row['id']}  {row['topic']}[/bold]  [cyan]{row['state']}[/cyan]")
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
@click.argument("lesson_id", type=int)
@click.option("--from", "stage", type=click.Choice(["harvest", "upload"]), default="harvest",
              show_default=True, help="Which phase to run again.")
@click.pass_obj
def redo(cfg: dict, lesson_id: int, stage: str) -> None:
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
    _, discarded = ticker.REDO_STAGES[stage]
    click.echo(f"lesson {lesson_id} rewound to {state}; discarded {discarded}")
    click.echo(f"run `sprigly tick --lesson {lesson_id}`")
    conn.close()
