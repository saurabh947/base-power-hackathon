import zipfile, time, pandas as pd, sys
p = sys.argv[1]
t0=time.time()
z = zipfile.ZipFile(p); f = z.open(z.namelist()[0])
sheets = pd.read_excel(f, sheet_name=None)
print(f"read_excel {time.time()-t0:.1f}s; sheets:", list(sheets))
df = pd.concat(sheets.values())
print(df.columns.tolist()); print(df.dtypes)
print(df.head(5).to_string())
for c in df.columns:
    if df[c].nunique() < 40: print(c, df[c].value_counts(dropna=False).to_dict())
