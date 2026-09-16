You are proposing candidate lessons for a single learner. Each lesson is ONE small, well-scoped
thing that can be taught well in roughly {{minutes}} minutes of audio. Small is good — a single
mechanism, a single design decision, a single result. Breadth across unrelated fields is wanted:
this learner deliberately moves between physics, numerics, human sciences and business.

Their tracks (larger goals being filled in by small lessons):
{{tracks}}

Recently taught, do not repeat:
{{recent}}

Tags already in use. Reuse one whenever it fits rather than inventing a near-duplicate:
{{known_tags}}

Propose {{n}} candidates.

Return ONLY a JSON array, no prose before or after, with each element shaped exactly:

[
  {
    "topic": "how rbf-fd builds a stencil",
    "domain": "numerics",
    "why": "one line on why this is worth learning now",
    "depth": 3,
    "tags": ["radial-basis-functions", "stencil"],
    "prereqs": ["linear-algebra"],
    "est_minutes": 20
  }
]

Rules for the fields:
- topic: lowercase, specific enough that a reader knows exactly what is covered.
- domain: a broad field, lowercase and hyphenated, e.g. numerics, business, human-sciences.
- depth: 1 orientation over a whole field, 3 how one method works, 5 one derivation or failure mode.
- tags and prereqs: lowercase, hyphen-separated, US English spelling, no abbreviations.
- prereqs name what a learner must already understand, using the same tag vocabulary.
