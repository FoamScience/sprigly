"""sprigly entrypoint."""

from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from enum import Enum
from pathlib import Path

import typer

from . import config

log = logging.getLogger(__name__)


def _fail(message: str) -> typer.Exit:
    """A user-facing failure: one line on stderr and a non-zero exit, not a traceback.

    Returned rather than raised so the call site keeps `raise` and reads as the exit it is.
    """
    typer.secho(f"Error: {message}", fg=typer.colors.RED, err=True)
    return typer.Exit(1)


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


main = typer.Typer(no_args_is_help=True, add_completion=False)


@main.callback()
def _load(ctx: typer.Context,
          config_file: Path = typer.Option(None, "--config",
                                           help="Config TOML to use instead of the XDG location.")
          ) -> None:
    """Learn one thing at a time."""
    import tomllib

    try:
        ctx.obj = config.load(path=config_file)
    except tomllib.TOMLDecodeError as err:
        # Falling back to the defaults rather than exiting, so `sprigly config edit` — the one
        # command that can repair this — is still reachable. Every other command says so first.
        typer.secho(f"! {config_file or config.config_path()}: {err}"
                    f" — running on defaults; fix it with `sprigly config edit`",
                    fg=typer.colors.YELLOW, err=True)
        ctx.obj = config.load(path=Path("/nonexistent.toml"))
    _setup_logging(ctx.obj["paths"]["log"])


config_app = typer.Typer(no_args_is_help=False, help="Show or edit the configuration file.")
main.add_typer(config_app, name="config", invoke_without_command=True)


@config_app.callback(invoke_without_command=True)
def config_default(ctx: typer.Context) -> None:
    """Show or edit the configuration file. With no subcommand, show it."""
    if ctx.invoked_subcommand is None:
        show_config(ctx)


@config_app.command("show")
def show_config(ctx: typer.Context) -> None:
    """Print the resolved configuration, in the format the config file itself uses."""
    import tomli_w

    cfg = ctx.obj

    path = config.config_path()
    typer.echo(f"# {path}  ({'in use' if path.is_file() else 'not present, showing defaults'})")
    typer.echo(f"# copy any section below into that file to change it\n")
    # Paths are resolved absolute at load time and are derived, not settings, so they are shown
    # separately as comments rather than offered as something to paste back.
    settings = {k: v for k, v in cfg.items() if k != "paths"}
    typer.echo(tomli_w.dumps(settings).rstrip())
    typer.echo("\n# resolved paths")
    for name, value in sorted(cfg["paths"].items()):
        typer.echo(f"#   {name:<8} {value}")


@config_app.command("edit")
def edit_config() -> None:
    """Open the configuration file in $EDITOR, and refuse to leave it unparseable."""
    import os
    import subprocess
    import tomllib

    path = config.config_path()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# sprigly configuration — `sprigly config show` prints every setting"
                        " with its current value.\n")
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
    subprocess.call([editor, str(path)])
    try:
        tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as err:
        raise _fail(f"{path} is not valid TOML: {err}")
    typer.echo(f"{path} saved")


