"""Parser tests (src/ingest/parse.py).

Workbooks here are SYNTHETIC: laid out like ERCOT's files, but every price,
load and MWh value is made up for the unit test (labelled SYNTHETIC) and
never reaches a database. The real-file checks at the bottom run only when
`python -m src.ingest.ercot --cache-dir data/raw` has cached the downloads.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.ingest import parse
from src.ingest.parse import ParseError

UTC = timezone.utc
SPRING, FALL, PLAIN = date(2025, 3, 9), date(2025, 11, 2), date(2025, 6, 1)
REPO = Path(__file__).resolve().parents[1]
RAW = REPO / "data" / "raw"
SNAP = REPO / "data" / "snapshot"


def utc(*args) -> pd.Timestamp:
    return pd.Timestamp(datetime(*args, tzinfo=UTC))


def hour_endings(day: date) -> list[tuple[int, str]]:
    """ERCOT hour-ending labels of a local day, with the repeated-hour flag."""
    if day == SPRING:
        return [(h, "N") for h in range(1, 25) if h != 3]  # 02:00-03:00 does not exist
    if day == FALL:
        return [(1, "N"), (2, "N"), (2, "Y")] + [(h, "N") for h in range(3, 25)]
    return [(h, "N") for h in range(1, 25)]


def synthetic_rt(day: date, points=(("LZ_HOUSTON", "LZ"), ("LZ_HOUSTON", "LZEW"), ("HB_NORTH", "HU"))) -> pd.DataFrame:
    """SYNTHETIC RT rows in the 13061 layout. Price encodes (hour, interval, pass, type) so tests can trace rows."""
    rows = []
    for he, flag in hour_endings(day):
        for interval in range(1, 5):
            for name, sp_type in points:
                price = 10.0 * he + interval + (0.5 if flag == "Y" else 0.0) + (1000.0 if sp_type == "LZEW" else 0.0)
                rows.append({"Delivery Date": day.strftime("%m/%d/%Y"), "Delivery Hour": he, "Delivery Interval": interval,
                             "Repeated Hour Flag": flag, "Settlement Point Name": name,
                             "Settlement Point Type": sp_type, "Settlement Point Price": price})
    return pd.DataFrame(rows)


def synthetic_da(day: date, points=("LZ_HOUSTON", "HB_NORTH")) -> pd.DataFrame:
    """SYNTHETIC DA rows in the 13060 layout."""
    rows = []
    for he, flag in hour_endings(day):
        for name in points:
            rows.append({"Delivery Date": day.strftime("%m/%d/%Y"), "Hour Ending": f"{he:02d}:00",
                         "Repeated Hour Flag": flag, "Settlement Point": name,
                         "Settlement Point Price": 10.0 * he + (0.5 if flag == "Y" else 0.0)})
    return pd.DataFrame(rows)


# --- localize_central -----------------------------------------------------------------


def test_localize_places_both_passes_of_the_fall_back_hour():
    naive = pd.Series(pd.to_datetime(["2025-11-02 01:15", "2025-11-02 01:15", "2025-06-01 12:00"]))
    out = parse.localize_central(naive, [False, True, False])
    assert list(out) == [utc(2025, 11, 2, 6, 15), utc(2025, 11, 2, 7, 15), utc(2025, 6, 1, 17, 0)]
    assert str(out.dtype) == "datetime64[ns, UTC]"


def test_localize_rejects_the_spring_forward_gap():
    with pytest.raises(ParseError, match="spring-forward"):
        parse.localize_central(pd.Series(pd.to_datetime(["2025-03-09 02:15"])), [False])


def test_localize_rejects_a_repeated_flag_outside_the_fall_back_hour():
    with pytest.raises(ParseError, match="not in the fall-back hour"):
        parse.localize_central(pd.Series(pd.to_datetime(["2025-11-02 03:00"])), [True])


# --- RT and DA settlement point prices ----------------------------------------------


def test_rt_fall_back_day_has_100_intervals_in_utc_order():
    out = parse.parse_spp({"Nov": synthetic_rt(FALL)}, "RT")
    h = out[out.location == "LZ_HOUSTON"]
    assert len(h) == 100 and h.ts.is_monotonic_increasing and h.ts.is_unique
    assert h.ts.iloc[0] == utc(2025, 11, 2, 5) and h.ts.iloc[-1] == utc(2025, 11, 3, 5, 45)
    by_ts = dict(zip(h.ts, h.price_usd_mwh))
    for k in range(4):  # HE02 first pass = 01:00 CDT = 06:xx UTC; repeated pass (flag Y) = 01:00 CST = 07:xx UTC
        assert by_ts[utc(2025, 11, 2, 6, 15 * k)] == 20.0 + k + 1
        assert by_ts[utc(2025, 11, 2, 7, 15 * k)] == 20.5 + k + 1
    assert (np.diff(h.ts.to_numpy()) == np.timedelta64(15, "m")).all()


def test_rt_spring_forward_day_has_92_intervals_and_no_utc_gap():
    h = parse.parse_spp({"Mar": synthetic_rt(SPRING)}, "RT").query("location == 'LZ_HOUSTON'")
    assert len(h) == 92
    assert h.ts.iloc[0] == utc(2025, 3, 9, 6) and h.ts.iloc[-1] == utc(2025, 3, 10, 4, 45)
    assert (np.diff(h.ts.to_numpy()) == np.timedelta64(15, "m")).all()


def test_rt_drops_energy_weighted_lzew_rows_and_keeps_every_point():
    out = parse.parse_spp({"Jun": synthetic_rt(PLAIN)}, "RT")
    assert set(out.location) == {"LZ_HOUSTON", "HB_NORTH"}
    assert (out.groupby("location").size() == 96).all()
    lz = out[out.location == "LZ_HOUSTON"]
    assert (lz.sp_type == "LZ").all() and lz.price_usd_mwh.max() < 1000  # no LZEW price leaked in
    assert list(out.columns) == ["ts", "location", "sp_type", "market", "price_usd_mwh"]
    assert str(out.ts.dtype) == "datetime64[ns, UTC]" and (out.market == "RT").all()


def test_rt_sheets_are_concatenated_and_sorted_by_location_then_time():
    out = parse.parse_spp({"Nov": synthetic_rt(FALL), "Jun": synthetic_rt(PLAIN)}, "RT")
    for _, g in out.groupby("location"):
        assert g.ts.is_monotonic_increasing and len(g) == 196
    assert list(out.location) == sorted(out.location)


@pytest.mark.parametrize("mutate, message", [
    (lambda d: pd.concat([d, d.iloc[[0]]]), "duplicate"),
    (lambda d: d.assign(**{"Repeated Hour Flag": np.where(d["Delivery Hour"] == 5, "Y", d["Repeated Hour Flag"])}),
     "not in the fall-back hour"),
    (lambda d: d.assign(**{"Repeated Hour Flag": "X"}), "unknown Repeated Hour Flag"),
    (lambda d: d.assign(**{"Settlement Point Price": np.where(d.index == 3, np.nan, d["Settlement Point Price"])}),
     "missing or not numbers"),
    (lambda d: d.assign(**{"Delivery Interval": 5}), "Delivery Interval"),
    (lambda d: d.drop(columns=["Repeated Hour Flag"]), "missing columns"),
])
def test_rt_bad_input_raises(mutate, message):
    with pytest.raises(ParseError, match=message):
        parse.parse_spp({"Jun": mutate(synthetic_rt(PLAIN))}, "RT")


def test_rt_row_in_the_spring_forward_gap_raises():
    bad = synthetic_rt(SPRING)
    bad.loc[0, "Delivery Hour"] = 3
    with pytest.raises(ParseError, match="spring-forward"):
        parse.parse_spp({"Mar": bad}, "RT")


def test_da_hour_ending_strings_and_dst():
    fall = parse.parse_spp({"Nov": synthetic_da(FALL)}, "DA").query("location == 'LZ_HOUSTON'")
    assert len(fall) == 25 and fall.ts.is_monotonic_increasing
    by_ts = dict(zip(fall.ts, fall.price_usd_mwh))
    assert by_ts[utc(2025, 11, 2, 6)] == 20.0 and by_ts[utc(2025, 11, 2, 7)] == 20.5  # HE02, then its repeat
    assert fall.ts.iloc[-1] == utc(2025, 11, 3, 5)  # HE 24:00 starts 23:00 CST
    spring = parse.parse_spp({"Mar": synthetic_da(SPRING)}, "DA").query("location == 'LZ_HOUSTON'")
    assert len(spring) == 23 and (np.diff(spring.ts.to_numpy()) == np.timedelta64(1, "h")).all()
    assert spring.sp_type.isna().all() and (spring.market == "DA").all()


def test_bad_market_is_rejected():
    with pytest.raises(ValueError):
        parse.parse_spp({"Jun": synthetic_rt(PLAIN)}, "RTM")


# --- current-year (year-to-date) workbooks ---------------------------------------------


def _col(i: int) -> str:
    s, i = "", i + 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def tiny_xlsx(sheets: dict[str, list[list]]) -> bytes:
    """A minimal real .xlsx (zip of SpreadsheetML), so the test reads it through calamine like the ingest does.
    Strings are inline strings; numbers are plain <v> cells."""
    import io
    import zipfile
    from xml.sax.saxutils import escape

    names = list(sheets)
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", (
            '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/'
            'content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.'
            'relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/'
            'workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            + "".join(f'<Override PartName="/xl/worksheets/sheet{i + 1}.xml" ContentType="application/vnd.'
                      'openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' for i in range(len(names)))
            + "</Types>"))
        z.writestr("_rels/.rels", (
            '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/'
            f'2006/relationships"><Relationship Id="rId1" Type="{rel}/officeDocument" Target="xl/workbook.xml"/>'
            "</Relationships>"))
        z.writestr("xl/workbook.xml", (
            f'<?xml version="1.0" encoding="UTF-8"?><workbook {ns} xmlns:r="{rel}"><sheets>'
            + "".join(f'<sheet name="{escape(n)}" sheetId="{i + 1}" r:id="rId{i + 1}"/>' for i, n in enumerate(names))
            + "</sheets></workbook>"))
        z.writestr("xl/_rels/workbook.xml.rels", (
            '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/'
            '2006/relationships">'
            + "".join(f'<Relationship Id="rId{i + 1}" Type="{rel}/worksheet" Target="worksheets/sheet{i + 1}.xml"/>'
                      for i in range(len(names)))
            + "</Relationships>"))
        for i, name in enumerate(names):
            rows = []
            for r, row in enumerate(sheets[name], start=1):
                cells = "".join(
                    f'<c r="{_col(c)}{r}" t="inlineStr"><is><t>{escape(v)}</t></is></c>' if isinstance(v, str)
                    else f'<c r="{_col(c)}{r}"><v>{v!r}</v></c>' for c, v in enumerate(row))
                rows.append(f'<row r="{r}">{cells}</row>')
            z.writestr(f"xl/worksheets/sheet{i + 1}.xml",
                       f'<?xml version="1.0" encoding="UTF-8"?><worksheet {ns}><sheetData>{"".join(rows)}'
                       "</sheetData></worksheet>")
    return buf.getvalue()


RT_HEADER = ["Delivery Date", "Delivery Hour", "Delivery Interval", "Repeated Hour Flag", "Settlement Point Name",
             "Settlement Point Type", "Settlement Point Price"]


def rt_2026_ytd_workbook(last_day: date = date(2026, 9, 19)) -> bytes:
    """SYNTHETIC prices in the layout of ERCOT's 2026 RT annual file (13061) as published on 2026-09-20: a sheet per
    month, the current month filled up to the last published day, the months to come header-only."""
    filled = [RT_HEADER]
    for d in (last_day - timedelta(days=1), last_day):
        for row in synthetic_rt(d, points=(("LZ_HOUSTON", "LZ"), ("LZ_HOUSTON", "LZEW"), ("HB_NORTH", "SH"))).itertuples(index=False):
            filled.append([str(row[0]), int(row[1]), int(row[2]), str(row[3]), str(row[4]), str(row[5]), float(row[6])])
    return tiny_xlsx({"Sep": filled, "Oct": [RT_HEADER], "Nov": [RT_HEADER], "Dec": [RT_HEADER]})


def test_rt_year_to_date_workbook_skips_the_empty_months_to_come():
    """The 2026 cloud ingest failed with "unexpected Hour Ending value 1": the header-only Oct-Dec sheets read as
    object columns, so the concatenated Delivery Hour column was object dtype holding Python ints."""
    sheets = parse.read_workbook(rt_2026_ytd_workbook())
    assert [len(d) for d in sheets.values()] == [2 * 96 * 3, 0, 0, 0]
    assert pd.concat(list(sheets.values()))["Delivery Hour"].dtype == object  # the trap the fixture reproduces
    out = parse.parse_spp(sheets, "RT")
    assert set(out.location) == {"LZ_HOUSTON", "HB_NORTH"} and (out.groupby("location").size() == 192).all()
    h = out[out.location == "LZ_HOUSTON"]
    assert h.ts.iloc[0] == utc(2026, 9, 18, 5) and h.ts.iloc[-1] == utc(2026, 9, 20, 4, 45)  # CDT day ends 05:00Z
    assert (np.diff(h.ts.to_numpy()) == np.timedelta64(15, "m")).all()
    assert h.price_usd_mwh.iloc[-1] == 10.0 * 24 + 4  # HE24, interval 4, from the LZ row (not LZEW)


@pytest.mark.parametrize("hours", [
    lambda s: s.astype(object),                          # Python ints in an object column
    lambda s: s.astype(str),                             # "1".."24"
    lambda s: s.map(lambda h: f"{h:02d}"),               # "01".."24"
    lambda s: s.astype(float),                           # 1.0..24.0
])
def test_rt_hour_ending_whole_numbers_in_any_dtype(hours):
    d = synthetic_rt(PLAIN)
    ref = parse.parse_spp({"Jun": d}, "RT")
    alt = parse.parse_spp({"Jun": d.assign(**{"Delivery Hour": hours(d["Delivery Hour"]),
                                               "Delivery Interval": d["Delivery Interval"].astype(object)})}, "RT")
    pd.testing.assert_frame_equal(ref, alt)


def test_rt_fractional_hour_ending_is_rejected():
    d = synthetic_rt(PLAIN).astype({"Delivery Hour": float})
    d.loc[0, "Delivery Hour"] = 1.5
    with pytest.raises(ParseError, match="Hour Ending"):
        parse.parse_spp({"Jun": d}, "RT")


def test_spp_workbook_without_rows_raises():
    """An all-empty file must never reach the store, which would delete that year's rows as stale."""
    empty = parse.read_workbook(tiny_xlsx({"Jan": [RT_HEADER], "Feb": [RT_HEADER]}))
    with pytest.raises(ParseError, match="no price rows"):
        parse.parse_spp(empty, "RT")


