"""sprigly entrypoint."""

from __future__ import annotations

import json
from pathlib import Path

import click

from . import config


@click.group()
@click.option("--config", "config_file", type=click.Path(path_type=Path),
              help="Config TOML to use instead of the XDG location.")
@click.pass_context
def main(ctx: click.Context, config_file) -> None:
    """Learn one thing at a time."""
    ctx.obj = config.load(path=config_file)


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
        conn.close()
    click.echo(", ".join(f"{v} -> {k}" for k, v in sorted(moved.items()) if v) or "nothing to do")
