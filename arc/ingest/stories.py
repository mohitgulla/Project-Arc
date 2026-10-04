"""Story clustering (E4.5, D30): near-duplicate docs from any sources -> one story.

Deterministic, no LLM:

1. **Headline** of a doc = its title, else its first sentence (<= 200 chars).
   :func:`normalize_headline` lower-cases, unescapes HTML, strips a trailing
   source suffix (``" - WSJ"``, ``" | Reuters"``), drops stop words and a plural
   ``s`` so "Nvidia shares rise" and "Nvidia share rises" compare equal.
2. **Similarity** = token-set Jaccard of the normalised headlines.
3. **Clustering** walks docs oldest first; a doc joins the most similar existing
   story whose members include a doc within ``window`` of it and with Jaccard >=
   ``threshold``; otherwise it opens a new story. Ties break on story id, so the
   result is a pure function of the input.
4. **Filings** never cluster by headline: an EDGAR filing joins the story for its
   filer (first ticker hint) and form type (filings are summarised per filer
   and form, not one by one; D47 files them under the ``company`` category).

A story's ``distinct_sources`` is the number of distinct registry source keys
among its docs. Only this count may raise corroboration (the Scout rule in
:mod:`arc.ingest.scout`); ten WSJ items about one story still count once.
"""

from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

if TYPE_CHECKING:
    import datetime as _dt
    from collections.abc import Sequence

__all__ = [
    "ClusterDoc",
    "Story",
    "canonical_url",
    "cluster_stories",
    "jaccard",
    "normalize_headline",
]

_STOP = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "has",
        "have",
        "in",
        "into",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "over",
        "says",
        "say",
        "said",
        "than",
        "that",
        "the",
        "their",
        "this",
        "to",
        "up",
        "was",
        "were",
        "will",
        "with",
        "after",
        "amid",
        "new",
        "report",
        "reports",
        "update",
        "updates",
        "live",
        "breaking",
        "exclusive",
    ]
)
_SUFFIX = re.compile(r"\s+[-–—|:]\s+[^-–—|:]{2,40}$")
_TAGS = re.compile(r"<[^>]+>")
_TOKEN = re.compile(r"[a-z0-9$][a-z0-9.$%]*")
# A sentence ends after [.!?] preceded by two word characters and followed by a
# capital: "U.S. stocks", "Inc. said", "No. 2" and "Jan. 5" do not end one.
_SENTENCE = re.compile(r"(?<=[a-z0-9%)\"'][a-z0-9%)\"'][.!?])\s+(?=[A-Z\"'(])")
_TRACKING = re.compile(r"^(utm_|mc_|mod$|cmpid$|ref$|refsrc$|siteid$|yptr$|__source$|feedtype$)")
_FORM = re.compile(r"\b(8-K|10-Q|10-K|FORM 4|S-1|6-K|20-F|13D|13G)\b", re.IGNORECASE)


def canonical_url(url: str) -> str:
    """Drop tracking query parameters (``utm_*``, ``mod``, ...) and the fragment."""
    parts = urlsplit(url.strip())
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)]
    kept = [(k, v) for k, v in query if not _TRACKING.match(k.lower())]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), ""))


def headline_of(title: str | None, text: str) -> str:
    """The doc's title, else the first sentence of its text (HTML stripped)."""
    if title and title.strip():
        return html.unescape(_TAGS.sub(" ", title)).strip()[:200]
    plain = " ".join(html.unescape(_TAGS.sub(" ", text)).split())
    return _SENTENCE.split(plain, maxsplit=1)[0][:200] if plain else ""


def _stem(tok: str) -> str:
    if len(tok) > 3 and tok.endswith("s") and not tok.endswith("ss"):
        return tok[:-1]
    return tok


def normalize_headline(headline: str) -> frozenset[str]:
    """Token set used for similarity (lower-case, suffix/stop words stripped)."""
    text = html.unescape(_TAGS.sub(" ", headline)).strip()
    text = _SUFFIX.sub("", text)
    toks = (_stem(t.strip(".")) for t in _TOKEN.findall(text.lower()))
    return frozenset(t for t in toks if t and t not in _STOP)


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a and not b:
        return 0.0
    return len(a & b) / len(a | b)


