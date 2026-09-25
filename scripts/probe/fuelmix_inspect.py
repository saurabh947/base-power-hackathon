import pandas as pd, time
t0=time.time()
s = pd.read_excel("raw/IntGenbyFuel2025.xlsx", sheet_name=None, engine="calamine")
print(f"read {time.time()-t0:.1f}s sheets:", {k: v.shape for k,v in s.items()})
k = [k for k in s if k.lower().startswith("jan")][0]
df = s[k]; print(df.columns[:12].tolist(), "...", df.columns[-4:].tolist()); print(df.head(12).iloc[:, :8].to_string())
print("Fuel values:", df.iloc[:,1].unique()[:20] if df.shape[1]>1 else None)
