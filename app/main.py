"""FastAPI app: JSON search API, background jobs (pull a day / check NSNs), and the static UI.

    uv run uvicorn app.main:app --reload
"""

from __future__ import annotations

import csv
import io
import sqlite3
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app import db, enrich, scrape
from app.codes import AMSC_GROUPS, AMSC_MEANINGS
from app.dibbs import DibbsClient
from app.fsc_names import FSC_NAMES
from app.jobs import JobBusy, runner

STATIC = Path(__file__).resolve().parent.parent / "static"
SECONDS_PER_NSN = 1.6  # measured: DIBBS NSN pages take ~6 s each, fetched 4 at a time
SECONDS_PER_GRID_PAGE = 8  # measured: ~50 RFQs per page


@asynccontextmanager
async def lifespan(_: FastAPI):
    conn = db.connect()
    db.load_all(conn)
    conn.close()
    yield


app = FastAPI(title="DIBBS RFQ Finder", lifespan=lifespan)


def get_conn():
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


def _latest_date(conn: sqlite3.Connection) -> str | None:
    return conn.execute("SELECT MAX(issue_date) FROM solicitations").fetchone()[0]


@dataclass
class Filters:
    """Query params shared by search, CSV export, and 'check these results'. Repeat list params to OR values."""

    date: str | None = None
    kw: list[str] = Query(default=[])
    nsn: list[str] = Query(default=[])
    fsc: list[str] = Query(default=[])
    set_aside: str | None = None
    include_closed: bool = False
    include_past_due: bool = False
    amsc: list[str] = Query(default=[])
    source: list[str] = Query(default=[])
    sort: str = "return_by"
    desc: bool = False

    def run(self, conn: sqlite3.Connection, limit: int | None, offset: int = 0) -> tuple[str | None, int, list[dict]]:
        issue_date = self.date or _latest_date(conn)
        total, rows = db.search(
            conn, issue_date=issue_date, keywords=self.kw, nsns=self.nsn, fscs=self.fsc,
            set_aside=self.set_aside, open_only=not self.include_closed, include_past_due=self.include_past_due,
            amsc_groups=self.amsc, sources=self.source, sort=self.sort, desc=self.desc, limit=limit, offset=offset,
        )
        for r in rows:
            r["fsc_name"] = FSC_NAMES.get(r["fsc"] or "")
        return issue_date, total, rows


# ---- search ------------------------------------------------------------------

@app.get("/api/solicitations")
def solicitations(f: Filters = Depends(), limit: int = Query(default=100, le=500), offset: int = 0,
                  conn: sqlite3.Connection = Depends(get_conn)):
    issue_date, total, rows = f.run(conn, limit, offset)
    return {"date": issue_date, "total": total, "limit": limit, "offset": offset, "rows": rows}


