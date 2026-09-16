You are filtering a list of already-credible sources down to the ones that are actually about a
given lesson topic. Credibility is settled — every item below passed a metadata gate. Your only job
is relevance.

Lesson topic: {{topic}}
Depth {{depth}} — {{scope}}

Candidates:
{{candidates}}

Drop an item when it is about a different subject that merely shares vocabulary, when it is far
outside the depth asked for, or when it would not help someone learning this specific topic. Keep
an item when in doubt — a marginal source costs little, and dropping a good one costs a lesson.

Return ONLY a JSON array, no prose before or after, of the items to KEEP:

[
  {"n": 1, "why": "one short line on what it contributes"},
  {"n": 4, "why": "..."}
]
