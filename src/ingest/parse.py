"""Parse ERCOT workbooks into tidy frames keyed by UTC interval START.

Pure functions: no network and no database. Every timestamp goes through
localize_central(), which places the fall-back repeated hour from an explicit
flag and raises on the spring-forward gap, so DST is never guessed. All
output timestamps are datetime64[ns, UTC], like data/snapshot.

Sources and their DST conventions (evidence in docs/notes/data.md):
- RT SPP, MIS report 13061 (annual zip, one sheet per month): Delivery Date,
  Delivery Hour (hour ENDING, 1-24), Delivery Interval (1-4), Repeated Hour
  Flag (Y on the second, CST pass of the fall-back hour). Load zones appear
  twice, as type LZ and as the energy-weighted LZEW; LZEW rows are dropped.
- DA SPP, MIS report 13060: Delivery Date, Hour Ending ("01:00".."24:00"),
  Repeated Hour Flag.
- Native load (ercot.com/gridinfo/load/load_hist yearly zip): "Hour Ending"
  = "MM/DD/YYYY HH:00". The repeated fall-back hour carries a " DST" suffix
  and is the second (CST) pass, the same convention as the MIS flag.
- Fuel mix (IntGenbyFuel{year}.xlsx): one sheet per month, one row per
  (Date, Fuel), 15-min columns labelled by interval END ("0:15".."0:00", where
  "0:00" means 24:00), values in MWh per interval. The November sheet adds
  "01:15 (DST)".."02:00 (DST)" columns: the second (CST) pass of the repeated
  hour.
"""
from __future__ import annotations

import io
import re
import zipfile
from collections.abc import Mapping
from pathlib import Path

import numpy as np
import pandas as pd

TZ = "America/Chicago"
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
FUEL_ID_COLUMNS = ("Date", "Fuel", "Settlement Type", "Total")
_FUEL_LABEL = re.compile(r"^(\d{1,2}):(\d{2})(\s*\(DST\))?$", re.IGNORECASE)
_LOAD_HOUR = re.compile(r"^(\d{1,2}/\d{1,2}/\d{4})\s+(\d{1,2}):(\d{2})$")

_SPP_COLUMNS = {
    "Delivery Date": "date",
    "Delivery Hour": "hour_ending",
    "Hour Ending": "hour_ending",
    "Delivery Interval": "interval",
    "Repeated Hour Flag": "repeated_flag",
    "DSTFlag": "repeated_flag",
    "Settlement Point Name": "location",
    "Settlement Point": "location",
    "Settlement Point Type": "sp_type",
    "Settlement Point Price": "price_usd_mwh",
}


class ParseError(ValueError):
    """A source file does not look the way the parser expects."""


# --- reading -------------------------------------------------------------------


def read_workbook(source: str | Path | bytes | io.IOBase) -> dict[str, pd.DataFrame]:
    """Every sheet of an xlsx/xls workbook (path, bytes or file object), read with calamine."""
    if isinstance(source, (bytes, bytearray)):
        source = io.BytesIO(source)
    return pd.read_excel(source, sheet_name=None, engine="calamine")


def read_zip_member(path: str | Path, pattern: str = r".*\.xlsx?$") -> tuple[str, bytes]:
    """(name, bytes) of the single zip member whose name matches `pattern` (case-insensitive)."""
    rx = re.compile(pattern, re.IGNORECASE)
    with zipfile.ZipFile(path) as z:
        names = [n for n in z.namelist() if rx.search(n.rsplit("/", 1)[-1])]
        if len(names) != 1:
            raise ParseError(f"{Path(path).name}: expected one member matching {pattern!r}, found {names}")
        return names[0], z.read(names[0])


# --- time ----------------------------------------------------------------------


