import pandas as pd
D="data"
rt = pd.read_parquet(f"{D}/ercot_rt_spp_2025_all_locations.parquet")
da = pd.read_parquet(f"{D}/ercot_da_spp_2025_all_locations.parquet")
for name, df, step in [("RT", rt, "15min"), ("DA", da, "1h")]:
    print("=====", name)
    h = df[df.Location=="HB_NORTH"].copy()
    h["utc"] = h["Interval Start"].dt.tz_convert("UTC")
    print("rows", len(h), "unique utc", h.utc.nunique(), "min", h.utc.min(), "max", h.utc.max())
    full = pd.date_range(h.utc.min(), h.utc.max(), freq=step)
    print("expected grid", len(full), "missing", len(full.difference(h.utc)), "extra", len(pd.Index(h.utc).difference(full)))
    h["ld"] = h["Interval Start"].dt.date.astype(str)
    c = h.groupby("ld").size()
    print("per-day counts value_counts:", c.value_counts().to_dict())
    print("odd days:", c[c != c.mode()[0]].to_dict())
    for d in ["2025-03-09", "2025-11-02"]:
        x = h[h.ld==d]
        print(d, x[["Interval Start","utc","SPP"]].iloc[4*(0 if step=='1h' else 1): ].head(14 if step=="15min" else 5).to_string() if False else "")
        print(x[(x["Interval Start"].dt.hour>=0)&(x["Interval Start"].dt.hour<=3)][["Interval Start","utc","SPP"]].to_string())
    # LZ duplicates
    lz = df[df.Location=="LZ_HOUSTON"]
    print("LZ_HOUSTON rows", len(lz), "unique Interval Start", lz["Interval Start"].nunique())
