"""Cards, grading and scheduling.

We author no questions. NotebookLM generates them from the same sources as the lesson, and this
module turns that file into cards, grades them, and hands the scheduling to fsrs — the algorithm
everyone else's spaced repetition already uses. Getting intervals subtly wrong wastes months of
review time before anyone notices.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from . import store
from .store import TS, utcnow

log = logging.getLogger(__name__)

# A quiz answered correctly first time is "Good", not "Easy" — Easy is for something that needed no
# thought at all, and only you can say that.
SUGGESTED = {"wrong": "Again", "retry": "Hard", "right": "Good"}


def _text(value) -> str:
    return " ".join(str(value).split()) if value is not None else ""


def extract_cards(data) -> list[dict]:
    """Pull question/answer pairs out of whatever shape the export happens to have.

    The exported schema is not contractual and has changed before, so this reads the shapes that
    occur rather than one blessed layout, and quietly ignores anything it cannot make a pair from.
    """
    if isinstance(data, dict):
        for key in ("questions", "cards", "flashcards", "items", "data"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            data = [data]
    if not isinstance(data, list):
        return []

    out = []
    for item in data:
        if not isinstance(item, dict):
            continue
        front = _text(next((item[k] for k in ("question", "front", "prompt", "term")
                            if item.get(k)), None))
        back = _text(next((item[k] for k in ("answer", "back", "response", "definition",
                                             "explanation", "bestAnswer") if item.get(k)), None))
        options = [_text(o) for o in item.get("options") or item.get("choices") or []
                   if _text(o)]
        if not back and item.get("correct_answer") is not None:
            back = _text(item["correct_answer"])
        if not back:
            back = _answer_from_options(item)
        grading = item.get("grading")
        if not back and isinstance(grading, dict):
            back = _text(grading.get("modelAnswer") or grading.get("answer"))
        if not back and item.get("acceptableAnswers"):
            back = "; ".join(_text(a) for a in item["acceptableAnswers"] if _text(a))
        if not back:
            back = _text(item.get("rationale"))
        if front and back:
            out.append({"front": front, "back": back, "options": options})
    return out


def _answer_from_options(item: dict) -> str:
    """Build the answer from a multiple-choice option list.

    The real export carries `answerOptions` as objects with `isCorrect` and a `rationale`, which no
    amount of guessing at key names would have found — it took a generated quiz to see it. Multiple
    options can be correct (`multiple_select`), so every correct one is kept.
    """
    correct = [o for o in item.get("answerOptions") or []
               if isinstance(o, dict) and o.get("isCorrect")]
    if not correct:
        return ""
    answer = " / ".join(_text(o.get("text")) for o in correct if _text(o.get("text")))
    reasons = [_text(o.get("rationale")) for o in correct if _text(o.get("rationale"))]
    return f"{answer}\n\n{reasons[0]}" if answer and reasons else answer


def front_hash(front: str) -> str:
    """Identity for dedup: the normalised question text, so the same card is not learned twice."""
    return hashlib.sha256(" ".join(front.lower().split()).encode()).hexdigest()[:32]


def import_cards(conn, cfg: dict, lesson_id: int) -> int:
    """Read a lesson's quiz and flashcard exports into the card table."""
    lesson_dir = cfg["paths"]["lessons"] / str(lesson_id)
    track = conn.execute("SELECT track_id FROM lesson WHERE id=?", (lesson_id,)).fetchone()
    added = 0
    for name in ("quiz.json", "flashcards.json"):
        path = lesson_dir / name
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError as err:
            log.warning("could not read %s: %s", path, err)
            continue
        for card in extract_cards(data):
            cur = conn.execute(
                "INSERT OR IGNORE INTO card (lesson_id, track_id, front, back, front_hash, due)"
                " VALUES (?,?,?,?,?,?)",
                (lesson_id, track["track_id"] if track else None, card["front"], card["back"],
                 front_hash(card["front"]), utcnow()))
            added += cur.rowcount
    if added:
        store.log_event(conn, "cards", lesson_id, json.dumps({"added": added}))
    return added


