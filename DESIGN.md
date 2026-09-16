# Sprigly — System Design

One thing at a time, taught well. Diverse domains. Small pieces accumulate into tracks.

## Assumptions (stated, not verified)

1. **NotebookLM has no *official* public API**, but `notebooklm-py` (unofficial, 32 releases
   Jan-Sep 2026, actively maintained) wraps the internal gRPC API and covers everything Sprigly
   needs. Its own README warns Google can change internal endpoints at any time and that heavy
   usage gets throttled — so the risk is "an upstream dependency may break", not "we must build
   and maintain scraping".
2. **NotebookLM produces every artifact directly.** Audio Overview, Video Overview, Slide Deck
   (PDF/PPTX), plus quiz, flashcards, mind map, report. Re-uploading the podcast as a source to
   "get slides" is unnecessary — generate all of them from the same source set.
3. **Google renamed NotebookLM to Gemini Notebook (July 2026).** From 0.9.0 the canonical
   distribution is `gemini-notebook-py`; the import name `notebooklm` stays. Pin `>=0.8,<0.9` and
   expect one rename.
4. Single user, single machine, no multi-tenancy, no public exposure.
5. **The machine is the primary study surface.** The phone is an optional secondary one, reached
   over LAN or Tailscale. Neither needs to be always-on.
6. Container formats are whatever NotebookLM actually emits — recorded per artifact, never assumed.

## Shape: CLI + a synced folder

**CLI** (Python, uv-managed) as the control plane. No Android app, no web app, **no RSS feed and
no web server**. Artifacts are files in a folder; Syncthing mirrors that folder to the phone over
LAN or Tailscale.

Dropping the feed removes `feedgen`, GUID stability, enclosure lengths, MIME negotiation, a public
hostname and a server process — none of which were buying anything once podcast delivery stopped
being the point. A lesson is a directory; anything that opens files can consume it.

Tradeoff accepted: choosing the next lesson requires a terminal.

## Components

```
  sprigly next              sprigly tick (systemd timer)
       |                            |
       v                            v
  +----------+   +-----------+   +-----------+   +-------------+   +----------+
  | Curator  |-->|  Picker   |-->| Harvester |-->|  NotebookLM |-->|   Drop   |
  | (agent)  |   | (scorer)  |   |  (agent)  |   |   Bridge    |   | (folder) |
  +----------+   +-----------+   +-----------+   +-------------+   +----------+
       ^              ^                                                  |
       |              |                                            Syncthing
       |              |                                                  v
       +--------------+---------- Store (SQLite) <---- Feedback <--- phone/machine
```

### 1. Store — `sprigly.db` (SQLite)

Single file under `$XDG_DATA_HOME/sprigly/` (falling back to `~/.local/share`), config TOML under
`$XDG_CONFIG_HOME/sprigly/`. Resolved with two lines of `os.environ.get`, not a dependency.

- `track` — a milestone *and* the focus mechanism: `goal`, `status`, `depth` (1-5), `active`,
  `exclusive`, `min_evidence`, `language`. **Several tracks may be active at once.**
- `lesson` — `topic`, `track_id?`, `parent_id?`, `depth`, `state`, `score`, `language`,
  `est_minutes`, `actual_minutes`, `thin`, `job_ref`, `polled_at`, `retry_count`, `last_error`,
  `next_attempt_at`, timestamps.
- `source` — `url`, `doi`, `tier`, `evidence_level`, `local_path`, `retracted`, `oa_status`.
  Unique on normalised DOI or URL **globally**, so a paper harvested twice is referenced, not
  re-downloaded.
- `artifact` — `kind`, `path`, `mime`, `bytes`, `duration`, `notebook_id`.
- `card` — flashcard front/back, `lesson_id`, fsrs state, `due`. Deduped by a hash of the
  normalised front text within a track.
