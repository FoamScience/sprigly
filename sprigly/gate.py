"""The credibility gate.

Credibility is decided here, in code, from metadata the adapters carried back. No agent reads an
abstract and forms an opinion about rigour — the agent only ranks relevance among what already
passed. Everything below is a rule you can point at afterwards and argue with.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlparse

from .sources import READABLE_TYPES, Work

# Tier -> the evidence level a source of that tier contributes.
TIER_LEVEL = {"A": "peer-reviewed", "A-": "preprint", "B": "institutional",
              "C": "practitioner", "M": "manual"}
# Weakest first. A lesson's evidence level is that of its weakest admitted source: what a lesson
# rests on is its floor, not its best citation.
LEVEL_ORDER = ["manual", "practitioner", "institutional", "preprint", "peer-reviewed"]
SCHOLARLY_VENUES = {"journal", "conference", "book series"}


@dataclass
class Verdict:
    tier: str | None
    reason: str = ""

    @property
    def level(self) -> str | None:
        return TIER_LEVEL.get(self.tier) if self.tier else None


@dataclass
class Admission:
    """What survived, what did not, and whether it is enough to teach from."""

    accepted: list[Work] = field(default_factory=list)
    rejected: list[tuple[Work, str]] = field(default_factory=list)
    status: str = "ok"          # ok | thin | unsourced
    evidence: str | None = None
    notes: list[str] = field(default_factory=list)


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def _channel_match(url: str, allowlist: list[str]) -> str | None:
    """Tier C is per channel, never per platform.

    Matching on hostname alone would turn one allowlisted channel into the whole of YouTube, so an
    entry containing a path is matched as a host+path prefix. An entry without a path still matches
    a bare host, which is what a dedicated documentary site looks like.
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    full = host + parsed.path.rstrip("/").lower()
    for entry in allowlist:
        e = entry.lower().strip("/")
        if "/" in e:
            if full == e or full.startswith(e + "/"):
                return entry
        elif host == e or host.endswith("." + e):
            return entry
    return None


def classify(w: Work, cfg: dict) -> Verdict:
    """One source, one tier — or a rejection with the reason attached."""
    s = cfg["sources"]

    if w.retracted:
        return Verdict(None, "retracted")
    if w.paratext:
        return Verdict(None, "paratext, not the work itself")
    if w.work_type and w.work_type not in s["allowed_types"]:
        return Verdict(None, f"work type {w.work_type!r} is not readable scholarship")

    host = _host(w.url)
    channel = _channel_match(w.url, s["channel_allowlist"])
    if channel:
        return Verdict("C", f"allowlisted channel {channel}")

    # Paywalled sources are rejected outright: a gate that admits what you cannot download is
    # worse than no gate, because it looks like it worked.
    if s["open_access_only"] and not w.is_oa and not w.pdf_url:
        return Verdict(None, "no open-access copy")

    if w.work_type == "preprint" or w.api == "arxiv":
        return Verdict("A-", f"preprint on {w.venue or 'a repository'}")
    if w.venue_type in SCHOLARLY_VENUES and w.work_type in READABLE_TYPES:
        return Verdict("A", f"peer-reviewed in {w.venue}")
    if any(host.endswith(suffix) for suffix in s["institutional_suffixes"]):
        return Verdict("B", f"institutional host {host}")
    return Verdict(None, "no recognised venue")


def _rank(w: Work, depth: int, cfg: dict) -> tuple:
    """Order within a tier. Surveys first while the lesson is broad, primary work once it is not."""
    wants_review = depth <= cfg["sources"]["require_review_at_depth_upto"]
    is_review = w.work_type == "review"
    return (0 if is_review == wants_review else 1, -w.cited_by, -(w.year or 0))