def localize_central(naive: pd.Series, repeated) -> pd.Series:
    """Naive America/Chicago wall-clock interval starts -> datetime64[ns, UTC].

    `repeated` is True for rows in the SECOND pass through the fall-back hour
    (CST) and False everywhere else (the first, CDT pass and every
    unambiguous time). Raises ParseError when a time falls in the
    spring-forward gap, or when a row flagged as repeated is not in the
    fall-back hour.
    """
    naive = pd.Series(naive)
    repeated = np.asarray(repeated, dtype=bool)
    if len(repeated) != len(naive):
        raise ParseError("repeated flags do not line up with the timestamps")
    if naive.isna().any():
        raise ParseError(f"{int(naive.isna().sum())} timestamps could not be built")
    try:
        probe = naive.dt.tz_localize(TZ, ambiguous="NaT", nonexistent="raise")
    except ValueError as exc:  # pandas raises ValueError for the spring-forward gap
        raise ParseError(f"interval start in the spring-forward gap: {exc}") from exc
    ambiguous = probe.isna().to_numpy()
    bad = repeated & ~ambiguous
    if bad.any():
        raise ParseError(
            f"{int(bad.sum())} rows are flagged as the repeated hour but are not in the fall-back hour, "
            f"e.g. {naive[bad].iloc[0]}"
        )
    local = naive.dt.tz_localize(TZ, ambiguous=~repeated, nonexistent="raise")
    return local.dt.tz_convert("UTC").dt.as_unit("ns")


def _dates(values: pd.Series) -> pd.Series:
    """Delivery dates (MM/DD/YYYY strings or datetimes) -> naive midnight datetimes."""
    if pd.api.types.is_datetime64_any_dtype(values):
        out = pd.Series(values).dt.normalize()
    else:
        codes, uniques = pd.factorize(values.astype(str).str.strip())
        try:
            parsed = pd.to_datetime(pd.Index(uniques), format="%m/%d/%Y")
        except ValueError:
            parsed = pd.to_datetime(pd.Index(uniques))  # e.g. ISO dates in older files
        out = pd.Series(parsed.to_numpy()[codes], index=values.index)
    if getattr(out.dt, "tz", None) is not None:
        raise ParseError("delivery dates must be naive local dates")
    return out


def _hour_ending(values: pd.Series) -> np.ndarray:
    """Hour ending 1-24, from ints (RT "Delivery Hour") or "HH:00" strings (DA "Hour Ending")."""
    if pd.api.types.is_numeric_dtype(values):
        he = values.to_numpy()
    else:
        parts = values.astype(str).str.strip().str.extract(r"^(\d{1,2}):(\d{2})$")
        if parts.isna().any().any() or (parts[1] != "00").any():
            bad = values[parts.isna().any(axis=1) | (parts[1] != "00")].iloc[0]
            raise ParseError(f"unexpected Hour Ending value {bad!r}")
        he = parts[0].astype(int).to_numpy()
    he = he.astype(np.int64)
    if ((he < 1) | (he > 24)).any():
        raise ParseError(f"hour ending outside 1-24: {sorted(set(he[(he < 1) | (he > 24)]))[:5]}")
    return he


# --- settlement point prices (RT 13061, DA 13060) ------------------------------


def parse_spp(sheets: Mapping[str, pd.DataFrame] | pd.DataFrame, market: str) -> pd.DataFrame:
    """ERCOT annual settlement point prices -> one row per (ts, location).

    Columns: ts (UTC interval start), location, sp_type, market, price_usd_mwh.
    RT rows are native 15-min, DA rows hourly. LZEW rows are dropped. Sorted
    by (location, ts). Raises ParseError on unknown flags, DST problems,
    missing prices or duplicate (ts, location).
    """
    market = market.upper()
    if market not in ("RT", "DA"):
        raise ValueError(f"market must be RT or DA, got {market!r}")
    raw = pd.concat(list(sheets.values()), ignore_index=True) if isinstance(sheets, Mapping) else sheets.copy()
    raw = raw.rename(columns=lambda c: _SPP_COLUMNS.get(str(c).strip(), str(c).strip()))
    missing = {"date", "hour_ending", "repeated_flag", "location", "price_usd_mwh"} - set(raw.columns)
    if missing:
        raise ParseError(f"{market} workbook is missing columns {sorted(missing)}; has {list(raw.columns)}")
    if "sp_type" not in raw.columns:
        raw["sp_type"] = pd.NA
    raw = raw[raw["sp_type"].fillna("").astype(str).str.strip().str.upper() != "LZEW"].reset_index(drop=True)

    he = _hour_ending(raw["hour_ending"])
    if "interval" in raw.columns:
        interval = raw["interval"].to_numpy().astype(np.int64)
        if ((interval < 1) | (interval > 4)).any():
            raise ParseError("Delivery Interval outside 1-4")
        minutes = (he - 1) * 60 + (interval - 1) * 15
    else:
        minutes = (he - 1) * 60
    flag = raw["repeated_flag"].astype(str).str.strip().str.upper()
    unknown = sorted(set(flag) - {"N", "Y"})
    if unknown:
        raise ParseError(f"unknown Repeated Hour Flag values {unknown}")
    naive = _dates(raw["date"]) + pd.to_timedelta(minutes, unit="min")
    ts = localize_central(naive, (flag == "Y").to_numpy())

    price = pd.to_numeric(raw["price_usd_mwh"], errors="coerce").astype("float64")
    if not np.isfinite(price.to_numpy()).all():
        raise ParseError(f"{int((~np.isfinite(price.to_numpy())).sum())} {market} prices are missing or not numbers")
    out = pd.DataFrame(
        {
            "ts": ts,
            "location": raw["location"].astype(str).str.strip().astype("string"),
            "sp_type": raw["sp_type"].astype("string").str.strip(),
            "market": market,
            "price_usd_mwh": price,
        }
    )
    dup = out.duplicated(["ts", "location"])
    if dup.any():
        first = out[dup].iloc[0]
        raise ParseError(f"{int(dup.sum())} duplicate {market} (ts, location) rows, e.g. {first.ts} {first.location}")
    return out.sort_values(["location", "ts"], kind="stable").reset_index(drop=True)