# --- native load -------------------------------------------------------------------------


def synthetic_load(labels: list[str]) -> pd.DataFrame:
    """SYNTHETIC native load rows (MW values made up)."""
    n = len(labels)
    return pd.DataFrame({"Hour Ending": labels, "COAST": np.arange(n) + 100.0, "NCENT": np.arange(n) + 200.0,
                         "ERCOT": 1000.0 - 10.0 * np.arange(n)})


def test_native_load_dst_suffix_is_the_second_pass():
    out = parse.parse_native_load({"Sheet1": synthetic_load(
        ["11/02/2025 01:00", "11/02/2025 02:00", "11/02/2025 02:00 DST", "11/02/2025 03:00"])})
    assert list(out.index) == [utc(2025, 11, 2, 5), utc(2025, 11, 2, 6), utc(2025, 11, 2, 7), utc(2025, 11, 2, 8)]
    assert list(out["ERCOT"]) == [1000.0, 990.0, 980.0, 970.0]  # file order is time order
    assert list(out.columns) == ["COAST", "NCENT", "ERCOT"] and out.index.name == "ts"
    assert str(out.index.dtype) == "datetime64[ns, UTC]"


def test_native_load_hour_24_is_the_last_hour_of_the_day():
    out = parse.parse_native_load(synthetic_load(["12/31/2025 23:00", "12/31/2025 24:00", "03/09/2025 04:00"]))
    assert list(out.index) == [utc(2025, 3, 9, 8), utc(2026, 1, 1, 4), utc(2026, 1, 1, 5)]