def admit(works: list[Work], depth: int, cfg: dict, min_evidence: str | None = None) -> Admission:
    """Apply the gate, then the composition rules, then decide whether it is enough.

    Composition is the real quality lever, not any single-source threshold. Deliberately absent: a
    citation-count minimum, which penalises recent work and means different things depending on
    which adapter found the paper — OpenAlex and Crossref disagree by a factor of two on the same
    DOI — and a recency cutoff, which is a tiebreak here and nothing more.
    """
    s = cfg["sources"]
    out = Admission()
    tiers: dict[str, str] = {}

    for w in works:
        v = classify(w, cfg)
        if v.tier is None:
            out.rejected.append((w, v.reason))
        else:
            tiers[w.key] = v.tier

    passed = [w for w in works if w.key in tiers]
    order = {"A": 0, "A-": 1, "B": 2, "C": 3, "M": 4}
    passed.sort(key=lambda w: (order[tiers[w.key]], _rank(w, depth, cfg)))

    budget = cfg["depth"][str(depth)]["sources"]
    max_preprints = int(budget * s["max_preprint_ratio"])
    kept, preprints = [], 0
    for w in passed:
        if len(kept) >= budget:
            out.rejected.append((w, "over the source budget"))
            continue
        if tiers[w.key] == "A-" and preprints >= max_preprints:
            out.rejected.append((w, "preprint quota spent"))
            continue
        preprints += tiers[w.key] == "A-"
        kept.append(w)

    out.accepted = kept
    n_a = sum(1 for w in kept if tiers[w.key] == "A")
    if n_a < s["min_tier_a"]:
        out.notes.append(f"only {n_a} peer-reviewed source(s), wanted {s['min_tier_a']}")
    if depth <= s["require_review_at_depth_upto"] and not any(w.work_type == "review" for w in kept):
        out.notes.append("no review or survey among the sources at this depth")

    if kept:
        out.evidence = min((TIER_LEVEL[tiers[w.key]] for w in kept), key=LEVEL_ORDER.index)

    if len(kept) < s["min_sources"]:
        out.status = "unsourced"
    elif len(kept) < budget * s["thin_ratio"]:
        out.status = "thin"
        out.notes.append(f"{len(kept)} of {budget} sources")

    # The bar is per track, and the system never silently downgrades — it says so instead.
    want = min_evidence or s["default_min_evidence"]
    if out.evidence and LEVEL_ORDER.index(out.evidence) < LEVEL_ORDER.index(want):
        out.notes.append(f"evidence is {out.evidence}, below the track's {want}")
        if out.status == "ok":
            out.status = "thin"
    return out


def tier_of(w: Work, cfg: dict) -> str | None:
    return classify(w, cfg).tier


