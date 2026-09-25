"""Find and download ERCOT's public data files. No credentials are needed.

- RT and DA settlement point prices: ERCOT MIS annual zips. The document list
  (IceDocListJsonWS?reportTypeId=13061 for RT, 13060 for DA) has one document
  per year, named e.g. RTMLZHBSPP_2025. The latest PublishDate wins.
- Native load: the yearly Native_Load_{year}.zip linked from
  ercot.com/gridinfo/load/load_hist.
- Fuel mix: IntGenbyFuel{year}.xlsx linked from ercot.com/gridinfo/generation.
  The link's file name changes from year to year (2026's is
  IntGenbyFuel2026-1-.xlsx), so it is scraped, never hard-coded. Past years sit
  in FuelMixReport_PreviousYears.zip ("Fuel Mix Report: 2007 - 2024").
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

MIS_DOC_LIST = "https://www.ercot.com/misapp/servlets/IceDocListJsonWS?reportTypeId={report_type_id}"
MIS_DOWNLOAD = "https://www.ercot.com/misdownload/servlets/mirDownload?doclookupId={doc_id}"
LOAD_HIST_PAGE = "https://www.ercot.com/gridinfo/load/load_hist"
GENERATION_PAGE = "https://www.ercot.com/gridinfo/generation"
REPORT_TYPE_IDS = {"rt_spp": 13061, "da_spp": 13060}
USER_AGENT = "fleet-sim-ingest/1.0 (+ERCOT public data; hackathon research)"
TIMEOUT = (30, 300)  # connect, read seconds


class SourceError(RuntimeError):
    """A source file could not be found or downloaded."""


@dataclass(frozen=True)
class RemoteFile:
    """One published file. `member` is a regex for the file to read inside a multi-year zip."""

    dataset: str
    year: int
    url: str
    name: str
    published_at: datetime | None = None
    member: str | None = None


@dataclass(frozen=True)
class Downloaded:
    remote: RemoteFile
    path: Path
    bytes: int
    sha256: str
    seconds: float
    last_modified: datetime | None
    cached: bool = False

    @property
    def published_at(self) -> datetime | None:
        return self.remote.published_at or self.last_modified


def session() -> requests.Session:
    """HTTP session with retries (connect errors, 429 and 5xx) and a descriptive User-Agent."""
    s = requests.Session()
    retry = Retry(total=4, backoff_factor=2.0, status_forcelist=(429, 500, 502, 503, 504), allowed_methods=("GET",))
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.headers["User-Agent"] = USER_AGENT
    return s


# --- discovery -----------------------------------------------------------------


def mis_annual(dataset: str, year: int, http: requests.Session | None = None) -> RemoteFile:
    """The MIS annual zip for rt_spp or da_spp in `year` (latest publication)."""
    http = http or session()
    report_type_id = REPORT_TYPE_IDS[dataset]
    r = http.get(MIS_DOC_LIST.format(report_type_id=report_type_id), timeout=TIMEOUT)
    r.raise_for_status()
    try:
        docs = [d["Document"] for d in r.json()["ListDocsByRptTypeRes"]["DocumentList"]]
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise SourceError(f"unexpected MIS document list for report {report_type_id}") from exc
    matches = [d for d in docs if str(d.get("FriendlyName", "")).endswith(f"_{year}")]
    if not matches:
        names = sorted(str(d.get("FriendlyName")) for d in docs)
        raise SourceError(f"no {dataset} document for {year} in MIS report {report_type_id}; have {names}")
    doc = max(matches, key=lambda d: datetime.fromisoformat(d["PublishDate"]))
    return RemoteFile(
        dataset=dataset,
        year=year,
        url=MIS_DOWNLOAD.format(doc_id=doc["DocID"]),
        name=doc["ConstructedName"],
        published_at=datetime.fromisoformat(doc["PublishDate"]),
    )


class _Links(HTMLParser):
    """Collects (href, title, text) of every <a> on a page."""

    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str, str]] = []
        self._open: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            a = dict(attrs)
            self._open = [a.get("href") or "", a.get("title") or "", ""]

    def handle_data(self, data):
        if self._open is not None:
            self._open[2] += data

    def handle_endtag(self, tag):
        if tag == "a" and self._open is not None:
            href, title, text = self._open
            if href:
                self.links.append((href, title.strip(), " ".join(text.split())))
            self._open = None


def page_links(url: str, http: requests.Session | None = None) -> list[tuple[str, str, str]]:
    """Absolute (href, title, text) of every link on an ercot.com page."""
    http = http or session()
    r = http.get(url, timeout=TIMEOUT)
    r.raise_for_status()
    parser = _Links()
    parser.feed(r.text)
    return [(urljoin(url, href), title, text) for href, title, text in parser.links]


def _basename(url: str) -> str:
    return url.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]


def native_load_file(year: int, http: requests.Session | None = None,
                     links: list[tuple[str, str, str]] | None = None) -> RemoteFile:
    """Native_Load_{year}.zip from the load_hist page."""
    links = links if links is not None else page_links(LOAD_HIST_PAGE, http)
    rx = re.compile(rf"^native_load_{year}\.(zip|xlsx?)$", re.IGNORECASE)
    for href, _title, _text in links:
        if rx.match(_basename(href)):
            return RemoteFile("native_load", year, href, _basename(href))
    raise SourceError(f"no Native_Load_{year} link on {LOAD_HIST_PAGE}")


def fuel_mix_file(year: int, http: requests.Session | None = None,
                  links: list[tuple[str, str, str]] | None = None) -> RemoteFile:
    """IntGenbyFuel{year} from the generation page, or its member of FuelMixReport_PreviousYears.zip."""
    links = links if links is not None else page_links(GENERATION_PAGE, http)
    direct = re.compile(rf"^intgenbyfuel{year}(?!\d)[^/]*\.xlsx?$", re.IGNORECASE)
    for href, _title, _text in links:
        if direct.match(_basename(href)):
            return RemoteFile("fuel_mix", year, href, _basename(href))
    for href, title, text in links:
        if re.match(r"^fuelmixreport_previousyears.*\.zip$", _basename(href), re.IGNORECASE):
            span = re.search(r"(\d{4})\s*-\s*(\d{4})", f"{title} {text}")
            if span and not int(span.group(1)) <= year <= int(span.group(2)):
                continue
            return RemoteFile("fuel_mix", year, href, _basename(href), member=rf"^intgenbyfuel{year}\.xlsx?$")
    raise SourceError(f"no fuel mix workbook for {year} on {GENERATION_PAGE}")


def find(dataset: str, year: int, http: requests.Session | None = None, pages: dict | None = None) -> RemoteFile:
    """Locate the file for (dataset, year). `pages` caches scraped link lists across calls."""
    pages = pages if pages is not None else {}
    if dataset in REPORT_TYPE_IDS:
        return mis_annual(dataset, year, http)
    if dataset == "native_load":
        if LOAD_HIST_PAGE not in pages:
            pages[LOAD_HIST_PAGE] = page_links(LOAD_HIST_PAGE, http)
        return native_load_file(year, links=pages[LOAD_HIST_PAGE])
    if dataset == "fuel_mix":
        if GENERATION_PAGE not in pages:
            pages[GENERATION_PAGE] = page_links(GENERATION_PAGE, http)
        return fuel_mix_file(year, links=pages[GENERATION_PAGE])
    raise ValueError(f"unknown dataset {dataset!r}")


# --- download ------------------------------------------------------------------


def download(remote: RemoteFile, dest_dir: Path, http: requests.Session | None = None,
             reuse: bool = False) -> Downloaded:
    """Stream `remote` into dest_dir/<name>. With reuse=True (dev cache only) an existing file is kept."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / remote.name
    if reuse and path.exists() and path.stat().st_size > 0:
        return Downloaded(remote, path, path.stat().st_size, _sha256(path), 0.0, None, cached=True)
    http = http or session()
    t0 = time.perf_counter()
    digest = hashlib.sha256()
    size = 0
    tmp = path.with_suffix(path.suffix + ".part")
    with http.get(remote.url, timeout=TIMEOUT, stream=True) as r:
        r.raise_for_status()
        with tmp.open("wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                f.write(chunk)
                digest.update(chunk)
                size += len(chunk)
        last_modified = r.headers.get("Last-Modified")
    if size == 0:
        raise SourceError(f"empty download from {remote.url}")
    tmp.replace(path)
    modified = parsedate_to_datetime(last_modified) if last_modified else None
    return Downloaded(remote, path, size, digest.hexdigest(), time.perf_counter() - t0, modified)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()