# --- native load (hourly, by weather zone) -------------------------------------


def parse_native_load(frame: Mapping[str, pd.DataFrame] | pd.DataFrame) -> pd.DataFrame:
    """ERCOT yearly native load workbook -> wide hourly frame.

    Index: ts (UTC hour start, named "ts"). Columns: weather zones as
    published (COAST, EAST, FWEST, NORTH, NCENT, SOUTH, SCENT, WEST) plus the
    ERCOT total, all MW (hourly averages), float64.
    """
    if isinstance(frame, Mapping):
        if len(frame) != 1:
            raise ParseError(f"native load workbook should have one sheet, has {list(frame)}")
        frame = next(iter(frame.values()))
    df = frame.rename(columns=lambda c: str(c).strip())
    he_col = next((c for c in df.columns if c.replace("_", " ").lower() in ("hour ending", "hourending", "hour end")), None)
    if he_col is None:
        raise ParseError(f"native load workbook has no Hour Ending column: {list(df.columns)}")
    df = df[df[he_col].notna()].reset_index(drop=True)
    label = df[he_col].astype(str).str.strip()
    repeated = label.str.upper().str.endswith("DST")
    base = label.str.replace(r"\s*DST$", "", regex=True, flags=re.IGNORECASE)
    parts = base.str.extract(_LOAD_HOUR)
    if parts.isna().any().any():
        raise ParseError(f"unexpected Hour Ending value {label[parts.isna().any(axis=1)].iloc[0]!r}")
    hour = parts[1].astype(int).to_numpy()
    if (parts[2] != "00").any() or ((hour < 1) | (hour > 24)).any():
        raise ParseError("native load Hour Ending must be HH:00 with HH in 01-24")
    naive = pd.to_datetime(parts[0], format="%m/%d/%Y") + pd.to_timedelta((hour - 1) * 60, unit="min")
    ts = localize_central(naive, repeated.to_numpy())

    zones = [c for c in df.columns if c != he_col]
    out = df[zones].apply(pd.to_numeric, errors="coerce").astype("float64")
    out.columns = [c.upper() for c in zones]
    if "ERCOT" not in out.columns:
        raise ParseError(f"native load workbook has no ERCOT total column: {zones}")
    if out["ERCOT"].isna().any():
        raise ParseError(f"{int(out['ERCOT'].isna().sum())} hours have no ERCOT total")
    out.index = pd.DatetimeIndex(ts, name="ts")
    if out.index.has_duplicates:
        raise ParseError(f"duplicate native load hours, e.g. {out.index[out.index.duplicated()][0]}")
    return out.sort_index()


# --- fuel mix (15-min generation by fuel) --------------------------------------


