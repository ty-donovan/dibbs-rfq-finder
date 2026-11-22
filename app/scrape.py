"""CLI: pull every RFQ issued on a date and save it as a JSON snapshot.

    uv run python -m app.scrape 2026-11-20
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup

from app.dibbs import DibbsClient, parse_rfq_grid

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def snapshot_path(d: date) -> Path:
    return DATA_DIR / f"snapshot-{d.isoformat()}.json"


def scrape(d: date, save_raw: bool = True) -> list[dict]:
    raw_dir = DATA_DIR / "raw" if save_raw else None
    if raw_dir:
        raw_dir.mkdir(parents=True, exist_ok=True)
    with DibbsClient() as client:
        rows = client.fetch_issue_date(d, raw_dir=raw_dir)
    scraped_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for r in rows:
        r["scraped_at"] = scraped_at
    DATA_DIR.mkdir(exist_ok=True)
    snapshot_path(d).write_text(json.dumps(rows, indent=1), encoding="utf-8")
    return rows


def rebuild_from_raw(d: date) -> list[dict]:
    """Re-parse previously saved grid pages (no network) - handy when the parser changes."""
    pages = sorted((DATA_DIR / "raw").glob(f"recs_{d.isoformat()}_p*.html"))
    rows: dict[tuple, dict] = {}
    for p in pages:
        for r in parse_rfq_grid(BeautifulSoup(p.read_text(encoding="utf-8"), "lxml")):
            r["issue_date"] = r["issue_date"] or d.isoformat()
            rows[(r["solicitation_number"], r["nsn"])] = r
    scraped_at = datetime.fromtimestamp(pages[-1].stat().st_mtime, timezone.utc).isoformat(timespec="seconds") if pages else None
    out = [r | {"scraped_at": scraped_at} for r in rows.values()]
    snapshot_path(d).write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(f"re-parsed {len(pages)} saved pages")
    return out


def main(argv: list[str]) -> None:
    args = [a for a in argv[1:] if not a.startswith("--")]
    d = date.fromisoformat(args[0]) if args else date.today()
    rows = rebuild_from_raw(d) if "--from-raw" in argv else scrape(d)
    print(f"{len(rows)} RFQs issued {d} -> {snapshot_path(d)}")
    for r in rows[:5]:
        print(f"  {r['solicitation_number']:<18} {r['nsn']:<15} {r['nomenclature'][:30]:<30} due {r['return_by_date']}")


if __name__ == "__main__":
    main(sys.argv)