@app.get("/api/solicitations.csv")
def solicitations_csv(f: Filters = Depends(), conn: sqlite3.Connection = Depends(get_conn)):
    """Everything matching the current filters, for vendors who work in Excel."""
    issue_date, _, rows = f.run(conn, limit=None)
    cols = ["solicitation_number", "nsn", "fsc", "fsc_name", "nomenclature", "quantity", "set_aside", "status",
            "issue_date", "return_by_date", "amsc", "approved_sources", "package_url", "solicitation_url"]
    for r in rows:
        r["approved_sources"] = "; ".join(f"{s['cage']} {s['part_number']}" for s in r["sources"])
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return StreamingResponse(
        iter([buf.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="dibbs-rfqs-{issue_date}.csv"'},
    )


@app.get("/api/facets")
def facets(date: str | None = None, conn: sqlite3.Connection = Depends(get_conn)):
    issue_date = date or _latest_date(conn)
    f = db.facets(conn, issue_date)
    for item in f["fscs"]:
        item["name"] = FSC_NAMES.get(item["fsc"])
    groups = [{"key": k, "label": g["label"], "letters": g["letters"], "hint": g["hint"]} for k, g in AMSC_GROUPS.items()]
    return {"date": issue_date, **f, "amsc_groups": groups, "amsc_meanings": AMSC_MEANINGS}


# ---- DIBBS dates + jobs --------------------------------------------------------

_dibbs_dates: dict = {"at": 0.0, "dates": []}


@app.get("/api/dibbs/dates")
def dibbs_dates(conn: sqlite3.Connection = Depends(get_conn)):
    """Issue dates DIBBS offers right now (cached 15 min), flagged if we already have them."""
    if time.time() - _dibbs_dates["at"] > 15 * 60:
        try:
            with DibbsClient(timeout=30) as client:
                _dibbs_dates["dates"] = [d.isoformat() for d in client.list_issue_dates()]
            _dibbs_dates["at"] = time.time()
        except Exception as e:
            raise HTTPException(502, f"Couldn't reach DIBBS: {e}")
    stored = db.stored_dates(conn)
    return [{"date": d, "stored": d in stored} for d in _dibbs_dates["dates"]]


def _start(kind: str, label: str, fn) -> dict:
    try:
        return runner.start(kind, label, fn)
    except JobBusy as e:
        raise HTTPException(409, str(e))


@app.post("/api/jobs/scrape")
def start_scrape(date_: str = Query(alias="date")):
    d = date.fromisoformat(date_)

    def work(job: dict) -> dict:
        def progress(page: int, pages: int) -> None:
            job["done"], job["total"] = page, pages
            job["message"] = f"Page {page} of {pages}"

        rows = scrape.scrape(d, on_progress=progress)
        conn = db.connect()
        try:
            db.load_snapshot(conn, scrape.snapshot_path(d))
            job["message"] = "Fetching DIBBS daily files (AMSC + approved sources)…"
            try:
                with DibbsClient() as client:
                    enrich.enrich_from_daily_files(client, conn, d)
            except RuntimeError:
                pass  # daily files not published for this date
        finally:
            conn.close()
        job["message"] = f"Pulled {len(rows):,} RFQs"
        return {"date": d.isoformat(), "rows": len(rows)}

    return _start("scrape", f"Pulling RFQs issued {d:%a %b %d}", work)


def _nsns_for(f: Filters, scope: str, conn: sqlite3.Connection) -> tuple[str, list[str]]:
    if scope == "day":  # every NSN issued that day, regardless of the current filters
        f = Filters(date=f.date, kw=[], nsn=[], fsc=[], amsc=[], source=[], include_closed=True, include_past_due=True)
    issue_date, _, rows = f.run(conn, limit=None)
    return issue_date, sorted({r["nsn"] for r in rows if r["fsc"]})


@app.get("/api/enrich/estimate")
def enrich_estimate(f: Filters = Depends(), conn: sqlite3.Connection = Depends(get_conn)):
    """How many NSN lookups 'these results' and 'whole day' would need, and roughly how long."""
    out = {}
    for scope in ("filtered", "day"):
        _, nsns = _nsns_for(f, scope, conn)
        todo = len(db.nsns_needing_info(conn, nsns))
        out[scope] = {"nsns": len(nsns), "to_check": todo, "minutes": round(todo * SECONDS_PER_NSN / 60, 1)}
    return out


@app.post("/api/jobs/enrich")
def start_enrich(f: Filters = Depends(), scope: str = "filtered", conn: sqlite3.Connection = Depends(get_conn)):
    issue_date, nsns = _nsns_for(f, scope, conn)
    if not issue_date:
        raise HTTPException(400, "No day selected")
    d = date.fromisoformat(issue_date)

    def work(job: dict) -> dict:
        job["total"] = len(nsns)

        def progress(i: int, n: int) -> None:
            job["done"], job["total"] = i, n
            job["message"] = f"Checked {i:,} of {n:,} NSNs"

        result = enrich.run(d, nsns, on_progress=progress)
        job["message"] = f"Checked {result['nsn_pages']['checked']:,} NSNs" + (
            f" ({result['nsn_pages']['errors']} failed; re-run to retry)" if result["nsn_pages"]["errors"] else "")
        return result

    todo = len(db.nsns_needing_info(conn, nsns))
    label = "Checking approved sources & AMSC for " + (
        f"the whole day ({todo:,} NSNs)" if scope == "day" else f"{todo:,} NSNs")
    return _start("enrich", label, work)


@app.get("/api/jobs/current")
def current_job():
    return runner.current


# ---- UI ------------------------------------------------------------------------

app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")