@pytest.mark.parametrize("labels", [["11/02/2025 01:30"], ["2025-11-02 01:00"], ["03/09/2025 03:00"],
                                    ["11/02/2025 05:00 DST"]])
def test_native_load_bad_labels_raise(labels):
    with pytest.raises(ParseError):
        parse.parse_native_load(synthetic_load(labels))


def test_native_load_needs_the_ercot_total():
    with pytest.raises(ParseError, match="ERCOT"):
        parse.parse_native_load(synthetic_load(["06/01/2025 01:00"]).drop(columns=["ERCOT"]))


# --- fuel mix ------------------------------------------------------------------------------

LABELS = [f"{(m // 60) % 24}:{m % 60:02d}" for m in range(15, 24 * 60 + 1, 15)]  # "0:15" .. "23:45", "0:00"
DST_LABELS = ["01:15 (DST)", "01:30 (DST)", "01:45 (DST)", "02:00 (DST)"]


def synthetic_fuel_sheet(days: list[date], fuels=("Gas", "Wind", "WSL"), dst_columns=False) -> pd.DataFrame:
    """SYNTHETIC IntGenbyFuel month sheet: MWh per interval = f(fuel, interval index); NaN where no interval exists."""
    labels = LABELS + (DST_LABELS if dst_columns else [])
    rows = []
    for day in days:
        for f, fuel in enumerate(fuels):
            row = {"Date": pd.Timestamp(day), "Fuel": fuel, "Settlement Type": "FINAL"}
            for i, label in enumerate(labels):
                hh = int(label.split(":")[0])
                in_gap = day == SPRING and label in ("2:15", "2:30", "2:45", "3:00")
                no_dst = label in DST_LABELS and day != FALL
                row[label] = np.nan if (in_gap or no_dst) else (-(i + 1.0) if fuel == "WSL" else 100.0 * (f + 1) + i + hh / 100)
            row["Total"] = np.nansum([row[c] for c in labels])
            rows.append(row)
    return pd.DataFrame(rows)[["Date", "Fuel", "Settlement Type", "Total", *labels]]


