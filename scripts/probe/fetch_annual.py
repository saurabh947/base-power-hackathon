import sys, time, gridstatus, pandas as pd
D = "/private/tmp/claude-501/-Users-sab-Documents-Study-AI-MyApps-base-power/72b0fc80-18f0-4cd0-a9ac-603978bf423b/scratchpad/dataprobe/data"
kind, year = sys.argv[1], int(sys.argv[2])
e = gridstatus.Ercot()
t0 = time.time()
df = e.get_rtm_spp(year=year, verbose=True) if kind == "rt" else e.get_dam_spp(year=year, verbose=True)
print(f"FETCH {kind} {year}: {time.time()-t0:.1f}s shape={df.shape}")
print(df.dtypes)
print(df.head(3).to_string())
print(df.tail(3).to_string())
print("locations:", sorted(df["Location"].unique()))
print(df.groupby(["Location","Location Type"]).size().to_string())
df.to_parquet(f"{D}/ercot_{kind}_spp_{year}_all_locations.parquet", index=False)
print(f"TOTAL {time.time()-t0:.1f}s")