def due(conn, lesson_id: int | None = None, now: str | None = None) -> list:
    now = now or utcnow()
    sql = "SELECT * FROM card WHERE due IS NOT NULL AND due <= ?"
    args: tuple = (now,)
    if lesson_id is not None:
        sql += " AND lesson_id = ?"
        args += (lesson_id,)
    return conn.execute(sql + " ORDER BY due", args).fetchall()


def grade(conn, card_row, rating_name: str, now: datetime | None = None) -> str:
    """Apply one review and return the new due date.

    The card's whole fsrs state round-trips through the database, so scheduling survives restarts
    and nothing is recomputed from a guess.
    """
    import fsrs

    scheduler = fsrs.Scheduler()
    state = card_row["fsrs_state"]
    card = fsrs.Card.from_dict(json.loads(state)) if state else fsrs.Card()
    updated, _log = scheduler.review_card(card, getattr(fsrs.Rating, rating_name),
                                          now or datetime.now(timezone.utc))
    due_at = updated.due.astimezone(timezone.utc).strftime(TS)
    conn.execute("UPDATE card SET fsrs_state=?, due=? WHERE id=?",
                 (json.dumps(updated.to_dict()), due_at, card_row["id"]))
    store.log_event(conn, "graded", card_row["lesson_id"],
                    json.dumps({"card": card_row["id"], "rating": rating_name, "due": due_at}))
    return due_at


def finish(conn, lesson_id: int) -> None:
    """A lesson whose cards have been graded has been reviewed.

    Grading is itself proof of engagement, so this promotes from `ready` as well as `consumed`.
    Requiring `consumed` first stranded quizzed lessons in `ready` forever — and because
    `mastered_tags` only counts reviewed lessons, prerequisite readiness and review pressure both
    stayed empty no matter how much was graded.
    """
    conn.execute(
        "UPDATE lesson SET state='reviewed', updated_at=? WHERE id=? AND state IN ('ready','consumed')",
        (utcnow(), lesson_id))
    store.log_event(conn, "reviewed", lesson_id)