@dataclass(frozen=True)
class ClusterDoc:
    """The fields clustering needs from one ``raw_docs`` row."""

    id: str
    source_key: str
    source: str  # connector (rss, edgar, ...)
    url: str
    published_at: _dt.datetime
    headline: str
    tickers: tuple[str, ...] = ()
    category: str = ""
    form_type: str | None = None  # filings


@dataclass
class Story:
    """One cluster of docs about the same event."""

    id: str
    headline: str
    docs: list[ClusterDoc] = field(default_factory=list)
    tokens: list[frozenset[str]] = field(default_factory=list)
    group_key: str | None = None  # filings: "<filer>|<form>"

    @property
    def source_keys(self) -> list[str]:
        return sorted({d.source_key for d in self.docs})

    @property
    def distinct_sources(self) -> int:
        return len(self.source_keys)

    @property
    def urls(self) -> list[str]:
        return list(dict.fromkeys(d.url for d in self.docs))

    @property
    def categories(self) -> list[str]:
        return sorted({d.category for d in self.docs if d.category})

    @property
    def category(self) -> str:
        """Category of the story's first doc (stories are batched by it)."""
        return self.docs[0].category if self.docs else ""

    @property
    def tickers(self) -> list[str]:
        return list(dict.fromkeys(t for d in self.docs for t in d.tickers))

    @property
    def first_published(self) -> _dt.datetime:
        return min(d.published_at for d in self.docs)

    @property
    def last_published(self) -> _dt.datetime:
        return max(d.published_at for d in self.docs)


def form_type_of(title: str | None, url: str, text: str) -> str:
    """Best-effort SEC form type (title first, then URL, then the text head)."""
    for hay in (title or "", url, text[:2000]):
        m = _FORM.search(hay.replace("_", " "))
        if m:
            return m.group(1).upper().replace("FORM ", "")
        low = hay.lower()
        if "_8k" in low or "8k.htm" in low:
            return "8-K"
    return "filing"


def _story_id(first: ClusterDoc) -> str:
    return "st-" + hashlib.sha256(f"{first.source_key}|{first.url}".encode()).hexdigest()[:12]


def cluster_stories(
    docs: Sequence[ClusterDoc],
    *,
    threshold: float,
    window: _dt.timedelta,
) -> list[Story]:
    """Cluster *docs* into stories (see module docstring). Output is oldest first."""
    ordered = sorted(docs, key=lambda d: (d.published_at, d.source_key, d.id))
    stories: list[Story] = []
    by_group: dict[str, Story] = {}
    for doc in ordered:
        if doc.form_type is not None or doc.source == "edgar":  # D47: filings live in `company`
            filer = doc.tickers[0] if doc.tickers else doc.source_key
            key = f"{filer}|{doc.form_type or 'filing'}"
            story = by_group.get(key)
            if story is None:
                story = Story(
                    id=_story_id(doc),
                    headline=f"{filer} {doc.form_type or 'filing'} filings",
                    group_key=key,
                )
                by_group[key] = story
                stories.append(story)
            story.docs.append(doc)
            story.tokens.append(frozenset())
            continue
        toks = normalize_headline(doc.headline)
        best: tuple[float, str] | None = None
        best_story: Story | None = None
        if toks:
            for story in stories:
                if story.group_key is not None:
                    continue
                score = 0.0
                for member, mtoks in zip(story.docs, story.tokens, strict=True):
                    if abs(member.published_at - doc.published_at) > window:
                        continue
                    score = max(score, jaccard(toks, mtoks))
                if score >= threshold and (best is None or (score, story.id) > best):
                    best = (score, story.id)
                    best_story = story
        if best_story is None:
            best_story = Story(id=_story_id(doc), headline=doc.headline)
            stories.append(best_story)
        best_story.docs.append(doc)
        best_story.tokens.append(toks)
    return stories