- `event` — append-only: proposed, offered, picked, skipped, generated, dropped, consumed, rated,
  reviewed, pruned, expired. The picker's only training data. Never delete rows.

State machine:

```
proposed -> picked -> harvesting -> uploading -> generating -> ready -> consumed -> reviewed
     |                     |                          |
     v                     v                          v
  expired             unsourced                    failed
```

`expired`, `unsourced` and `failed` are terminal. `thin` is a flag, not a state. `sprigly tick`
selects rows by state and advances each one step — the DB is the queue.

**Generation is polled, never blocked on.** Entering `generating` stores `job_ref` (notebook id
plus per-artifact job ids) and returns immediately. Later ticks poll and advance when ready.

**Failures park in place, not in `failed`.** A retryable error leaves the lesson's state untouched
and sets `next_attempt_at`; only exhausting `max_retries` moves it to terminal `failed`. The retry
then needs no record of which state to return to. A spent daily quota and an unsourceable topic are
not failures and cost no retries.

A lesson advances at most once per pass, enforced by a seen-set — without it a lesson moved into
`uploading` gets picked up again by the `uploading` handler later in the same loop and runs the
whole pipeline in one go, defeating both the poll and the generation cap.

Operational details: `flock` on a lockfile so overlapping timer runs exit quietly; retries capped
at 5 with exponential backoff via `next_attempt_at`; `sqlite3.Connection.backup()` into
`data/backups/` each tick, keeping the last 7; stdlib `logging` to `data/sprigly.log`.

### 2. Curator (AI agent)

Two backends, `opencode` and `claude`, invoked as subprocesses. Models are configurable
throughout; defaults are an OpenRouter free model for cheap bulk passes and Sonnet for judgement
calls (syllabus decomposition, harvester relevance ranking).

Output is requested as JSON and schema-checked. On malformed output, retry twice with the
validation error appended to the prompt, then fail that tick step.

**Unfocused** it prospects ~8 unrelated candidates. **Focused** it decomposes one topic into an
ordered syllabus, capped at 25 lessons per decomposition — finer coverage comes from
`sprigly deeper`, not one giant dump. Re-decomposition matches on normalised topic text: reviewed
and picked lessons survive, only `proposed` ones are replaced.

There is no separate "does this topic exist" verification pass. The harvester's gate is the same
test: a topic that yields no credible sources is misnamed or too niche, and the lesson goes to
`unsourced`.

### 3. Picker (the algorithm — the part worth inventing)

Pure functions over a `Snapshot`; nothing in scoring touches the DB, which is what makes the
weights fittable offline against replayed history.

Six signals, each normalised to 0-1, combined as a weighted mean, plus a flat bonus for lessons in
any active track:

- **prerequisite readiness** — fraction of prereq tags already covered. A soft weight, not a gate:
  a half-met prerequisite is often where the interesting lesson is.
- **review pressure** — overlap with tags of **cards** that are due. Cards are the fsrs unit; a
  lesson's due date is its earliest due card.
- **domain diversity** — half-life decay on when that domain was last taught. **Suspended while
  any track is focused**, because focus is the explicit instruction to stop spreading.
- **track debt** — stale tracks get a boost. With several tracks active this is also what keeps
  them advancing fairly, for free.
- **effort fit** — `est_minutes` against the day's budget. The estimate is the curator's guess;
  `actual_minutes` is written after generation and both are kept.
- **revealed preference** — Beta(1,1) posterior mean of picks over offers per domain, so an unseen
  domain sits at 0.5 rather than 0 or a division by zero.

`offer()` then takes the top k by greedy MMR on tag overlap. Straight top-k returns five phrasings
of the same topic, which is not a choice. All k are logged as offered and the taken one as picked —
**the four skips are worth as much as the pick**.

**Mastery decay comes free from fsrs**: a tag counts as mastered while at least one card carrying
it is not overdue. No second forgetting model.