@main.command()
def tick(ctx: typer.Context,
         lesson_ref: str = typer.Option(None, "--lesson", metavar="LESSON",
                                        help="Advance only this lesson, ignoring its backoff"
                                             " window.")) -> None:
    """Advance every lesson one step. Safe to run from a timer."""
    from . import store, tick as ticker

    from rich.console import Console

    cfg = ctx.obj
    console = Console()
    _echo_warnings(console)
    with ticker.lock(cfg["paths"]["data"] / "tick.lock") as held:
        if not held:
            typer.echo("another tick is running")
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
def status(ctx: typer.Context,
           lesson_ref: str = typer.Argument(None, metavar="LESSON")) -> None:
    """Pipeline state, parked failures and review load. With an id, inspect one lesson."""
    from rich.console import Console
    from rich.table import Table

    from . import store

    cfg = ctx.obj
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
    raw = typer.prompt("pick", default="", show_default=False,
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
def next(ctx: typer.Context,
         budget: int = typer.Option(None, "--budget", help="Minutes available today."),
         k: int = typer.Option(None, "-k", help="How many to offer (default from config)."),
         show_all: bool = typer.Option(False, "--all",
                                       help="Offer every eligible candidate instead of a selected"
                                            " menu.")) -> None:
    """Offer the next lessons and record which one you take."""
    from rich.console import Console
    from rich.table import Table

    from . import picker, snapshot, store

    cfg = ctx.obj
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
        typer.echo("nothing to offer — run `sprigly curate` to propose lessons,"
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
            typer.echo(f"picked {name}: {taken.candidate.topic}"
                       f" — run `sprigly tick` to harvest and generate it")
        else:
            typer.echo(f"review {taken.candidate.id}: {taken.candidate.topic}")
    conn.close()


@main.command("fit")
def fit_cmd(ctx: typer.Context,
            min_sets: int = typer.Option(None, "--min-sets",
                                         help="Refuse to fit below this many usable choice sets.")
            ) -> None:
    """Fit the scoring weights to the offerings you actually picked from."""
    from rich.console import Console
    from rich.table import Table

    from . import fit as fitting
    from . import picker, store

    cfg = ctx.obj
    console = Console()
    conn = store.connect(cfg["paths"]["db"])
    sets = fitting.choice_sets(conn)
    conn.close()
    try:
        f = fitting.fit(sets, fitting.MIN_CHOICE_SETS if min_sets is None else min_sets)
    except ValueError as exc:
        console.print(f"[dim]{exc}[/dim]")
        return

    w = cfg["scoring"]
    total_w = sum(w[k] for k in picker.SIGNALS) or 1.0
    table = Table("signal", "weight now", "coefficient", "std err", "p",
                  title=f"conditional logit · {f.n_sets} choice sets,"
                        f" {f.n_picks} picks out of {f.n_rows} offers")
    for k in picker.SIGNALS:
        table.add_row(k.replace("_", "-"), f"{w[k] / total_w:.2f}", f"{f.coef[k]:+.2f}",
                      f"{f.stderr[k]:.2f}", f"{f.pvalue[k]:.3f}")
    console.print(table)

    fitted = f.weights()
    if fitted is None:
        console.print("[yellow]![/yellow] a coefficient came out negative: the scorer normalises by"
                      " the weight total, so there is no [scoring] block that reproduces this fit")
        return
    console.print("\n[dim]paste into the config to adopt the fit:[/dim]")
    typer.echo("[scoring]")
    for k in picker.SIGNALS:
        typer.echo(f"{k} = {fitted[k]:.3f}")


@main.command()
def curate(ctx: typer.Context,
           track: int = typer.Option(None, "--track",
                                     help="Decompose this track instead of prospecting."),
           n: int = typer.Option(None, "-n", help="How many lessons to ask for."),
           lang: str = typer.Option(None, "--lang",
                                    help="Language for the generated material, e.g. de."
                                         " Default from config."),
           dry_run: bool = typer.Option(False, "--dry-run",
                                        help="Print the prompt without calling the agent.")
           ) -> None:
    """Ask the agent for candidate lessons."""
    from . import curator, store

    cfg = ctx.obj
    conn = store.connect(cfg["paths"]["db"])
    if lang:
        cfg = {**cfg, "lesson": {**cfg["lesson"], "default_language": lang}}
    if dry_run:
        prompt, role, _ = curator.build_prompt(conn, cfg, track, n)
        backend = cfg["agent"]["backend"]
        model = cfg["agent"]["models"].get(backend, {}).get(role, "?")
        typer.echo(f"# backend={backend} role={role} model={model}\n")
        typer.echo(prompt)
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
            raise _fail(str(err))
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
        raise _fail(f"no lesson {lesson_id}")

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
        table = Table("kind", "size", "length", "mime", "path", title="artifacts")
        for a in arts:
            secs = a["duration"]
            table.add_row(a["kind"], f"{(a['bytes'] or 0) // 1024}k",
                          f"{secs / 60:.0f}m" if secs else "-", a["mime"] or "-", str(a["path"]))
        console.print(table)

    if row["job_ref"]:
        console.print(f"[dim]jobs[/dim] {row['job_ref']}")


class Stage(str, Enum):
    harvest = "harvest"
    upload = "upload"
    freshen = "freshen"


@main.command()
def redo(ctx: typer.Context,
         lesson_ref: str = typer.Argument(..., metavar="LESSON"),
         stage: Stage = typer.Option(Stage.harvest, "--from", help="Which phase to run again."),
         lang: str = typer.Option(None, "--lang",
                                  help="Regenerate in this language, e.g. de.")) -> None:
    """Rewind a lesson so the next tick re-runs a phase.

    `--from harvest` throws away its sources and brief and searches again. `--from upload` keeps the
    sources but discards the notebook and artifacts. Either way the old notebook is deleted, so a
    redo does not quietly consume the account's notebook cap.
    """
    from rich.console import Console

    from . import store, tick as ticker

    cfg = ctx.obj
    conn = store.connect(cfg["paths"]["db"])
    _echo_warnings(Console())
    lesson_id = _ref(conn, lesson_ref)
    try:
        state = ticker.redo(conn, cfg, lesson_id, stage.value)
    except ValueError as err:
        raise _fail(str(err))
    if lang:
        conn.execute("UPDATE lesson SET language=? WHERE id=?", (lang, lesson_id))
        typer.echo(f"language set to {lang}")
    _, discarded = ticker.REDO_STAGES[stage.value]
    typer.echo(f"lesson {lesson_id} rewound to {state}; discarded {discarded}")
    typer.echo(f"run `sprigly tick --lesson {lesson_id}` to run that phase again")
    conn.close()


@main.command()
def ask(ctx: typer.Context,
        lesson_ref: str = typer.Argument(..., metavar="LESSON"),
        question: list[str] = typer.Argument(..., metavar="QUESTION...")) -> None:
    """Ask a question about a lesson, answered from its own sources.

    Notebooks are deleted once their artifacts are downloaded, so the first question about an older
    lesson rebuilds one from the sources still on disk. That is slower than the rest, and cheaper
    than keeping every notebook alive against the account's cap.
    """
    from rich.console import Console
    from rich.markdown import Markdown

    from . import bridge, store

    cfg = ctx.obj
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
            raise _fail(f"lesson {lesson_id} has no sources to answer from")
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
        raise _fail(str(err)) from None
    except LookupError as err:
        raise _fail(str(err)) from None


def _mark_consumed(conn, lesson_id: int, via: str) -> bool:
    """Move a ready lesson on. Returns whether it actually moved."""
    from . import store

    row = conn.execute("SELECT state FROM lesson WHERE id=?", (lesson_id,)).fetchone()
    if not row:
        raise _fail(f"no lesson {lesson_id}")
    store.log_event(conn, "consumed", lesson_id, json.dumps({"via": via}))
    if row["state"] == "ready":
        conn.execute("UPDATE lesson SET state='consumed', updated_at=? WHERE id=?",
                     (store.utcnow(), lesson_id))
        return True
    return False


@main.command()
def play(ctx: typer.Context,
         lesson_ref: str = typer.Argument(..., metavar="LESSON"),
         kind: str = typer.Option("audio", "--kind",
                                  help="Which artifact to open: audio, video, slides, quiz.")
         ) -> None:
    """Open a lesson's artifact and record that you consumed it.

    This is the machine-side counterpart of deleting the file on the phone. Opening is taken as
    consumption because you asked for it by name — a lesson never advances on its own.
    """
    import shutil
    import subprocess

    from . import store

    cfg = ctx.obj
    conn = store.connect(cfg["paths"]["db"])
    lesson_id = _ref(conn, lesson_ref)
    art = conn.execute("SELECT path FROM artifact WHERE lesson_id=? AND kind=?",
                       (lesson_id, kind)).fetchone()
    if not art:
        have = [r[0] for r in conn.execute(
            "SELECT kind FROM artifact WHERE lesson_id=?", (lesson_id,))]
        raise _fail(
            f"lesson {lesson_id} has no {kind}" + (f"; it has {', '.join(have)}" if have else ""))
    path = Path(art["path"])
    if not path.exists():
        raise _fail(f"{path} is missing — try `sprigly redo {lesson_id} --from upload`")

    opener = shutil.which("xdg-open") or shutil.which("open")
    if opener:
        subprocess.Popen([opener, str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        typer.echo(f"opened {path}")
    else:
        typer.echo(str(path))
    if _mark_consumed(conn, lesson_id, f"play:{kind}"):
        typer.echo(f"lesson {lesson_id} marked consumed — `sprigly done {lesson_id}` to rate it")
    conn.close()


@main.command()
def done(ctx: typer.Context,
         lesson_ref: str = typer.Argument(..., metavar="LESSON"),
         rating: int = typer.Option(None, "--rating", min=1, max=5,
                                    help="How useful it was, 1 to 5."),
         note: str = typer.Option("", "--note",
                                  help="Anything worth remembering about it.")) -> None:
    """Record what you thought of a lesson, and mark it consumed if it was not already."""
    from . import store

    cfg = ctx.obj
    conn = store.connect(cfg["paths"]["db"])
    lesson_id = _ref(conn, lesson_ref)
    _mark_consumed(conn, lesson_id, "done")
    store.log_event(conn, "rated", lesson_id,
                    json.dumps({"rating": rating, "note": note.strip() or None}))
    typer.echo(f"recorded for lesson {lesson_id}"
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
        typer.prompt("press enter to reveal", default="", show_default=False, prompt_suffix="")
        console.print(Panel(card["back"], border_style="green"))
        choice = typer.prompt("  1 again  2 hard  3 good  4 easy  (q to stop)",
                              default="3", show_default=False)
        if choice.strip().lower().startswith("q"):
            break
        rating = RATING_KEYS.get(choice.strip(), "Good")
        when = review.grade(conn, card, rating)
        console.print(f"  [dim]{rating.lower()}, due {when[:10]}[/dim]\n")
        graded += 1
    return graded


@main.command()
def quiz(ctx: typer.Context,
         lesson_ref: str = typer.Argument(..., metavar="LESSON")) -> None:
    """Work through a lesson's questions and schedule them for review.

    The questions come from NotebookLM, generated from the same sources as the lesson. Grading is
    yours: only you know whether an answer was recalled or reconstructed.
    """
    from rich.console import Console

    from . import review, store

    cfg = ctx.obj
    console = Console()
    conn = store.connect(cfg["paths"]["db"])
    lesson_id = _ref(conn, lesson_ref)
    added = review.import_cards(conn, cfg, lesson_id)
    if added:
        console.print(f"[dim]imported {added} cards[/dim]")
    cards = conn.execute("SELECT * FROM card WHERE lesson_id=? ORDER BY id",
                         (lesson_id,)).fetchall()
    if not cards:
        raise _fail(
            f"lesson {lesson_id} has no questions — is 'quiz' in bridge.artifacts, and has it "
            f"finished generating? `sprigly status {lesson_id}` shows what arrived")

    graded = _drill(conn, console, cards, cfg)
    if graded:
        review.finish(conn, lesson_id)
        console.print(f"[green]✓[/green] graded {graded} of {len(cards)}")
    conn.close()


@main.command("review")
def review_due(ctx: typer.Context,
               limit: int = typer.Option(20, "--limit", help="Most cards to drill.")) -> None:
    """Drill every card that has come due, across all lessons."""
    from rich.console import Console

    from . import review, store

    cfg = ctx.obj
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
def focus(ctx: typer.Context,
          goal: str = typer.Argument(None),
          depth: int = typer.Option(None, "--depth", min=1, max=5,
                                    help="How fine-grained the lessons should be."),
          lang: str = typer.Option(None, "--lang", help="Language for this track's material."),
          only: bool = typer.Option(False, "--only",
                                    help="Offer nothing outside the active tracks."),
          shared: bool = typer.Option(False, "--shared", help="Undo --only."),
          min_evidence: str = typer.Option(None, "--min-evidence",
                                           help="peer-reviewed, preprint, institutional,"
                                                " practitioner."),
          off: bool = typer.Option(False, "--off",
                                   help="Deactivate: the named track, or all of them.")) -> None:
    """Activate a track, re-tune it, or show what is active.

    Several tracks may be active at once — parallel interests are the normal case, and the picker's
    track-debt signal keeps them advancing fairly without a scheduler. With no arguments this lists
    the tracks and changes nothing.
    """
    from rich.console import Console
    from rich.table import Table

    from . import store

    cfg = ctx.obj
    console = Console()
    conn = store.connect(cfg["paths"]["db"])

    if off:
        if goal:
            n = conn.execute("UPDATE track SET active=0 WHERE goal=?", (goal,)).rowcount
            typer.echo(f"deactivated {n} track(s) matching {goal!r}")
        else:
            conn.execute("UPDATE track SET active=0")
            typer.echo("all tracks deactivated")
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
            typer.echo(f"track {tid}: {goal}")
        _retune(conn, tid, depth, lang, only, shared, min_evidence)
    else:
        targets = [r["id"] for r in conn.execute("SELECT id FROM track WHERE active=1")]
        if any(v is not None and v is not False for v in (depth, lang, min_evidence)) or only or shared:
            if not targets:
                raise _fail("no active track to re-tune — name one")
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
            typer.echo(f"depth {depth}; dropped {len(stale)} pending proposal(s) —"
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
def deeper(ctx: typer.Context,
           lesson_ref: str = typer.Argument(..., metavar="LESSON"),
           n: int = typer.Option(None, "-n", help="How many children to ask for.")) -> None:
    """Split a lesson into finer children, one depth level down."""
    from rich.console import Console

    from . import curator, store

    cfg = ctx.obj
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
        raise _fail(str(err))
    finally:
        status.stop()
    console.print(f"[green]✓[/green] {len(ids)} finer lessons under {lesson_id}"
                  f" — run `sprigly next` to pick one")
    conn.close()


@main.command()
def notebooks(ctx: typer.Context,
              prune: bool = typer.Option(False, "--prune",
                                         help="Offer unused notebooks for deletion, one at a"
                                              " time.")) -> None:
    """List the notebooks on your NotebookLM account and which lesson uses each.

    Sprigly never deletes a notebook on its own. Generating a lesson leaves one behind, and a redo
    abandons one; both stay until you say otherwise here.
    """
    from rich.console import Console
    from rich.table import Table

    from . import bridge, store

    cfg = ctx.obj
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
        if typer.confirm(f"delete {nb['id'][:8]}  {label}?", default=False):
            bridge.delete(nb["id"], cfg)
            typer.echo(f"  deleted {nb['id'][:8]}")
    conn.close()


def _selfcheck() -> None:
    """Exercise every command's wiring without touching the network.

    The module self-checks cover their own logic; nothing covered this file, so a stale name in one
    command went unnoticed until it was run by hand. `--help` on every command import-checks its
    body, and the read-only commands are run for real against a scratch store.
    """
    import tempfile

    from typer.testing import CliRunner

    runner = CliRunner()
    commands = sorted(typer.main.get_command(main).commands)
    assert {"config", "curate", "next", "tick", "status", "quiz", "review", "ask", "play",
            "done", "redo", "deeper", "focus", "notebooks", "fit"} <= set(commands), commands

    for name in commands:
        result = runner.invoke(main, [name, "--help"])
        assert result.exit_code == 0, f"{name} --help: {result.output[-300:]}"

    with tempfile.TemporaryDirectory() as td:
        env = {"XDG_DATA_HOME": td, "XDG_CONFIG_HOME": td}
        for argv in (["config"], ["status"], ["focus"], ["next"], ["review"], ["fit"]):
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
        assert runner.invoke(main, ["config", "show"], env=env).output == printed, \
            "bare `config` and `config show` print the same thing"

        # `config edit` writes through the editor, and refuses to leave the file unparseable.
        import stat

        scratch = Path(td) / "sprigly"
        editor = Path(td) / "editor.sh"
        editor.write_text('#!/bin/sh\nprintf "[lesson]\\nbudget_minutes = 45\\n" >> "$1"\n')
        editor.chmod(editor.stat().st_mode | stat.S_IEXEC)
        env_edit = {**env, "EDITOR": str(editor)}
        assert runner.invoke(main, ["config", "edit"], env=env_edit).exit_code == 0
        assert "budget_minutes = 45" in (scratch / "config.toml").read_text()

        broken = Path(td) / "broken.sh"
        broken.write_text('#!/bin/sh\necho "not toml [[[" >> "$1"\n')
        broken.chmod(broken.stat().st_mode | stat.S_IEXEC)
        result = runner.invoke(main, ["config", "edit"], env={**env, "EDITOR": str(broken)})
        assert result.exit_code != 0 and "not valid TOML" in result.output, result.output
        # And an already-broken file must not lock the user out of the command that repairs it.
        repair = Path(td) / "repair.sh"
        repair.write_text('#!/bin/sh\nprintf "[lesson]\\nbudget_minutes = 45\\n" > "$1"\n')
        repair.chmod(repair.stat().st_mode | stat.S_IEXEC)
        result = runner.invoke(main, ["config", "edit"], env={**env, "EDITOR": str(repair)})
        assert result.exit_code == 0, result.output
        (scratch / "config.toml").unlink()

        # An unknown reference is a message, not a traceback.
        result = runner.invoke(main, ["status", "nothing-like-this"], env=env)
        assert result.exit_code != 0 and "no lesson" in result.output, result.output

        # A command whose body only `--help` ever touched can carry a NameError for months, which
        # is how `redo` lost its reference resolution. Run one that takes a lesson for real.
        result = runner.invoke(main, ["redo", "nothing-like-this"], env=env)
        assert result.exit_code != 0 and "no lesson" in result.output, \
            result.exception or result.output

        # What `next` writes must be what the fit reads back: the choice sets live only in these
        # payloads, and a renamed key would lose the training data silently.
        import os

        from . import fit as fitting
        from . import store

        os.environ.update(env)
        try:
            db = config.load()["paths"]["db"]
            conn = store.connect(db)
            for i in range(3):
                conn.execute("INSERT INTO lesson (topic, domain, state, est_minutes)"
                             " VALUES (?, 'numerics', 'proposed', 20)", (f"candidate {i}",))
            conn.commit()
            conn.close()
            result = runner.invoke(main, ["next"], env=env, input="1 2\n")
            assert result.exit_code == 0, result.exception or result.output[-300:]
            conn = store.connect(db)
            sets = fitting.choice_sets(conn)
            conn.close()
            assert len(sets) == 1, f"one offering, one choice set: {sets}"
            assert sum(1 for *_, picked in sets[0].rows if picked) == 2, "both picks recovered"
        finally:
            for k in env:
                os.environ.pop(k, None)

    print("cli selfcheck ok")


if __name__ == "__main__":
    _selfcheck()