def test_fuel_mix_fall_back_dst_columns_are_the_second_pass():
    sheet = synthetic_fuel_sheet([date(2025, 11, 1), FALL], dst_columns=True)
    out = parse.parse_fuel_mix({"Disclaimer": pd.DataFrame({"x": [1]}), "Nov": sheet})
    gas = out[out.fuel == "Gas"].set_index("ts")
    day2 = gas[(gas.index >= utc(2025, 11, 2, 5)) & (gas.index < utc(2025, 11, 3, 6))]
    assert len(day2) == 100 and len(gas) == 196
    src = sheet[(sheet.Fuel == "Gas") & (sheet.Date == pd.Timestamp(FALL))].iloc[0]
    assert day2.loc[utc(2025, 11, 2, 6, 0), "gen_mw"] == 4 * src["1:15"]  # first pass, 01:00 CDT
    assert day2.loc[utc(2025, 11, 2, 7, 0), "gen_mw"] == 4 * src["01:15 (DST)"]  # second pass, 01:00 CST
    assert day2.loc[utc(2025, 11, 2, 7, 45), "gen_mw"] == 4 * src["02:00 (DST)"]
    assert day2.loc[utc(2025, 11, 2, 8, 0), "gen_mw"] == 4 * src["2:15"]  # 02:00 CST
    first_day = gas[gas.index < utc(2025, 11, 2, 5)]
    assert first_day.index[-1] == utc(2025, 11, 2, 4, 45)  # "0:00" is 24:00: 23:45 CDT start
    assert (out.groupby("ts").size() == 3).all() and set(out.settlement_type) == {"FINAL"}
    assert out.ts.is_monotonic_increasing and str(out.ts.dtype) == "datetime64[ns, UTC]"


