"""Tag and domain normalisation.

Prerequisite readiness, review pressure and the DPP similarity kernel all work by set intersection,
so `RBF` and `rbf` and `radial-basis-functions` silently send three signals to zero. A maintained
controlled vocabulary does not survive topics ranging from quantum mechanics to business, so the
vocabulary is left to emerge — normalised on write, and fed back to the curator as "reuse these".

Form: US English, lowercase, no abbreviations, hyphen-separated.
"""

from __future__ import annotations

import difflib
import re
import sqlite3
import unicodedata

# Curated, because the rules that look general are not. "-our" to "-or" would wreck four, your,
# tour, hour, flour and contour; "-re" to "-er" would wreck are, here, more and genre.
SPELLING = {
    "behaviour": "behavior", "colour": "color", "flavour": "flavor", "favour": "favor",
    "honour": "honor", "labour": "labor", "neighbour": "neighbor", "vapour": "vapor",
    "harbour": "harbor", "rumour": "rumor", "humour": "humor", "odour": "odor",
    "armour": "armor", "endeavour": "endeavor", "savour": "savor", "valour": "valor",
    "tumour": "tumor", "moulding": "molding", "mould": "mold", "programme": "program",
    "centre": "center", "metre": "meter", "litre": "liter", "fibre": "fiber",
    "theatre": "theater", "calibre": "caliber", "sabre": "saber", "spectre": "specter",
    "sombre": "somber", "lustre": "luster", "manoeuvre": "maneuver", "meagre": "meager",
    "modelling": "modeling", "modelled": "modeled", "labelling": "labeling",
    "cancelled": "canceled", "travelling": "traveling", "fuelled": "fueled",
    "ageing": "aging", "analogue": "analog", "catalogue": "catalog", "dialogue": "dialog",
    "defence": "defense", "offence": "offense", "licence": "license", "practise": "practice",
    "grey": "gray", "aluminium": "aluminum", "sulphur": "sulfur", "aeroplane": "airplane",
    "haemoglobin": "hemoglobin", "oestrogen": "estrogen", "anaemia": "anemia",
    "orthopaedic": "orthopedic", "encyclopaedia": "encyclopedia",
}

# Words ending in -ise/-isation that are not British variants of -ize/-ization.
NOT_IZE = {
    "rise", "wise", "precise", "concise", "promise", "exercise", "surprise", "advise",
    "revise", "devise", "supervise", "compromise", "franchise", "merchandise", "disguise",
    "improvise", "arise", "demise", "otherwise", "likewise", "noise", "cruise", "paradise",
    "expertise", "treatise", "premise", "enterprise", "chastise", "despise", "excise",
    "incise", "apprise", "comprise", "reprise", "guise", "anise", "poise", "tortoise",
}


def _americanise(word: str) -> str:
    if word in SPELLING:
        return SPELLING[word]
    if word in NOT_IZE:
        return word
    for brit, us in (("isation", "ization"), ("isations", "izations"), ("ising", "izing"),
                     ("ised", "ized"), ("ise", "ize"), ("ises", "izes"), ("yse", "yze"),
                     ("ysed", "yzed"), ("ysing", "yzing"), ("yses", "yzes")):
        if word.endswith(brit) and len(word) > len(brit) + 2:
            return word[: -len(brit)] + us
    return word


