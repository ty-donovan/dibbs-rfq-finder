"""Parsers for DIBBS date list, NSN page, and daily files - all fixtures are real DIBBS responses."""

from datetime import date
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from app import db
from app.codes import amsc_group
from app.dibbs import parse_approved_sources_file, parse_index_file, parse_issue_dates, parse_nsn_page

FIX = Path(__file__).parent / "fixtures"


def soup(name: str) -> BeautifulSoup:
    return BeautifulSoup((FIX / name).read_text(encoding="utf-8"), "lxml")


def test_issue_dates_listing():
    dates = parse_issue_dates(soup("dates_issue.html"))
    assert len(dates) == 27
    assert dates[0] == date(2026, 11, 21) and date(2026, 11, 20) in dates
    assert dates == sorted(dates, reverse=True)


def test_nsn_page_amsc_and_approved_sources():
    info = parse_nsn_page(soup("nsn_page_sample.html"))
    assert info["amsc"] == "Z"
    assert info["sources"] == [
        {"cage": "1XUY5", "part_number": "NDC51660052605", "company": "AMERISOURCEBERGEN DRUG CORP"},
        {"cage": "8Z535", "part_number": "NDC51660-0526-05", "company": None},
    ]


def test_daily_index_file_amsc_column():
    amsc = parse_index_file((FIX / "in_sample.txt").read_text(encoding="latin-1"))
    assert amsc["8455017433421"] == "G"
    assert amsc["6630015255546"] == "Z"


def test_daily_approved_sources_file():
    sources = parse_approved_sources_file((FIX / "as_sample.txt").read_text(encoding="utf-8"))
    assert [s["cage"] for s in sources["1005012394199"]] == ["8V613", "98459"]
    assert sources["1005010590501"][0]["part_number"] == "209F530"


@pytest.mark.parametrize("letter,group", [("G", "open"), ("Z", "open"), ("C", "approved"), ("T", "approved"),
                                          ("D", "no_data"), ("L", "no_data"), ("E", "other"), (None, None)])
def test_amsc_groups(letter, group):
    assert amsc_group(letter) == group


def test_amsc_source_and_past_due_filters(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(db, "NSN_INFO_PATH", tmp_path / "nsn_info.jsonl")
    conn = db.connect()
    base = {c: None for c in db.COLUMNS} | {"status": "Open", "issue_date": "2026-11-20", "return_by_date": "2999-01-01"}
    rows = [
        base | {"solicitation_number": "A", "nsn": "5330000000001", "fsc": "5330", "nomenclature": "GASKET"},
        base | {"solicitation_number": "B", "nsn": "5330000000002", "fsc": "5330", "nomenclature": "SEAL"},
        base | {"solicitation_number": "C", "nsn": "5330000000003", "fsc": "5330", "nomenclature": "O-RING"},
        base | {"solicitation_number": "D", "nsn": "5330000000004", "fsc": "5330", "nomenclature": "OLD", "return_by_date": "2000-01-01"},
    ]
    conn.executemany(f"INSERT INTO solicitations ({', '.join(db.COLUMNS)}) VALUES ({', '.join('?' * len(db.COLUMNS))})",
                     [tuple(r[c] for c in db.COLUMNS) for r in rows])
    db.save_nsn_info(conn, [
        {"nsn": "5330000000001", "amsc": "G", "sources": [], "origin": "nsn_page"},
        {"nsn": "5330000000002", "amsc": "C", "origin": "nsn_page",
         "sources": [{"cage": "8V613", "part_number": "7-611", "company": "THE BOEING COMPANY"}]},
    ])

    sols = lambda **kw: [r["solicitation_number"] for r in db.search(conn, **kw)[1]]
    assert sols() == ["A", "B", "C"]  # D is past its return-by date
    assert "D" in sols(include_past_due=True)
    assert sols(amsc_groups=["open"]) == ["A"]
    assert sols(amsc_groups=["approved", "unchecked"]) == ["B", "C"]
    assert sols(sources=["8V613"]) == ["B"] and sols(sources=["boeing"]) == ["B"]
    assert db.nsns_needing_info(conn, ["5330000000001", "5330000000003"]) == ["5330000000003"]
    assert (tmp_path / "nsn_info.jsonl").read_text().count("\n") == 2  # persisted for reload