def test_fuel_mix_spring_forward_day_has_92_intervals():
    out = parse.parse_fuel_mix({"Mar": synthetic_fuel_sheet([SPRING])})
    assert out.ts.nunique() == 92


def test_fuel_mix_value_in_the_spring_gap_raises():
    sheet = synthetic_fuel_sheet([SPRING])
    sheet.loc[0, "2:15"] = 1.0
    sheet.loc[0, "Total"] += 1.0
    with pytest.raises(ParseError, match="spring-forward"):
        parse.parse_fuel_mix({"Mar": sheet})


def test_fuel_mix_rows_must_add_up_to_total():
    sheet = synthetic_fuel_sheet([PLAIN])
    sheet.loc[1, "Total"] += 5.0
    with pytest.raises(ParseError, match="Total"):
        parse.parse_fuel_mix({"Jun": sheet})


def test_fuel_mix_blank_future_rows_are_skipped():
    sheet = synthetic_fuel_sheet([PLAIN, date(2025, 6, 2)])
    sheet.loc[sheet.Date == pd.Timestamp(2025, 6, 2), LABELS + ["Total"]] = np.nan  # year-to-date file: no data yet
    out = parse.parse_fuel_mix({"Jun": sheet})
    assert out.ts.nunique() == 96 and out.ts.max() < utc(2025, 6, 2, 5)


