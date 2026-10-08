"""E14.2 (D60): EDGAR hygiene — form set, titles, clean text, filer-only ticker hints."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

import arc.ingest.edgar as E
from arc.config import ArcSettings
from arc.store.db import connect
from arc.store.migrate import migrate

if TYPE_CHECKING:
    import sqlite3

FIXTURES = Path(__file__).parent / "fixtures" / "edgar"
NVDA_8K = (FIXTURES / "nvda-8k-20260826.htm").read_text(encoding="utf-8", errors="replace")
FORM4_XSL = (FIXTURES / "form4-xsl-render.html").read_text(encoding="utf-8", errors="replace")


@pytest.fixture()
def conn() -> sqlite3.Connection:
    c = connect(":memory:")
    migrate(c)
    return c


# ---------------------------------------------------------------------------
# form set
# ---------------------------------------------------------------------------


def test_form_set_is_8k_10q_10k_with_amendments() -> None:
    assert set(E.FORM_TYPES) == {"8-K", "8-K/A", "10-Q", "10-Q/A", "10-K", "10-K/A"}
    assert "4" not in E.FORM_TYPES


# ---------------------------------------------------------------------------
# titles
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("filing", "title"),
    [
        ({"form": "8-K", "items": "2.02,9.01"}, "8-K NVDA Items 2.02, 9.01"),
        ({"form": "8-K", "items": "8.01"}, "8-K NVDA Item 8.01"),
        ({"form": "8-K/A", "items": " 5.02 , "}, "8-K/A NVDA Item 5.02"),
        ({"form": "8-K", "items": ""}, "8-K NVDA"),
        ({"form": "10-Q", "reportDate": "2026-07-26"}, "10-Q NVDA period 2026-07-26"),
        ({"form": "10-K/A", "reportDate": "2026-01-25"}, "10-K/A NVDA period 2026-01-25"),
        ({"form": "10-K", "reportDate": ""}, "10-K NVDA"),
        ({}, "filing NVDA"),
    ],
)
def test_filing_title(filing: dict[str, Any], title: str) -> None:
    assert E.filing_title(filing, "nvda") == title


def test_filings_of_form_carries_items_and_period() -> None:
    data = {
        "filings": {
            "recent": {
                "form": ["8-K", "10-Q"],
                "accessionNumber": ["a", "b"],
                "filingDate": ["2026-08-26", "2026-08-26"],
                "reportDate": ["2026-08-26", "2026-07-26"],
                "items": ["2.02,9.01", ""],
                "primaryDocument": ["x.htm", "y.htm"],
            }
        }
    }
    (k8,) = E._filings_of_form(data, "1", "8-K", count=5)
    (q,) = E._filings_of_form(data, "1", "10-Q", count=5)
    assert (k8["items"], q["reportDate"]) == ("2.02,9.01", "2026-07-26")
    # older submissions documents without the arrays still parse
    data["filings"]["recent"].pop("items")
    data["filings"]["recent"].pop("reportDate")
    assert E._filings_of_form(data, "1", "8-K", count=5)[0]["items"] == ""


# ---------------------------------------------------------------------------
# text cleaning (real fixture filings)
# ---------------------------------------------------------------------------


def test_clean_8k_inline_xbrl_drops_header_and_keeps_prose() -> None:
    text = E.clean_filing_text(NVDA_8K)
    assert text.startswith("UNITED STATES SECURITIES AND EXCHANGE COMMISSION")
    assert "FORM 8-K" in text
    assert "NVIDIA CORPORATION" in text  # a <span> split inside the word is joined
    assert "Results of Operations and Financial Condition" in text
    for junk in ("xbrli", "EntityCentralIndexKey", "schemaRef", "Workiva", "<", "font-family"):
        assert junk not in text, junk


def test_clean_xsl_render_drops_css() -> None:
    text = E.clean_filing_text(FORM4_XSL)
    assert text.startswith("SEC Form 4")
    assert "STATEMENT OF CHANGES IN BENEFICIAL OWNERSHIP" in text
    for junk in (".FormData", "{color", "font-size", "background-color"):
        assert junk not in text, junk


def test_clean_handles_entities_comments_and_cap() -> None:
    raw = "<!-- c --><p>A&amp;B</p><script>var x=1;</script><div>" + "y " * 40_000 + "</div>"
    text = E.clean_filing_text(raw)
    assert text.startswith("A&B y")
    assert "var x" not in text and len(text) == E.MAX_TEXT_CHARS


def test_clean_keeps_braces_that_are_not_css() -> None:
    assert E.clean_filing_text("<p>Set {A, B} of options</p>") == "Set {A, B} of options"


# ---------------------------------------------------------------------------
# fetch_edgar: titles stored, filer-only hints, no Form 4
# ---------------------------------------------------------------------------


class _Uni:
    seed = ("NVDA",)

    def cik(self, _t: str) -> str:
        return "0001045810"

    def tickers_in(self, _text: str) -> list[str]:  # must not be consulted
        raise AssertionError("E14.2: no ticker regex over the filing body")


def test_fetch_edgar_titles_hints_and_forms(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    subs = {
        "filings": {
            "recent": {
                "form": ["4", "8-K", "10-Q", "4", "10-K/A", "S-8"],
                "accessionNumber": ["f4a", "k8", "q10", "f4b", "k10a", "s8"],
                "filingDate": ["2026-10-07"] * 6,
                "acceptanceDateTime": ["2026-10-07T20:00:00.000Z"] * 6,
                "reportDate": ["", "2026-10-07", "2026-07-26", "", "2026-01-25", ""],
                "items": ["", "2.02,9.01", "", "", "", ""],
                "primaryDocument": ["f.xml", "k8.htm", "q.htm", "g.xml", "ka.htm", "s.htm"],
            }
        }
    }
    fetched: list[str] = []

    def filing_text(url: str, _s: Any) -> str:
        fetched.append(url.rsplit("/", 1)[-1])
        # the body names other symbols; none may become a hint
        return E.clean_filing_text(NVDA_8K) + " A D PARK TSM"

    monkeypatch.setattr(E, "_fetch_submissions", lambda _cik, _s: subs)
    monkeypatch.setattr(E, "_fetch_filing_text", filing_text)
    monkeypatch.setattr(E.IngestUniverse, "from_settings", classmethod(lambda cls, s, **_k: _Uni()))
    settings = ArcSettings(universe=["NVDA"], _env_file=None)  # type: ignore[call-arg]

    docs = E.fetch_edgar(conn, settings)

    assert sorted(fetched) == ["k8.htm", "ka.htm", "q.htm"]  # no Form 4, no S-8
    assert all(d.tickers_hint == ["NVDA"] for d in docs)
    rows = conn.execute(
        "SELECT title, tickers_hint, substr(text, 1, 13) FROM raw_docs ORDER BY title"
    ).fetchall()
    assert [r[0] for r in rows] == [
        "10-K/A NVDA period 2026-01-25",
        "10-Q NVDA period 2026-07-26",
        "8-K NVDA Items 2.02, 9.01",
    ]
    assert {r[1] for r in rows} == {'["NVDA"]'}
    assert {r[2] for r in rows} == {"UNITED STATES"}
