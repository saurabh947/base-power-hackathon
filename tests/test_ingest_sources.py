"""Source discovery and download (src/ingest/sources.py), offline.

HTTP is replaced by a fake session serving SYNTHETIC pages, document lists and
bytes shaped like ercot.com's. The live endpoints are exercised by
`python -m src.ingest.ercot` itself.
"""
from __future__ import annotations

import hashlib
import json

import pytest

from src.ingest import sources
from src.ingest.sources import SourceError

GENERATION_HTML = """
<a download href="https://www.ercot.com/files/docs/2026/02/09/IntGenbyFuel2026-1-.xlsx" title="Fuel Mix Report: 2026">Fuel Mix Report: 2026</a>
<a download href="/files/docs/2025/02/07/IntGenbyFuel2025.xlsx" title="Fuel Mix Report: 2025" class="d-block">Fuel Mix Report: 2025</a>
<a download href="https://www.ercot.com/files/docs/2021/03/10/FuelMixReport_PreviousYears.zip"
   title="Fuel Mix Report: 2007 - 2024">Fuel Mix Report: 2007 - 2024</a>
<a href="https://www.ercot.com/files/docs/2014/01/16/resource_mod.zip">Other</a>
"""
LOAD_HTML = """
<a download href="https://www.ercot.com/files/docs/2025/02/11/Native_Load_2025.zip" title="2025 ERCOT Hourly Load Data">2025</a>
<a download href="https://www.ercot.com/files/docs/2019/01/07/native_load_2018.zip">2018</a>
"""


class FakeResponse:
    def __init__(self, body: bytes, headers=None):
        self.body, self.headers = body, headers or {}

    @property
    def text(self):
        return self.body.decode()

    def json(self):
        return json.loads(self.body)

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size=1):
        for i in range(0, len(self.body), 3):
            yield self.body[i:i + 3]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeSession:
    def __init__(self, routes: dict[str, bytes], headers=None):
        self.routes, self.headers, self.calls = routes, headers or {}, []

    def get(self, url, timeout=None, stream=False):
        self.calls.append(url)
        return FakeResponse(self.routes[url], self.headers)


def links(html: str, base: str):
    return sources.page_links(base, FakeSession({base: html.encode()}))


def test_page_links_resolve_relative_hrefs_and_keep_titles():
    got = links(GENERATION_HTML, sources.GENERATION_PAGE)
    assert ("https://www.ercot.com/files/docs/2025/02/07/IntGenbyFuel2025.xlsx", "Fuel Mix Report: 2025",
            "Fuel Mix Report: 2025") in got
    assert len(got) == 4


def test_fuel_mix_prefers_the_direct_workbook_even_with_an_odd_file_name():
    ls = links(GENERATION_HTML, sources.GENERATION_PAGE)
    r25 = sources.fuel_mix_file(2025, links=ls)
    assert r25.url.endswith("/IntGenbyFuel2025.xlsx") and r25.member is None and r25.name == "IntGenbyFuel2025.xlsx"
    assert sources.fuel_mix_file(2026, links=ls).name == "IntGenbyFuel2026-1-.xlsx"


def test_fuel_mix_past_year_comes_from_the_previous_years_zip():
    r = sources.fuel_mix_file(2024, links=links(GENERATION_HTML, sources.GENERATION_PAGE))
    assert r.name == "FuelMixReport_PreviousYears.zip" and r.member == r"^intgenbyfuel2024\.xlsx?$"


def test_fuel_mix_year_outside_every_link_raises():
    with pytest.raises(SourceError):
        sources.fuel_mix_file(2027, links=links(GENERATION_HTML, sources.GENERATION_PAGE))


def test_native_load_link_is_matched_case_insensitively():
    ls = links(LOAD_HTML, sources.LOAD_HIST_PAGE)
    assert sources.native_load_file(2025, links=ls).name == "Native_Load_2025.zip"
    assert sources.native_load_file(2018, links=ls).name == "native_load_2018.zip"
    with pytest.raises(SourceError):
        sources.native_load_file(2024, links=ls)


def _doc(name, doc_id, published):
    return {"Document": {"FriendlyName": name, "DocID": str(doc_id), "PublishDate": published,
                         "ConstructedName": f"rpt.00013061.{doc_id}.{name}.zip"}}


def test_mis_annual_picks_the_latest_publication_of_the_year():
    listing = {"ListDocsByRptTypeRes": {"DocumentList": [
        _doc("RTMLZHBSPP_2025", 1, "2026-01-01T13:25:58-05:00"),
        _doc("RTMLZHBSPP_2025", 2, "2026-01-02T08:00:00-06:00"),
        _doc("RTMLZHBSPP_2024", 3, "2025-01-01T08:18:28-05:00"),
    ]}}
    http = FakeSession({sources.MIS_DOC_LIST.format(report_type_id=13061): json.dumps(listing).encode()})
    r = sources.mis_annual("rt_spp", 2025, http)
    assert r.url == sources.MIS_DOWNLOAD.format(doc_id=2) and r.name.endswith("RTMLZHBSPP_2025.zip")
    assert r.published_at.isoformat() == "2026-01-02T08:00:00-06:00"
    with pytest.raises(SourceError, match="no rt_spp document for 2023"):
        sources.mis_annual("rt_spp", 2023, http)


def test_download_streams_hashes_and_reuses_the_dev_cache(tmp_path):
    body = b"PK synthetic zip bytes"
    remote = sources.RemoteFile("native_load", 2025, "https://example.test/Native_Load_2025.zip", "Native_Load_2025.zip")
    http = FakeSession({remote.url: body}, headers={"Last-Modified": "Wed, 04 Feb 2026 22:35:00 GMT"})
    got = sources.download(remote, tmp_path, http)
    assert got.path.read_bytes() == body and got.bytes == len(body)
    assert got.sha256 == hashlib.sha256(body).hexdigest() and not got.cached
    assert got.published_at.year == 2026 and not list(tmp_path.glob("*.part"))
    again = sources.download(remote, tmp_path, http, reuse=True)
    assert again.cached and again.sha256 == got.sha256 and len(http.calls) == 1
    sources.download(remote, tmp_path, http)  # without reuse: always a fresh download (stateless job)
    assert len(http.calls) == 2


def test_empty_download_raises(tmp_path):
    remote = sources.RemoteFile("rt_spp", 2025, "https://example.test/x.zip", "x.zip")
    with pytest.raises(SourceError, match="empty"):
        sources.download(remote, tmp_path, FakeSession({remote.url: b""}))
