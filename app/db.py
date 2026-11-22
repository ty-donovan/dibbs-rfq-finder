"""SQLite store.

Sources of truth are flat files: data/snapshot-<date>.json (RFQs) and data/nsn_info.jsonl (AMSC +
approved sources per NSN). The DB is rebuilt from them at startup and kept in sync as jobs run.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from app.codes import AMSC_GROUPS, amsc_group

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DB_PATH = DATA_DIR / "dibbs.db"
NSN_INFO_PATH = DATA_DIR / "nsn_info.jsonl"
_jsonl_lock = threading.Lock()

COLUMNS = [
    "solicitation_number", "nsn", "fsc", "nomenclature", "quantity", "pr_number", "status",
    "set_aside", "fast_award", "mil_spec", "issue_date", "return_by_date", "package_url",
    "solicitation_url", "nsn_url", "tech_docs_url", "scraped_at",
]

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS solicitations (
    {", ".join(COLUMNS)},
    PRIMARY KEY (solicitation_number, nsn, pr_number)
);
CREATE INDEX IF NOT EXISTS ix_issue ON solicitations(issue_date);
CREATE INDEX IF NOT EXISTS ix_fsc ON solicitations(fsc);
CREATE INDEX IF NOT EXISTS ix_nsn ON solicitations(nsn);
CREATE TABLE IF NOT EXISTS nsn_info (
    nsn TEXT PRIMARY KEY,
    amsc TEXT,
    sources TEXT,          -- JSON list of objects: cage, part_number, company
    origin TEXT,           -- 'daily_file' | 'nsn_page'
    checked_at TEXT,
    error TEXT
);
"""

SORTABLE = {
    "return_by": "s.return_by_date", "solicitation": "s.solicitation_number", "nsn": "s.nsn",
    "nomenclature": "s.nomenclature", "fsc": "s.fsc", "qty": "s.quantity", "set_aside": "s.set_aside",
    "amsc": "COALESCE(i.amsc, '~')",  # unchecked NSNs sort after every AMSC letter
}

SET_ASIDE_ANY = "Any set-aside"


def connect() -> sqlite3.Connection:
    """One connection per request/job; WAL lets job threads write while requests read."""
    DATA_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


# ---- loading ---------------------------------------------------------------

def load_snapshot(conn: sqlite3.Connection, path: Path) -> int:
    rows = json.loads(path.read_text(encoding="utf-8"))
    conn.execute("DELETE FROM solicitations WHERE issue_date = ?", (path.stem.removeprefix("snapshot-"),))
    conn.executemany(
        f"INSERT OR REPLACE INTO solicitations ({', '.join(COLUMNS)}) VALUES ({', '.join('?' * len(COLUMNS))})",
        [tuple(r.get(c) for c in COLUMNS) for r in rows],
    )
    conn.commit()
    return len(rows)


