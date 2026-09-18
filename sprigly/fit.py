"""Layer 4: fitting the scoring weights to the choices actually made.

Every `sprigly next` stamps one `choice_set` id on the `offered` event of each candidate on the
menu and on the `picked` event of each one taken, and the offered payload carries the same
pool-normalised signal vector the scorer used. So the choice sets replay exactly: no feature is
recomputed here, and nothing is inferred that was not recorded.

A single pick out of k is a top-1-of-k choice whose exact likelihood is the conditional logit
(McFadden), equivalently Plackett-Luce top-1. The picker allows several picks from one offering,
so some sets carry m > 1 positives. Those are fitted with the exact matched-set conditional
likelihood (statsmodels `ConditionalLogit`), which conditions on "these m of the k were chosen"
without an order among them. The alternative — expanding a set into m independent draws — reuses
the same k - m negatives m times and counts them as independent evidence, which shrinks the
standard errors it has no right to shrink. Recorded here because the bead asked for the choice to
be recorded.

The score is linear in its signals, so these coefficients are the same object as the hand-set
`[scoring]` weights: the scorer is the model, not a placeholder for it.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from .picker import SIGNALS

# Below this the fit is noise dressed as a number, and the hand-set weights stand.
MIN_CHOICE_SETS = 30


@dataclass
class ChoiceSet:
    id: str
    rows: list[tuple[int, dict[str, float], bool]]  # (lesson_id, signals, picked)


@dataclass
class Fit:
    coef: dict[str, float]
    stderr: dict[str, float]
    pvalue: dict[str, float]
    n_sets: int
    n_rows: int
    n_picks: int
    loglik: float

    def weights(self) -> dict[str, float] | None:
        """Coefficients rescaled to sum to 1, the form the `[scoring]` section takes.

        A negative coefficient means the scorer's sum-normalisation cannot express the fit (the
        weights divide by their own total), so there is nothing honest to suggest and this returns
        None rather than a rescaling that changes the ranking.
        """
        if any(v <= 0 for v in self.coef.values()):
            return None
        total = sum(self.coef.values())
        return {k: v / total for k, v in self.coef.items()}


def choice_sets(conn: sqlite3.Connection) -> list[ChoiceSet]:
    """Rebuild the recorded offerings from the event log.

    A set is usable only if it separates something: at least two candidates, at least one pick, and
    not every candidate picked. A set where everything was taken carries no comparison and drops
    out of the conditional likelihood anyway.
    """
    offered: dict[str, list[tuple[int, dict[str, float]]]] = {}
    taken: dict[str, set[int]] = {}
    for kind, lesson_id, payload in conn.execute(
            "SELECT kind, lesson_id, payload FROM event WHERE kind IN ('offered','picked')"
            " ORDER BY id"):
        if lesson_id is None or not payload:
            continue  # a lesson deleted out from under its events takes its row with it
        try:
            p = json.loads(payload)
        except json.JSONDecodeError:
            continue
        cs = p.get("choice_set")
        if not cs:
            continue  # offerings recorded before the id existed are unrecoverable
        if kind == "picked":
            taken.setdefault(cs, set()).add(lesson_id)
            continue
        sig = p.get("signals") or {}
        if not all(k in sig for k in SIGNALS):
            continue
        offered.setdefault(cs, []).append((lesson_id, {k: float(sig[k]) for k in SIGNALS}))

    out = []
    for cs, rows in offered.items():
        picks = taken.get(cs, set())
        if len(rows) < 2 or not picks & {lid for lid, _ in rows}:
            continue
        marked = [(lid, sig, lid in picks) for lid, sig in rows]
        if all(picked for _, _, picked in marked):
            continue
        out.append(ChoiceSet(cs, marked))
    return out


def fit(sets: list[ChoiceSet], min_sets: int = MIN_CHOICE_SETS) -> Fit:
    """Maximum likelihood over the recorded choice sets. Raises if there is too little history."""
    if len(sets) < min_sets:
        raise ValueError(f"{len(sets)} usable choice sets, need {min_sets};"
                         " the hand-set weights stand until there is more history")
    import numpy as np
    from statsmodels.discrete.conditional_models import ConditionalLogit

    x, y, groups = [], [], []
    for i, cs in enumerate(sets):
        for _, sig, picked in cs.rows:
            x.append([sig[k] for k in SIGNALS])
            y.append(1.0 if picked else 0.0)
            groups.append(i)
    res = ConditionalLogit(np.array(y), np.array(x), groups=np.array(groups)).fit(disp=0)
    return Fit(
        coef=dict(zip(SIGNALS, (float(v) for v in res.params))),
        stderr=dict(zip(SIGNALS, (float(v) for v in res.bse))),
        pvalue=dict(zip(SIGNALS, (float(v) for v in res.pvalues))),
        n_sets=len(sets),
        n_rows=len(y),
        n_picks=int(sum(y)),
        loglik=float(res.llf),
    )


def _selfcheck() -> None:
    import math
    import random

    from . import store

    # --- reconstruction from a real event log
    conn = store.connect(":memory:")
    for i in range(3):
        conn.execute("INSERT INTO lesson (topic, state) VALUES (?, 'proposed')", (f"t{i}",))
    flat = {k: 0.5 for k in SIGNALS}
    for lid in (1, 2, 3):
        store.log_event(conn, "offered", lid, json.dumps({"choice_set": "cs1", "signals": flat}))
    store.log_event(conn, "picked", 1, json.dumps({"choice_set": "cs1"}))
    store.log_event(conn, "picked", 2, json.dumps({"choice_set": "cs1"}))
    # a legacy offering with no choice_set, and a well-formed one-candidate set
    store.log_event(conn, "offered", 3, json.dumps({"signals": flat}))
    store.log_event(conn, "offered", 3, json.dumps({"choice_set": "cs2", "signals": flat}))
    store.log_event(conn, "picked", 3, json.dumps({"choice_set": "cs2"}))

    got = choice_sets(conn)
    assert [c.id for c in got] == ["cs1"], "unrecoverable and one-candidate offerings drop out"
    assert len(got[0].rows) == 3, "a multi-pick offering stays ONE set, not one per pick"
    assert sum(1 for *_, picked in got[0].rows if picked) == 2
    conn.close()

    # --- recovery: simulate choices from known weights and check the fit finds them back
    rng = random.Random(7)
    truth = {"prereq": 2.0, "review": 0.0, "learning_progress": 1.0,
             "track_debt": 0.0, "effort": 0.0, "preference": -1.5}
    sets = []
    for n in range(400):
        rows = [(i, {k: rng.random() for k in SIGNALS}) for i in range(5)]
        util = [math.exp(sum(truth[k] * sig[k] for k in SIGNALS)) for _, sig in rows]
        total = sum(util)
        r, acc, chosen = rng.random() * total, 0.0, 0
        for i, u in enumerate(util):
            acc += u
            if r <= acc:
                chosen = i
                break
        sets.append(ChoiceSet(f"s{n}", [(i, sig, i == chosen) for i, sig in rows]))

    try:
        fit(sets[:5])
        raise AssertionError("too little history must refuse to fit")
    except ValueError:
        pass

    f = fit(sets)
    assert f.n_sets == 400 and f.n_rows == 2000 and f.n_picks == 400
    for k, want in truth.items():
        assert abs(f.coef[k] - want) < 4 * f.stderr[k] + 0.3, \
            f"{k}: fitted {f.coef[k]:.2f}, generated with {want}"
    assert f.pvalue["prereq"] < 0.01, "a signal that drove every choice must not read as noise"
    assert f.pvalue["effort"] > 0.05, "a signal that drove nothing must not read as significant"
    assert f.weights() is None, "a negative coefficient has no honest [scoring] weight"

    print("fit selfcheck ok")


if __name__ == "__main__":
    _selfcheck()
