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

**CLI** (Python, uv-managed) as the control plane. No Android app, no web app, no RSS feed, no web
server, and nothing synced anywhere. A lesson is a directory on the machine; on the phone it is a
notebook in the NotebookLM app.

Every delivery mechanism considered — a podcast feed, then a Syncthing projection — existed to
rebuild something the NotebookLM app already does, and each added a second place where state could
drift. Both were removed.

Tradeoff accepted: choosing the next lesson requires a terminal.

## Components

```
  sprigly next              sprigly tick (systemd timer)
       |                            |
       v                            v
  +----------+   +-----------+   +-----------+   +-------------+   +-----------+
  | Curator  |-->|  Picker   |-->| Harvester |-->|  NotebookLM |-->|  Lesson   |
  | (agent)  |   | (scorer)  |   |  (agent)  |   |   Bridge    |   | directory |
  +----------+   +-----------+   +-----------+   +-------------+   +-----------+
       ^              ^                                 |                 |
       |              |                        NotebookLM app        sprigly play
       |              |                          (phone)                  |
       +--------------+---------- Store (SQLite) <---- Feedback <---------+
                                                    (sprigly done)
```

### 1. Store — `sprigly.db` (SQLite)

Single file under `$XDG_DATA_HOME/sprigly/` (falling back to `~/.local/share`), config TOML under
`$XDG_CONFIG_HOME/sprigly/`. Resolved with two lines of `os.environ.get`, not a dependency.

- `track` — a milestone *and* the focus mechanism: `goal`, `status`, `depth` (1-5), `active`,
  `exclusive`, `min_evidence`, `language`. **Several tracks may be active at once.**
- `lesson` — `slug`, `topic`, `track_id?`, `parent_id?`, `depth`, `state`, `score`, `language`,
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

Two backends, `opencode` and `claude`, invoked as subprocesses. Model ids are **keyed by backend**,
because they are not interchangeable — opencode wants `provider/model`, the claude CLI wants a short
alias — and a shared field would silently break the moment the backend changed. A cheap model runs
the bulk passes; judgement calls (syllabus decomposition, harvester relevance ranking) get a
stronger one.

Both CLIs already emit newline-delimited JSON events — `opencode run --format json`,
`claude -p --output-format stream-json` — so watching an agent work live needs no SDK and no extra
dependency, only the right flag and a small parser per backend. A run with a progress callback is
streamed and narrates itself; without one it stays a plain blocking call.

Output is requested as JSON and schema-checked. Agents narrate, so asking for bare JSON is
necessary but never sufficient: the parser finds the array, tracking bracket depth and string
escapes so that a bracketed aside in the prose does not derail it. Validation **rejects rather than
repairs** — a malformed proposal is cheap to ask for again — and a retry carries the complaint back
to the agent. After `max_retries` the tick step fails rather than writing garbage rows.

**Unfocused** it prospects ~8 unrelated candidates. **Focused** it decomposes one topic into an
ordered syllabus, capped at 25 lessons per decomposition — finer coverage comes from
`sprigly deeper`, not one giant dump. Re-decomposition matches on normalised topic text: reviewed
and picked lessons survive, only `proposed` ones are replaced.

The prompt carries three lists back to the agent: what was recently taught, **what is already
proposed and still waiting**, and the tag vocabulary so far. The pending list is not optional — with
only "recently taught" in scope, a second `curate` run cannot see the standing pool and re-proposes
all of it at a slightly different grain.

There is no separate "does this topic exist" verification pass. The harvester's gate is the same
test: a topic that yields no credible sources is misnamed or too niche, and the lesson goes to
`unsourced`.

### 3. Picker (the algorithm — the part worth inventing)

Four layers, each borrowed from a different literature. The survey and citations are in
[docs/picker-literature.md](docs/picker-literature.md).

**Layer 1 — eligibility gate.** Drop candidates below `track.min_evidence`, outside the active
tracks under `--only`, or whose groundwork has demonstrably lapsed. Knowledge Space Theory calls
this the *outer fringe*: ALEKS excludes what you are not ready for and lets you choose from the
rest, rather than ranking everything and hoping the unready items lose. Partial readiness stays a
soft weight — a half-met prerequisite is often where the good lesson is.

Only prerequisites the learner has **already met** count toward the gate. A prerequisite never seen
is evidence of unexplored ground, not of unreadiness, and counting it inverts the whole mechanism:
with an empty knowledge state nothing is mastered, so every candidate carrying two prerequisites
disappears — and the candidates carrying two prerequisites are the technical ones. The gate then
silently deletes physics, numerics and mathematics from the menu and leaves the soft domains
behind. This is not hypothetical; it is what the first real run did.

**Layer 2 — relevance score.** Pure functions over a `Snapshot`; nothing here touches the DB. A
weighted sum of:

