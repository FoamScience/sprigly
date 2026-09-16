# What to teach next — a survey for the picker

Sprigly's picker sits in an unusual spot. It is not a recommender (no catalogue, no other users), not
a tutor (no problem bank, no fine-grained skill model), and not a flashcard scheduler (lessons are
half-hour artifacts, not items). It borrows from all three. This is what the relevant literatures
offer, what is worth taking, and what is not.

Constraints that rule things in or out: one user, tens of events rather than millions, no curated
prerequisite graph (we rejected maintaining one), a deliberate goal of spread across unrelated
fields, and an explicit user choice at every step — the system proposes, the human picks.

---

## A. Knowledge structure and readiness

**Knowledge Space Theory / Learning Spaces** — Doignon & Falmagne (1985); deployed as ALEKS.
A knowledge state is the set of items a learner has mastered. The **outer fringe** of a state is the
set of items `q` such that `state ∪ {q}` is still a valid state — formally, "what you are ready to
learn next". ALEKS presents the outer fringe to the student and lets them choose from it.

- [Knowledge Spaces and Learning Spaces (arXiv 1511.06757)](https://arxiv.org/abs/1511.06757)
- [Research behind ALEKS](https://www.aleks.com/about_aleks/knowledge_space_theory)
- [A practical perspective on knowledge space theory: ALEKS and its data](https://www.sciencedirect.com/science/article/abs/pii/S0022249621000134)

**Take:** the fringe is a *filter*, not a score. ALEKS does not rank all items by readiness and hope
the unready ones lose — it excludes them, then lets the human choose from what remains. That is
exactly the "propose, human picks" shape Sprigly already has, and it argues for the prerequisite
floor rather than a pure soft weight.

**Leave:** the machinery. A real learning space needs a curated item set closed under union and
well-graded. We deliberately refused to hand-curate a prerequisite DAG. Our normalised tags are a
poor approximation of a knowledge structure and will stay one.

## B. Curriculum by learning progress

**ZPDES and RiARiT** — Clement, Roy, Oudeyer & Lopes, *Multi-Armed Bandits for Intelligent Tutoring
Systems*. ZPDES ("Zone of Proximal Development and Empirical Success") runs a bandit over activity
groups, but the reward is not success — it is **learning progress**, the *change* in empirical
success rate. Activities that are already mastered stop being rewarding, and so do activities that
are hopeless. The ZPD falls out of the reward definition instead of being declared.

- [Multi-Armed Bandits for Intelligent Tutoring Systems (arXiv 1310.3174)](https://arxiv.org/pdf/1310.3174)
- [ERIC full text (JEDM)](https://files.eric.ed.gov/fulltext/EJ1115278.pdf)
- [Hierarchical Multi-Armed Bandits for concurrent tutoring of concepts and problems (arXiv 2408.07208)](https://arxiv.org/abs/2408.07208)

**Take:** reward the *derivative*, not the level. Sprigly currently scores revealed preference
(what you clicked) and mastery (what you reviewed). Neither notices that a domain has gone stale —
you keep picking it and keep scoring the same on its quizzes. A learning-progress term computed per
domain or per track from quiz history is a genuinely new signal, and it is the one most aligned with
"teaches me one thing at a time, but does it very well".

**Take also:** the exploration/exploitation framing. Sprigly already stores a Beta(1,1) posterior per
domain for revealed preference. **Sampling** from that posterior instead of taking its mean *is*
Thompson sampling — one line, principled exploration, no epsilon parameter to tune. This is the
cheapest good idea in this document.

## C. Memory models and when to review

**FSRS** — already a dependency. Per-card difficulty/stability/retrievability, fitted on large
public review logs.

**Half-Life Regression** — Settles & Meeder (ACL 2016), Duolingo. Models an item's memory half-life
as a log-linear function of features, trained on 13M traces; ~45% error reduction over baselines and
a 12% engagement lift in production.

- [A Trainable Spaced Repetition Model for Language Learning](https://research.duolingo.com/papers/settles.acl16.pdf)
- [duolingo/halflife-regression](https://github.com/duolingo/halflife-regression)

**MEMORIZE** — Tabibian, Upadhyay, De, Zarezade, Schölkopf & Gomez-Rodriguez, PNAS 2019. Frames
review scheduling as stochastic optimal control of an SDE with jumps. The headline result: under
standard memory models, maximising recall subject to a cost on review frequency gives an optimal
reviewing *intensity proportional to the recall probability itself* — a review **rate**, not a due
date.

- [Enhancing human learning via spaced repetition optimization (PNAS)](https://www.pnas.org/doi/abs/10.1073/pnas.1815156116)
- [Networks-Learning/memorize](https://github.com/Networks-Learning/memorize)
- [project page](https://learning.mpi-sws.org/memorize/)

**Take:** the *rate* framing, not a replacement scheduler. It says review load is a continuous
pressure rather than a binary due/not-due, which is precisely what justifies reserving a
*proportion* of the offered slots for review instead of letting a due card win or lose a ranking.
FSRS stays; MEMORIZE informs how much of the menu review should occupy.

**Leave:** HLR's model itself — it needs volume we will never have. Its transferable lesson is
structural: a log-linear model over hand-chosen features, fitted from logs, beats hand-set
constants once data exists. That is the same shape as our weighted sum, which is reassuring.

## D. Diverse and calibrated sets

**MMR** — Carbonell & Goldstein (1998). Greedy, myopic: at each step, relevance minus max similarity
to what is already chosen.

**DPP with fast greedy MAP** — Chen et al., NeurIPS 2018. A determinantal point process scores a
*set* by the determinant of a kernel `L = diag(q) · S · diag(q)`, cleanly separating item quality `q`
from pairwise similarity `S`. MAP is NP-hard, but their greedy acceleration brings it to O(M³) and
beats MMR on the relevance/diversity trade-off in online A/B tests.

- [Fast Greedy MAP Inference for DPP to Improve Recommendation Diversity (arXiv 1709.05135)](https://arxiv.org/abs/1709.05135)
- [NeurIPS 2018 proceedings](https://proceedings.neurips.cc/paper/2018/hash/dbbf603ff0e99629dda5d75b6f75f966-Abstract.html)
- [Recent Advances in Diversified Recommendation (arXiv 1905.06589)](https://arxiv.org/pdf/1905.06589)

**Calibrated Recommendations** — Steck, RecSys 2018 (Netflix). Rather than penalising similarity, fit
the *distribution* of the recommended set to the user's own distribution over categories: if you
watch 70% romance and 30% action, the list should be 70/30. Greedy post-processing, submodular
objective.

- [Calibrated Recommendations (ACM DL)](https://dl.acm.org/doi/pdf/10.1145/3240323.3240372)
- [Calibrated Recommendations: Survey and Future Directions (arXiv 2507.02643)](https://arxiv.org/pdf/2507.02643)
- [reference implementation](https://github.com/karlhigley/calibrator)

**Take — the most valuable idea here:** replace the domain-diversity *penalty* with **calibration to
a target domain mix**. Sprigly's goal was never "punish repetition"; it was "keep me spread across
quantum mechanics, human sciences and business". That is a target distribution, and a penalty term
is a clumsy proxy for it. Calibration also composes correctly with focus: a focused track simply
sets the target distribution to that track, instead of the penalty and the focus bonus fighting.

**Take:** DPP's kernel factorisation. At k = 5 the cost argument is irrelevant, so we can afford the
better formulation. Under focus, shrink the similarity kernel rather than reducing the score — which
is the principled version of the "MMR fights focus" patch.

## E. Coverage and teaching sequences

**Adaptive submodularity** — Golovin & Krause, JAIR 2011. When an objective is adaptive submodular,
a greedy policy is competitive with the optimal adaptive policy, with lazy evaluation for speed.
Covers active learning and stochastic set cover as special cases.

- [Adaptive Submodularity (arXiv 1003.3967)](https://arxiv.org/abs/1003.3967)
- [JAIR](https://www.jair.org/index.php/jair/article/view/10731)

**Take, later:** in focused mode, picking the next syllabus item is not "highest score" — it is
"which lesson most increases expected coverage of this track's concepts, given what the quizzes have
now revealed". That is an adaptive submodular objective with a real guarantee, and it is the right
frame for `sprigly focus` once quiz data exists. Not before.

## F. Learning the weights from choices

Every `sprigly next` produces one chosen candidate and k−1 rejected ones, with full feature vectors
for all of them. That is not a binary classification problem — it is a **top-1-of-k choice**, whose
exact likelihood is the conditional logit / multinomial logit (McFadden), equivalently the
Plackett-Luce top-1 model.

**Take:** fit a conditional logit on the choice sets. It needs no invented ground truth, uses the
skips as real negatives, and is correctly specified for how the data is generated. Crucially it is
linear in the features — so the weighted-sum scorer is not a placeholder to be thrown away, it is
the model that gets fitted. That property is worth preserving in any change to the scorer.

---

## Proposed combination

Four layers, each taken from a different literature, each replacing something hand-waved:

1. **Eligibility filter (KST fringe).** Drop candidates below the prerequisite floor, below
   `track.min_evidence`, or outside the active tracks under `--only`. Readiness is a gate, not
   merely a weight.
2. **Relevance score.** Weighted sum of signals normalised *within the candidate pool*: partial
   prerequisite readiness, **learning progress** (new, from ZPDES), track debt, effort fit, and
   revealed preference **sampled from its Beta posterior** rather than averaged (Thompson sampling).
   Domain diversity leaves this layer entirely.
3. **Set selection.** Greedy DPP over quality × similarity, subject to **calibration** against a
   target domain mix, with a reserved proportion of slots for review pressure sized by the MEMORIZE
   rate argument rather than a binary due flag.
4. **Fitting (`.18.4`).** Conditional logit on the recorded choice sets.

What this changes versus the current design: diversity stops being a signal and becomes a set-level
constraint; a learning-progress signal appears; exploration becomes free via posterior sampling;
the prerequisite floor gains a justification; and the review lane gets a principled size.

What it does not change: scoring stays pure functions over a `Snapshot`, and stays linear in its
features so that layer 4 remains valid.
