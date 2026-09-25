"""Tidy ERCOT IntGenbyFuel2025.xlsx -> 15-min UTC, MW by fuel. Values in file are MWh per 15-min interval (x4 = avg MW)."""
import pandas as pd, numpy as np, time
t0 = time.time()
s = pd.read_excel("raw/IntGenbyFuel2025.xlsx", sheet_name=None, engine="calamine")
months = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
recs = []
for m in months:
    d = s[m]
    tcols = [c for c in d.columns if c not in ("Date","Fuel","Settlement Type","Total")]
    long = d.melt(id_vars=["Date","Fuel"], value_vars=tcols, var_name="label", value_name="mwh").dropna(subset=["mwh"])
    recs.append(long)
x = pd.concat(recs, ignore_index=True)
x["dst2"] = x.label.str.contains("DST")
lab = x.label.str.replace(" (DST)", "", regex=False)
hh = lab.str.split(":").str[0].astype(int); mm = lab.str.split(":").str[1].astype(int)
naive_end = pd.to_datetime(x.Date) + pd.to_timedelta(hh, "h") + pd.to_timedelta(mm, "m")
naive_end = naive_end.where(~((hh == 0) & (mm == 0)), naive_end + pd.Timedelta(days=1))   # "0:00" = 24:00
naive_start = naive_end - pd.Timedelta("15min")
# repeated hour on 2025-11-02: base columns = first (CDT) pass, "(DST)" columns = second (CST) pass  [ASSUMPTION]
amb = np.where(x.dst2, False, True)
x["ts"] = naive_start.dt.tz_localize("US/Central", ambiguous=amb, nonexistent="raise").dt.tz_convert("UTC")
x["mw"] = x.mwh * 4
w = x.pivot_table(index="ts", columns="Fuel", values="mw", aggfunc="sum").sort_index()
grid = pd.date_range("2025-01-01 06:00", "2026-01-01 05:45", freq="15min", tz="UTC")
print("fuel-mix rows", len(w), "expected", len(grid), "missing", len(grid.difference(w.index)), "extra", len(w.index.difference(grid)), f"{time.time()-t0:.1f}s")
print("fuels", w.columns.tolist())
print("annual avg MW:", w.mean().round(0).to_dict())
w.to_parquet("data/ercot_fuel_mix_15min_2025.parquet")