- **prerequisite readiness** — fraction of prereq tags already covered
- **review pressure** — overlap with tags of due cards
- **learning progress** — the *change* in quiz success rate per domain, not its level, measured
  over a window of recent grades against the window before it. ZPDES rewards the derivative, so a
  mastered domain and a hopeless one are both neutral, and what attracts effort is where the
  success rate is still moving; the zone of proximal development falls out instead of being
  declared. A grade of `Again` is a lapse and everything else counts as recall — the difference
  between `Hard` and `Good` is the scheduler's business, not the question of whether you knew it.
  A domain with too little history is **absent** rather than zero, and reads as 0.5: no evidence
  and evidence of decline must not score the same.
- **track debt** — stale tracks get a boost, which is also what keeps several active tracks
  advancing fairly with no scheduler
- **effort fit** — `est_minutes` against the day's budget. That is the curator's guess, because
  scoring happens before generation; `actual_minutes` is written afterwards from the audio artifact
  and never overwrites it. Keeping both is what leaves any evidence of how wrong the estimate was
- **revealed preference** — a Beta(1,1) posterior per domain, **sampled rather than averaged**. That
  is Thompson sampling: principled exploration for one line, no epsilon to tune, and it stops a
  domain skipped twice from sinking permanently.

Every signal is normalised **within the candidate pool**, not absolutely. What matters is not that a
candidate's effort fit is 0.91 but whether it beats the others on offer today; absolute
normalisation compresses realistic candidates into a few thousandths of each other and lets noise
decide. Guard: rescale only when the raw spread exceeds an epsilon, or min-max turns genuinely
equivalent candidates into a confident ranking.

Domain diversity is **not** a signal. It belongs to the next layer.

**Layer 3 — set selection.** Greedy MAP over a DPP kernel `L = diag(q) · S · diag(q)`, with `q` the
relevance score and `S` tag similarity. The factorisation separates quality from similarity, so a
focus shrinks the similarity term instead of fighting the score — which is what MMR could not do.

On top of it, **calibration**: fit the offered set's domain distribution to a target mix rather than
penalising repetition. The goal was never to punish repeating a domain, it was to stay spread across
unrelated fields, and that is a target distribution. It also composes with focus for free — a
focused track sets the target to itself.

And a **reserved review lane**: a proportion of the k slots goes to lessons with due cards whenever
any exist. MEMORIZE shows the optimal schedule is a review *intensity* proportional to recall
probability — a rate, not a binary due flag — which is what justifies sizing a lane. As one signal
among six, a due card can be outvoted indefinitely, and fsrs degrades when reviews run late.

`sprigly next` offers the k candidates through a fuzzy picker; Tab marks several, so one offering
can yield more than one pick. Every candidate shown is logged as offered and every one taken as
picked, all stamped with the same `choice_set` id — nothing else records which candidates competed
against each other, and without it the choice sets cannot be reconstructed. **The skips are half the
training data.** Without a terminal on both ends — a script, a test — it falls back to a numbered
table accepting comma-separated numbers, rather than failing inside the subprocess.

**Layer 4 — fitting.** Each `next` is a pick out of k with full feature vectors, which is a
top-1-of-k choice; its exact likelihood is the conditional logit (McFadden), equivalently
Plackett-Luce top-1. Multi-pick offerings give a choice set several positives and need an explicit
decision — independent draws, or an unordered top-m likelihood — since the naive expansion
double-counts the shared negatives. No invented ground truth, skips used as real negatives. Because it is linear in
the features, the hand-weighted scorer is not a placeholder — it is the model that gets fitted.
Keeping the score linear, and scoring pure, are the two properties every later change must preserve.

Housekeeping: candidates unpicked after `candidate_ttl_days` (default 30) go to `expired`, so the
proposed pool cannot grow without bound. Cold start needs no special case — with no history,
preference sits at its prior and every other signal is flat, so the ranking falls back to curator
order. Mastery
decay comes free from fsrs — a tag counts as mastered while at least one card carrying it is not
overdue, so there is no second forgetting model.

### 4. Tags and domains — normalised, not controlled

A maintained controlled vocabulary does not survive topics ranging from quantum mechanics to
business. Instead, **normalise on write**: US English spelling, lowercase, no abbreviations,
hyphen-separated words. Domains get the same treatment.

Two mechanisms, both cheap. `tags.write()` is the single place tags enter the database, so no writer
can forget to normalise. And near-duplicates snap onto the existing vocabulary — singular against
plural, a stray suffix — at a deliberately high cutoff, because `rbf-fd` and `rbf-qr` are different
methods and must never collapse into one.

