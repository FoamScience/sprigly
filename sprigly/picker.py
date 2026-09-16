"""Choosing what to teach next.

Four layers, each from a different literature; docs/picker-literature.md carries the citations.

  1. eligibility  - a readiness gate, after Knowledge Space Theory's outer fringe
  2. score        - weighted signals, normalised within the candidate pool
  3. offer        - greedy DPP selection, calibrated to a target domain mix, with a review lane
  4. fitting      - conditional logit over the recorded choice sets (not here; see .18.4)

Nothing in this module touches the database, and the score stays linear in its signals. Layer 4
depends on both properties.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

from .snapshot import Candidate, Snapshot

SIGNALS = ("prereq", "review", "learning_progress", "track_debt", "effort", "preference")


@dataclass
class Offer:
    candidate: Candidate
    kind: str  # "new" or "review"
    score: float
    signals: dict[str, float] = field(default_factory=dict)


# --- layer 1: eligibility -----------------------------------------------------------------------

def readiness(c: Candidate, s: Snapshot) -> float:
    if not c.prereqs:
        return 1.0
    return len(c.prereqs & s.mastered_tags) / len(c.prereqs)


def eligible(c: Candidate, s: Snapshot, cfg: dict) -> bool:
    """The fringe: what you could actually learn next. A gate, not a weight.

    Partial readiness stays soft — a half-met prerequisite is often where the good lesson is — but
    a lesson whose groundwork you demonstrably lapsed on is excluded outright.

    Only prerequisites the learner has actually MET before count toward that. A prerequisite never
    seen is no evidence of unreadiness, merely of unexplored ground, and counting it inverts the
    whole point: on a cold start nothing is mastered, so every candidate with two prerequisites
    disappears — and the candidates carrying two prerequisites are the technical ones. The gate
    would quietly delete physics, numerics and mathematics from the menu and leave the soft
    domains behind.
    """
    if s.focus_exclusive and s.focus_track_ids and c.track_id not in s.focus_track_ids:
        return False
    floor = cfg["offer"]["prereq_floor_min_prereqs"]
    met_before = c.prereqs & s.seen_tags
    if len(met_before) >= floor and not (met_before & s.mastered_tags):
        return False
    # Evidence level is only knowable once sources exist, so an unharvested candidate passes here
    # and is gated again at generation time.
    want = s.min_evidence.get(c.track_id) if c.track_id else None
    if want and c.evidence_level:
        from .gate import LEVEL_ORDER

        if c.evidence_level in LEVEL_ORDER and want in LEVEL_ORDER \
                and LEVEL_ORDER.index(c.evidence_level) < LEVEL_ORDER.index(want):
            return False
    return True


# --- layer 2: signals ---------------------------------------------------------------------------

def raw_signals(c: Candidate, s: Snapshot, cfg: dict, rng: random.Random) -> dict[str, float]:
    """Each signal in [0, 1], before pool normalisation."""
    sc = cfg["scoring"]

    review = len(c.tags & s.due_tags) / len(c.tags) if c.tags and s.due_tags else 0.0

    debt = 0.0
    if c.track_id is not None:
        last = s.track_last_lesson.get(c.track_id)
        debt = 1.0 if last is None else min(_days_between(last, s.now) / sc["track_debt_saturation_days"], 1.0)

    budget = s.budget_minutes or 1
    effort = math.exp(-(((c.est_minutes - budget) / budget) ** 2))

    # Beta(1,1) posterior over pick rate. Sampling from it is Thompson sampling: exploration with
    # no epsilon to tune, and a domain skipped twice cannot sink permanently.
    offers, picks = s.offers.get(c.domain, 0), s.picks.get(c.domain, 0)
    alpha, beta = picks + 1, max(offers - picks, 0) + 1
    pref = rng.betavariate(alpha, beta) if sc["thompson"] else alpha / (alpha + beta)

    return {
        "prereq": readiness(c, s),
        "review": review,
        # Constant across the pool until the feedback loop fills it, and a constant signal
        # contributes nothing once normalised.
        "learning_progress": s.progress.get(c.domain, 0.0),
        "track_debt": debt,
        "effort": effort,
        "preference": pref,
    }


def _days_between(a: str, b: str) -> float:
    from datetime import datetime, timezone

    from .store import TS

    fmt = lambda t: datetime.strptime(t, TS).replace(tzinfo=timezone.utc)
    return (fmt(b) - fmt(a)).total_seconds() / 86400.0


def _rescale(values: list[float], epsilon: float) -> list[float]:
    """Min-max within the pool, but only when the pool actually separates the candidates.

    Absolute normalisation compresses realistic candidates into a few thousandths of each other and
    lets noise decide the ranking. Rescaling fixes that, and introduces the opposite hazard: on a
    pool that is genuinely equivalent it magnifies rounding into a confident order. Below epsilon
    the signal stays flat.
    """
    lo, hi = min(values), max(values)
    if hi - lo < epsilon:
        return [0.5] * len(values)
    return [(v - lo) / (hi - lo) for v in values]


def score_pool(pool: list[Candidate], s: Snapshot, cfg: dict,
               rng: random.Random | None = None) -> list[Offer]:
    """Score every candidate relative to the others on offer."""
    rng = rng or random.Random()
    if not pool:
        return []
    raw = [raw_signals(c, s, cfg, rng) for c in pool]
    scaled = {k: _rescale([r[k] for r in raw], cfg["scoring"]["pool_epsilon"]) for k in SIGNALS}
    w = cfg["scoring"]
    total_w = sum(w[k] for k in SIGNALS) or 1.0
    out = []
    for i, c in enumerate(pool):
        sig = {k: scaled[k][i] for k in SIGNALS}
        out.append(Offer(c, "new", sum(w[k] * sig[k] for k in SIGNALS) / total_w, sig))
    return out


# --- layer 3: set selection ---------------------------------------------------------------------

def _similarity(a: Candidate, b: Candidate) -> float:
    """Cosine over tag indicator vectors — a Gram matrix, so the DPP kernel stays PSD.

    Jaccard would read as the natural choice and is not positive semi-definite in general, which
    would make the log-determinant below undefined. An untagged candidate is treated as holding one
    private tag: orthogonal to everything, similar only to itself.
    """
    if a.id == b.id:
        return 1.0
    if not a.tags or not b.tags:
        return 0.0
    return len(a.tags & b.tags) / math.sqrt(len(a.tags) * len(b.tags))


def _logdet(m: list[list[float]]) -> float:
    """Cholesky log-determinant, with a ridge if rounding pushes the matrix off PSD."""
    n = len(m)
    for ridge in (0.0, 1e-9, 1e-6):
        chol = [[0.0] * n for _ in range(n)]
        ok = True
        for i in range(n):
            for j in range(i + 1):
                acc = m[i][j] + (ridge if i == j else 0.0)
                acc -= sum(chol[i][k] * chol[j][k] for k in range(j))
                if i == j:
                    if acc <= 0:
                        ok = False
                        break
                    chol[i][i] = math.sqrt(acc)
                else:
                    chol[i][j] = acc / chol[j][j]
            if not ok:
                break
        if ok:
            return 2.0 * sum(math.log(chol[i][i]) for i in range(n))
    return -math.inf


def _distribution(items: list[Candidate]) -> dict[str, float]:
    if not items:
        return {}
    n = len(items)
    out: dict[str, float] = {}
    for c in items:
        out[c.domain] = out.get(c.domain, 0.0) + 1.0 / n
    return out


def _kl(target: dict[str, float], actual: dict[str, float], alpha: float = 0.01) -> float:
    """KL(target || actual), smoothed toward the target so an unrepresented domain is finite."""
    total = 0.0
    for dom, p in target.items():
        q = (1 - alpha) * actual.get(dom, 0.0) + alpha * p
        if p > 0 and q > 0:
            total += p * math.log(p / q)
    return total


def target_mix(pool: list[Candidate], s: Snapshot, cfg: dict) -> dict[str, float]:
    """What the offered set's domain distribution should look like.

    Spread across fields is a target distribution, not a penalty on any one candidate — which is
    also why a focus needs no separate bonus: it simply retargets this.
    """
    configured = cfg["offer"]["domain_mix"]
    if configured:
        total = sum(configured.values()) or 1.0
        return {k: v / total for k, v in configured.items()}
    scope = [c for c in pool if c.track_id in s.focus_track_ids] if s.focus_track_ids else []
    domains = sorted({c.domain for c in (scope or pool)})
    return {d: 1.0 / len(domains) for d in domains} if domains else {}


def select(scored: list[Offer], s: Snapshot, cfg: dict, k: int) -> list[Offer]:
    """Greedy DPP MAP with a calibration term on the domain mix."""
    if k <= 0 or not scored:
        return []
    o = cfg["offer"]
    lam = o["focus_similarity_scale"] if s.focus_track_ids else 1.0
    target = target_mix([x.candidate for x in scored], s, cfg)

    # q floors the quality so a zero-scoring candidate cannot collapse the determinant.
    q = {x.candidate.id: 0.1 + 0.9 * x.score for x in scored}

    def kernel(items: list[Candidate]) -> list[list[float]]:
        return [[q[a.id] * q[b.id] * (_similarity(a, b) * lam + (1 - lam if a.id == b.id else 0.0))
                 for b in items] for a in items]

    chosen: list[Offer] = []
    remaining = list(scored)
    while remaining and len(chosen) < k:
        picked_now = [x.candidate for x in chosen]
        base_det = _logdet(kernel(picked_now)) if picked_now else 0.0
        base_kl = _kl(target, _distribution(picked_now)) if picked_now else _kl(target, {})
        best, best_gain = None, -math.inf
        for cand in remaining:
            items = picked_now + [cand.candidate]
            gain = (_logdet(kernel(items)) - base_det) \
                + o["calibration_weight"] * (base_kl - _kl(target, _distribution(items)))
            if gain > best_gain:
                best, best_gain = cand, gain
        chosen.append(best)
        remaining.remove(best)
    return chosen


def offer(pool: list[Candidate], due: list[Candidate], s: Snapshot, cfg: dict,
          rng: random.Random | None = None) -> list[Offer]:
    """The menu: a reserved review lane, then calibrated new candidates.

    Reviews get reserved slots rather than competing as one signal among six, where they can be
    outvoted indefinitely. MEMORIZE shows the optimal review schedule is an intensity proportional
    to recall probability — a rate, not a binary flag — so a proportion of the menu is the right
    shape, and fsrs degrades when reviews run systematically late.
    """
    k = cfg["offer"]["k"]
    rng = rng or random.Random()

    n_review = min(len(due), round(k * cfg["offer"]["review_lane"])) if due else 0
    reviews = sorted(score_pool(due, s, cfg, rng), key=lambda x: -x.score)[:n_review]
    for r in reviews:
        r.kind = "review"

    fresh = [c for c in pool if eligible(c, s, cfg)]
    return reviews + select(score_pool(fresh, s, cfg, rng), s, cfg, k - len(reviews))


def offer_all(pool: list[Candidate], due: list[Candidate], s: Snapshot, cfg: dict,
              rng: random.Random | None = None) -> list[Offer]:
    """Every eligible candidate, score-ordered, with due reviews first.

    Skips the DPP and the calibration entirely — this is the escape hatch for browsing, not the
    curated menu. The choice set logged is still exactly what was shown, so the fit stays honest.
    """
    rng = rng or random.Random()
    reviews = sorted(score_pool(due, s, cfg, rng), key=lambda x: -x.score)
    for r in reviews:
        r.kind = "review"
    fresh = sorted(score_pool([c for c in pool if eligible(c, s, cfg)], s, cfg, rng),
                   key=lambda x: -x.score)
    return reviews + fresh


def _selfcheck() -> None:
    from . import config

    from pathlib import Path

    cfg = config.load(path=Path("/nonexistent.toml"))
    rng = random.Random(7)
    now = "2026-09-16T12:00:00Z"

    def cand(i, domain="numerics", tags=(), prereqs=(), minutes=20, track=None, ev=None):
        return Candidate(i, f"topic-{i}", domain, frozenset(tags), frozenset(prereqs), minutes,
                         track, ev)

    # --- layer 1: the fringe gate
    seen = {"linear-algebra", "calculus", "group-theory", "quantum-field-theory"}
    s = Snapshot(now=now, mastered_tags={"linear-algebra"}, seen_tags=seen)
    ready = cand(1, prereqs=["linear-algebra", "calculus"])
    lapsed = cand(2, prereqs=["quantum-field-theory", "group-theory"])
    assert eligible(ready, s, cfg), "partial readiness stays soft"
    assert not eligible(lapsed, s, cfg), "groundwork met before and now lapsed: excluded"
    assert eligible(cand(3, prereqs=["group-theory"]), s, cfg), "a single lapsed prerequisite is soft"

    # The regression that shipped: an empty knowledge state must not delete every technical topic.
    cold = Snapshot(now=now)
    physics = cand(4, domain="physics", prereqs=["statistical-mechanics", "scaling-laws"])
    assert eligible(physics, cold, cfg), "unseen prerequisites are not evidence of unreadiness"
    partial = Snapshot(now=now, seen_tags={"linear-algebra"}, mastered_tags=set())
    assert eligible(cand(5, prereqs=["linear-algebra", "never-encountered"]), partial, cfg), \
        "one lapsed prerequisite plus one unseen is below the floor"

    excl = Snapshot(now=now, focus_track_ids={9}, focus_exclusive=True)
    assert not eligible(cand(4, track=None), excl, cfg)
    assert eligible(cand(5, track=9), excl, cfg)

    ev = Snapshot(now=now, min_evidence={9: "peer-reviewed"})
    assert not eligible(cand(6, track=9, ev="practitioner"), ev, cfg)
    assert eligible(cand(7, track=9, ev="peer-reviewed"), ev, cfg)
    assert eligible(cand(8, track=9, ev=None), ev, cfg), "unharvested candidates are gated later"

    # --- layer 2: pool normalisation must actually separate near-identical candidates
    s = Snapshot(now=now, budget_minutes=20)
    near = [cand(10, minutes=20), cand(11, minutes=26)]
    absolute = [raw_signals(c, s, cfg, random.Random(1))["effort"] for c in near]
    assert abs(absolute[0] - absolute[1]) < 0.1, "raw signals barely separate"
    scaled = _rescale(absolute, cfg["scoring"]["pool_epsilon"])
    assert scaled == [1.0, 0.0], "within the pool the same difference is decisive"

    assert _rescale([0.5, 0.5, 0.5], 0.02) == [0.5] * 3, "an equivalent pool stays flat"
    assert _rescale([0.50, 0.505], 0.02) == [0.5] * 2, "noise below epsilon must not be amplified"

    # --- Thompson sampling: the mean would rank two skipped domains identically forever
    s = Snapshot(now=now, offers={"a": 6, "b": 6}, picks={"a": 0, "b": 0})
    draws = {raw_signals(cand(12, domain="a"), s, cfg, random.Random(i))["preference"]
             for i in range(20)}
    assert len(draws) > 1, "sampling explores"
    cfg["scoring"]["thompson"] = False
    fixed = {raw_signals(cand(12, domain="a"), s, cfg, random.Random(i))["preference"]
             for i in range(5)}
    assert len(fixed) == 1, "the mean is deterministic"
    cfg["scoring"]["thompson"] = True

    # --- the DPP kernel must stay positive semi-definite
    a, b = cand(20, tags=["rbf", "stencil"]), cand(21, tags=["rbf", "stencil"])
    assert _similarity(a, b) == 1.0 and _similarity(a, cand(22, tags=["cash-flow"])) == 0.0
    assert _similarity(a, cand(23)) == 0.0, "an untagged candidate is orthogonal"
    assert _logdet([[1.0, 0.0], [0.0, 1.0]]) == 0.0
    assert _logdet([[1.0, 1.0], [1.0, 1.0]]) < -10, "a duplicated row is near-singular"

    # --- layer 3: near-duplicates must not fill the menu
    s = Snapshot(now=now)
    clones = [cand(30 + i, tags=["rbf", "stencil"]) for i in range(4)]
    odd = cand(40, domain="business", tags=["cash-flow"])
    picked = offer(clones + [odd], [], s, cfg, random.Random(3))
    assert odd.id in [x.candidate.id for x in picked[:2]], "repulsion surfaces the outlier early"

    # --- calibration steers the mix toward the target
    cfg["offer"]["domain_mix"] = {"numerics": 0.5, "business": 0.5}
    pool = [cand(50 + i, domain="numerics", tags=[f"n{i}"]) for i in range(8)] + \
           [cand(60 + i, domain="business", tags=[f"b{i}"]) for i in range(8)]
    got = _distribution([x.candidate for x in offer(pool, [], s, cfg, random.Random(5))])
    assert got.get("business", 0) >= 0.4, f"calibration should balance the menu, got {got}"
    cfg["offer"]["domain_mix"] = {}

    # --- the review lane is reserved, not contested
    due = [cand(70 + i, domain="numerics", tags=[f"r{i}"]) for i in range(3)]
    menu = offer(pool, due, s, cfg, random.Random(5))
    kinds = [x.kind for x in menu]
    assert len(menu) == cfg["offer"]["k"]
    assert kinds.count("review") == round(cfg["offer"]["k"] * cfg["offer"]["review_lane"])
    assert kinds.count("new") == cfg["offer"]["k"] - kinds.count("review")

    assert offer(pool, [], s, cfg, random.Random(5))[0].kind == "new", "no reviews, no lane"
    assert len(offer([], due, s, cfg, random.Random(5))) == 2, "reviews alone still offer"

    # --- cold start: no history, no tags, no domains, fewer candidates than slots
    empty = Snapshot(now=now)
    bare = [cand(100 + i, domain="") for i in range(3)]
    menu = offer(bare, [], empty, cfg, random.Random(2))
    assert len(menu) == 3, "fewer candidates than slots offers all of them"
    assert len({x.candidate.id for x in menu}) == 3, "no candidate is offered twice"
    assert all(x.signals["preference"] is not None for x in menu)
    assert offer([], [], empty, cfg, random.Random(2)) == [], "an empty pool offers nothing"
    assert all(x.score >= 0 for x in menu)

    # --- offer_all is a browse, not a menu: everything eligible, nothing deduplicated
    s = Snapshot(now=now)
    every = offer_all(clones + [odd], due[:1], s, cfg, random.Random(3))
    assert len(every) == 6, "all five candidates plus the due review"
    assert every[0].kind == "review" and [x.kind for x in every[1:]] == ["new"] * 5
    assert all(every[i].score >= every[i + 1].score for i in range(1, len(every) - 1)), \
        "new candidates come back score-ordered"
    gated = offer_all([cand(200, prereqs=["a", "b"])], [],
                      Snapshot(now=now, seen_tags={"a", "b"}), cfg)
    assert gated == [], "browsing still respects the eligibility gate"

    # --- focus retargets calibration instead of needing a bonus
    focused = Snapshot(now=now, focus_track_ids={1})
    tracked = [cand(80 + i, domain="numerics", tags=[f"t{i}"], track=1) for i in range(4)]
    others = [cand(90 + i, domain="business", tags=[f"o{i}"]) for i in range(4)]
    menu = offer(tracked + others, [], focused, cfg, random.Random(11))
    assert sum(1 for x in menu if x.candidate.track_id == 1) >= 3, "focus dominates the menu"

    print("picker selfcheck ok")


if __name__ == "__main__":
    _selfcheck()
