"""sprigly entrypoint."""

from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path

import click

from . import config

log = logging.getLogger(__name__)


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
@click.pass_obj
def tick(cfg: dict) -> None:
    """Advance every lesson one step. Safe to run from a timer."""
    from . import store, tick as ticker

    with ticker.lock(cfg["paths"]["data"] / "tick.lock") as held:
        if not held:
            click.echo("another tick is running")
            return
        conn = store.connect(cfg["paths"]["db"])
        moved = ticker.run(conn, cfg)
        # Only when something actually moved. Backing up an unchanged database every few minutes
        # would rotate the useful history out of the window.
        if any(moved.values()):
            store.backup(conn, cfg["paths"]["backups"], cfg["store"]["backup_keep"])
        conn.close()
    log.info("tick moved %s", dict(moved))
    click.echo(", ".join(f"{v} -> {k}" for k, v in sorted(moved.items()) if v) or "nothing to do")


@main.command()
@click.pass_obj
def status(cfg: dict) -> None:
    """Pipeline state, parked failures and review load."""
    from rich.console import Console
    from rich.table import Table

    from . import store

    conn = store.connect(cfg["paths"]["db"])
    console = Console()

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


def _choose(menu: list, cfg: dict) -> int:
    """Fuzzy-pick from the menu, falling back to a numbered table.

    fzf needs a terminal on both ends, so a piped or redirected stdin — a script, a test — has to
    keep working rather than blowing up inside the subprocess.
    """
    lines = [f"{i:>2}  {o.kind:<6} {o.candidate.domain:<15} {o.candidate.est_minutes:>3}m "
             f"{o.score:.2f}  {o.candidate.topic}   [{_why(o, cfg)}]"
             for i, o in enumerate(menu, 1)]
    if sys.stdin.isatty() and sys.stdout.isatty():
        from iterfzf import iterfzf

        picked = iterfzf(lines, prompt="pick> ", exact=False,
                         header="enter to take one, esc to skip all")
        return int(picked.split()[0]) if picked else 0

    from rich.console import Console
    from rich.table import Table

    table = Table("#", "kind", "topic", "domain", "min", "score", "why")
    for i, o in enumerate(menu, 1):
        table.add_row(str(i), o.kind, o.candidate.topic, o.candidate.domain,
                      str(o.candidate.est_minutes), f"{o.score:.2f}", _why(o, cfg))
    Console().print(table)
    return click.prompt("pick", type=click.IntRange(0, len(menu)), default=0,
                        show_default=False, prompt_suffix=" (0 to skip all): ")


def _why(offer, cfg: dict) -> str:
    """The two signals contributing most to this score. The full breakdown is in the event log."""
    from . import picker

    w = cfg["scoring"]
    ranked = sorted(picker.SIGNALS, key=lambda s: -w[s] * offer.signals[s])[:2]
    return " ".join(f"{s.replace('_', '-')} {offer.signals[s]:.2f}" for s in ranked)


@main.command()
@click.option("--budget", type=int, help="Minutes available today.")
@click.pass_obj
def next(cfg: dict, budget: int | None) -> None:
    """Offer the next lessons and record which one you take."""
    import json as _json

    from rich.console import Console
    from rich.table import Table

    from . import picker, snapshot, store

    conn = store.connect(cfg["paths"]["db"])
    snap = snapshot.load(conn, cfg)
    if budget:
        snap.budget_minutes = budget
    menu = picker.offer(snapshot.candidates(conn), snapshot.due_reviews(conn), snap, cfg)
    if not menu:
        click.echo("nothing to offer — run the curator, or check `sprigly status`")
        conn.close()
        return

    for o in menu:
        store.log_event(conn, "offered", o.candidate.id,
                        _json.dumps({"kind": o.kind, "score": o.score, "signals": o.signals}))

    choice = _choose(menu, cfg)
    if choice:
        taken = menu[choice - 1]
        store.log_event(conn, "picked", taken.candidate.id, _json.dumps({"kind": taken.kind}))
        if taken.kind == "new":
            conn.execute("UPDATE lesson SET state='picked', score=?, updated_at=? WHERE id=?",
                         (taken.score, store.utcnow(), taken.candidate.id))
            click.echo(f"picked {taken.candidate.id}: {taken.candidate.topic} — run `sprigly tick`")
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
                    f"[yellow]retry {attempt}[/yellow] [dim]{err[:60]}[/dim]"))
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
