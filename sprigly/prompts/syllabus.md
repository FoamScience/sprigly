You are decomposing ONE topic into an ordered syllabus for a single learner.

Topic: {{goal}}
Granularity: depth {{depth}} — {{scope}}
Target length per lesson: about {{minutes}} minutes of audio.

Already covered in this track, do not repeat:
{{recent}}

Tags already in use. Reuse one whenever it fits rather than inventing a near-duplicate:
{{known_tags}}

Break the topic into at most {{n}} lessons at that granularity, ordered so each builds on the ones
before it. Each lesson is ONE thing taught well, not a survey. If the topic is too small to fill
{{n}} lessons at this depth, return fewer — padding is worse than stopping.

Return ONLY a JSON array, no prose before or after, with each element shaped exactly:

[
  {
    "topic": "how rbf-fd builds a stencil",
    "domain": "numerics",
    "why": "one line on where this sits in the sequence",
    "depth": {{depth}},
    "tags": ["radial-basis-functions", "stencil"],
    "prereqs": ["linear-algebra"],
    "est_minutes": 20
  }
]

Rules for the fields:
- topic: lowercase, specific enough that a reader knows exactly what is covered.
- domain: a broad field, lowercase and hyphenated.
- tags and prereqs: lowercase, hyphen-separated, US English spelling, no abbreviations.
- prereqs name what a learner must already understand, using the same tag vocabulary.
