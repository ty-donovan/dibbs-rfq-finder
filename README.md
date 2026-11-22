# DIBBS RFQ Finder

DLA posts hundreds to thousands of RFQs to [DIBBS](https://www.dibbs.bsm.dla.mil/) every weekday
(4,399 were issued on Fri Nov 20, 2026). Almost all of them are irrelevant to any one vendor.
This tool pulls a day's RFQs and lets a vendor narrow them down to the few worth quoting, then
jump straight back to each one on DIBBS.

## Quick start

```bash
uv sync
uv run uvicorn app.main:app      # http://localhost:8000
```

A snapshot of the RFQs issued on **2026-11-20** is committed in `data/`, so the app works
immediately (and even if DIBBS blocks your network). To pull another day:

```bash
uv run python -m app.scrape 2026-11-21     # ~8 s per 50 RFQs; a full day takes ~10-12 min
```

Restart the server and the new day shows up in the date picker.

## How a vendor finds opportunities

Distributors already know what they sell. They think in **what the part is**, **which exact
parts they stock**, and **which product classes they cover**, so those are the three primary filters:

| Filter | How it works | Why |
|---|---|---|
| **Nomenclature** | Keyword chips, OR'd together (`valve`, `gasket`, `o-ring`) and highlighted in results | Fastest way to scan for the kind of item you sell |
| **NSN** | Full or partial, dashes optional, so a NIIN or a pasted NSN both work | Vendors keep a list of the NSNs they've quoted or stock |
| **FSC** | The first 4 digits of the NSN (Federal Supply Class), e.g. `5310` Nuts and Washers. Typing 2 digits matches the whole group (`53` = hardware). | How most distributors define their line card |

Different filters AND together. All filter state lives in the URL, so a vendor's search is a
bookmark they can open every morning.

Supporting touches, all aimed at triage speed:
- **Sorted by return-by date**, with a red "N days left" badge when ≤ 3 days remain.
- **Busiest classes this day** lists the top FSCs with names and counts, which shows where the
  day's volume is. Clicking any FSC (there or in the table) adds it as a filter.
- **Set-aside filter** (Small Business, HUBZone, SDVOSB, 8(a)). DIBBS shows these as tiny icons,
  but for a small business they decide whether an RFQ is worth the effort.
- Cancelled and removed RFQs are hidden by default.
- **Export CSV** of the current results, because vendors live in Excel.

Each row links to the solicitation's **DIBBS Package View** (its original page), the RFQ
**PDF**, the **NSN** history page, and the **technical documents** when DLA provides them.

## How the data is pulled

`app/dibbs.py` scrapes "RFQs by Issue Date", the same view a vendor uses in a browser:

1. DIBBS sits behind a WAF that rejects non-browser clients, so requests send browser headers.
2. Every page first redirects to the DoD Notice & Consent banner (an ASP.NET form). The client
   posts it back and keeps the session cookie. The banner's `?goto=` drops all but the first
   query parameter, so the original URL is requested again afterwards.
3. The date link is taken from DIBBS's own date list, and the results grid is paged with
   ASP.NET `__doPostBack` postbacks (~50 rows per page).
4. Rows are parsed from the grid's labeled cells (`..._lblNsn`, `..._lblSolicitation`, ...)
   instead of column positions, which keeps parsing stable if columns move.

Each pull is saved as `data/snapshot-<date>.json`, which is the source of truth. On startup the
server loads every snapshot into SQLite for filtering and sorting. Raw pages are kept in
`data/raw/` (git-ignored), so `--from-raw` can re-parse them without hitting DIBBS again.

## Tradeoffs and what I left out

- **Section B of the solicitation PDF** has the richest detail (approved sources by CAGE and
  part number, packaging, delivery). It's the obvious next step: on-demand PDF text extraction
  when a row is expanded. I left it out of the MVP because it means one PDF fetch per RFQ.
- **No "Refresh" button in the UI.** A full day takes about 10 minutes to page through, which
  belongs in a scheduled job (cron at 7am) rather than a request handler. Pulling is a CLI for now.
- **No accounts, saved profiles, or alerts.** URL-encoded searches cover "my saved search"
  without needing a backend. Email alerts for new matches would come next, once there's a daily job.
- **FSC names** cover the roughly 250 classes DLA solicits most. Others show only the code.
- **Server-side filtering in SQLite** instead of shipping all ~4,400 rows to the browser keeps
  the page fast and the filtering logic testable.

## Project layout

```
app/dibbs.py      DIBBS client: WAF headers, consent banner, grid paging, row parser
app/scrape.py     CLI: pull a date -> data/snapshot-<date>.json
app/db.py         SQLite schema, snapshot loading, search + facets
app/main.py       FastAPI: /api/solicitations, /api/solicitations.csv, /api/facets, static UI
app/fsc_names.py  FSC code -> name lookup
static/           Single-page UI (vanilla HTML/CSS/JS, no build step)
tests/            Parser tests on a real DIBBS grid sample + search filter tests
```

```bash
uv run pytest
```
