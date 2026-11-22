"""DIBBS client: gets past the WAF + DoD consent banner and pulls RFQs by issue date."""

from __future__ import annotations

import csv
import io
import re
import zipfile
from datetime import date

import httpx
from bs4 import BeautifulSoup

BASE = "https://www.dibbs.bsm.dla.mil"
ARCHIVE = "https://dibbs2.bsm.dla.mil/Downloads/RFQ/Archive/"

# DIBBS sits behind an F5 WAF that rejects non-browser clients outright.
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def _is_consent_page(resp: httpx.Response) -> bool:
    """www.dibbs redirects to dodwarning.aspx; dibbs2 serves the banner in place at the file's own URL."""
    if "dodwarning.aspx" in str(resp.url).lower():
        return True
    return "html" in resp.headers.get("content-type", "") and "butAgree" in resp.text and "dodwarning.aspx" in resp.text


def hidden_fields(soup: BeautifulSoup) -> dict[str, str]:
    """All ASP.NET hidden inputs (__VIEWSTATE*, __EVENTVALIDATION, ...) needed to post a form back."""
    return {
        i["name"]: i.get("value", "")
        for i in soup.select("input[type=hidden]")
        if i.get("name")
    }


class DibbsClient:
    def __init__(self, timeout: float = 60.0):
        self.http = httpx.Client(
            headers=BROWSER_HEADERS, follow_redirects=True, timeout=timeout
        )

    def close(self) -> None:
        self.http.close()

    def __enter__(self) -> DibbsClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def get(self, path_or_url: str) -> httpx.Response:
        """GET a page, clicking through the DoD consent banner if DIBBS redirects us to it."""
        url = path_or_url if path_or_url.startswith("http") else BASE + path_or_url
        resp = self.http.get(url)
        if _is_consent_page(resp):
            self._accept_consent(resp)
            # The banner's ?goto= keeps only the first query param, so re-request the real URL.
            resp = self.http.get(url)
        if "Request Rejected" in resp.text[:300]:
            raise RuntimeError("DIBBS firewall rejected the request")
        return resp

    def _accept_consent(self, warning_page: httpx.Response) -> httpx.Response:
        soup = BeautifulSoup(warning_page.text, "lxml")
        form = soup.find("form")
        data = hidden_fields(soup) | {"butAgree": "OK"}
        action = httpx.URL(str(warning_page.url)).join(form["action"])
        return self.http.post(action, data=data)


    # ---- RFQs by issue date -------------------------------------------------

    def list_issue_dates(self) -> list[date]:
        """Issue dates DIBBS currently offers (roughly the last month), newest first."""
        return parse_issue_dates(BeautifulSoup(self.get("/RFQ/RfqDates.aspx?category=issue").text, "lxml"))

    def fetch_issue_date(self, d: date, raw_dir=None, on_progress=None, max_pages: int = 200) -> list[dict]:
        """All RFQs issued on `d`, following the ASP.NET GridView pager via __doPostBack.

        on_progress(page, total_pages) is called after each page (~8 s apiece on DIBBS).
        """
        resp = self.get(f"{BASE}/RFQ/RfqRecs.aspx?category=issue&TypeSrch=dt&Value={mmddyyyy(d)}")
        rows: list[dict] = []
        page, total_pages = 1, None
        while True:
            if raw_dir is not None:
                (raw_dir / f"recs_{d.isoformat()}_p{page}.html").write_text(resp.text, encoding="utf-8")
            soup = BeautifulSoup(resp.text, "lxml")
            if total_pages is None:
                count = record_count(soup)
                total_pages = max(1, -(-count // 50)) if count else None
            page_rows = parse_rfq_grid(soup)
            if page == 1 and not page_rows:
                notes = [_text(soup.find(id=i)) for i in ("ctl00_cph1_lblRecCount", "ctl00_cph1_lblErrNarr", "ctl00_cph1_lblNegResp")]
                raise RuntimeError(f"No RFQs found for {d}: {' | '.join(n for n in notes if n) or 'DIBBS returned no rows'}")
            rows.extend(page_rows)
            if on_progress:
                on_progress(page, total_pages or page)
            target = next_page_target(soup, page + 1)
            if target is None or page >= max_pages:
                break
            data = hidden_fields(soup) | {"__EVENTTARGET": target, "__EVENTARGUMENT": f"Page${page + 1}"}
            resp = self.http.post(str(resp.url), data=data)
            page += 1
        # A solicitation can carry several purchase requests for the same NSN, so PR is part of the key.
        unique = {(r["solicitation_number"], r["nsn"], r["pr_number"]): r for r in rows}
        for r in unique.values():
            r["issue_date"] = r["issue_date"] or d.isoformat()
        return list(unique.values())

    # ---- approved sources + AMSC ---------------------------------------------

    def fetch_daily_files(self, d: date) -> tuple[str, str]:
        """The day's index file (has AMSC) and approved-source file. They cover only the
        batch-quotable RFQs (~25% of a day), but cost two small downloads."""
        stamp = d.strftime("%y%m%d")
        index = self.get(f"{ARCHIVE}in{stamp}.txt")
        bq = self.get(f"{ARCHIVE}bq{stamp}.zip")
        if index.status_code != 200 or bq.status_code != 200 or bq.content[:2] != b"PK":
            raise RuntimeError(f"DIBBS daily files for {d} not available")
        with zipfile.ZipFile(io.BytesIO(bq.content)) as z:
            approved = z.read(f"as{stamp}.txt").decode("latin-1")
        return index.content.decode("latin-1"), approved

    def fetch_nsn_info(self, nsn: str) -> dict:
        """AMSC + approved sources from DIBBS's NSN page (one small page per NSN)."""
        return parse_nsn_page(BeautifulSoup(self.get(nsn_page_url(nsn)).text, "lxml"))


# ---- parsing -----------------------------------------------------------------

SOL_RE = re.compile(r"\b(SP[A-Z0-9]{4}-?\d{2}-?[A-Z]-?[A-Z0-9]{4})\b")
DATE_RE = re.compile(r"(\d{1,2})[-/](\d{1,2})[-/](\d{4})")
PAGER_RE = re.compile(r"__doPostBack\('([^']+)','Page\$(\d+)'\)")


def mmddyyyy(d: date) -> str:
    return d.strftime("%m-%d-%Y")


def digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def iso_date(text: str) -> str | None:
    m = DATE_RE.search(text or "")
    if not m:
        return None
    mm, dd, yyyy = m.groups()
    return f"{yyyy}-{int(mm):02d}-{int(dd):02d}"


def format_nsn(nsn_digits: str) -> str:
    d = nsn_digits
    return f"{d[:4]}-{d[4:6]}-{d[6:9]}-{d[9:]}" if len(d) == 13 else d


def solicitation_pdf_url(sol: str) -> str:
    """DIBBS files solicitation PDFs in a folder named after the number's last character."""
    compact = sol.replace("-", "")
    return f"https://dibbs2.bsm.dla.mil/Downloads/RFQ/{compact[-1]}/{compact}.PDF"


def nsn_page_url(nsn_digits: str) -> str:
    return f"{BASE}/RFQ/RFQNsn.aspx?value={nsn_digits}&category=issue"


def _text(el) -> str:
    return " ".join(el.get_text(" ", strip=True).split()) if el else ""


GRID_ID = "ctl00_cph1_grdRfqSearch"

# DIBBS renders set-aside type as an icon; normalize its alt text to a short label.
SET_ASIDES = [
    ("service disabled", "SDVOSB"),
    ("hubzone", "HUBZone"),
    ("8a", "8(a)"),
    ("women", "WOSB"),
    ("small business", "Small Business"),
    ("unrestricted", "Unrestricted"),
]


def _span(tr, field: str):
    """Each grid cell holds a span whose id ends in _lbl<Field> (e.g. ..._ctl03_lblNsn)."""
    return tr.find("span", id=re.compile(rf"_lbl{field}$"))


def parse_rfq_grid(soup: BeautifulSoup) -> list[dict]:
    table = soup.find("table", id=GRID_ID)
    if table is None:
        return []
    out: list[dict] = []
    for tr in table.find_all("tr", recursive=False):
        sol_span = _span(tr, "Solicitation")
        if sol_span is None:
            continue  # pager / header rows
        m = SOL_RE.search(_text(sol_span))
        if not m:
            continue
        sol = m.group(1)
        pdf_link = sol_span.find("a", href=re.compile(r"\.pdf$", re.I))
        pkg_link = sol_span.find("a", href=re.compile(r"rfqrec\.aspx", re.I))
        alts = [img.get("alt", "") for img in sol_span.find_all("img")]

        nsn_span = _span(tr, "Nsn")
        nsn_link = nsn_span.find("a", href=True) if nsn_span else None
        nsn_text = _text(nsn_link) or _text(nsn_span)
        nsn_digits = digits(nsn_text)
        is_nsn = len(nsn_digits) == 13
        tags = [_text(s) for s in nsn_span.find_all("span")] if nsn_span else []

        tech_span = _span(tr, "TechnicalDocuments")
        tech_link = tech_span.find("a", href=True) if tech_span else None

        status_span = _span(tr, "Status")
        status_inner = status_span.find("span") if status_span else None

        pr_text = _text(_span(tr, "Pr"))
        qty_m = re.search(r"qty\W*([\d,]+)", pr_text, re.I)
        pr_m = re.match(r"\s*(\w+)", pr_text)

        out.append(
            {
                "solicitation_number": sol,
                "nsn": nsn_digits if is_nsn else nsn_text,
                "fsc": nsn_digits[:4] if is_nsn else None,
                "nomenclature": _text(_span(tr, "Nomenclature")),
                "quantity": int(qty_m.group(1).replace(",", "")) if qty_m else None,
                "pr_number": pr_m.group(1) if pr_m and not pr_m.group(1).upper().startswith("QTY") else None,
                "status": _text(status_inner) or None,
                "set_aside": _set_aside(alts),
                "fast_award": any(a.lower().startswith("fast award") for a in alts),
                "mil_spec": any("mil-spec" in t.lower() for t in tags),
                "issue_date": iso_date(_text(_span(tr, "Issued"))),
                "return_by_date": iso_date(_text(_span(tr, "ReturnBy"))),
                "package_url": _abs(pkg_link["href"]) if pkg_link else f"{BASE}/rfq/rfqrec.aspx?sn={sol.replace('-', '')}",
                "solicitation_url": _abs(pdf_link["href"]) if pdf_link else solicitation_pdf_url(sol),
                "nsn_url": _abs(nsn_link["href"]) if nsn_link else (nsn_page_url(nsn_digits) if is_nsn else None),
                "tech_docs_url": tech_link["href"] if tech_link else None,
            }
        )
    return out


def _set_aside(alts: list[str]) -> str | None:
    for alt in alts:
        low = alt.lower().replace("(", "").replace(")", "")
        for needle, label in SET_ASIDES:
            if needle in low:
                return label
    return None


def _abs(href: str) -> str:
    return str(httpx.URL(BASE + "/RFQ/").join(href))


def next_page_target(soup: BeautifulSoup, wanted: int) -> str | None:
    """Return the __EVENTTARGET whose pager link asks for page `wanted` (incl. the '...' link)."""
    for a in soup.find_all("a", href=True):
        m = PAGER_RE.search(a["href"])
        if m and int(m.group(2)) == wanted:
            return m.group(1)
    return None


def parse_issue_dates(soup: BeautifulSoup) -> list[date]:
    """Dates on 'RFQs by Issue Date' (span#..._dtlDateList_ctlNN_thedate > a > MM-DD-YYYY)."""
    out = []
    for span in soup.find_all("span", id=re.compile(r"_thedate$")):
        iso = iso_date(_text(span))
        if iso:
            out.append(date.fromisoformat(iso))
    return sorted(set(out), reverse=True)


def record_count(soup: BeautifulSoup) -> int | None:
    m = re.search(r"(\d[\d,]*)", _text(soup.find(id="ctl00_cph1_lblRecCount")))
    return int(m.group(1).replace(",", "")) if m else None


AMSC_RE = re.compile(r"AMSC:\s*([A-Z])\b")


def parse_nsn_page(soup: BeautifulSoup) -> dict:
    """DIBBS NSN page: 'AMSC: X' in the header legend + the 'Approved Source Data' table."""
    m = AMSC_RE.search(_text(soup))
    sources = []
    table = soup.find("table", title="Approved Source Data")
    if table:
        for tr in table.select("tbody tr"):
            cells = {td.get("headers", [""])[0]: _text(td) for td in tr.find_all("td")}
            if cells.get("h1"):
                sources.append({"cage": cells["h1"], "part_number": cells.get("h2", ""), "company": cells.get("h3") or None})
    return {"amsc": m.group(1) if m else None, "sources": sources}


def parse_index_file(text: str) -> dict[str, str]:
    """Daily in{yymmdd}.txt, fixed width (140): solicitation 0-13, NSN 13-26, ..., AMSC at col 134.

    AMSC position isn't documented by DLA; verified against the approved-source file (AMSC G -> 0% of
    NSNs have approved sources, C/B/D/R/L/Q -> ~100%)."""
    out = {}
    for line in text.splitlines():
        if len(line) >= 135 and line[13:26].isdigit() and line[134].isalpha():
            out[line[13:26]] = line[134]
    return out


def parse_approved_sources_file(text: str) -> dict[str, list[dict]]:
    """Daily as{yymmdd}.txt inside bq{yymmdd}.zip: "NSN","CAGE","Part Number","" per approved source."""
    out: dict[str, list[dict]] = {}
    for row in csv.reader(io.StringIO(text)):
        if len(row) >= 3 and row[0]:
            out.setdefault(row[0], []).append({"cage": row[1], "part_number": row[2], "company": None})
    return out