def normalize(raw: str, expansions: dict[str, str] | None = None) -> str:
    """One tag, in canonical form. Returns "" for anything that normalises away to nothing."""
    text = unicodedata.normalize("NFKD", raw or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    if not text:
        return ""
    # Abbreviations are expanded only from an explicit map. Guessing is how "fem" ends up meaning
    # both the finite element method and field emission microscopy in the same database.
    exp = {k.lower(): v for k, v in (expansions or {}).items()}
    if text in exp:
        return normalize(exp[text])
    words = [exp.get(w, w) for w in text.split("-")]
    words = [w for word in words for w in normalize_word_split(word)]
    return "-".join(_americanise(w) for w in words if w)


def normalize_word_split(word: str) -> list[str]:
    """An expansion may itself be multi-word, so it re-enters as separate words."""
    return [w for w in re.split(r"[^a-z0-9]+", word) if w]


def known_tags(conn: sqlite3.Connection, kind: str = "tag") -> list[str]:
    """The vocabulary so far. Fed to the curator so it reuses terms instead of inventing them."""
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT tag FROM lesson_tag WHERE kind=? ORDER BY tag", (kind,))]


def canonical(raw: str, known: list[str], cutoff: float = 0.92,
              expansions: dict[str, str] | None = None) -> str:
    """Normalise, then snap to an existing tag if one is near enough.

    Catches the drift normalisation cannot: singular against plural, and a stray suffix. The cutoff
    is deliberately high — `rbf-fd` and `rbf-qr` are different methods and must not collapse.
    """
    tag = normalize(raw, expansions)
    if not tag or tag in known:
        return tag
    close = difflib.get_close_matches(tag, known, n=1, cutoff=cutoff)
    return close[0] if close and close[0][0] == tag[0] else tag


def write(conn: sqlite3.Connection, lesson_id: int, values: list[str], kind: str,
          cfg: dict) -> list[str]:
    """The single place tags enter the database, so no writer can forget to normalise."""
    opts = cfg["tags"]
    known = known_tags(conn, kind)
    out = []
    for raw in values:
        tag = canonical(raw, known, opts["snap_cutoff"], opts["expansions"])
        if not tag:
            continue
        conn.execute("INSERT OR IGNORE INTO lesson_tag VALUES (?,?,?)", (lesson_id, tag, kind))
        if tag not in known:
            known.append(tag)
        out.append(tag)
    return out


def _selfcheck() -> None:
    import tempfile
    from pathlib import Path

    from . import config, store

    assert normalize("RBF") == "rbf"
    assert normalize("  Radial   Basis  Functions ") == "radial-basis-functions"
    assert normalize("Finite_Element/Method") == "finite-element-method"
    assert normalize("naïve Bayes") == "naive-bayes", "accents fold"
    assert normalize("") == "" and normalize("---") == "" and normalize("!!!") == ""

    assert normalize("discretisation") == "discretization"
    assert normalize("Behaviour Modelling") == "behavior-modeling"
    assert normalize("analyse") == "analyze"
    assert normalize("centre-of-mass") == "center-of-mass"
    # The suffix rules must not fire on words that merely look British.
    for word in ("rise", "exercise", "precise", "promise", "expertise", "compromise"):
        assert normalize(word) == word, word
    assert normalize("four-hour-tour") == "four-hour-tour", "-our is a curated list, not a rule"
    assert normalize("genre") == "genre", "-re is a curated list, not a rule"

    assert normalize("fem", {"fem": "finite element method"}) == "finite-element-method"
    assert normalize("fem") == "fem", "nothing is expanded without an explicit map"

    known = ["radial-basis-functions", "rbf-fd"]
    assert canonical("Radial Basis Function", known) == "radial-basis-functions", "plural drift"
    assert canonical("rbf-qr", known) == "rbf-qr", "distinct methods must not collapse"
    assert canonical("stencil", known) == "stencil"

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = config.load(path=root / "absent.toml", root=root)
        conn = store.connect(cfg["paths"]["db"])
        lid = conn.execute("INSERT INTO lesson (topic) VALUES ('rbf-fd stencils')").lastrowid
        assert write(conn, lid, ["RBF-FD", "Stencils"], "tag", cfg) == ["rbf-fd", "stencils"]
        l2 = conn.execute("INSERT INTO lesson (topic) VALUES ('another')").lastrowid
        assert write(conn, l2, ["Stencil"], "tag", cfg) == ["stencils"], "snaps to the vocabulary"
        assert write(conn, l2, ["", "  "], "tag", cfg) == [], "empty tags are dropped"
        assert known_tags(conn) == ["rbf-fd", "stencils"]
        conn.close()

    print("tags selfcheck ok")


if __name__ == "__main__":
    _selfcheck()
