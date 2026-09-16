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
