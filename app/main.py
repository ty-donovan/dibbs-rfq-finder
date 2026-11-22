"""FastAPI app: JSON search API + the static single-page UI.

    uv run uvicorn app.main:app --reload
"""

from __future__ import annotations

import csv
import io
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from fastapi import Depends, FastAPI, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app import db
from app.fsc_names import FSC_NAMES

STATIC = Path(__file__).resolve().parent.parent / "static"
conn = db.connect()


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.load_snapshots(conn)
    yield


app = FastAPI(title="DIBBS RFQ Finder", lifespan=lifespan)


def _latest_date() -> str | None:
    return conn.execute("SELECT MAX(issue_date) FROM solicitations").fetchone()[0]


@dataclass
class Filters:
    """Query params shared by the JSON and CSV endpoints. Repeat kw/nsn/fsc to OR values together."""

    date: str | None = None
    kw: list[str] = Query(default=[])
    nsn: list[str] = Query(default=[])
    fsc: list[str] = Query(default=[])
    set_aside: str | None = None
    include_closed: bool = False
    sort: str = "return_by"
    desc: bool = False

    def run(self, limit: int, offset: int = 0) -> tuple[str | None, int, list[dict]]:
        issue_date = self.date or _latest_date()
        total, rows = db.search(
            conn, issue_date=issue_date, keywords=self.kw, nsns=self.nsn, fscs=self.fsc,
            set_aside=self.set_aside, open_only=not self.include_closed,
            sort=self.sort, desc=self.desc, limit=limit, offset=offset,
        )
        for r in rows:
            r["fsc_name"] = FSC_NAMES.get(r["fsc"] or "")
        return issue_date, total, rows


@app.get("/api/solicitations")
def solicitations(f: Filters = Depends(), limit: int = Query(default=100, le=500), offset: int = 0):
    issue_date, total, rows = f.run(limit, offset)
    return {"date": issue_date, "total": total, "limit": limit, "offset": offset, "rows": rows}


@app.get("/api/solicitations.csv")
def solicitations_csv(f: Filters = Depends()):
    """Everything matching the current filters, for vendors who work in Excel."""
    issue_date, _, rows = f.run(limit=100_000)
    cols = ["solicitation_number", "nsn", "fsc", "fsc_name", "nomenclature", "quantity", "set_aside",
            "status", "issue_date", "return_by_date", "package_url", "solicitation_url"]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return StreamingResponse(
        iter([buf.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="dibbs-rfqs-{issue_date}.csv"'},
    )


@app.get("/api/facets")
def facets(date: str | None = None):
    issue_date = date or _latest_date()
    f = db.facets(conn, issue_date)
    for item in f["fscs"]:
        item["name"] = FSC_NAMES.get(item["fsc"])
    return {"date": issue_date, **f}


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")
