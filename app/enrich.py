"""Enrich RFQs with AMSC + approved sources (CAGE / part number / company), cached per NSN.

Two DIBBS sources, cheapest first:
1. Daily files (in/as) - 2 small downloads cover the batch-quotable ~25% of a day.
2. The NSN page - one small HTML page per remaining NSN (4 polite parallel workers).

    uv run python -m app.enrich 2026-11-20                 # daily files + every NSN that day
    uv run python -m app.enrich 2026-11-20 --fsc 5330      # daily files + NSNs in one FSC
"""

from __future__ import annotations

import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

from app import db
from app.dibbs import DibbsClient, nsn_page_url, parse_approved_sources_file, parse_index_file

WORKERS = 4
BATCH = 25


def enrich_from_daily_files(client: DibbsClient, conn: sqlite3.Connection, d: date) -> int:
    """Store AMSC + approved sources for the NSNs in the day's batch files. Returns NSNs stored."""
    index_text, approved_text = client.fetch_daily_files(d)
    amsc = parse_index_file(index_text)
    sources = parse_approved_sources_file(approved_text)
    # Skip NSNs already known (NSN-page records are richer: they include company names).
    known = {r[0] for r in conn.execute("SELECT nsn FROM nsn_info WHERE error IS NULL")}
    records = [
        {"nsn": n, "amsc": amsc.get(n), "sources": sources.get(n, []), "origin": "daily_file"}
        for n in sorted(set(amsc) | set(sources)) if n not in known
    ]
    db.save_nsn_info(conn, records)
    return len(records)


def enrich_nsns(client: DibbsClient, conn: sqlite3.Connection, nsns: list[str], on_progress=None) -> dict:
    """Fetch the DIBBS NSN page for each NSN. Failures are stored with an error and retried next run."""
    if not nsns:
        return {"checked": 0, "errors": 0, "seconds": 0.0}
    started = time.monotonic()
    client.get(nsn_page_url(nsns[0]))  # accept the consent banner once, before going parallel

    def lookup(nsn: str) -> dict:
        for attempt in range(2):
            try:
                return {"nsn": nsn, **client.fetch_nsn_info(nsn), "origin": "nsn_page"}
            except Exception as e:  # network hiccup / WAF: one retry, then record the error
                if attempt:
                    return {"nsn": nsn, "amsc": None, "sources": [], "origin": "nsn_page", "error": str(e)[:200]}
                time.sleep(2)

    done, errors, batch = 0, 0, []
    with ThreadPoolExecutor(WORKERS) as pool:
        for fut in as_completed(pool.submit(lookup, n) for n in nsns):
            rec = fut.result()
            errors += bool(rec.get("error"))
            batch.append(rec)
            done += 1
            if len(batch) >= BATCH:
                db.save_nsn_info(conn, batch)
                batch = []
            if on_progress:
                on_progress(done, len(nsns))
    db.save_nsn_info(conn, batch)
    return {"checked": done, "errors": errors, "seconds": round(time.monotonic() - started, 1)}


def run(d: date, nsns: list[str] | None, on_progress=None, include_daily_files: bool = True) -> dict:
    """Daily files for `d` first (cheap), then NSN pages for whatever is still missing.

    nsns=None means every NSN issued on `d`."""
    conn = db.connect()
    try:
        with DibbsClient() as client:
            from_files = 0
            if include_daily_files:
                try:
                    from_files = enrich_from_daily_files(client, conn, d)
                except RuntimeError:
                    pass  # files not published for this date; NSN pages still work
            if nsns is None:
                nsns = [r[0] for r in conn.execute(
                    "SELECT DISTINCT nsn FROM solicitations WHERE issue_date = ? AND fsc IS NOT NULL", (d.isoformat(),))]
            todo = db.nsns_needing_info(conn, nsns)
            stats = enrich_nsns(client, conn, todo, on_progress)
        return {"from_daily_files": from_files, "nsn_pages": stats, "requested": len(nsns)}
    finally:
        conn.close()


def main(argv: list[str]) -> None:
    d = date.fromisoformat(argv[1])
    nsns = None
    if "--fsc" in argv:
        fsc = argv[argv.index("--fsc") + 1]
        conn = db.connect()
        db.load_all(conn)
        nsns = [r[0] for r in conn.execute(
            "SELECT DISTINCT nsn FROM solicitations WHERE issue_date = ? AND fsc = ?", (d.isoformat(), fsc))]
        conn.close()
    progress = lambda i, n: print(f"\r  NSN pages: {i}/{n}", end="", flush=True)
    result = run(d, nsns, on_progress=progress)
    print("\n", result)


if __name__ == "__main__":
    main(sys.argv)
