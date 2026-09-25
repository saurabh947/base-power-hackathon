"""Parse ERCOT annual RT (NP6-785-ER) / DA (NP4-180-ER) zips directly, keeping
Settlement Point Type so LZ vs LZEW aren't conflated. DST via gridstatus.parse_doc."""
import zipfile, time, sys, pandas as pd, gridstatus
D = "data"
KEEP = ["HB_NORTH", "HB_HOUSTON", "HB_WEST", "HB_SOUTH", "HB_BUSAVG", "LZ_HOUSTON", "LZ_NORTH"]
e = gridstatus.Ercot()

def load(path):
    z = zipfile.ZipFile(path); f = z.open(z.namelist()[0])
    return pd.concat(pd.read_excel(f, sheet_name=None, engine="calamine").values(), ignore_index=True)

def clean(path, market, year):
    t0 = time.time()
    raw = load(path)
    raw = raw.rename(columns={"Settlement Point Name": "location", "Settlement Point": "location",
                              "Settlement Point Type": "sp_type", "Settlement Point Price": "price_usd_mwh"})
    if "sp_type" not in raw: raw["sp_type"] = None
    raw = raw[raw["sp_type"] != "LZEW"].copy()          # drop energy-weighted LZ duplicates (RT only)
    extra = raw[["location", "sp_type", "price_usd_mwh"]]
    parsed = e.parse_doc(raw.drop(columns=["location", "sp_type", "price_usd_mwh"]).copy())
    # parse_doc sorts by Time; realign by index
    out = pd.DataFrame({
        "ts": parsed["Interval Start"].dt.tz_convert("UTC"),
        "interval_start_local": parsed["Interval Start"],
        "location": extra.loc[parsed.index, "location"].astype("string"),
        "sp_type": extra.loc[parsed.index, "sp_type"].astype("string"),
        "market": market,
        "price_usd_mwh": extra.loc[parsed.index, "price_usd_mwh"].astype(float),
    }).sort_values(["location", "ts"]).reset_index(drop=True)
    dup = out.duplicated(["ts", "location"]).sum()
    assert dup == 0, dup
    out.to_parquet(f"{D}/ercot_{market.lower()}_{year}_all_locations_clean.parquet", index=False)
    sub = out[out.location.isin(KEEP)]
    sub.to_parquet(f"{D}/ercot_{market.lower()}_{year}_hubs.parquet", index=False)
    print(f"{market} {year}: {len(out)} rows, {out.location.nunique()} locs, {time.time()-t0:.1f}s; per-loc rows: {sub.groupby('location').size().to_dict()}")
    return out

for y in (2024, 2025):
    clean(f"raw/RTMLZHBSPP_{y}.zip", "RT", y)
    clean(f"raw/DAMLZHBSPP_{y}.zip", "DA", y)