def test_fuel_mix_unknown_column_raises():
    sheet = synthetic_fuel_sheet([PLAIN]).assign(Notes="x")
    with pytest.raises(ParseError, match="unexpected columns"):
        parse.parse_fuel_mix({"Jun": sheet})


def test_fuel_shares_exclude_wsl_clip_negatives_and_sum_to_one():
    idx = pd.DatetimeIndex([utc(2025, 6, 1, 5), utc(2025, 6, 1, 5, 15)], name="ts")
    mw = pd.DataFrame({"Gas": [300.0, 100.0], "Solar": [-1.0, 100.0], "WSL": [-50.0, 20.0], "Wind": [100.0, 200.0]},
                      index=idx)  # SYNTHETIC MW
    shares = parse.fuel_shares(mw)
    assert list(shares.columns) == ["Gas", "Solar", "Wind"]
    assert shares.iloc[0].tolist() == [0.75, 0.0, 0.25]
    assert np.allclose(shares.sum(axis=1), 1.0)


def test_fuel_shares_equal_the_snapshot_backend_to_the_bit_whatever_the_layout(monkeypatch):
    """Real committed snapshot data: the ingest's shares must be exactly what series.fuel_mix computes."""
    from src.data import series

    monkeypatch.setenv("SERIES_BACKEND", "snapshot")
    mw = pd.read_parquet(SNAP / "ercot_fuel_mix_15min_2025.parquet")
    long = mw.stack().rename("gen_mw").reset_index().rename(columns={"Fuel": "fuel"})
    pivot_like = parse.fuel_mw_wide(long)  # what the ingest builds: same numbers, other memory layout
    assert pivot_like._mgr.blocks[0].values.flags["F_CONTIGUOUS"] != mw._mgr.blocks[0].values.flags["F_CONTIGUOUS"]
    naive = pivot_like.drop(columns=["WSL"]).clip(lower=0)
    assert not np.array_equal(naive.sum(axis=1).to_numpy(), mw.drop(columns=["WSL"]).clip(lower=0).sum(axis=1).to_numpy())
    ref = series.fuel_mix(mw.index[0], mw.index[-1] + timedelta(minutes=15))
    for frame in (mw, pivot_like):
        mine = parse.fuel_shares(frame)
        mine.columns = [c.lower() for c in mine.columns]
        assert np.array_equal(mine.to_numpy(), ref.to_numpy()) and list(mine.columns) == list(ref.columns)


# --- real ERCOT files (only when cached in data/raw) ---------------------------------------


