import gridstatus, time, pandas as pd
e = gridstatus.Ercot()
def tryit(label, fn):
    t0 = time.time()
    try:
        df = fn(); print(label, "OK", df.shape, f"{time.time()-t0:.1f}s"); return df
    except Exception as ex:
        print(label, "FAIL", type(ex).__name__, str(ex)[:250], f"{time.time()-t0:.1f}s")
tryit("get_load(2025-03-15)", lambda: e.get_load("2025-03-15"))
tryit("get_fuel_mix(2025-03-15)", lambda: e.get_fuel_mix("2025-03-15"))
tryit("get_system_wide_actual_load(2025-03-15 12:00)", lambda: e.get_system_wide_actual_load(pd.Timestamp("2025-03-15 12:00", tz="US/Central")))
fm = tryit("get_fuel_mix(latest)", lambda: e.get_fuel_mix("latest"))
if fm is not None: print(fm.tail(3).to_string()); print("fuel-mix step:", fm.Time.diff().value_counts().head(3).to_dict())
ld = tryit("get_hourly_load_post_settlements(2025)", lambda: e.get_hourly_load_post_settlements("2025-01-01"))
if ld is not None:
    print(ld.dtypes); print(ld.head(3).to_string()); print(ld.tail(3).to_string())
    ld.to_parquet("data/ercot_hourly_native_load_2025.parquet", index=False)
