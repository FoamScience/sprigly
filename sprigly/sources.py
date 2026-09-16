"""Literature adapters.

These return structured metadata, never prose. Credibility is decided from these fields by the
gate, not by an agent reading abstracts, so every field the gate needs is carried here even when
the adapter has to leave it unknown.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# OpenAlex work types that are a piece of readable scholarship. Everything else — datasets,
# editorials, retraction notices, peer-review reports — is not a lesson source.
READABLE_TYPES = {"article", "review", "book-chapter", "book", "preprint", "report", "dissertation"}


@dataclass
class Work:
    """One candidate source, as the gate needs to see it."""

    title: str
    url: str
    doi: str | None = None
    venue: str | None = None
    venue_type: str | None = None          # journal | conference | repository | ...
    year: int | None = None
    work_type: str | None = None
    cited_by: int = 0
    is_oa: bool = False
    pdf_url: str | None = None
    retracted: bool = False
    paratext: bool = False
    # Every OA copy that offers a file, repository-hosted first. Publishers increasingly answer a
    # direct pdf link with a bot challenge; a university repository usually just serves the file.
    pdf_urls: list[str] = field(default_factory=list)
    api: str = ""
    authors: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        """Identity for dedup: the DOI when there is one, else the URL."""
        return (self.doi or self.url).lower()


PUBLISHER_HOSTS = ("link.springer.com", "sciencedirect.com", "onlinelibrary.wiley.com",
                   "tandfonline.com", "nature.com", "science.org", "ieeexplore.ieee.org",
                   "dl.acm.org", "cambridge.org", "oup.com", "sagepub.com")


def _pdf_candidates(w: dict) -> list[str]:
    """Distinct OA pdf links, repositories before publishers."""
    seen: dict[str, int] = {}
    for loc in [w.get("best_oa_location") or {}, *(w.get("locations") or [])]:
        url = loc.get("pdf_url")
        if not url or not loc.get("is_oa") or url in seen:
            continue
        host = (loc.get("source") or {}).get("type") == "repository"
        publisher = any(p in url for p in PUBLISHER_HOSTS)
        seen[url] = 0 if host and not publisher else (2 if publisher else 1)
    return sorted(seen, key=seen.get)


def _openalex_work(w: dict) -> Work:
    loc = (w.get("primary_location") or {})
    src = (loc.get("source") or {})
    oa = (w.get("open_access") or {})
    best = (w.get("best_oa_location") or {})
    doi = (w.get("doi") or "").replace("https://doi.org/", "") or None
    return Work(
        title=w.get("display_name") or w.get("title") or "",
        url=best.get("landing_page_url") or loc.get("landing_page_url") or w.get("id") or "",
        doi=doi,
        venue=src.get("display_name"),
        venue_type=src.get("type"),
        year=w.get("publication_year"),
        work_type=w.get("type"),
        cited_by=w.get("cited_by_count") or 0,
        is_oa=bool(oa.get("is_oa")),
        pdf_url=best.get("pdf_url") or loc.get("pdf_url"),
        pdf_urls=_pdf_candidates(w),
        retracted=bool(w.get("is_retracted")),
        paratext=bool(w.get("is_paratext")),
        api="openalex",
        authors=[(a.get("author") or {}).get("display_name", "")
                 for a in (w.get("authorships") or [])[:5]],
    )


def search_openalex(query: str, limit: int = 20, mailto: str | None = None) -> list[Work]:
    """Primary index. Carries retraction, open-access and venue metadata in one call."""
    import pyalex

    # Identifying yourself puts requests in OpenAlex's faster "polite pool". It is off unless the
    # user configures an address — sending their email to a third party is their call, not ours.
    if mailto:
        pyalex.config.email = mailto
    rows = pyalex.Works().search(query).get(per_page=min(limit, 50))
    return [_openalex_work(w) for w in rows][:limit]


def search_arxiv(query: str, limit: int = 10) -> list[Work]:
    """Preprints. Always open access, never peer reviewed — the gate tiers them accordingly."""
    import arxiv

    out = []
    for r in arxiv.Client(page_size=min(limit, 50), delay_seconds=3.0).results(
            arxiv.Search(query=query, max_results=limit)):
        out.append(Work(
            title=r.title.strip(),
            url=r.entry_id,
            doi=r.doi,
            venue="arXiv",
            venue_type="repository",
            year=r.published.year if r.published else None,
            work_type="preprint",
            is_oa=True,
            pdf_url=r.pdf_url,
            api="arxiv",
            authors=[a.name for a in r.authors[:5]],
        ))
    return out


def resolve_doi(doi: str, mailto: str | None = None) -> Work | None:
    """Crossref, for a DOI that arrived from somewhere without metadata attached."""
    from habanero import Crossref

    try:
        msg = Crossref(mailto=mailto).works(ids=doi)["message"]
    except Exception as err:  # a DOI that does not resolve is a normal outcome, not a failure
        log.info("crossref could not resolve %s: %s", doi, err)
        return None
    titles = msg.get("title") or [""]
    issued = (msg.get("issued") or {}).get("date-parts") or [[None]]
    return Work(
        title=titles[0],
        url=msg.get("URL") or f"https://doi.org/{doi}",
        doi=msg.get("DOI"),
        venue=(msg.get("container-title") or [None])[0],
        venue_type="journal" if msg.get("type") == "journal-article" else msg.get("type"),
        year=issued[0][0],
        work_type=msg.get("type"),
        cited_by=msg.get("is-referenced-by-count") or 0,
        retracted=bool(msg.get("update-to")) and any(
            u.get("type") == "retraction" for u in (msg.get("update-to") or [])),
        api="crossref",
        authors=[f"{a.get('given', '')} {a.get('family', '')}".strip()
                 for a in (msg.get("author") or [])[:5]],
    )


def search(query: str, limit: int = 20, cfg: dict | None = None, report=None) -> list[Work]:
    """Every adapter, merged and deduplicated.

    An adapter that is down must not take the harvest with it — a partial result set is worth more
    than an exception, and the gate downstream decides whether what survived is enough.

    Reported per adapter with its timing: this is the slowest part of a harvest and the part with
    no agent narrating it, so without this the command looks stalled for minutes.
    """
    import time

    say = report or (lambda _msg: None)
    mailto = ((cfg or {}).get("sources", {}) or {}).get("mailto") or None
    found: dict[str, Work] = {}
    for name, call in (("openalex", lambda: search_openalex(query, limit, mailto)),
                       ("arxiv", lambda: search_arxiv(query, max(limit // 2, 3)))):
        say(f"querying {name}")
        started = time.monotonic()
        try:
            rows = call()
        except Exception as err:
            log.warning("%s adapter failed for %r: %s", name, query, err)
            say(f"{name} failed after {time.monotonic() - started:.0f}s")
            continue
        fresh = 0
        for w in rows:
            if w.title and w.key not in found:
                found[w.key] = w
                fresh += 1
        say(f"{name}: {len(rows)} works, {fresh} new ({time.monotonic() - started:.0f}s)")
    return list(found.values())


def _selfcheck() -> None:
    """Offline. The adapters are verified against the live APIs separately; what is asserted here
    is the mapping, which is where a field quietly goes missing and the gate then cannot see it."""
    raw = {
        "id": "https://openalex.org/W123",
        "display_name": "The overlapped radial basis function-finite difference method",
        "doi": "https://doi.org/10.1016/j.jcp.2017.07.014",
        "publication_year": 2017,
        "type": "article",
        "cited_by_count": 62,
        "is_retracted": False,
        "is_paratext": False,
        "primary_location": {"landing_page_url": "https://example.org/landing",
                             "source": {"display_name": "Journal of Computational Physics",
                                        "type": "journal"}},
        "open_access": {"is_oa": True},
        "best_oa_location": {"pdf_url": "https://example.org/paper.pdf"},
        "authorships": [{"author": {"display_name": "V Bayona"}}],
    }
    w = _openalex_work(raw)
    assert w.doi == "10.1016/j.jcp.2017.07.014", "the doi prefix is stripped so keys match crossref"
    assert (w.venue, w.venue_type, w.year, w.work_type, w.cited_by) == \
        ("Journal of Computational Physics", "journal", 2017, "article", 62)
    assert w.is_oa and w.pdf_url.endswith(".pdf") and not w.retracted

    # Every OA copy is worth a try, and a repository copy is tried before the publisher's.
    many = _openalex_work({**raw, "locations": [
        {"is_oa": True, "pdf_url": "https://link.springer.com/content/pdf/x.pdf",
         "source": {"type": "journal"}},
        {"is_oa": True, "pdf_url": "https://repo.example.edu/x.pdf",
         "source": {"type": "repository"}},
        {"is_oa": False, "pdf_url": "https://paywalled.example.com/x.pdf",
         "source": {"type": "journal"}},
    ], "best_oa_location": {"pdf_url": "https://link.springer.com/content/pdf/x.pdf"}})
    assert many.pdf_urls[0] == "https://repo.example.edu/x.pdf", "repositories come first"
    assert "paywalled.example.com" not in " ".join(many.pdf_urls), "a closed copy is not a candidate"
    assert len(many.pdf_urls) == len(set(many.pdf_urls)), "no duplicates"
    assert w.key == w.doi, "a work with a doi is identified by it"

    bare = _openalex_work({"display_name": "x", "id": "https://openalex.org/W9"})
    assert bare.key == "https://openalex.org/w9", "without a doi the url is the key"
    assert bare.cited_by == 0 and not bare.is_oa and not bare.retracted, \
        "missing fields default to the conservative value, never to None"

    retracted = _openalex_work({"display_name": "x", "id": "u", "is_retracted": True})
    assert retracted.retracted, "the retraction flag survives the mapping"

    # Dedup is by key, so the same paper from two adapters collapses to one row.
    a = _openalex_work({"display_name": "p", "id": "u1", "doi": "https://doi.org/10.1/x"})
    b = Work(title="p", url="https://arxiv.org/abs/1", doi="10.1/X", api="arxiv")
    assert a.key == b.key, "doi comparison is case-insensitive"

    assert "article" in READABLE_TYPES and "peer-review" not in READABLE_TYPES
    print("sources selfcheck ok")


if __name__ == "__main__":
    _selfcheck()