def _selfcheck() -> None:
    import tempfile

    from . import config

    # The export schema is not contractual; several shapes must yield the same cards.
    shapes = [
        {"questions": [{"question": "what is a stencil?", "answer": "a local set of nodes"}]},
        [{"front": "what is a stencil?", "back": "a local set of nodes"}],
        {"cards": [{"prompt": "what is a stencil?", "definition": "a local set of nodes"}]},
        {"items": [{"question": "what is a stencil?", "options": ["a", "b"],
                    "correct_answer": "a local set of nodes"}]},
    ]
    for shape in shapes:
        got = extract_cards(shape)
        assert len(got) == 1, shape
        assert got[0]["front"] == "what is a stencil?"
        assert got[0]["back"] == "a local set of nodes"

    # The real export, which no guess at key names would have produced.
    real = {"title": "Cognitive Quiz", "questions": [
        {"type": "multiple_choice", "question": "why are worked examples more efficient?",
         "answerOptions": [
             {"text": "they remove means-ends analysis", "isCorrect": True,
              "rationale": "it frees working memory"},
             {"text": "they are shorter", "isCorrect": False, "rationale": "length is not the point"}],
         "hint": "think about working memory"},
        {"type": "multiple_select", "question": "which reduce extraneous load?",
         "answerOptions": [
             {"text": "worked examples", "isCorrect": True},
             {"text": "split attention", "isCorrect": False},
             {"text": "signalling", "isCorrect": True}]},
        {"type": "short_answer", "question": "what is germane load?",
         "acceptableAnswers": ["effort that builds schemas"]},
        {"type": "short_answer", "question": "what is expert blindness?",
         "grading": {"modelAnswer": "experts cannot see what confuses a novice"}},
        {"type": "fill_in_the_blank", "question": "load is ___ when materials are split",
         "bestAnswer": "extraneous"},
    ]}
    cards = extract_cards(real)
    assert len(cards) == 5, f"every question type must yield a card, got {len(cards)}"
    assert cards[0]["back"].startswith("they remove means-ends analysis")
    assert "frees working memory" in cards[0]["back"], "the rationale is the explanation"
    assert cards[1]["back"] == "worked examples / signalling", "multiple_select keeps every answer"
    assert cards[2]["back"] == "effort that builds schemas"
    assert cards[3]["back"] == "experts cannot see what confuses a novice", \
        "short answers sometimes carry the answer under grading.modelAnswer"
    assert cards[4]["back"] == "extraneous"

    assert extract_cards({"questions": [{"question": "no answer here"}]}) == [], \
        "half a pair is not a card"
    assert extract_cards("not a quiz") == [] and extract_cards([1, 2]) == []
    assert front_hash("What  is   a Stencil?") == front_hash("what is a stencil?"), \
        "spacing and case must not create a second copy of the same card"

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = config.load(path=root / "absent.toml", root=root)
        conn = store.connect(cfg["paths"]["db"])
        t = conn.execute("INSERT INTO track (goal) VALUES ('meshless methods')").lastrowid
        lid = conn.execute(
            "INSERT INTO lesson (topic, state, track_id) VALUES ('stencils','consumed',?)",
            (t,)).lastrowid
        d = cfg["paths"]["lessons"] / str(lid)
        d.mkdir(parents=True)
        (d / "quiz.json").write_text(json.dumps({"questions": [
            {"question": "what is a stencil?", "answer": "a local set of nodes"},
            {"question": "why does conditioning matter?", "answer": "weights blow up"},
        ]}))
        assert import_cards(conn, cfg, lid) == 2
        assert import_cards(conn, cfg, lid) == 0, "re-importing must not duplicate cards"

        cards = due(conn, lid)
        assert len(cards) == 2, "freshly imported cards are due now"

        first = cards[0]
        again = grade(conn, first, "Again")
        row = conn.execute("SELECT * FROM card WHERE id=?", (first["id"],)).fetchone()
        assert row["fsrs_state"], "the scheduler state is persisted, not recomputed"
        assert row["due"] == again

        # A lapse comes back sooner than a success, which is the entire point of the scheduler.
        second = due(conn, lid)[0]
        good = grade(conn, second, "Good")
        assert good > again, f"Good should schedule later than Again: {good} vs {again}"

        assert grade(conn, conn.execute("SELECT * FROM card WHERE id=?",
                                        (first["id"],)).fetchone(), "Easy") > good

        finish(conn, lid)
        assert conn.execute("SELECT state FROM lesson WHERE id=?", (lid,)).fetchone()[0] == "reviewed"

        # Quizzing a lesson you never marked consumed still counts: grading is the engagement.
        straight = conn.execute(
            "INSERT INTO lesson (topic, state) VALUES ('quizzed from ready','ready')").lastrowid
        finish(conn, straight)
        assert conn.execute("SELECT state FROM lesson WHERE id=?",
                            (straight,)).fetchone()[0] == "reviewed"

        # A lesson still in flight is not promoted by a stray call.
        early = conn.execute(
            "INSERT INTO lesson (topic, state) VALUES ('mid-flight','generating')").lastrowid
        finish(conn, early)
        assert conn.execute("SELECT state FROM lesson WHERE id=?",
                            (early,)).fetchone()[0] == "generating"
        kinds = {r[0] for r in conn.execute("SELECT kind FROM event WHERE lesson_id=?", (lid,))}
        assert {"cards", "graded", "reviewed"} <= kinds
        conn.close()

    print("review selfcheck ok")


if __name__ == "__main__":
    _selfcheck()