Housekeeping: candidates unpicked after `candidate_ttl_days` (default 30) go to `expired`. Cold
start — no history — falls back to curator order, since preference is 0.5 everywhere and diversity
is 1.0.

### 4. Tags and domains — normalised, not controlled

A maintained controlled vocabulary does not survive topics ranging from quantum mechanics to
business. Instead, **normalise on write**: US English spelling, lowercase, no abbreviations,
hyphen-separated words. Domains get the same treatment.

The drift mitigation is cheap: the existing tag list is passed into the curator prompt as "reuse
these where they fit". The vocabulary emerges and stays consistent without anyone maintaining it.

This matters more than it looks — prereq readiness, review pressure and MMR all work by set
intersection, and `RBF` vs `rbf` vs `radial-basis-functions` silently sends three of six signals
to zero.

### 5. Harvester (AI agent) — open access only

Output: sources plus a framing brief that becomes the NotebookLM prompt, bounded by the depth
table's source budget and a wall-clock cap. Downloads into the lesson directory so the bridge
uploads files, not URLs.

**Credibility is a deterministic metadata gate, never an LLM judgement.** OpenAlex is the primary
index; Crossref resolves DOIs found elsewhere; arXiv covers preprints. The agent only ranks
relevance among items that already passed.

Hard reject: `is_retracted`, `is_paratext`, a `type` outside {article, review, book-chapter,
preprint, report, dissertation}, or **no open-access location** — paywalled sources are out.

| Tier | What | Note |
|---|---|---|
| A | Peer-reviewed journal or conference, OA fulltext | |
| A− | Preprint (arXiv, bioRxiv) | Never the *only* source for a lesson |
| B | Institutional: `.edu`, `.ac.*`, `.gov`, national labs, standards bodies, statistical agencies | |
| C | Video, by explicit per-channel allowlist | Channel IDs, never "YouTube" |
| M | `sprigly harvest --allow <url>` | Manual override, logged as such |

Deliberately **not** gates: a citation-count minimum (it penalises recent work, and a good 2024
review teaches better than an uncited 1987 classic) and a recency cutoff (recency is a tiebreak
only). The real quality lever is **composition per lesson**: at least one Tier A, preprints capped
around 40%, a review or survey required at depth ≤3, primary articles preferred at depth ≥4.

Shortfall policy: below 60% of the source budget the lesson is generated but flagged `thin`, and
the flag is shown in its notes; below two sources it goes to `unsourced` and is never generated.

OA resolution order: OpenAlex `best_oa_location.pdf_url`, then an arXiv version, then drop with a
logged reason. `trafilatura` converts HTML to clean text; `yt-dlp` handles Tier C **only if** the
bridge cannot take a video URL as a source directly — verify that in step 0.

### 6. Evidence levels — labelling, not one universal bar

"Scientifically proven" maps cleanly onto physics and numerics and poorly onto business and the
human sciences, where the good primary material is working papers, regulator filings, standards and
statistics rather than journal articles.

So the bar is per-track, not global. Every source carries a tier; every lesson derives an
`evidence_level` of `peer-reviewed`, `preprint`, `institutional`, `practitioner` or `mixed`, shown
in its notes and in `sprigly sources <id>`. `track.min_evidence` sets the tolerance — defaulting to
`peer-reviewed` for STEM tracks and `institutional` for business ones. **The system never silently
downgrades**; it flags or refuses.

Per-domain source classes: human sciences prefer meta-analyses and systematic reviews and flag
single-study claims; business admits working papers (NBER, SSRN with institutional affiliation),
regulator filings, standards, national statistics and textbooks, and excludes consultancy
whitepapers and trade magazines as primary sources.

Both the tier thresholds and these class lists are settled by **measurement, not argument**: run
the gate over three topics — one numerics, one human science, one business — at depth 3 and 5, and
read the yield.

### 7. NotebookLM Bridge — thin wrapper over `notebooklm-py`