Spelling is a curated map, not a rule. "-our to -or" would wreck *four*, *your*, *tour*, *hour*,
*flour* and *contour*; "-re to -er" would wreck *are*, *here* and *genre*. Only the -ise/-ize family
gets a suffix rule, guarded by the standard exception list (*rise*, *exercise*, *promise*,
*expertise*, ...). Abbreviations expand solely from an explicit config map: guessing is how `fem`
ends up meaning both the finite element method and field emission microscopy in one database.

The existing tag list is also passed into the curator prompt as "reuse these where they fit", so the
vocabulary emerges and stays consistent without anyone maintaining it.

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
| C | Video, by explicit per-channel allowlist | Matched as host **and path**: matching on hostname alone turns one allowlisted channel into the whole of YouTube |
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

Adapters run concurrently, and so do the source downloads — both are independent and dominated by
waiting, so running them in sequence adds their latencies for nothing. Every network call carries a
timeout: one stalled host must not hold a harvest open indefinitely.

Only the strongest `rank_limit` candidates are put in front of the relevance agent, ordered by
citation count. A long listing is what a small model stalls on, and anything not ranked can still
top the selection up to the budget, so nothing is lost outright. The pass also gets a shorter
timeout than a curate call: it degrades open, so waiting the full budget only to keep everything is
wasted time.

**Relevance is a separate pass from credibility, and the only one the agent runs.** The gate cannot
catch a paper that is peer-reviewed, open access and about something else entirely: searching
*meshfree radial basis function* on arXiv returns *Radial velocity follow-up of CoRoT transiting
exoplanets*, which is impeccable and about exoplanets. The relevance pass **degrades open on any failure** — unreachable, unusable,
timed out, or a binary that is not there. It keeps every source and logs why. The gate has already
established these are credible, so harvesting a few loose ones beats harvesting nothing. Catching
only the library's own error type was not enough: a subprocess timeout escaped it and parked a
lesson after 871 seconds with credible sources already in hand. An agent that rejects *everything*
is likewise not believed.

Each lesson also gets a `brief.md` written next to its sources: depth, target length, evidence
level, any composition caveats, and the list of what was admitted. That becomes the NotebookLM
prompt.

### 6. Evidence levels — labelling, not one universal bar

"Scientifically proven" maps cleanly onto physics and numerics and poorly onto business and the
human sciences, where the good primary material is working papers, regulator filings, standards and
statistics rather than journal articles.

So the bar is per-track, not global. Every source carries a tier; every lesson derives an
`evidence_level` — `peer-reviewed`, `preprint`, `institutional`, `practitioner` or `manual` — from
its **weakest** admitted source, since what a lesson rests on is its floor and not its best
citation. One preprint among nine journal articles makes the lesson a preprint lesson. Shown in its
notes and in `sprigly sources <id>`. `track.min_evidence` sets the tolerance — defaulting to
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
unattended ticks viable. The library is async and the rest of Sprigly is not, so there is one
`asyncio.run()` per call at this boundary and the click app stays plain synchronous code.

**Sources go up as files *or* URLs** — `sources.add_url` exists, so anything the harvester could not
download is still uploaded by link, and Tier C video needs no separate downloader. `yt-dlp` is
therefore not a dependency after all.

Sources must finish indexing before generation is requested, or the artifact comes back empty, so
`start` waits on `wait_all_until_ready` before asking for anything — that wait is on indexing, not
on generation, which is still polled.

Which artifacts a lesson gets is a config list: **audio, video and quiz by default**. The video
overview is narrated slides, so it covers what a static deck would and adds the moving explanation;
`"slides"` can be added alongside it for a PDF to skim. Because video is a primary artifact here
rather than a bulky extra, `retention_video_days` defaults to 0 — nothing prunes it — and pruning
stays available for when disk becomes the problem.

Each artifact is asked for with **its own prompt**, from `sprigly/prompts/artifact_*.md`: what
suits a podcast does not suit a slide deck or a quiz. The audio prompt bans throat-clearing and
asks for the mechanism; the slides prompt asks for what audio carries badly — equations, labelled
diagrams, exact statements; the quiz prompt asks for questions about what breaks when an
assumption fails, not what a term is called. All four forbid inventing figures, numbers or
citations not in the sources. The lesson's `brief.md` is appended to each.

**Sprigly never deletes a notebook on its own.** Deleting something on the user's Google account is
not a decision a background timer gets to make — not after a download, not when a redo abandons
one, not even when a failed setup leaves an empty one behind. `notebook_id` stays on the lesson and
the artifact row, and `sprigly notebooks` lists what exists and which lesson uses it;
`--prune` offers the unused ones for deletion one at a time, each with a confirmation.

Accounts do have a notebook cap. Reaching it is a prompt to prune, not a licence to delete
unattended. `bridge.delete_notebooks` can be turned on for anyone who wants the old behaviour.

Generation is paced by `max_generations_per_day`; a quota error parks the lesson and retries the
next day rather than failing it.

