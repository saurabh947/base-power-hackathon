import gridstatus, time, traceback, pandas as pd
e = gridstatus.Ercot()
for d in ["2025-03-15", "today"]:
    t0 = time.time()
    try:
        df = e.get_lmp(date=d if d!="today" else pd.Timestamp.now(tz="US/Central").normalize(), verbose=False)
        print(d, "OK", df.shape, f"{time.time()-t0:.1f}s")
        if len(df):
            h = df[df.Location=="HB_NORTH"]
            print(h.head(3).to_string()); print("HB_NORTH rows", len(h), "min/max", h["Interval Start"].min(), h["Interval Start"].max())
            print("distinct SCED per hour ~", len(h)/max(1,(h["Interval Start"].max()-h["Interval Start"].min()).total_seconds()/3600))
    except Exception as ex:
        print(d, "FAIL", type(ex).__name__, str(ex)[:400], f"{time.time()-t0:.1f}s")