def _raw(pattern: str) -> Path:
    found = sorted(RAW.glob(pattern)) if RAW.is_dir() else []
    if not found:
        pytest.skip(f"no cached {pattern} in data/raw (run: python -m src.ingest.ercot --cache-dir data/raw)")
    return found[-1]


@pytest.mark.parametrize("market, year", [("RT", 2024), ("RT", 2025), ("DA", 2024), ("DA", 2025)])
def test_real_price_files_equal_the_snapshot(market, year):
    prefix = "RTMLZHBSPP" if market == "RT" else "DAMLZHBSPP"
    _, data = parse.read_zip_member(_raw(f"*{prefix}_{year}.zip"))
    out = parse.parse_spp(parse.read_workbook(data), market)
    assert out.location.nunique() == 15
    snap = pd.read_parquet(SNAP / f"ercot_{market.lower()}_{year}_hubs.parquet").sort_values(["location", "ts"])
    mine = out[out.location.isin(snap.location.unique())]
    assert len(mine) == len(snap)
    assert np.array_equal(mine.ts.to_numpy(), snap.ts.to_numpy())
    assert np.array_equal(mine.location.to_numpy(dtype=str), snap.location.to_numpy(dtype=str))
    assert np.array_equal(mine.price_usd_mwh.to_numpy(), snap.price_usd_mwh.to_numpy())


def test_real_fuel_mix_2025_equals_the_snapshot_mw():
    long = parse.parse_fuel_mix(parse.read_workbook(_raw("IntGenbyFuel2025*.xls*")))
    wide = parse.fuel_mw_wide(long)
    snap = pd.read_parquet(SNAP / "ercot_fuel_mix_15min_2025.parquet")
    assert wide.index.equals(snap.index) and list(wide.columns) == list(snap.columns)
    assert np.array_equal(wide.to_numpy(), snap.to_numpy())


def test_real_native_load_2025_matches_the_snapshot_including_its_corrected_fall_back_hour():
    """data/snapshot/README.md, correction of 2026-09-25: the snapshot's 2025-11-02 fall-back passes are in
    ERCOT's order now (they used to be swapped), so the parsed workbook equals it everywhere, to the last bit
    of the snapshot's rounding."""
    _, data = parse.read_zip_member(_raw("Native_Load_2025.zip"))
    mine = parse.parse_native_load(parse.read_workbook(data))["ERCOT"]
    snap = pd.read_parquet(SNAP / "ercot_hourly_native_load_2025.parquet")
    ref = pd.Series(snap["ERCOT"].to_numpy(), index=pd.DatetimeIndex(snap["Interval Start"]).tz_convert("UTC"))
    assert mine.index.equals(ref.index)
    a, b = utc(2025, 11, 2, 6), utc(2025, 11, 2, 7)
    assert mine[a] != mine[b]  # the two passes of the repeated hour really differ, so their order is tested
    assert np.allclose(mine.to_numpy(), ref.to_numpy(), rtol=1e-15, atol=0)


@pytest.mark.parametrize("market", ["RT", "DA"])
def test_real_2026_year_to_date_files_parse_whole_days_on_the_same_grid(market):
    """The current year's annual files (republished weekly) hold Jan 1 up to the last published day, and empty
    sheets for the months to come. Same layout as 2025, still 15-minute RT intervals after RTC+B (Dec 5, 2025)."""
    prefix = "RTMLZHBSPP" if market == "RT" else "DAMLZHBSPP"
    _, data = parse.read_zip_member(_raw(f"*{prefix}_2026.zip"))
    out = parse.parse_spp(parse.read_workbook(data), market)
    step = np.timedelta64(15 if market == "RT" else 60, "m")
    assert out.location.nunique() == 15 and out.groupby("location").size().nunique() == 1
    for _, g in out.groupby("location"):
        assert g.ts.iloc[0] == utc(2026, 1, 1, 6) and (np.diff(g.ts.to_numpy()) == step).all()
        end_local = (g.ts.iloc[-1] + pd.Timedelta(step)).tz_convert(parse.TZ)
        assert (end_local.hour, end_local.minute) == (0, 0)  # the last published day is complete