```
notebooklm login --master-token --account <you>   # once
notebooklm auth refresh --quiet                   # from the same timer as tick
```

Master-token auth mints fresh cookies on demand with no browser per session, which is what makes
unattended ticks viable. One `asyncio.run()` at the bridge boundary; the click app stays sync.

Notebooks are **deleted after their artifacts are downloaded and verified**, keeping `notebook_id`
on the artifact row for traceability — otherwise the account's notebook cap ends the project around
month one. Generation is paced by `max_generations_per_day`; a quota error parks the lesson and
retries the next day rather than failing it.

Any library or API error parks the lesson with its error text for retry, never aborts the tick.

### 8. Drop and delivery

`data/lessons/<id>/` holds the originals. `data/drop/<slug>/` is a projection of what goes to the
phone — audio, slides, and a `notes.md` carrying the topic, the framing brief, the source list with
tiers, and the lesson's evidence level. Syncthing mirrors `data/drop/` and nothing else.

**Deletion is the consumption signal, for any material, not just audio.** When a lesson's drop
folder is emptied or removed — the podcast app's delete-after-playback, or you deleting a PDF you
finished — the deletion syncs back and the next tick marks the lesson `consumed`. Verify the
local-folder plus auto-delete combination during step 0; it is assumed, not confirmed.

Two guards this needs: sprigly's own retention pruning writes a `pruned` event so it is never
mistaken for consumption, and only the drop projection is ever deleted — originals stay.

**There is no auto-advance.** A lesson sits in `ready` until something actually reports back:
deletion-sync, `sprigly play <id>` on the machine (which opens the artifact and logs the event), or
an explicit `sprigly done <id>`. A stalled queue is honest; a fabricated completion is not.

Retention: audio and notes are kept indefinitely (they are small); video is pruned after
`retention_video_days` (default 30) while its slide deck survives.

### 9. Feedback and review

`sprigly done <id>` records a rating and note. `sprigly quiz <id>` runs the CLI quiz over the
questions NotebookLM generated — we write no questions ourselves. A multiple-choice result
suggests an fsrs rating (wrong → Again, right after a retry → Hard, right first time → Good) and
the final say is yours, including Easy. Grades go to `fsrs`, which returns the next due date.
Flashcards export to Anki if wanted.

When a lesson comes due, **the original material is presented again**. `--freshen` re-harvests with
the original sources as the baseline plus up to `freshen_new_sources` (default 3) recent additions,
and regenerates the audio only — not the video or slides, which are the expensive parts.

## Focus mode and the depth knob

A focus is a track that is active, and depth is a column on it. **Multiple tracks can be active at
once** — parallel interests are the normal case, not an edge case, and track debt keeps them
advancing fairly without a scheduler.

```bash
sprigly focus "meshless methods" --depth 5 --lang en
sprigly focus --depth 4                       # re-tune the active track
sprigly focus --only                          # offer nothing outside the active tracks
sprigly focus --off "meshless methods"        # deactivate one
sprigly focus --off                           # deactivate all
```

`--only` filters **new candidates** and never suppresses due reviews. Reviews are scheduled work,
not exploration; hiding them to honour a focus flag is how a spaced-repetition queue quietly rots.

### What depth changes

One integer, mapped by a config table, feeding the curator prompt, the harvester's source budget
and NotebookLM's generation parameters:

| depth | lesson scope | audio length | sources | "meshless methods" at this depth |
|---|---|---|---|---|
| 1 | the whole field, one orientation pass | brief | ~5 | "What meshless methods are, and when they beat FEM" |
| 2 | major families | brief | ~8 | "SPH" · "RBF collocation" · "MLS / EFG" |
| 3 | how one method actually works | deep-dive | ~10 | "How RBF-FD builds a stencil" |
| 4 | a single design decision inside a method | deep-dive | ~12 | "Choosing the shape parameter in RBF-FD" |
| 5 | one derivation, one paper, one failure mode | deep-dive | ~15 | "Ill-conditioning as the shape parameter goes to zero, and the RBF-QR fix" |

