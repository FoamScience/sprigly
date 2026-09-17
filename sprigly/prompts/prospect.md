You are proposing candidate lessons for a single learner. Each lesson is ONE small, well-scoped
thing that can be taught well in roughly {{minutes}} minutes of audio. Small is good — a single
mechanism, a single design decision, a single result. Breadth across unrelated fields is wanted:
this learner deliberately moves between physics, numerics, human sciences and business.

Their tracks. These are larger goals, and a **separate** command decomposes each one into its own
lessons. Do NOT propose lessons that belong to a track — that work is already handled:
{{tracks}}

What they have accumulated so far, by field:
{{coverage}}

Weight your proposals AWAY from the fields that already dominate that list and TOWARDS the ones
that are thin or missing. If one field is most of the list, propose little or nothing in it.

Recently taught, do not repeat:
{{recent}}

Already proposed and still waiting to be taught. These are taken — do not propose them again, and
do not propose a narrower slice or a rephrasing of one. This list is not a guide to what to propose
next; it is what to avoid:
{{pending}}

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
