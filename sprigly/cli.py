"""sprigly entrypoint."""

from __future__ import annotations

import json
import logging
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

    table = Table("#", "kind", "topic", "domain", "min", "score", "why")
    for i, o in enumerate(menu, 1):
        table.add_row(str(i), o.kind, o.candidate.topic, o.candidate.domain,
                      str(o.candidate.est_minutes), f"{o.score:.2f}", _why(o, cfg))
    Console().print(table)

    for o in menu:
        store.log_event(conn, "offered", o.candidate.id,
                        _json.dumps({"kind": o.kind, "score": o.score, "signals": o.signals}))

    choice = click.prompt("pick", type=click.IntRange(0, len(menu)), default=0,
                          show_default=False, prompt_suffix=" (0 to skip all): ")
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