def load_all(conn: sqlite3.Connection) -> None:
    for path in sorted(DATA_DIR.glob("snapshot-*.json")):
        load_snapshot(conn, path)
    if NSN_INFO_PATH.exists():
        records = [json.loads(line) for line in NSN_INFO_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
        latest = {r["nsn"]: r for r in records}  # later lines win
        _upsert_nsn_info(conn, list(latest.values()))
        if len(latest) < len(records):  # compact: one line per NSN
            with _jsonl_lock:
                NSN_INFO_PATH.write_text("".join(json.dumps(r) + "\n" for r in latest.values()), encoding="utf-8")


def stored_dates(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT DISTINCT issue_date FROM solicitations")}


# ---- NSN info (AMSC + approved sources) -------------------------------------

def save_nsn_info(conn: sqlite3.Connection, records: list[dict]) -> None:
    """records: {nsn, amsc, sources, origin, error?}. Persisted to the JSONL file and the DB."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    records = [r | {"checked_at": r.get("checked_at") or now} for r in records]
    with _jsonl_lock, NSN_INFO_PATH.open("a", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    _upsert_nsn_info(conn, records)


def _upsert_nsn_info(conn: sqlite3.Connection, records: list[dict]) -> None:
    conn.executemany(
        "INSERT OR REPLACE INTO nsn_info (nsn, amsc, sources, origin, checked_at, error) VALUES (?, ?, ?, ?, ?, ?)",
        [(r["nsn"], r.get("amsc"), json.dumps(r.get("sources") or []), r.get("origin"), r.get("checked_at"), r.get("error"))
         for r in records],
    )
    conn.commit()


def nsns_needing_info(conn: sqlite3.Connection, nsns: list[str]) -> list[str]:
    """NSNs with no successful lookup yet (errors are retried)."""
    checked = {r[0] for r in conn.execute("SELECT nsn FROM nsn_info WHERE error IS NULL")}
    return sorted({n for n in nsns if n not in checked})


# ---- search ----------------------------------------------------------------

def search(
    conn: sqlite3.Connection,
    *,
    issue_date: str | None = None,
    keywords: list[str] = (),
    nsns: list[str] = (),
    fscs: list[str] = (),
    set_aside: str | None = None,
    open_only: bool = True,
    include_past_due: bool = False,
    amsc_groups: list[str] = (),
    sources: list[str] = (),
    sort: str = "return_by",
    desc: bool = False,
    limit: int | None = 100,
    offset: int = 0,
) -> tuple[int, list[dict]]:
    """Filters AND together; multiple values within one filter OR together."""
    where, params = [], []
    if issue_date:
        where.append("s.issue_date = ?")
        params.append(issue_date)
    _any_of(where, params, "s.nomenclature LIKE ?", [f"%{k.strip()}%" for k in keywords if k.strip()])
    # NSNs: dashes optional; a partial (e.g. a NIIN) matches anywhere in the 13 digits.
    _any_of(where, params, "s.nsn LIKE ?", [f"%{re.sub(r'[^0-9A-Za-z]', '', n)}%" for n in nsns if n.strip()])
    # FSC: a 4-digit class, or a 2-digit group (e.g. "53" = hardware & abrasives).
    _any_of(where, params, "s.fsc LIKE ?", [f"{f.strip()}%" for f in fscs if f.strip()])
    if set_aside == SET_ASIDE_ANY:
        where.append("s.set_aside IS NOT NULL AND s.set_aside != 'Unrestricted'")
    elif set_aside:
        where.append("s.set_aside = ?")
        params.append(set_aside)
    if open_only:
        where.append("s.status = 'Open'")
    if not include_past_due:
        # Return-by is the quote deadline; due-today RFQs stay visible.
        where.append("(s.return_by_date IS NULL OR s.return_by_date >= date('now', 'localtime'))")
    if amsc_groups:
        ors = []
        for g in amsc_groups:
            if g == "unchecked":
                ors.append("(i.nsn IS NULL OR i.error IS NOT NULL)")
            elif g in AMSC_GROUPS:
                letters = AMSC_GROUPS[g]["letters"]
                ors.append(f"i.amsc IN ({', '.join('?' * len(letters))})")
                params.extend(letters)
            elif g == "not_listed":
                ors.append("(i.nsn IS NOT NULL AND i.error IS NULL AND i.amsc IS NULL)")
        if ors:
            where.append("(" + " OR ".join(ors) + ")")
    # Approved source: CAGE, part number, or company name (matched inside the JSON list).
    _any_of(where, params, "i.sources LIKE ?", [f"%{s.strip()}%" for s in sources if s.strip()])

    joins = "FROM solicitations s LEFT JOIN nsn_info i ON i.nsn = s.nsn"
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    total = conn.execute(f"SELECT COUNT(*) {joins} {clause}", params).fetchone()[0]
    order = f"{SORTABLE.get(sort, 's.return_by_date')} {'DESC' if desc else 'ASC'}, s.solicitation_number"
    page = "LIMIT ? OFFSET ?" if limit is not None else ""
    rows = conn.execute(
        f"SELECT s.*, i.amsc, i.sources, i.origin AS info_origin, i.checked_at AS info_checked_at, i.error AS info_error "
        f"{joins} {clause} ORDER BY {order} {page}",
        [*params, *([limit, offset] if limit is not None else [])],
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["sources"] = json.loads(d["sources"]) if d["sources"] else []
        d["info_checked"] = d["info_checked_at"] is not None and d["info_error"] is None
        d["amsc_group"] = amsc_group(d["amsc"])
        out.append(d)
    return total, out


def _any_of(where: list, params: list, expr: str, values: list[str]) -> None:
    if values:
        where.append("(" + " OR ".join([expr] * len(values)) + ")")
        params.extend(values)


def facets(conn: sqlite3.Connection, issue_date: str | None) -> dict:
    """Counts that help a vendor orient: which dates exist, where the day's volume is, AMSC mix."""
    dates = [dict(r) for r in conn.execute(
        "SELECT issue_date AS date, COUNT(*) AS count, MAX(scraped_at) AS scraped_at "
        "FROM solicitations GROUP BY issue_date ORDER BY issue_date DESC")]
    args = [issue_date] if issue_date else []
    day = "s.issue_date = ?" if issue_date else "1=1"
    fscs = [dict(r) for r in conn.execute(
        f"SELECT s.fsc, COUNT(*) AS count FROM solicitations s WHERE {day} AND s.fsc IS NOT NULL "
        "AND s.status = 'Open' GROUP BY s.fsc ORDER BY count DESC", args)]
    set_asides = [dict(r) for r in conn.execute(
        f"SELECT s.set_aside, COUNT(*) AS count FROM solicitations s WHERE {day} GROUP BY s.set_aside ORDER BY count DESC", args)]
    amsc_counts: dict[str, int] = {}
    for r in conn.execute(
        f"SELECT i.amsc, i.error, i.nsn IS NOT NULL AS has_info, COUNT(*) AS n FROM solicitations s "
        f"LEFT JOIN nsn_info i ON i.nsn = s.nsn WHERE {day} AND s.fsc IS NOT NULL GROUP BY 1, 2, 3", args):
        key = "unchecked" if not r["has_info"] or r["error"] else (amsc_group(r["amsc"]) or "not_listed")
        amsc_counts[key] = amsc_counts.get(key, 0) + r["n"]
    return {"dates": dates, "fscs": fscs, "set_asides": set_asides, "amsc": amsc_counts}