Any library or API error parks the lesson with its error text for retry, never aborts the tick.

### 8. Where lessons are consumed

`data/lessons/<id>/` holds everything: the sources, `brief.md`, the artifacts, and a `notes.md`
listing the sources with their tiers and the lesson's evidence level, so a lesson directory
explains itself without the database.

On the machine, `sprigly play <id>` opens an artifact. **On the phone, the NotebookLM app is the
consumption surface** — its own audio and video players, its own interactive sessions, no files to
sync and nothing to keep in step. This is why notebooks are never deleted: the app needs them to
still be there.

The cost is that the phone reports nothing back. There is no callback, no play position, no
deletion to observe. So progress is recorded deliberately, on the machine, with
`sprigly done <id> --rating N --note "…"`.

An earlier design mirrored a projection of each lesson to the phone with Syncthing and treated the
deletion of a file as the signal it had been consumed. It worked, but it existed to reconstruct
something the NotebookLM app already does better, and it made the phone a second place where state
could drift. Removed.

### 9. Feedback and review

`sprigly done <id>` records a rating and note. `sprigly quiz <id>` runs the CLI quiz over the
questions NotebookLM generated — we write no questions ourselves. A multiple-choice result
suggests an fsrs rating (wrong → Again, right after a retry → Hard, right first time → Good) and
the final say is yours, including Easy. Grades go to `fsrs`, which returns the next due date.
Flashcards export to Anki if wanted.

When a lesson comes due, **the original material is presented again**. `sprigly redo <id> --from
freshen` re-harvests with the original sources kept as the baseline plus up to
`freshen_new_sources` (default 3) recent additions, and regenerates **the audio only** — the video
and slides are the expensive parts and nothing about them has changed.

Keeping the originals is the point: a refresher that replaced its sources would be a different
lesson wearing the same name. The harvester is told what the lesson already has and searches for
what is new, most recent first; `lesson.artifacts` carries the one-run restriction so the rest of
the pipeline needs no special case.

## Focus mode and the depth knob

A focus is a track that is active, and depth is a column on it. **Multiple tracks can be active at
once** — parallel interests are the normal case, not an edge case, and track debt keeps them
advancing fairly without a scheduler.

```bash
sprigly focus                                 # list tracks, change nothing
sprigly focus "meshless methods" --depth 5 --lang en --min-evidence preprint
sprigly focus --depth 4                       # re-tune every active track
sprigly focus --only                          # offer nothing outside the active tracks
sprigly focus --shared                        # undo --only
sprigly focus --off "meshless methods"        # deactivate one
sprigly focus --off                           # deactivate all
sprigly deeper 12                             # split lesson 12 into finer children
```

Changing a track's depth **expires its pending proposals** and says so, so the next
`sprigly curate --track N` redecomposes at the new granularity. Lessons already reviewed are
untouched: re-tuning granularity never throws away work.

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

### Asking a lesson a question

`sprigly ask <id> "why does the shape parameter matter?"` answers from that lesson's own sources.
Notebooks are deleted once their artifacts are downloaded, so the first question about an older
lesson **rebuilds one from the sources still on disk** — slower than the rest, and cheaper than
keeping every notebook alive against the account cap. The rebuild uploads and generates nothing, so
it spends no generation quota.

The same rebuilt notebook is what a live audio or video session would attach to; `notebooklm-py`
does not expose those yet, and `notebooks.get_share_url` is already wired for when it does.

### Naming things

Integers are a lookup table you have to keep in your head, so every lesson and track also carries a
**slug**: `cognitive-load-theory-interface`, or `meshless-methods.rbf-fd-stencil-construction` for
a lesson inside a track. Filler words are dropped, the name is capped at four words, and a lesson
is never named after its own track twice.

The integer primary key stays. It is stable, foreign keys point at it, and renaming a topic must
not break the graph — the slug is a second name resolved on the way in, not a replacement.

Every command that takes a lesson accepts the id, the exact slug, a unique prefix, or a unique
substring: `sprigly status rbf-fd-stencil` is enough. An ambiguous reference is an error that names
the candidates, because silently picking one is how you grade the wrong lesson.

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
| Literature metadata | `habanero` (Crossref), `arxiv`; OpenAlex is called directly over `httpx`, since `pyalex` offers no timeout |
| URL to clean text | `trafilatura` |
| CLI / output / HTTP | `click`, `rich`, `httpx` — already transitive deps of `notebooklm-py` |
| Fuzzy picking | `iterfzf` — ships the `fzf` binary in the wheel, so nothing to install separately |
| Store | stdlib `sqlite3` |
| Scheduling, locking, backup, logging, paths | stdlib and systemd |
| Curator / Harvester | `opencode` or `claude` CLI via `subprocess`, prompts as text files |

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
3. Lesson notes and `sprigly play`. Usable at this point, fed manually.
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