Default 3.

### Changing depth mid-track

- `sprigly focus --depth N` — re-decompose the remaining syllabus; reviewed lessons untouched.
- `sprigly deeper <lesson-id>` — split one lesson into children at `depth + 1` via `parent_id`.
  This is the knob actually reached for, after hearing a podcast and finding it thin. The global
  setting is the one nobody predicts correctly in advance.

`shallower` and sibling merging are not built — marking children reviewed and moving on covers it.

A track is **done** when it has no `proposed` lessons left and all its lessons are `reviewed`. The
curator can be asked to extend it.

## Configuration

One TOML file. Everything below is a knob, and none of it is a code change:

- agent backend (`opencode` | `claude`) and per-role models — default an OpenRouter free model for
  bulk passes, Sonnet for judgement
- scoring weights, `budget_minutes` (with a `--budget` override on `next`), `candidate_ttl_days`
- the depth table, `max_syllabus`, `default_language` (`en`)
- tier rules, the Tier C channel allowlist, composition ratios, `min_evidence` defaults
- `max_generations_per_day`, `keep_notebooks`, `retention_video_days`, `freshen_new_sources`
- paths for the store, lessons and drop

## Dependencies — buy, don't build

| Need | Library |
|---|---|
| NotebookLM control | `notebooklm-py` (pinned `>=0.8,<0.9`) |
| Spaced repetition | `fsrs` |
| Literature metadata | `pyalex` (OpenAlex), `habanero` (Crossref), `arxiv` |
| URL to clean text | `trafilatura` |
| Video sources | `yt-dlp`, only if the bridge cannot take a URL directly |
| CLI / output / HTTP | `click`, `rich`, `httpx` — already transitive deps of `notebooklm-py` |
| Store | stdlib `sqlite3` |
| Scheduling, locking, backup, logging, paths | stdlib and systemd |
| Phone delivery | Syncthing (external, no code) |
| Curator / Harvester | `opencode` or `claude` CLI via `subprocess` |

### What is actually new code

1. **`score()` and `offer()`** — the picking algorithm. The reason the project exists.
2. **The lesson state machine** driving `tick`, including the generation poll.
3. **The credibility gate** — deterministic, and the one place source quality is decided.
4. **The CLI quiz runner** and its mapping into fsrs.
5. **Curator and harvester prompts** — text files, not code.

## Build order

0. `notebooklm login` and push one topic through by hand. Also settles three unknowns the design
   assumes: whether a video URL works as a source, the per-notebook and per-account caps, and what
   container the audio actually is.
1. Store, state machine, `tick` with the generation poll.
2. Bridge wrapper. Real artifacts end to end for a hardcoded topic.
3. Drop folder, notes, Syncthing, deletion-as-consumed. Usable at this point, fed manually.
4. Curator, tag normalisation, `next` with a hand-weighted `score()`.
5. Focus, multiple active tracks, depth.
6. Harvester and the credibility gate, then the yield spike that sets its thresholds.
7. Quiz runner, cards, fsrs. Only then fit the scoring weights — earlier is fitting noise.

Step 0 is non-negotiable and takes ten minutes. It moves the real unknowns to the front.

## Explicitly not built

- RSS feed, web server, public hostname — a synced folder replaces all three.
- Web UI or mobile app.
- Auto-advancing a lesson nobody reported back on.
- A controlled tag vocabulary — normalisation plus prompt feedback instead.
- Custom audio or video rendering, quiz or flashcard authoring, a spaced-repetition scheduler.
- Browser automation, an ORM, a migration tool, a task queue.
- A prerequisite graph editor — the curator infers prereq tags; hand-curating a DAG is a trap.
- `shallower` / sibling merging, and a plugin system for scorers.