def _selfcheck() -> None:
    from . import config

    cfg = config.load(path=__import__("pathlib").Path("/nonexistent.toml"))
    cfg["sources"]["channel_allowlist"] = ["youtube.com/@veritasium"]

    def w(key, **kw):
        kw.setdefault("url", f"https://example.org/{key}")
        return Work(title=key, doi=f"10.1/{key}", **kw)

    journal = dict(venue="JCP", venue_type="journal", work_type="article", is_oa=True)
    review = dict(venue="Acta Numerica", venue_type="journal", work_type="review", is_oa=True)
    pre = dict(venue="arXiv", work_type="preprint", api="arxiv", is_oa=True)

    # --- hard rejects
    assert classify(w("r", retracted=True, **journal), cfg).tier is None
    assert classify(w("p", paratext=True, **journal), cfg).tier is None
    assert classify(w("d", venue_type="journal", work_type="dataset", is_oa=True), cfg).tier is None
    assert classify(w("w", venue="JCP", venue_type="journal", work_type="article"), cfg).tier is None, \
        "paywalled is rejected outright"
    # A paywalled record that still exposes a pdf is admissible; the point is reachability.
    assert classify(w("x", pdf_url="https://e.org/a.pdf", **{**journal, "is_oa": False}), cfg).tier == "A"

    # --- tiers
    assert classify(w("a", **journal), cfg).tier == "A"
    assert classify(w("b", **pre), cfg).tier == "A-"
    assert classify(Work("n", "https://ocw.mit.edu/x", work_type="report", is_oa=True), cfg).tier == "B"
    assert classify(Work("v", "https://youtube.com/@veritasium/video/1"), cfg).tier == "C", \
        "an allowlisted channel is admitted without needing open-access metadata"
    assert classify(Work("z", "https://youtube.com/@someone-else/1"), cfg).tier is None, \
        "one allowlisted channel must not admit the whole platform"
    assert classify(Work("v2", "https://youtube.com/@veritasium"), cfg).tier == "C"
    cfg["sources"]["channel_allowlist"].append("archive.org")
    assert classify(Work("s", "https://archive.org/details/x"), cfg).tier == "C", \
        "an entry without a path still matches a whole site"
    cfg["sources"]["channel_allowlist"].pop()
    assert classify(Work("q", "https://blog.example.com/p", work_type="article", is_oa=True),
                    cfg).tier is None

    # --- the preprint ratio is enforced against the budget, not the result
    depth, budget = 3, cfg["depth"]["3"]["sources"]
    many_pre = [w(f"pre{i}", **pre) for i in range(budget)]
    got = admit(many_pre + [w("j", **review)], depth, cfg)
    n_pre = sum(1 for x in got.accepted if x.api == "arxiv")
    assert n_pre <= int(budget * cfg["sources"]["max_preprint_ratio"]), f"{n_pre} preprints admitted"
    assert any(x.work_type == "review" for x in got.accepted), "the review survives the cull"

    # --- evidence is the floor, not the ceiling
    assert admit([w(f"a{i}", **journal) for i in range(budget)], depth, cfg).evidence == "peer-reviewed"
    mixed = admit([w("a", **journal), w("b", **pre)] + [w(f"c{i}", **journal) for i in range(4)],
                  depth, cfg)
    assert mixed.evidence == "preprint", "one preprint sets the floor for the whole lesson"

    # --- composition notes fire without blocking
    only_pre = admit([w(f"p{i}", **pre) for i in range(3)], depth, cfg)
    assert any("peer-reviewed source" in n for n in only_pre.notes)
    assert any("review or survey" in n for n in admit([w("a", **journal)] * 1, depth, cfg).notes)

    # --- status thresholds
    assert admit([], depth, cfg).status == "unsourced"
    assert admit([w("a", **journal)], depth, cfg).status == "unsourced", "one source is not enough"
    thin = admit([w(f"a{i}", **journal) for i in range(3)], depth, cfg)
    assert thin.status == "thin" and any("of 10 sources" in n for n in thin.notes)
    full = admit([w(f"a{i}", **journal) for i in range(budget)], depth, cfg)
    assert full.status == "ok" and len(full.accepted) == budget
    over = admit([w(f"a{i}", **journal) for i in range(budget + 4)], depth, cfg)
    assert len(over.accepted) == budget and any(r == "over the source budget"
                                                for _, r in over.rejected)

    # --- the track's bar is reported, never silently met
    strict = admit([w("a", **journal), w("b", **pre)] + [w(f"c{i}", **journal) for i in range(4)],
                   depth, cfg, min_evidence="peer-reviewed")
    assert strict.status == "thin" and any("below the track" in n for n in strict.notes)
    assert admit([w("a", **journal), w("b", **pre)] + [w(f"c{i}", **journal) for i in range(4)],
                 depth, cfg, min_evidence="institutional").status == "ok"

    # --- surveys lead while the lesson is broad, primary work once it is not
    pair = [w("primary", **journal), w("survey", **review)]
    assert admit(pair, 2, cfg).accepted[0].work_type == "review"
    assert admit(pair, 5, cfg).accepted[0].work_type == "article"

    print("gate selfcheck ok")


if __name__ == "__main__":
    _selfcheck()
