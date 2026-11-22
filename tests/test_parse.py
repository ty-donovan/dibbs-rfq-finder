from datetime import date
from pathlib import Path

from bs4 import BeautifulSoup

from app import db
from app.dibbs import format_nsn, iso_date, next_page_target, parse_rfq_grid, solicitation_pdf_url

FIXTURE = Path(__file__).parent / "fixtures" / "grid_sample.html"


def grid():
    return BeautifulSoup(FIXTURE.read_text(encoding="utf-8"), "lxml")


def test_parses_real_dibbs_grid_rows():
    rows = parse_rfq_grid(grid())
    assert len(rows) == 2  # pager + header rows are skipped
    r = rows[0]
    assert r["solicitation_number"] == "SPE1C1-27-Q-0209"
    assert r["nsn"] == "8455017433421" and r["fsc"] == "8455"
    assert r["nomenclature"] == "BADGE, QUALIFICATION"
    assert r["quantity"] == 5000 and r["pr_number"] == "7019045224"
    assert r["issue_date"] == "2026-11-20" and r["return_by_date"] == "2026-11-27"
    assert r["status"] == "Open" and r["set_aside"] == "Unrestricted" and r["mil_spec"] is True
    assert r["package_url"].endswith("rfqrec.aspx?sn=SPE1C127Q0209")
    assert r["solicitation_url"] == "https://dibbs2.bsm.dla.mil/Downloads/RFQ/9/SPE1C127Q0209.PDF"


def test_pager_target_found():
    assert next_page_target(grid(), 2) == "ctl00$cph1$grdRfqSearch"
    assert next_page_target(grid(), 999) is None


def test_helpers():
    assert format_nsn("5330012345678") == "5330-01-234-5678"
    assert iso_date("11-27-2026") == "2026-11-27"
    assert solicitation_pdf_url("SPE7M1-26-T-0478").endswith("/8/SPE7M126T0478.PDF")
    assert date.fromisoformat(iso_date("1/5/2026")) == date(2026, 1, 5)


def test_search_filters(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    conn = db.connect()
    base = {c: None for c in db.COLUMNS} | {"status": "Open", "issue_date": "2026-11-20", "set_aside": "Unrestricted"}
    rows = [
        base | {"solicitation_number": "A", "nsn": "5330012345678", "fsc": "5330", "nomenclature": "GASKET", "return_by_date": "2026-11-27"},
        base | {"solicitation_number": "B", "nsn": "4820004553256", "fsc": "4820", "nomenclature": "VALVE, GATE", "return_by_date": "2026-11-25", "set_aside": "HUBZone"},
        base | {"solicitation_number": "C", "nsn": "5310011123119", "fsc": "5310", "nomenclature": "NUT", "status": "Cancelled"},
    ]
    conn.executemany(f"INSERT INTO solicitations ({', '.join(db.COLUMNS)}) VALUES ({', '.join('?' * len(db.COLUMNS))})",
                     [tuple(r[c] for c in db.COLUMNS) for r in rows])

    sols = lambda **kw: [r["solicitation_number"] for r in db.search(conn, **kw)[1]]
    assert sols() == ["B", "A"]  # cancelled hidden; soonest return-by first
    assert sols(keywords=["gasket", "valve"]) == ["B", "A"]  # keywords OR together
    assert sols(nsns=["5330-01-234-5678"]) == ["A"]  # dashes optional
    assert sols(nsns=["004553256"]) == ["B"]  # NIIN / partial match
    assert sols(fscs=["53"]) == ["A"]  # FSG prefix
    assert sols(keywords=["gasket"], fscs=["4820"]) == []  # different filters AND together
    assert sols(set_aside=db.SET_ASIDE_ANY) == ["B"]
    assert "C" in sols(open_only=False)
