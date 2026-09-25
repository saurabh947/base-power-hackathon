import pandas as pd
rt = pd.read_parquet("data/ercot_rt_2025_hubs.parquet")
da = pd.read_parquet("data/ercot_da_2025_hubs.parquet")
rt["ld"] = rt.interval_start_local.dt.tz_convert("US/Central").dt.date
rows = []
for loc, g in rt.groupby("location"):
    p = g.price_usd_mwh
    top = g.groupby("ld").price_usd_mwh.max().sort_values(ascending=False).head(5)
    rows.append(dict(loc=loc, n=len(p), min=p.min(), p25=p.quantile(.25), median=p.median(), mean=p.mean(), p90=p.quantile(.9), p99=p.quantile(.99), max=p.max(),
                     ge1000=(p>=1000).sum(), le0=(p<=0).sum(), lt0=(p<0).sum(), std=p.std(),
                     top5=", ".join(f"{d}:{v:.0f}" for d, v in top.items())))
s = pd.DataFrame(rows).set_index("loc")
pd.set_option("display.width", 250)
print(s.drop(columns="top5").round(2).to_string()); print()
for loc, r in s.iterrows(): print(loc, "top5 days by max 15-min RT:", r.top5)
print("\nDA 2025:")
print(da.groupby("location").price_usd_mwh.describe(percentiles=[.25,.5,.9]).round(2).to_string())
# daily spread (max-min) median, useful for arbitrage
d = rt.groupby(["location","ld"]).price_usd_mwh.agg(lambda x: x.max()-x.min())
print("\nmedian daily RT spread (max-min):", d.groupby("location").median().round(1).to_dict())
# load check
ld = pd.read_parquet("data/ercot_hourly_native_load_2025.parquet")
u = ld["Interval Start"].dt.tz_convert("UTC")
print("\nload rows", len(ld), "unique UTC", u.nunique(), "grid", len(pd.date_range(u.min(), u.max(), freq="1h")), "ERCOT MW min/mean/max", ld.ERCOT.min().round(0), ld.ERCOT.mean().round(0), ld.ERCOT.max().round(0))
