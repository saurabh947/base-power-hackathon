import pandas as pd, zipfile
D="data"
gs = pd.read_parquet(f"{D}/ercot_rt_spp_2025_all_locations.parquet")
cl = pd.read_parquet(f"{D}/ercot_rt_2025_all_locations_clean.parquet")
for loc in ["HB_NORTH","HB_HOUSTON"]:
    a = gs[gs.Location==loc].set_index(gs[gs.Location==loc]["Interval Start"].dt.tz_convert("UTC"))["SPP"].sort_index()
    b = cl[cl.location==loc].set_index("ts")["price_usd_mwh"].sort_index()
    print(loc, "identical to gridstatus:", a.index.equals(b.index) and (a.values==b.values).all())
# LZ vs LZEW
z = zipfile.ZipFile("raw/RTMLZHBSPP_2025.zip"); raw = pd.concat(pd.read_excel(z.open(z.namelist()[0]), sheet_name=None, engine="calamine").values(), ignore_index=True)
for loc in ["LZ_HOUSTON","LZ_NORTH"]:
    x = raw[raw["Settlement Point Name"]==loc]
    lz = x[x["Settlement Point Type"]=="LZ"]["Settlement Point Price"].values
    ew = x[x["Settlement Point Type"]=="LZEW"]["Settlement Point Price"].values
    d = lz-ew
    print(loc, "LZ vs LZEW: n", len(lz), len(ew), "frac differ", (abs(d)>1e-9).mean().round(4), "max abs diff", abs(d).max(), "mean LZ", lz.mean().round(3), "mean LZEW", ew.mean().round(3))
    # gridstatus order check: for LZ_HOUSTON, what does gs have per ts?
# gap/NaN check
for y in (2024, 2025):
    for m, step in (("rt","15min"),("da","1h")):
        df = pd.read_parquet(f"{D}/ercot_{m}_{y}_hubs.parquet")
        for loc, g in df.groupby("location"):
            grid = pd.date_range(g.ts.min(), g.ts.max(), freq=step)
            miss = len(grid.difference(g.ts)); nan = g.price_usd_mwh.isna().sum()
            if loc in ("HB_NORTH",) or miss or nan:
                print(y, m, loc, "rows", len(g), "grid", len(grid), "missing", miss, f"({100*miss/len(grid):.3f}%)", "NaN", nan, g.ts.min(), g.ts.max())