def parse_fuel_mix(sheets: Mapping[str, pd.DataFrame]) -> pd.DataFrame:
    """IntGenbyFuel{year} workbook -> long frame: ts, fuel, gen_mw, settlement_type.

    gen_mw is the average MW over the 15-min interval: the file's MWh x 4.
    WSL (storage charging) is kept, and is negative. Checks that each row's
    interval cells, including the "(DST)" ones, add up to its Total column.
    """
    parts = []
    for name, d in sheets.items():
        if str(name).strip()[:3].title() not in MONTHS or not {"Date", "Fuel"} <= set(map(str, d.columns)):
            continue  # Disclaimer, Summary and chart-data sheets
        d = d.rename(columns=lambda c: str(c).strip())
        d = d[d["Fuel"].notna()]
        labels = [c for c in d.columns if _FUEL_LABEL.match(c)]
        unknown = [c for c in d.columns if c not in labels and c not in FUEL_ID_COLUMNS]
        if unknown:
            raise ParseError(f"fuel mix sheet {name!r} has unexpected columns {unknown}")
        values = d[labels].apply(pd.to_numeric, errors="coerce")
        has_data = values.notna().any(axis=1)  # a year-to-date workbook may carry blank future rows
        d, values = d[has_data], values[has_data]
        if "Total" in d.columns:
            total = pd.to_numeric(d["Total"], errors="coerce")
            ok = np.isclose(values.sum(axis=1, min_count=1), total, rtol=1e-6, atol=1e-3)
            if not ok.all():
                bad = d[~ok].iloc[0]
                raise ParseError(f"fuel mix sheet {name!r}: intervals do not add up to Total for {bad['Date']} {bad['Fuel']}")
        ids = d[["Date", "Fuel"]].copy()
        ids["settlement_type"] = d["Settlement Type"].astype(str).str.strip() if "Settlement Type" in d.columns else None
        long = pd.concat([ids, values], axis=1).melt(
            id_vars=["Date", "Fuel", "settlement_type"], value_vars=labels, var_name="label", value_name="mwh"
        )
        parts.append(long.dropna(subset=["mwh"]))
    if not parts:
        raise ParseError(f"no month sheets in fuel mix workbook (sheets: {list(sheets)})")
    x = pd.concat(parts, ignore_index=True)

    lab = x["label"].str.extract(_FUEL_LABEL)
    hh, mm = lab[0].astype(int).to_numpy(), lab[1].astype(int).to_numpy()
    if ((mm % 15) != 0).any() or (hh > 24).any():
        raise ParseError("fuel mix interval labels must be H:MM on the 15-minute grid")
    repeated = lab[2].notna().to_numpy()
    end = _dates(x["Date"]) + pd.to_timedelta(hh * 60 + mm, unit="min")
    end = end.where(~((hh == 0) & (mm == 0)), end + pd.Timedelta(days=1))  # "0:00" is 24:00
    ts = localize_central(end - pd.Timedelta(minutes=15), repeated)

    out = pd.DataFrame(
        {
            "ts": ts,
            "fuel": x["Fuel"].astype(str).str.strip(),
            "gen_mw": x["mwh"].astype("float64") * 4.0,
            "settlement_type": x["settlement_type"],
        }
    )
    if not np.isfinite(out["gen_mw"].to_numpy()).all():
        raise ParseError("non-finite fuel mix values")
    dup = out.duplicated(["ts", "fuel"])
    if dup.any():
        first = out[dup].iloc[0]
        raise ParseError(f"{int(dup.sum())} duplicate fuel mix (ts, fuel) rows, e.g. {first.ts} {first.fuel}")
    return out.sort_values(["ts", "fuel"], kind="stable").reset_index(drop=True)


def fuel_mw_wide(long: pd.DataFrame) -> pd.DataFrame:
    """Long fuel mix -> wide MW frame: index ts, one column per fuel (sorted), like the snapshot parquet."""
    wide = long.pivot(index="ts", columns="fuel", values="gen_mw").sort_index()
    wide = wide[sorted(wide.columns)]
    wide.columns.name = "Fuel"
    return wide


def fuel_shares(mw_wide: pd.DataFrame) -> pd.DataFrame:
    """Fractions of positive generation with WSL excluded.

    The same math as src.data.series.fuel_mix: drop WSL, clip at 0, divide
    by the row total. The total is summed column by column, left to right
    (NaN counts as 0, like pandas' skipna). That is the order pandas uses on
    the snapshot parquet's memory layout. `df.sum(axis=1)` would pick its
    order from the frame's layout, and a pivoted frame differs from it in the
    last bit, so both backends agree to the bit only because of this loop.
    Column labels stay as published (Biomass, Gas-CC, ...).
    """
    df = mw_wide.drop(columns=["WSL"], errors="ignore").clip(lower=0)
    total = np.zeros(len(df))
    for column in df.columns:
        total = total + df[column].fillna(0.0).to_numpy()
    return df.div(pd.Series(total, index=df.index), axis=0)
