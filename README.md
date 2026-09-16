# Sprigly

Teaches one small, well-scoped thing at a time, across diverse domains — numerics, physics, human
sciences, business. A CLI orchestrates the pipeline: an agent proposes topics and harvests open-access
source material, NotebookLM turns that material into a podcast, a slide deck and a quiz, and the
artifacts land in a folder that Syncthing mirrors to your phone. Larger milestones are *tracks*,
filled in by the small lessons.

Design and rationale: [DESIGN.md](DESIGN.md).

## Status

Early implementation. NotebookLM account setup is done and the project skeleton, CLI entrypoint and configuration layer exist. The store, the pipeline and the picker do not yet.

## Prerequisites

- Python, managed by `pyenv`
- [`uv`](https://docs.astral.sh/uv/) — `curl -LsSf https://astral.sh/uv/install.sh | sh`
- A Google account with NotebookLM (Gemini Notebook) access
- Chrome, signed in to that account
- Syncthing on both machine and phone (only needed once delivery exists)

## Step 0 — NotebookLM account setup

Sprigly drives NotebookLM through [`notebooklm-py`](https://github.com/teng-lin/notebooklm-py), an
unofficial client for the internal API. This step is done once, by hand, before any Sprigly code
runs. It is deliberately first: it is the only part of the system with a hard external dependency
we do not control.

Install the CLI into its own isolated environment. The `android` extra provides the master-token
flow; `browser` provides the login path.

```bash
uv tool install "notebooklm-py[android,browser]"
```

Authenticate. `--browser chrome` reuses your signed-in Chrome rather than downloading Playwright's
bundled Chromium; `--master-token` mints a durable credential that refreshes web cookies on demand,
with no browser per session. That is what makes unattended `sprigly tick` runs possible.

```bash
notebooklm login --browser chrome --master-token --account you@example.com
```

Expected output names the profile it wrote and how many notebooks the account already has:

```
Master-token login OK — 5 notebooks. Saved to ~/.notebooklm/profiles/default/storage_state.json
```

Verify:

```bash
notebooklm auth check --test --json
```

`"status": "ok"` with all five `checks` true — `storage_exists`, `json_valid`, `cookies_present`,
`sid_cookie`, `token_fetch` — means the account is ready. The reported `psidts.expires_at` is roughly
a year out; the master token refreshes it automatically before then.

### Credential handling

The master token is a **full-account** Google credential, not a scoped API key. It lives in
`~/.notebooklm/profiles/default/`, outside this repository, and Sprigly never reads or copies it —
`notebooklm-py` owns that store. Do not move it into the project directory, and do not commit it.

### Keepalive

Once `sprigly tick` exists it runs from a systemd timer, and the same timer should run:

```bash
notebooklm auth refresh --quiet
```

## Where lessons live

Everything lands in `~/.local/share/sprigly/lessons/<id>/`: the sources, `brief.md`, the artifacts,
and a `notes.md` listing the sources with their tiers and the lesson's evidence level.

On the machine, `sprigly play <id>` opens one. On the phone, open the lesson's notebook in the
NotebookLM app — that is why Sprigly never deletes a notebook.

Nothing reports back from the phone, so tell Sprigly yourself:

```bash
sprigly done 3 --rating 4 --note "good, wanted more on the shape parameter"
```

## Step 0 — remaining checks

Authentication is confirmed. Four assumptions in the design are not yet verified, and each one
changes downstream work. Push one topic through the CLI by hand — create a notebook, add sources,
generate an audio overview and a slide deck, download both — and record:

1. **Can a video URL be added as a source directly?** If yes, `yt-dlp` is unnecessary for
   documentary and lecture material.
2. **What are the per-notebook source cap and the per-account notebook cap?** These decide whether
   notebooks must be deleted after their artifacts are downloaded.
3. **What container and MIME is the audio actually?** Never assumed; recorded per artifact.
4. **Account caps:** how many notebooks may exist at once? Nothing deletes them automatically, so
   this decides how often `sprigly notebooks --prune` is needed.

## Upstream notes

- Google renamed NotebookLM to Gemini Notebook in July 2026. From `0.9.0` the canonical distribution
  is `gemini-notebook-py`; the import name `notebooklm` stays. Sprigly pins `>=0.8,<0.9` and expects
  one rename.
- The API is unofficial. Google can change internal endpoints at any time, and heavy usage is
  throttled — so generation is paced, and failures park a lesson for retry rather than aborting.

## Development

```bash
uv sync                        # install dependencies (includes a bundled fzf binary)
uv run sprigly config          # resolved configuration and paths
uv run python sprigly/config.py  # module self-check
```

Configuration is read from `$XDG_CONFIG_HOME/sprigly/config.toml` (absent by default — the built-in
defaults apply), and data lives under `$XDG_DATA_HOME/sprigly/`. Override the config file per
invocation with `sprigly --config <path>`.

Work is tracked in [beads](https://github.com/gastownhall/beads) under epic `tasks-8wk`.

```bash
bd ready                    # available work
bd show tasks-8wk           # the epic
bd swarm validate tasks-8wk # dependency graph and ordering
```
