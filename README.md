# Sprigly

Teaches one small, well-scoped thing at a time, across diverse domains — numerics, physics, human
sciences, business. An agent proposes topics and harvests open-access sources; NotebookLM turns
them into a podcast, a video overview and a quiz; you listen, then grade the quiz and the schedule
takes care of when to see it again. Larger goals are *tracks*, filled in by the small lessons.

## Setup

Needs Python (managed by pyenv), [`uv`](https://docs.astral.sh/uv/), a Google account with
NotebookLM, and Chrome signed in to it.

```bash
uv sync
uv tool install "notebooklm-py[android,browser]"
notebooklm login --browser chrome --master-token --account you@example.com
notebooklm auth check --test --json          # expect "status": "ok"
```

The `android` extra provides master-token auth, which refreshes cookies without a browser and is
what makes unattended runs work. That token is a **full-account Google credential**, not a scoped
API key — it lives in `~/.notebooklm/`, outside this repo. Do not move or commit it.

Configuration is read from `$XDG_CONFIG_HOME/sprigly/config.toml`, absent by default; data lives
under `$XDG_DATA_HOME/sprigly/`. `sprigly config` prints what is in effect as TOML, so any section
can be pasted straight into that file to override it.

Installing as a tool (`uv tool install --editable .`) gives it its own environment, so after a
dependency changes, re-run that command with `--reinstall`.

```bash
sprigly config                # resolved settings and paths
sprigly status                # pipeline state, parked failures, review load
```

Sprigly never deletes a NotebookLM notebook on its own. `sprigly notebooks` lists them and
`--prune` offers only the ones Sprigly created, one confirmation each.

## A worked example

Set a goal and let the agent decompose it:

```bash
sprigly focus "meshless methods" --depth 3
sprigly curate --track 1
```

Pick something to learn. The menu is fuzzy-searchable; Tab marks several:

```bash
sprigly next
#  1  new  numerics  20m  0.57  rbf-fd stencil construction   [track-debt 1.00 preference 0.93]
```

Advance it. Each `tick` moves every lesson one step — harvest, upload, generate, download — so run
it a few times, or from a systemd timer. Generation takes minutes, which is why it polls rather
than waits:

```bash
sprigly tick
sprigly status meshless-methods.rbf-fd-stencil-construction
```

Lessons are addressed by name, not number: an id, a slug, or any unique fragment of one
(`sprigly status rbf-fd` is enough). Everything lands in
`~/.local/share/sprigly/lessons/<id>/` — sources, `brief.md`, the artifacts, and a `notes.md`
listing what the lesson was built from and how good the evidence is.

Consume it on the machine, or open the notebook in the NotebookLM app on your phone:

```bash
sprigly play rbf-fd --kind video
sprigly ask rbf-fd "why does the shape parameter matter?"
```

Nothing reports back from a phone, so record progress yourself, then work the quiz NotebookLM
generated from the same sources:

```bash
sprigly done rbf-fd --rating 4 --note "wanted more on the flat limit"
sprigly quiz rbf-fd
sprigly review                # everything that has come due, across all lessons
```

When a lesson turns out too shallow, split it rather than replacing it. When one comes due again,
freshen it — the original sources stay as the baseline and a few recent ones join them:

```bash
sprigly deeper rbf-fd
sprigly redo rbf-fd --from freshen
sprigly tick --lesson rbf-fd
```
