"""SQLite store. Snapshots (data/snapshot-*.json) are the source of truth; the DB is rebuilt from them."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DB_PATH = DATA_DIR / "dibbs.db"

COLUMNS = [
    "solicitation_number", "nsn", "fsc", "nomenclature", "quantity", "pr_number", "status",
    "set_aside", "fast_award", "mil_spec", "issue_date", "return_by_date", "package_url",
    "solicitation_url", "nsn_url", "tech_docs_url", "scraped_at",
]

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS solicitations (
    {", ".join(COLUMNS)},
    PRIMARY KEY (solicitation_number, nsn)
);
CREATE INDEX IF NOT EXISTS ix_issue ON solicitations(issue_date);
CREATE INDEX IF NOT EXISTS ix_fsc ON solicitations(fsc);
"""

SORTABLE = {
    "return_by": "return_by_date", "solicitation": "solicitation_number", "nsn": "nsn",
    "nomenclature": "nomenclature", "fsc": "fsc", "qty": "quantity", "set_aside": "set_aside",
}

SET_ASIDE_ANY = "Any set-aside"


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def load_snapshots(conn: sqlite3.Connection) -> int:
    """Upsert every snapshot file into the DB. Cheap enough to run on each startup."""
    total = 0
    for path in sorted(DATA_DIR.glob("snapshot-*.json")):
        rows = json.loads(path.read_text(encoding="utf-8"))
        conn.executemany(
            f"INSERT OR REPLACE INTO solicitations ({', '.join(COLUMNS)}) "
            f"VALUES ({', '.join('?' * len(COLUMNS))})",
            [tuple(r.get(c) for c in COLUMNS) for r in rows],
        )
        total += len(rows)
    conn.commit()
    return total


def search(
    conn: sqlite3.Connection,
    *,
    issue_date: str | None = None,
    keywords: list[str] = (),
    nsns: list[str] = (),
    fscs: list[str] = (),
    set_aside: str | None = None,
    open_only: bool = True,
    sort: str = "return_by",
    desc: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> tuple[int, list[dict]]:
    """Filters AND together; multiple values within one filter OR together."""
    where, params = [], []
    if issue_date:
        where.append("issue_date = ?")
        params.append(issue_date)
    _any_of(where, params, "nomenclature LIKE ?", [f"%{k.strip()}%" for k in keywords if k.strip()])
    # NSNs: dashes optional; a partial (e.g. a NIIN) matches anywhere in the 13 digits.
    _any_of(where, params, "nsn LIKE ?", [f"%{re.sub(r'[^0-9A-Za-z]', '', n)}%" for n in nsns if n.strip()])
    # FSC: a 4-digit class, or a 2-digit group (e.g. "53" = hardware & abrasives).
    _any_of(where, params, "fsc LIKE ?", [f"{f.strip()}%" for f in fscs if f.strip()])
    if set_aside == SET_ASIDE_ANY:
        where.append("set_aside IS NOT NULL AND set_aside != 'Unrestricted'")
    elif set_aside:
        where.append("set_aside = ?")
        params.append(set_aside)
    if open_only:
        where.append("status = 'Open'")

    clause = f"WHERE {' AND '.join(where)}" if where else ""
    order_col = SORTABLE.get(sort, "return_by_date")
    order = f"{order_col} {'DESC' if desc else 'ASC'}, solicitation_number"
    total = conn.execute(f"SELECT COUNT(*) FROM solicitations {clause}", params).fetchone()[0]
    rows = conn.execute(
        f"SELECT * FROM solicitations {clause} ORDER BY {order} LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ).fetchall()
    return total, [dict(r) for r in rows]


def _any_of(where: list, params: list, expr: str, values: list[str]) -> None:
    if values:
        where.append("(" + " OR ".join([expr] * len(values)) + ")")
        params.extend(values)


def facets(conn: sqlite3.Connection, issue_date: str | None) -> dict:
    """Counts that help a vendor orient: which dates exist, where today's volume is."""
    dates = [dict(r) for r in conn.execute(
        "SELECT issue_date AS date, COUNT(*) AS count, MAX(scraped_at) AS scraped_at "
        "FROM solicitations GROUP BY issue_date ORDER BY issue_date DESC")]
    day = "WHERE issue_date = ?" if issue_date else ""
    args = [issue_date] if issue_date else []
    fscs = [dict(r) for r in conn.execute(
        f"SELECT fsc, COUNT(*) AS count FROM solicitations {day} {'AND' if day else 'WHERE'} fsc IS NOT NULL "
        "AND status = 'Open' GROUP BY fsc ORDER BY count DESC", args)]
    set_asides = [dict(r) for r in conn.execute(
        f"SELECT set_aside, COUNT(*) AS count FROM solicitations {day} GROUP BY set_aside ORDER BY count DESC", args)]
    return {"dates": dates, "fscs": fscs, "set_asides": set_asides}
