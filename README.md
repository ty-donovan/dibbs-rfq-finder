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

A snapshot of the RFQs issued on **2026-11-20** is committed in `data/`, along with AMSC and
approved sources for part of that day, so the app works immediately, even if DIBBS blocks your network.

**To add a day,** pick it under **Pull from DIBBS** in the header and click **Pull**. A full day
takes ~10 minutes (DIBBS serves 50 RFQs per ~8 s page), so it runs in the background with a
progress bar while you keep searching. When it finishes, the day appears under **Searching**.
The same thing from the command line: `uv run python -m app.scrape 2026-11-21`.

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

### Can I actually win it? AMSC and approved sources

Matching a part isn't enough. Many NSNs can only be bought from **approved sources**: specific
manufacturers (by CAGE code) and their part numbers. An alternate part rarely wins. The
**AMSC** code says why the government buys the way it does, and the app groups it into what
matters for a distributor:

| Group | AMSC | Meaning |
|---|---|---|
| 🟢 **Open competition** | G, Z | Full data or a commercial item. Alternates can compete. |
| 🟠 **Approved / qualified sources only** | C, B, T, S, K, M, N, V | You must quote an approved or qualified source's part. |
| ⚪ **Government lacks data or rights** | D, H, P, R, A, Q, U, Y, L | Locked to the current sources. |

The sidebar filters by these groups and by **approved source (CAGE, part number, or company)**,
so a distributor can find RFQs where a manufacturer they carry is the approved source. Each row
shows its AMSC and approved sources. In FSC 5330 (gaskets) on 11-20, 72% of NSNs were "government
lacks data" and only 12% were open competition. That's the kind of triage this enables.

**Where the data comes from.** I checked three DIBBS sources against each other:
- **Daily files** `in<yymmdd>.txt` + `bq<yymmdd>.zip` (which contains `as<yymmdd>.txt`, the
  approved sources). Two small downloads, fetched automatically with every pull, but they cover
  only the batch-quotable ~25% of a day. AMSC sits in column 134 of the index file. DLA doesn't
  document that column; I verified it against the approved-source file (AMSC G → 0% of NSNs have
  approved sources; C/B/D/R/L/Q → ~100%).
- **DIBBS NSN page**: `AMSC` plus an *Approved Source Data* table (CAGE, part number, company).
  One small page per NSN, cached, so each NSN is checked once and reused across solicitations
  and days. This covers everything the daily files miss.
- **Solicitation PDFs**: list approved sources in Section B but **don't print the AMSC**, and
  they're 20–30 pages each. Not used.

NSN pages take ~6 s each on DIBBS (fetched 4 at a time, ~1.6 s per NSN overall), so checking is
on demand: **Check approved sources & AMSC ▾** offers *These results* (minutes for a filtered
list) or *Whole day* (~75 min, in the background). On 211 checked NSNs, AMSC was missing for only 1.

### Other triage aids
- **Sorted by return-by date**, with a red "N days left" badge when ≤ 3 days remain.
- RFQs **past their return-by date** are hidden by default. Return-by is the quote deadline, and
  DIBBS removes the Quote button after it. One checkbox brings them back. (Fast Award Candidates can
  be awarded *before* return-by, so "open" isn't a guarantee.) Cancelled/removed RFQs are hidden too.
- **Busiest classes this day** lists the top FSCs with names and counts. Clicking any FSC adds it as a filter.
- **Set-aside filter** (Small Business, HUBZone, SDVOSB, 8(a)).
- **Export CSV** of the current results, including AMSC and approved sources.

Each row links to the solicitation's **DIBBS Package View** (its original page), the RFQ
**PDF**, the **NSN** page, and the **technical documents** when DLA provides them.

## How the data is pulled

`app/dibbs.py` scrapes "RFQs by Issue Date", the same view a vendor uses in a browser:

1. DIBBS sits behind a WAF that rejects non-browser clients, so requests send browser headers.
2. Every page first shows the DoD Notice & Consent banner (an ASP.NET form). `www` redirects to
   it, and `dibbs2` (file downloads) serves it at the file's own URL. The client posts it back,
   keeps the session cookie, and re-requests the original URL (the banner's `?goto=` drops query params).
3. The results grid is paged with ASP.NET `__doPostBack` postbacks (~50 rows per page). "Records
   Found" gives the page count for the progress bar.
4. Rows are parsed from the grid's labeled cells (`..._lblNsn`, `..._lblSolicitation`, ...)
   instead of column positions, which keeps parsing stable if columns move.

Flat files are the source of truth: `data/snapshot-<date>.json` (RFQs) and `data/nsn_info.jsonl`
(AMSC and approved sources per NSN). The server loads them into SQLite at startup and keeps the
DB in sync as background jobs run. Raw grid pages go to `data/raw/` (git-ignored), so
`uv run python -m app.scrape <date> --from-raw` re-parses without hitting DIBBS again.

## Tradeoffs and what I left out

- **AMC (the number) isn't published on DIBBS.** It's on the NSN's FLIS record. A FLIS lookup is
  the next step if vendors want AMC 1/2 vs 3/4/5. AMSC alone already separates "open" from "locked".
- **Whole-day checks are slow (~75 min)** because DIBBS is slow and I keep it to 4 parallel
  requests to be polite to a government site. A nightly job that checks each new day's NSNs
  (most repeat across days, and they're cached) would make this invisible.
- **One background job at a time, in-process.** That's enough for a single-user tool. A
  multi-user version would use a real queue.
- **No accounts, saved profiles, or alerts.** URL-encoded searches cover "my saved search".
  Saved line cards (FSCs + CAGEs) with an email of new matches would come next, once there's a daily job.
- **FSC names** cover the roughly 250 classes DLA solicits most. Others show only the code.

## Project layout

```
app/dibbs.py      DIBBS client: WAF headers, consent banner, grid paging, NSN page, daily files, parsers
app/scrape.py     Pull a date -> data/snapshot-<date>.json (CLI + used by the Pull job)
app/enrich.py     AMSC + approved sources: daily files, then NSN pages (CLI + used by the Check job)
app/jobs.py       Minimal one-at-a-time background job runner
app/db.py         SQLite schema, loading, search + facets
app/codes.py      AMSC groups and meanings
app/main.py       FastAPI: search, CSV, facets, DIBBS dates, jobs, static UI
app/fsc_names.py  FSC code -> name lookup
static/           Single-page UI (vanilla HTML/CSS/JS, no build step)
tests/            Parsers tested on real DIBBS responses (grid, date list, NSN page, daily files) + filters
```

```bash
uv run pytest
```
